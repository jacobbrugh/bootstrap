"""Linux onepassword — block until the 1Password CLI can read account data.

Unlike Darwin, this phase does not install a GUI app — the user is
expected to sign in via `op signin` in another terminal, or via the
1Password Linux desktop app if they already have it installed.
"""

from __future__ import annotations

import logging
import os

from bootstrap.lib import op
from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    if ctx.dry_run:
        _log.info("would wait for 1Password CLI sign-in")
        return
    # Headless bootstrap path: SOPS_AGE_KEY_FILE is set, so the secrets
    # layer will read the bootstrap age key directly from disk and
    # `op` is never called. Nothing for this phase to unblock.
    if os.environ.get("SOPS_AGE_KEY_FILE"):
        _log.info("SOPS_AGE_KEY_FILE set — skipping 1Password signin (not needed)")
        return
    if await op.is_signed_in():
        _log.info("1Password CLI already able to read data")
        return
    _log.info(
        "sign in to 1Password in another terminal: [bold]op signin[/] "
        "(or use the 1Password desktop app + 'Integrate with 1Password CLI')"
    )
    await op.signin_wait()
