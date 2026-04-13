"""NixOS onepassword — poll `op whoami` until the user signs in."""

from __future__ import annotations

from bootstrap.lib import log, op
from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = log.get(__name__)


def run(ctx: Context) -> None:
    if ctx.dry_run:
        _log.info("would wait for 1Password CLI sign-in")
        return
    if op.whoami():
        _log.info("1Password CLI is already signed in")
        return
    _log.info(
        "sign in to 1Password in another terminal: [bold]op signin[/] "
        "(or use the 1Password Linux desktop app + CLI integration)"
    )
    op.signin_wait()
