"""Darwin switch — run `darwin-rebuild switch`.

On a freshly-bootstrapped Mac, `darwin-rebuild` isn't on PATH yet — the
first switch is done via `nix run github:nix-darwin/nix-darwin -- switch`,
which runs the same script. Both honor the `/etc/nix-darwin/flake.nix`
default-path lookup (installed as a symlink by the register phase), so
neither invocation needs a `--flake` argument.
"""

from __future__ import annotations

import shutil

from bootstrap.lib import log, sh
from bootstrap.lib.runtime import Context

NAME = "switch"

_log = log.get(__name__)


def run(ctx: Context) -> None:
    if shutil.which("darwin-rebuild") is not None:
        _log.info("running `darwin-rebuild switch`")
        sh.run(
            ["darwin-rebuild", "switch"],
            dry_run=ctx.dry_run,
            destructive=True,
        )
        return

    _log.info("bootstrapping nix-darwin for the first time via nix run")
    sh.run(
        [
            "nix",
            "run",
            "--extra-experimental-features",
            "nix-command flakes",
            "github:nix-darwin/nix-darwin",
            "--",
            "switch",
        ],
        dry_run=ctx.dry_run,
        destructive=True,
    )
