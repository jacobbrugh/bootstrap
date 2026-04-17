"""Darwin onepassword — install GUI, launch, block until CLI can read data."""

from __future__ import annotations

import logging
import os

from bootstrap.lib import brew, op, sh
from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    # Headless path (SOPS_AGE_KEY_FILE pre-staged) skips the 1Password
    # GUI install and signin wait entirely — the secrets layer reads
    # the bootstrap age key from disk without ever calling `op`. Rare
    # on Darwin but valid (e.g. restoring a dev machine from a backup
    # that already has the key file, or a CI Darwin runner).
    if os.environ.get("SOPS_AGE_KEY_FILE"):
        _log.info("SOPS_AGE_KEY_FILE set — skipping 1Password install + signin (not needed)")
        return

    await brew.install_cask("1password", dry_run=ctx.dry_run)

    if ctx.dry_run:
        _log.info("would launch 1Password GUI and poll until op can read account data")
        return

    if await op.is_signed_in():
        _log.info("1Password CLI already able to read data — skipping GUI launch")
        return

    _log.info("launching 1Password GUI — sign in, then enable")
    _log.info("  Settings > Developer > 'Integrate with 1Password CLI' and unlock once")
    await sh.run(["open", "-a", "1Password"], destructive=True)

    await op.signin_wait()
