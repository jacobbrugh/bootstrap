"""Darwin onepassword — install 1Password GUI, fetch age key, activate Phase 0.

Darwin equivalent of the operator pre-staging the bootstrap age key at
`/var/lib/nixos-bootstrap/age-key` before a NixOS install + boot.
Difference: NixOS operators put the key there manually before first
boot; Darwin fetches it from 1Password at runtime via `op read`.

Flow:
  1. Install 1Password GUI (`brew install --cask 1password`).
  2. If `op` isn't signed in, launch the app and poll until integration
     is live.
  3. `op read op://Personal/<item>/credential` — the bootstrap age key.
  4. `sudo tee` the key to `/var/lib/nixos-bootstrap/age-key` (root:admin,
     0400, parent 0700). Pipe on stdin so it never lands in a
     user-writable file.
  5. Activate Phase 0 via `sudo nix run nix-darwin -- switch --flake
     <bootstrap>#bootstrap`. sops-nix-darwin at that activation reads
     the age key and writes plaintext to
     `/run/secrets/bootstrap-github-token`.

After this, `ephemeral_secrets` reads the plaintext same as on NixOS —
no Python decryption anywhere. The `post` phase shreds
`/var/lib/nixos-bootstrap/age-key` at the end of bootstrap, mirroring
the NixOS `phase0-firstboot` systemd service's shred step so the
bootstrap key doesn't persist on the installed system.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from bootstrap.lib import brew, op, sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = logging.getLogger(__name__)

_AGE_KEY_PATH = Path("/var/lib/nixos-bootstrap/age-key")
_PLAINTEXT_TOKEN_PATH = Path("/run/secrets/bootstrap-github-token")

_OP_DEVBOX_AGE_KEY_PATH = "op://Personal/bw2otnlpjhm434grbcbpb6dady/credential"
_OP_SANDBOX_AGE_KEY_PATH = "op://Personal/TODO-sandbox-bootstrap-age-key/credential"


async def run(ctx: Context) -> None:
    if _PLAINTEXT_TOKEN_PATH.is_file():
        _log.info("%s already present — Phase 0 already active, skipping", _PLAINTEXT_TOKEN_PATH)
        return

    if not _AGE_KEY_PATH.is_file():
        await _stage_age_key(ctx)

    await _activate_phase0(ctx)


async def _stage_age_key(ctx: Context) -> None:
    """Install 1Password GUI, sign in, op-read age key, sudo-tee to disk."""
    await brew.install_cask("1password", dry_run=ctx.dry_run)

    if ctx.dry_run:
        _log.info(
            "would launch 1Password, poll until signed in, `op read` age key, sudo tee → %s",
            _AGE_KEY_PATH,
        )
        return

    if not await op.is_signed_in():
        _log.info("launching 1Password GUI — sign in, then enable")
        _log.info("  Settings > Developer > 'Integrate with 1Password CLI' and unlock once")
        await sh.run(["open", "-a", "1Password"], destructive=True)
        await op.signin_wait()

    op_path = _OP_SANDBOX_AGE_KEY_PATH if ctx.is_sandbox else _OP_DEVBOX_AGE_KEY_PATH
    _log.info("reading bootstrap age key from %s", op_path)
    key = await op.read(op_path)

    await sh.prime_sudo(dry_run=False)
    await sh.sudo_run(
        ["install", "-d", "-m", "0700", "-o", "root", "-g", "admin", str(_AGE_KEY_PATH.parent)],
        destructive=True,
    )
    # `sudo tee` pipes the key in on stdin — never touches a user-writable
    # file on the way to /var/lib/.
    await sh.run(
        ["sudo", "-nH", "tee", str(_AGE_KEY_PATH)],
        input_text=key + "\n",
        destructive=True,
    )
    await sh.sudo_run(["chown", "root:admin", str(_AGE_KEY_PATH)], destructive=True)
    await sh.sudo_run(["chmod", "0400", str(_AGE_KEY_PATH)], destructive=True)


async def _activate_phase0(ctx: Context) -> None:
    """`sudo nix run nix-darwin -- switch --flake <bootstrap>#bootstrap`.

    Builds + activates the minimal Phase 0 darwin config defined at
    `nix/darwin/default.nix`. sops-nix-darwin at activation reads
    `/var/lib/nixos-bootstrap/age-key`, decrypts
    `secrets/bootstrap-secrets.sops.yaml`, and writes plaintext to
    `/run/secrets/bootstrap-github-token`. That's the only thing Phase 0
    Darwin does — the subsequent `switch` phase replaces it with the
    user's full dotfiles darwinConfiguration.
    """
    if ctx.dry_run:
        _log.info(
            "would activate Phase 0 Darwin config (sops-nix decrypts → %s)",
            _PLAINTEXT_TOKEN_PATH,
        )
        return

    flake_ref = os.environ.get("BOOTSTRAP_FLAKE", "github:jacobbrugh/bootstrap")
    nix_path = shutil.which("nix")
    if nix_path is None:
        raise BootstrapError(
            "nix not found in PATH — the bootstrap wrapper should have put it there"
        )

    _log.info("activating Phase 0 Darwin via nix-darwin (unlocks %s)", _PLAINTEXT_TOKEN_PATH)
    await sh.prime_sudo(dry_run=False)
    await sh.sudo_run(
        [
            nix_path,
            "run",
            "--extra-experimental-features",
            "nix-command flakes",
            "github:nix-darwin/nix-darwin",
            "--",
            "switch",
            "--flake",
            f"{flake_ref}#bootstrap",
        ],
        capture=False,
        destructive=True,
    )
