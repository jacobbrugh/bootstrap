"""Darwin switch — run `sudo darwin-rebuild switch`.

On a freshly-bootstrapped Mac, `darwin-rebuild` isn't on PATH yet — the
first switch is done via `sudo nix run github:nix-darwin/nix-darwin --
switch`, which runs the same script. Both honor the
`/etc/nix-darwin/flake.nix` default-path lookup (installed as a symlink
by the register phase), so neither invocation needs a `--flake` argument.

Both calls go through `sh.sudo_run`. `darwin-rebuild`'s activation
script has required root for a long time — it writes to `/etc`, toggles
launchd services, etc. — so running it without sudo just dies with
"system activation must now be run as root".

Because `sudo` resets `PATH` to the system default (per `secure_path` in
`/etc/sudoers`), we can't rely on the bootstrap wrapper's `PATH` carrying
`nix` or `darwin-rebuild` into the sudo'd child. Resolve the absolute
path via `shutil.which` on the parent side and pass it to sudo.
"""

from __future__ import annotations

import logging
import shutil

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context

NAME = "switch"

_log = logging.getLogger(__name__)

# sudo env_reset strips GIT_SSH_COMMAND before nix sees it, so we prepend
# /usr/bin/env to set it after sudo's env scrub. accept-new = auto-accept
# first-time host keys (TOFU), still rejects if a known key changes.
_ENV_PREFIX = ["/usr/bin/env", "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"]


async def run(ctx: Context) -> None:
    # Re-prime sudo before the switch. The sudo cache from the initial
    # hostname rename (cli.py) may have expired during the register phase.
    # sudo_run with capture=False can't detect a cache miss from stderr
    # (stderr goes to the terminal, result.stderr is ""), so we prime
    # explicitly here rather than relying on the auto-retry in sudo_run.
    await sh.prime_sudo(dry_run=ctx.dry_run)

    darwin_rebuild = shutil.which("darwin-rebuild")
    if darwin_rebuild is not None:
        _log.info("running `sudo darwin-rebuild switch`")
        await sh.sudo_run(
            [*_ENV_PREFIX, darwin_rebuild, "switch"],
            capture=False,
            dry_run=ctx.dry_run,
            destructive=True,
        )
        return

    nix_path = shutil.which("nix")
    if nix_path is None:
        raise BootstrapError(
            "nix not found in PATH — the bootstrap wrapper should have "
            "put it there, something is wrong with the Nix install"
        )
    _log.info("bootstrapping nix-darwin for the first time via `sudo nix run`")
    await sh.sudo_run(
        [
            *_ENV_PREFIX,
            nix_path,
            "run",
            "--extra-experimental-features",
            "nix-command flakes",
            "github:nix-darwin/nix-darwin",
            "--",
            "switch",
        ],
        capture=False,
        dry_run=ctx.dry_run,
        destructive=True,
    )
