from __future__ import annotations

import logging
import shutil

from bootstrap.lib import flake_splice, sh
from bootstrap.lib.errors import BootstrapError, ShellError
from bootstrap.lib.runtime import Context

NAME = "switch"

_log = logging.getLogger(__name__)

_ENV_PREFIX = ["/usr/bin/env", "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new"]


async def _switch(cmd: list[str], *, dry_run: bool) -> None:
    result = await sh.sudo_run(cmd, check=False, capture=False, dry_run=dry_run, destructive=True)
    if result.dry_run_skipped or result.ok():
        return
    raise ShellError(list(result.cmd), result.returncode, result.stderr)


async def run(ctx: Context) -> None:
    await sh.prime_sudo(dry_run=ctx.dry_run)

    darwin_rebuild = shutil.which("darwin-rebuild")
    if darwin_rebuild is not None:
        _log.info("running `sudo darwin-rebuild switch`")
        await _switch([*_ENV_PREFIX, darwin_rebuild, "switch"], dry_run=ctx.dry_run)
        return

    nix_path = shutil.which("nix")
    if nix_path is None:
        raise BootstrapError(
            "nix not found in PATH — the bootstrap wrapper should have "
            "put it there, something is wrong with the Nix install"
        )
    nix_darwin_ref = await flake_splice.nix_darwin_pinned_ref()
    _log.info(
        "bootstrapping nix-darwin for the first time via `sudo nix run %s`",
        nix_darwin_ref,
    )
    await _switch(
        [
            *_ENV_PREFIX,
            nix_path,
            "run",
            "--extra-experimental-features",
            "nix-command flakes",
            nix_darwin_ref,
            "--",
            "switch",
        ],
        dry_run=ctx.dry_run,
    )
