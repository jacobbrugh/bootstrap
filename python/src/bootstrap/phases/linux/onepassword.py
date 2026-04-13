"""Linux onepassword — poll `op whoami` until the user signs in.

Unlike Darwin, this phase does not install a GUI app — the user is
expected to sign in via `op signin` in another terminal, or via the
1Password Linux desktop app if they already have it installed. The
polling loop is the same: block until `op whoami` succeeds.
"""

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
        "(or use the 1Password desktop app + 'Integrate with 1Password CLI')"
    )
    op.signin_wait()
