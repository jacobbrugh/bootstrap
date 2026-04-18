"""Ephemeral lifecycle for the bootstrap GitHub PAT + age-key path.

The bootstrap CLI doesn't decrypt anything — sops-nix does, at Phase 0
activation. At activation, sops-nix reads the operator-pre-staged age
key from `/var/lib/nixos-bootstrap/age-key`, decrypts the bundled
`secrets/bootstrap-secrets.sops.yaml` (encrypted to that key), and
writes the plaintext `github_token` field to
`/run/secrets/bootstrap-github-token`.

Per-platform activation:
  - NixOS: sops-nix nixosModule, activated by `nixosConfigurations.bootstrap`
    at first boot (or `wsl-bootstrap` inside WSL).
  - Darwin: sops-nix-darwin module, activated transiently by
    `phases/darwin/onepassword.py` via `sudo nix run nix-darwin -- switch
    --flake <bootstrap>#bootstrap`. The age key arrives via `op read`
    against the user's 1Password devbox/sandbox item, gets `sudo tee`'d
    to the same `/var/lib/nixos-bootstrap/age-key` path, and is shredded
    at the end of bootstrap by `phases/darwin/post.py` (mirroring the
    NixOS `phase0-firstboot` `shred -u` step).

This module just reads the plaintext file, puts the token in
`ctx.github_token` for the ssh + register phases, and clears it on exit.

For the age-key path (`ctx.bootstrap_age_key_file`): the register phase
passes it to `sops updatekeys` when re-encrypting `bot-secrets.yaml` +
`secrets.yaml`. That's still a sops CLI subprocess call, which is fine
— it's not decrypting a *bundled* secret, it's re-keying *existing*
secrets in the cloned dotfiles. The age key itself stays on disk
(/var/lib/nixos-bootstrap/age-key) — Python never reads or copies it.

Override paths via env vars for non-Phase-0 contexts (CI harnesses,
one-off debugging, whatever):
  - BOOTSTRAP_GITHUB_TOKEN_FILE — plaintext token file.
    Default: /run/secrets/bootstrap-github-token
  - BOOTSTRAP_AGE_KEY_FILE — age key file for sops updatekeys.
    Default: /var/lib/nixos-bootstrap/age-key
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context

_log = logging.getLogger(__name__)

_DEFAULT_TOKEN_PATH = Path("/run/secrets/bootstrap-github-token")
_DEFAULT_AGE_KEY_PATH = Path("/var/lib/nixos-bootstrap/age-key")


def _token_path() -> Path:
    return Path(os.environ.get("BOOTSTRAP_GITHUB_TOKEN_FILE", _DEFAULT_TOKEN_PATH))


def _age_key_path() -> Path:
    return Path(os.environ.get("BOOTSTRAP_AGE_KEY_FILE", _DEFAULT_AGE_KEY_PATH))


@contextlib.asynccontextmanager
async def ephemeral_secrets(ctx: Context) -> AsyncIterator[None]:
    """Populate `ctx.github_token` + `ctx.bootstrap_age_key_file`.

    In dry-run mode, yields without touching either — consumers gate
    real work on `ctx.dry_run` and never read the unset fields.
    """
    if ctx.dry_run:
        _log.info("[dry-run] skipping secrets load")
        yield
        return

    token_path = _token_path()
    if not token_path.is_file():
        raise BootstrapError(
            f"bootstrap github token not found at {token_path}. On NixOS, "
            f"Phase 0 sops-nix writes this file at activation — pre-stage "
            f"your bootstrap age key at /var/lib/nixos-bootstrap/age-key "
            f"and boot into `nixosConfigurations.bootstrap` before running "
            f"this. Override the path via BOOTSTRAP_GITHUB_TOKEN_FILE if "
            f"you're driving this from a CI harness."
        )

    age_key_path = _age_key_path()
    if not age_key_path.is_file():
        raise BootstrapError(
            f"bootstrap age key not found at {age_key_path}. The register "
            f"phase's `sops updatekeys` call needs it to re-encrypt "
            f"bot-secrets.yaml + secrets.yaml. Override via "
            f"BOOTSTRAP_AGE_KEY_FILE if it's staged elsewhere."
        )

    ctx.github_token = token_path.read_text().strip()
    ctx.bootstrap_age_key_file = age_key_path
    _log.info(
        "bootstrap secrets ready (github_token from %s, age key at %s)",
        token_path,
        age_key_path,
    )
    try:
        yield
    finally:
        ctx.github_token = None
        ctx.bootstrap_age_key_file = None
