from __future__ import annotations

import logging

from bootstrap.lib import brew, op, secrets, sh
from bootstrap.lib.runtime import Context

NAME = "secret_zero"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    """Ensure the 1Password session the memory-tier secrets load needs.

    Darwin holds the bootstrap age key in memory only (see
    lib/secrets.py) — there is nothing to stage on disk and no nix-darwin
    activation here; the first nix-darwin activation happens at the
    switch phase, against the dotfiles config.
    """
    if ctx.force_sandbox or secrets.file_tier_active():
        _log.info("file-tier age key seam active — skipping 1Password setup (sandbox/e2e harness)")
        return

    await brew.install_cask("1password", dry_run=ctx.dry_run)

    if ctx.dry_run:
        _log.info("would launch 1Password and poll until the CLI is signed in")
        return

    if not await op.is_signed_in():
        _log.info("launching 1Password GUI — sign in, then enable")
        _log.info("  Settings > Developer > 'Integrate with 1Password CLI' and unlock once")
        await sh.run(["open", "-a", "1Password"], destructive=True)
        await op.signin_wait()
