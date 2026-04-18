"""Darwin post — shred the bootstrap age key, then open TCC gate panes.

The bootstrap age key at `/var/lib/nixos-bootstrap/age-key` has done its
job by this point: `onepassword` activated Phase 0 (sops-nix decrypted
the bundled PAT), `register` used it via `sops updatekeys` to re-encrypt
the dotfiles sops files against the new host's age key, and `switch`
activated the user's full dotfiles darwinConfiguration (which uses the
host's own age key at `~/.config/sops/age/keys.txt`, not the bootstrap
key). Shred so the bootstrap key doesn't persist on the installed
system — mirrors the NixOS `phase0-firstboot` systemd service's
`shred -u` step at `nix/nixos/default.nix:180`.

The remaining TCC-pane work is the irreducibly-manual portion of the
Darwin bootstrap: macOS TCC permissions (Accessibility, Input
Monitoring) and System Extension approvals can't be granted
programmatically on a SIP-enabled personal Mac without MDM.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bootstrap.lib import sh, tcc
from bootstrap.lib.runtime import Context

NAME = "post"

_log = logging.getLogger(__name__)

_AGE_KEY_PATH = Path("/var/lib/nixos-bootstrap/age-key")


async def run(ctx: Context) -> None:
    await _shred_bootstrap_age_key(ctx)

    _log.info(
        "[bold green]bootstrap complete[/] — opening System Settings panes "
        "for the manual TCC gates",
    )
    for step in tcc.STEPS:
        _log.info(
            "[bold]%s[/] — needed by: %s",
            step.name,
            ", ".join(step.required_by),
        )
        _log.info("    %s", step.instructions)
        await sh.run(
            ["open", step.pane_url],
            check=False,  # best-effort — never fail the bootstrap here
            dry_run=ctx.dry_run,
            destructive=True,
        )


async def _shred_bootstrap_age_key(ctx: Context) -> None:
    """`rm -Pf /var/lib/nixos-bootstrap/age-key`.

    `rm -P` is macOS's overwrite-before-unlink (3 passes) — no `shred` on
    Darwin's base install. Best-effort: if the file is already gone
    (re-run), or the overwrite fails for any reason, don't fail the
    bootstrap.
    """
    if not _AGE_KEY_PATH.exists() and not ctx.dry_run:
        _log.info("bootstrap age key already absent at %s", _AGE_KEY_PATH)
        return
    _log.info("shredding bootstrap age key at %s", _AGE_KEY_PATH)
    await sh.sudo_run(
        ["rm", "-Pf", str(_AGE_KEY_PATH)],
        check=False,
        dry_run=ctx.dry_run,
        destructive=True,
    )
