"""NixOS switch — `sudo nixos-rebuild switch`.

The symlink at `/etc/nixos/flake.nix` (installed by the register phase)
points at the canonical repo, so nixos-rebuild picks it up by default
and no `--flake` argument is needed.
"""

from __future__ import annotations

import logging

from bootstrap.lib import sh
from bootstrap.lib.runtime import Context

NAME = "switch"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    _log.info("running `sudo nixos-rebuild switch`")
    await sh.sudo_run(
        ["nixos-rebuild", "switch"],
        dry_run=ctx.dry_run,
        destructive=True,
    )
