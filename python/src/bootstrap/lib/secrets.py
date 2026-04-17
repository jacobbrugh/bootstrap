"""Ephemeral lifecycle for the bootstrap age key + GitHub PAT.

Exposes one thing: the `ephemeral_secrets(ctx)` async context manager. On
entry it resolves a bootstrap-tier age key from one of two sources and
uses that key to decrypt a bundled sops file, populating
`ctx.bootstrap_age_key_file` / `ctx.github_token`. On exit — success or
failure — the age-key file is shredded (only if ephemeral) and both
context fields are cleared.

## Two tiers of bootstrap age key

`ctx.is_sandbox` selects between:

- **devbox** — full-privilege bootstrap key. Decrypts
  `bootstrap-secrets-devbox.sops.yaml` which contains the user's real
  GitHub PAT. Used on normal dev hosts (a developer's Mac, a long-lived
  NixOS workstation).
- **sandbox** — restricted bootstrap key. Decrypts
  `bootstrap-secrets-sandbox.sops.yaml` which contains the
  jacobbrugh-bot GitHub PAT. Used on CI runners, throwaway NixOS VMs,
  kubevirt instances. By construction the sandbox key has no power to
  decrypt any privileged sops file — the user's personal PAT and other
  privileged fields live in the devbox sops file only.

The `_NON_SENSITIVE_TAGS` logic in the register phase also excludes
sandbox hosts' own generated age keys from `nix/secrets.yaml`'s
creation_rule, so a sandbox host can neither bootstrap-decrypt the
devbox secrets nor post-bootstrap-decrypt the privileged host secrets.

## Two sources for the bootstrap age key

1. **`SOPS_AGE_KEY_FILE` env var** (headless production + CI). Standard
   upstream-sops env var. If set, we read the age key file from that
   path and use it to decrypt the appropriate bundled sops file.
   Operators on headless hosts pre-stage the key via SCP / cloud-init /
   systemd credentials / etc. before running bootstrap. CI uses the
   same mechanism with a fresh test key.
2. **1Password via `op read`** (Darwin GUI integration). If
   `SOPS_AGE_KEY_FILE` is unset AND we're on Darwin, we fetch the age
   key from 1Password via `op read` using one of the two
   `OP_{DEVBOX,SANDBOX}_AGE_KEY_PATH` constants. On non-Darwin with no
   env var, we hard-fail with a pointer to the env var — `op` does not
   work on a headless box at all.

## Decryption failures

The most common failure is `SOPS_AGE_KEY_FILE` pointing at the wrong key
— e.g. a user had it set to `~/.config/sops/age/keys.txt` (their host
key) for their normal sops workflow, then tried to bootstrap a new host
without unsetting it. We catch the sops `ShellError` and raise a
targeted `BootstrapError` naming the file and telling them what kind of
key they actually need.

Cleanup lives in a `finally:` — no atexit, no signal handlers. The
caller (orchestrator or standalone phase entry point) owns the lifetime
by wrapping its phase-running code in
`async with secrets.ephemeral_secrets(ctx):`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from collections.abc import AsyncIterator
from importlib import resources
from pathlib import Path
from typing import cast

from ruamel.yaml import YAML

from bootstrap.lib import op, sh
from bootstrap.lib.errors import BootstrapError, ShellError
from bootstrap.lib.paths import HOME
from bootstrap.lib.runtime import Context
from bootstrap.platform import Platform

_log = logging.getLogger(__name__)

# 1Password op:// paths for the two bootstrap-tier age keys. The devbox
# path is the original item; the sandbox path is a separate 1Password
# item created alongside this code change. Until the sandbox item is
# provisioned, Darwin + sandbox runs will fail at `op read` — which is
# fine because that combination doesn't yet exist in the wild.
OP_DEVBOX_AGE_KEY_PATH = "op://Personal/bw2otnlpjhm434grbcbpb6dady/credential"
OP_SANDBOX_AGE_KEY_PATH = "op://Personal/TODO-sandbox-bootstrap-age-key/credential"

# Legacy pre-sandbox sops file name. Grace-period fallback: if the new
# tier-specific files don't exist in package data yet, we use this one
# so existing hosts keep working during the migration. Removed after the
# real `.sops.yaml` + sops files land.
_LEGACY_SECRETS_RESOURCE = "bootstrap-secrets.sops.yaml"


def _runtime_dir() -> Path:
    """Return a private, user-writable runtime directory for ephemeral files."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "bootstrap"
    # macOS doesn't set XDG_RUNTIME_DIR by default. Fall back to a private
    # dir under $HOME — still single-user, still shredded at exit.
    return HOME / ".cache" / "bootstrap" / "runtime"


def _shred(path: Path) -> None:
    """Overwrite a file with zeros then unlink it. Best-effort — never raises."""
    try:
        if not path.exists():
            return
        size = path.stat().st_size
        with path.open("r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
        path.unlink()
    except OSError as exc:
        _log.warning("failed to shred %s: %s", path, exc)


def _variant(ctx: Context) -> str:
    """Return `'sandbox'` or `'devbox'` based on `ctx.is_sandbox`."""
    return "sandbox" if ctx.is_sandbox else "devbox"


def _secrets_resource_name(ctx: Context) -> str:
    """Return the bundled sops resource name appropriate for `ctx`.

    Prefers `bootstrap-secrets-{devbox,sandbox}.sops.yaml`. If neither
    exists yet (grace period before the real sops file migration), falls
    back to the monolithic `bootstrap-secrets.sops.yaml` with a warning.
    Raises `BootstrapError` if nothing is available.
    """
    variant = _variant(ctx)
    preferred = f"bootstrap-secrets-{variant}.sops.yaml"
    data_root = resources.files("bootstrap.data")
    if (data_root / preferred).is_file():
        return preferred
    if (data_root / _LEGACY_SECRETS_RESOURCE).is_file():
        _log.warning(
            "%s not found in package data — falling back to legacy %s. "
            "Re-sops into -devbox/-sandbox variants to silence this warning.",
            preferred,
            _LEGACY_SECRETS_RESOURCE,
        )
        return _LEGACY_SECRETS_RESOURCE
    raise BootstrapError(
        f"neither {preferred} nor {_LEGACY_SECRETS_RESOURCE} is bundled as "
        f"package data — the bootstrap build is incomplete"
    )


async def _decrypt_sops_secrets(
    age_key_file: Path,
    resource_name: str,
) -> dict[str, str]:
    """Decrypt a bundled sops file and return its top-level fields.

    `age_key_file` is passed through as `SOPS_AGE_KEY_FILE` in the sops
    subprocess's environment — the standard upstream-sops env var. The
    result is parsed with ruamel.yaml (safe loader) and returned as a
    flat `{field: value}` dict of strings.
    """
    resource = resources.files("bootstrap.data") / resource_name
    with resources.as_file(resource) as sops_path:
        env = {**os.environ, "SOPS_AGE_KEY_FILE": str(age_key_file)}
        result = await sh.run(
            ["sops", "decrypt", str(sops_path)],
            env=env,
            destructive=False,
        )
    yaml = YAML(typ="safe")
    loaded = yaml.load(result.stdout)
    if not isinstance(loaded, dict):
        raise BootstrapError(f"expected a mapping in {resource_name}, got {type(loaded).__name__}")
    return {str(k): str(v) for k, v in cast("dict[object, object]", loaded).items()}


def _extract_github_token(fields: dict[str, str], resource_name: str) -> str:
    """Pull the `github_token` field out of a decrypted sops dict or raise."""
    try:
        return fields["github_token"]
    except KeyError as exc:
        raise BootstrapError(f"{resource_name} missing required field 'github_token'") from exc


@contextlib.asynccontextmanager
async def ephemeral_secrets(ctx: Context) -> AsyncIterator[None]:
    """Materialize the bootstrap age key + GH token for the duration of `ctx`.

    Populates `ctx.bootstrap_age_key_file` and `ctx.github_token` on entry.
    Shreds the age-key file (only when the bootstrap itself wrote it, i.e.
    the op-fetched path) and clears both context fields on exit, whether
    the wrapped block succeeded or raised.

    In dry-run mode, yields without touching 1Password or sops and without
    populating the context fields.
    """
    if ctx.dry_run:
        _log.info("[dry-run] skipping 1Password extraction and sops decrypt")
        yield
        return

    variant = _variant(ctx)
    resource_name = _secrets_resource_name(ctx)

    # TEMPORARY test bypass (Chunk A → removed in Chunk C). The CI
    # test-register script sets BOOTSTRAP_TEST_GITHUB_TOKEN alongside
    # SOPS_AGE_KEY_FILE to skip the sops decrypt entirely, because the
    # test age key can't decrypt the real bundled sops file. Chunk B's
    # `bootstrapForTest` Nix derivation replaces this with a
    # test-specific bundled sops file that the test key CAN decrypt, at
    # which point this bypass goes away.
    test_github_token = os.environ.get("BOOTSTRAP_TEST_GITHUB_TOKEN")
    if test_github_token:
        age_key_env = os.environ.get("SOPS_AGE_KEY_FILE")
        if not age_key_env:
            raise BootstrapError(
                "BOOTSTRAP_TEST_GITHUB_TOKEN is set but SOPS_AGE_KEY_FILE is not — "
                "the test bypass requires both"
            )
        _log.info(
            "test bypass active: BOOTSTRAP_TEST_GITHUB_TOKEN + SOPS_AGE_KEY_FILE=%s",
            age_key_env,
        )
        ctx.bootstrap_age_key_file = Path(age_key_env)
        ctx.github_token = test_github_token
        try:
            yield
        finally:
            ctx.bootstrap_age_key_file = None
            ctx.github_token = None
        return

    # Path 1 — headless / pre-staged: `SOPS_AGE_KEY_FILE` is set. Read
    # the file directly and use it to decrypt the bundled sops file.
    # Nothing gets shredded on exit because we never wrote to the file.
    age_key_env = os.environ.get("SOPS_AGE_KEY_FILE")
    if age_key_env:
        age_path = Path(age_key_env)
        if not age_path.is_file():
            raise BootstrapError(
                f"SOPS_AGE_KEY_FILE={age_path} does not exist or is not a regular file"
            )
        _log.info(
            "SOPS_AGE_KEY_FILE=%s — using as the bootstrap %s age key",
            age_path,
            variant,
        )
        ctx.bootstrap_age_key_file = age_path
        try:
            fields = await _decrypt_sops_secrets(age_path, resource_name)
        except ShellError as exc:
            ctx.bootstrap_age_key_file = None
            raise BootstrapError(
                f"SOPS_AGE_KEY_FILE={age_path} cannot decrypt "
                f"{resource_name}. Is it set to a bootstrap {variant} age "
                f"key? The key listed in the bootstrap repo's `.sops.yaml` "
                f"creation_rule for {resource_name} is the one you need "
                f"(if you're on a sandbox host, make sure the sandbox "
                f"bootstrap key is pre-staged at this path, not your "
                f"devbox key or host key). sops stderr: "
                f"{exc.stderr.strip()[:200]}"
            ) from exc
        try:
            ctx.github_token = _extract_github_token(fields, resource_name)
            _log.info(
                "bootstrap %s secrets ready (age key at %s; %d field(s) decrypted from %s)",
                variant,
                age_path,
                len(fields),
                resource_name,
            )
            yield
        finally:
            ctx.bootstrap_age_key_file = None
            ctx.github_token = None
        return

    # Path 2 — Darwin GUI: fetch the age key from 1Password via `op`.
    # Only works on Darwin because `op` uses desktop-app integration
    # that requires a GUI. On any other platform, hard-fail with a
    # pointer at the SOPS_AGE_KEY_FILE mechanism.
    if ctx.platform is not Platform.DARWIN:
        raise BootstrapError(
            f"SOPS_AGE_KEY_FILE is not set and 1Password CLI integration is "
            f"Darwin-only. On headless {ctx.platform.value} hosts, pre-stage "
            f"your bootstrap {variant} age key on disk and export "
            f"SOPS_AGE_KEY_FILE pointing at it before running bootstrap."
        )

    op_path = OP_SANDBOX_AGE_KEY_PATH if ctx.is_sandbox else OP_DEVBOX_AGE_KEY_PATH
    _log.info(
        "materializing bootstrap %s secret zero from 1Password (%s)",
        variant,
        op_path,
    )
    runtime = _runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(stat.S_IRWXU)

    age_file = runtime / "bootstrap-age-key"
    age_file.write_text(await op.read(op_path) + "\n")
    age_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

    try:
        try:
            fields = await _decrypt_sops_secrets(age_file, resource_name)
        except ShellError as exc:
            raise BootstrapError(
                f"age key fetched from {op_path} cannot decrypt {resource_name}. "
                f"Verify the 1Password item at {op_path} contains the correct "
                f"bootstrap {variant} age key. sops stderr: "
                f"{exc.stderr.strip()[:200]}"
            ) from exc
        ctx.github_token = _extract_github_token(fields, resource_name)
        ctx.bootstrap_age_key_file = age_file
        _log.info(
            "bootstrap %s secrets ready (age key at %s; %d field(s) decrypted from %s)",
            variant,
            age_file,
            len(fields),
            resource_name,
        )
        try:
            yield
        finally:
            _log.info("shredding bootstrap secrets")
            _shred(age_file)
            ctx.bootstrap_age_key_file = None
            ctx.github_token = None
    except BaseException:
        # If we raised before entering the yield block (e.g., sops decrypt
        # failed), still shred the age key file before propagating.
        _shred(age_file)
        raise
