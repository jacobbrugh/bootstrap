"""Linux switch — `home-manager switch`.

On a freshly-bootstrapped Linux host, `home-manager` isn't on PATH yet;
the first switch runs via `nix run github:nix-community/home-manager`.
Both forms pick up the `~/.config/home-manager/flake.nix` symlink
installed by the register phase, so neither needs a `--flake` argument.
"""

from __future__ import annotations

import logging
import shutil

from bootstrap.lib import sh
from bootstrap.lib.runtime import Context

NAME = "switch"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    if shutil.which("home-manager") is not None:
        _log.info("running `home-manager switch`")
        await sh.run(
            ["home-manager", "switch"],
            dry_run=ctx.dry_run,
            destructive=True,
        )
        return

    _log.info("bootstrapping home-manager for the first time via nix run")
    await sh.run(
        [
            "nix",
            "run",
            "--extra-experimental-features",
            "nix-command flakes",
            "github:nix-community/home-manager",
            "--",
            "switch",
        ],
        dry_run=ctx.dry_run,
        destructive=True,
    )
