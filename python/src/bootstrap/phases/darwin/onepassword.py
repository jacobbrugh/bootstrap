"""Darwin onepassword — install GUI + AgileBits-signed CLI, launch, block until signed in.

We MUST install `1password-cli` via Homebrew here (in addition to the
GUI cask), even though the Nix-provided PATH already exposes an `op`
binary. The macOS desktop app verifies CLI authenticity by XPC code
signature check; the Nix-packaged `_1password-cli` is not AgileBits-
signed and is rejected with "account is not signed in" on every call.
The Homebrew cask ships the official signed binary that the desktop
app trusts. See `bootstrap.lib.op` for the full explanation and the
`_op_binary()` helper that selects the right one.
"""

from __future__ import annotations

from bootstrap.lib import brew, log, op, sh
from bootstrap.lib.runtime import Context

NAME = "onepassword"

_log = log.get(__name__)


def run(ctx: Context) -> None:
    brew.install_cask("1password", dry_run=ctx.dry_run)
    brew.install_cask("1password-cli", dry_run=ctx.dry_run)

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
