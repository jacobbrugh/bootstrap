from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from bootstrap.lib import age_ops, flake_splice, op, sh, sops_anchors
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.paths import BOOTSTRAP_AGE_KEY
from bootstrap.lib.runtime import Context, Tier
from bootstrap.platform import Platform

_log = logging.getLogger(__name__)

_DEFAULT_TOKEN_PATH = Path("/run/secrets/bootstrap-github-token")

#: The bot token every tier reads: the registry clone at flash, and the
#: https clone and push of register on a sandbox host. Nothing at switch time
#: needs it: dotfiles has no private flake input.
BOT_SECRETS_FILE = "secrets.yaml"

#: The one file only the privileged bootstrap age key can decrypt (see
#: .sops.yaml). It holds the `jacobbrugh`-owned token the ssh phase uploads a
#: privileged host's key with. Never declared to sops-nix, never on disk in
#: plaintext: decrypted on demand into memory and handed to `gh` via env.
PRIVILEGED_SECRETS_FILE = "secrets-privileged.yaml"
PRIVILEGED_TOKEN_KEY = "github_ssh_key_token"
#: The committed placeholder; the real value is set with `sops set` by the
#: operator (README § Bare-metal flash flow).
PRIVILEGED_TOKEN_UNSET = "unset"


def _token_path() -> Path:
    return Path(os.environ.get("BOOTSTRAP_GITHUB_TOKEN_FILE", _DEFAULT_TOKEN_PATH))


def _age_key_path() -> Path:
    return Path(os.environ.get("BOOTSTRAP_AGE_KEY_FILE", BOOTSTRAP_AGE_KEY))


def _is_file_or_protected(path: Path) -> bool:
    try:
        return path.is_file()
    except PermissionError:
        return True


def file_tier_active() -> bool:
    """A harness staged the age key via the BOOTSTRAP_AGE_KEY_FILE seam."""
    return "BOOTSTRAP_AGE_KEY_FILE" in os.environ


async def _sops_extract(key: str, *, env_overlay: dict[str, str], file: str) -> str:
    """Decrypt one top-level string from a sops file in the bootstrap flake source."""
    src = await flake_splice.source_path(flake_splice.flake_ref())
    result = await sh.run(
        ["sops", "-d", "--extract", f'["{key}"]', str(src / file)],
        env={**os.environ, **env_overlay},
        destructive=False,
    )
    return result.stdout.strip()


async def privileged_github_token(ctx: Context) -> str:
    """The `jacobbrugh`-owned SSH-key token, decrypted with the loaded age key.

    Only a privileged identity can decrypt the file, so a sandbox-keyed host
    fails here with sops' own "no key could decrypt" — but callers gate on
    ctx.is_privileged first and never reach this on the sandbox path.
    """
    if not ctx.sops_env_overlay:
        raise BootstrapError(
            "the privileged GitHub token needs the bootstrap age key, but none "
            "is loaded (shredded by an earlier completed bootstrap?)"
        )
    token = await _sops_extract(
        PRIVILEGED_TOKEN_KEY,
        env_overlay=ctx.sops_env_overlay,
        file=PRIVILEGED_SECRETS_FILE,
    )
    if not token or token == PRIVILEGED_TOKEN_UNSET:
        raise BootstrapError(
            f"{PRIVILEGED_SECRETS_FILE} still carries the placeholder "
            f"{PRIVILEGED_TOKEN_KEY}={PRIVILEGED_TOKEN_UNSET!r}. Set the real "
            "jacobbrugh token (fine-grained, 'Git SSH keys: Read and write'):\n"
            f"  SOPS_AGE_KEY=$(op read {op.BOOTSTRAP_KEY_ITEM}) "
            f"sops set {PRIVILEGED_SECRETS_FILE} '[\"{PRIVILEGED_TOKEN_KEY}\"]' "
            "'\"github_pat_…\"'\n"
            "then rebuild the media."
        )
    return token


def tier_for_pubkey(pubkey: str, anchors: dict[str, str]) -> Tier:
    """Which tier an age public key belongs to, by the .sops.yaml anchors.

    A key matching neither anchor is refused here rather than at the first
    GitHub call: media built from a rotated or foreign key must not get as far
    as uploading anything.
    """
    missing = [
        name
        for name in (sops_anchors.PRIVILEGED_ANCHOR, sops_anchors.SANDBOX_ANCHOR)
        if name not in anchors
    ]
    if missing:
        raise BootstrapError(
            f".sops.yaml declares no anchor(s) named {', '.join(missing)}; "
            f"found {', '.join(sorted(anchors)) or 'none'}"
        )
    if pubkey == anchors[sops_anchors.PRIVILEGED_ANCHOR]:
        return Tier.PRIVILEGED
    if pubkey == anchors[sops_anchors.SANDBOX_ANCHOR]:
        return Tier.SANDBOX
    raise BootstrapError(
        f"the loaded bootstrap age key ({pubkey[:24]}…) is neither the "
        f"{sops_anchors.PRIVILEGED_ANCHOR} nor the {sops_anchors.SANDBOX_ANCHOR} "
        "identity declared in the bootstrap flake's .sops.yaml — media built from "
        "a rotated key, or a key that is not a bootstrap identity at all"
    )


async def detect_tier(ctx: Context) -> Tier:
    """Decide the tier from the loaded key and the flake source's .sops.yaml."""
    if ctx.bootstrap_age_key is not None:
        pubkey = await age_ops.public_key_of_identity(ctx.bootstrap_age_key)
    elif ctx.bootstrap_age_key_file is not None:
        pubkey = await age_ops.extract_public_key(ctx.bootstrap_age_key_file)
    else:
        raise BootstrapError("detect_tier called with no bootstrap age key loaded")
    src = await flake_splice.source_path(flake_splice.flake_ref())
    return tier_for_pubkey(pubkey, sops_anchors.anchor_pubkeys(src / ".sops.yaml"))


async def _load_darwin(ctx: Context) -> None:
    """Darwin tier: the key never touches the filesystem.

    The age key comes from 1Password (one `op read` — desktop-app
    integration may demand a Touch ID approval per op invocation, so read
    once and hold in memory) unless a harness staged a key file via
    BOOTSTRAP_AGE_KEY_FILE (the sandbox/e2e seam, same as NixOS). The
    github token is decrypted straight out of the flake-source
    secrets.yaml — no sops-nix activation, no /run/secrets.
    """
    if ctx.force_sandbox or file_tier_active():
        key_file = _age_key_path()
        if not _is_file_or_protected(key_file):
            raise BootstrapError(
                f"sandbox/harness bootstrap needs a staged age key, but none "
                f"is at {key_file} (set BOOTSTRAP_AGE_KEY_FILE to its location)"
            )
        ctx.bootstrap_age_key_file = key_file
        _log.info("bootstrap age key: file tier (%s)", key_file)
    else:
        _log.info("reading bootstrap age key from 1Password (%s)", op.BOOTSTRAP_KEY_ITEM)
        ctx.bootstrap_age_key = await op.read(op.BOOTSTRAP_KEY_ITEM)
        _log.info("bootstrap age key: memory tier (never written to disk)")

    token_path = _token_path()
    if token_path.is_file():
        ctx.github_token = token_path.read_text().strip()
        _log.info("github token from %s", token_path)
    else:
        ctx.github_token = await _sops_extract(
            "github_token", env_overlay=ctx.sops_env_overlay, file=BOT_SECRETS_FILE
        )
        _log.info("github token decrypted from the bootstrap flake secrets")


def _load_nixos(ctx: Context) -> None:
    """File tier: bootstrap-flash staged the key; sops-nix wrote the token."""
    token_path = _token_path()
    if not token_path.is_file():
        raise BootstrapError(
            f"bootstrap github token not found at {token_path}. On NixOS, "
            f"sops-nix writes this file when the secret-zero phase activates "
            f"`#bootstrap-secret-zero`, and post shreds the bootstrap age key "
            f"once a bootstrap completes. This host needs to register again "
            f"(its age key no longer opens its secrets, or its host files "
            f"changed) but holds no bootstrap age key to do it with: re-flash "
            f"it from media built from the current bootstrap "
            f"(bootstrap-create-usb), which stages the key at "
            f"{BOOTSTRAP_AGE_KEY} and runs the full `bootstrap` on first boot. "
            f"Override the token path via BOOTSTRAP_GITHUB_TOKEN_FILE if you're "
            f"driving this from a CI harness."
        )
    ctx.github_token = token_path.read_text().strip()

    age_key_path = _age_key_path()
    if _is_file_or_protected(age_key_path):
        ctx.bootstrap_age_key_file = age_key_path
        _log.info(
            "bootstrap secrets ready (github_token from %s, age key at %s)",
            token_path,
            age_key_path,
        )
    else:
        # Post's shred removes the key after a completed bootstrap; a
        # re-run must not die here. A registered host — one whose own key
        # decrypts every tier file — returns from register before it needs
        # the bootstrap key; a host that has to rekey raises a clear error
        # naming the re-flash that restores it.
        ctx.bootstrap_age_key_file = None
        _log.warning(
            "bootstrap age key absent at %s (already shredded?) — "
            "continuing; register will fail with a clear error if it "
            "actually needs it",
            age_key_path,
        )


@contextlib.asynccontextmanager
async def ephemeral_secrets(ctx: Context) -> AsyncIterator[None]:
    if ctx.dry_run:
        _log.info("[dry-run] skipping secrets load — tier unknown, treated as sandbox")
        yield
        return

    if ctx.platform is Platform.DARWIN:
        await _load_darwin(ctx)
    else:
        _load_nixos(ctx)

    if ctx.bootstrap_age_key is not None or ctx.bootstrap_age_key_file is not None:
        ctx.tier = await detect_tier(ctx)
        _log.info(
            "bootstrap tier: %s%s",
            ctx.tier.value,
            " (BOOTSTRAP_SANDBOX overrides to sandbox)"
            if ctx.force_sandbox and ctx.tier is Tier.PRIVILEGED
            else "",
        )
    else:
        ctx.tier = None

    try:
        yield
    finally:
        ctx.github_token = None
        ctx.bootstrap_age_key_file = None
        ctx.bootstrap_age_key = None
        ctx.tier = None
