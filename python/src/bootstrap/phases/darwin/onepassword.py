"""Darwin onepassword — install GUI, launch, block until sign-in completes.

The 1Password CLI (`op`) is available during the bootstrap run via the
Nix-provided PATH from `makeWrapperArgs` in the flake, so we don't need
to install it here — only the GUI app, which is the gate for the user to
sign in and enable CLI integration in the app's Developer settings.
"""

from __future__ import annotations

from bootstrap.lib import brew, log, op, sh
from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = log.get(__name__)


def run(ctx: Context) -> None:
    brew.install_cask("1password", dry_run=ctx.dry_run)

    if ctx.dry_run:
        _log.info("would launch 1Password GUI and poll `op whoami` until signed in")
        return

    if op.whoami():
        _log.info("1Password CLI is already signed in — skipping GUI launch")
        return

    _log.info("launching 1Password GUI — sign in, then enable")
    _log.info("  Settings > Developer > 'Integrate with 1Password CLI' and unlock once")
    sh.run(["open", "-a", "1Password"], destructive=True)

    op.signin_wait()
