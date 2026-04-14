"""NixOS prereqs — ensure dirs exist.

NixOS doesn't need Homebrew or /etc conflict resolution. The only work is
making sure `~/.ssh` and `~/.config/sops/age` exist with mode 0700 so the
downstream `ssh` and `register` phases can write to them.
"""

from __future__ import annotations

import logging

from bootstrap.lib.paths import SOPS_AGE_DIR, SSH_DIR
from bootstrap.lib.runtime import Context

NAME = "prereqs"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    for path in (SSH_DIR, SOPS_AGE_DIR):
        if path.exists():
            continue
        if ctx.dry_run:
            _log.info("would mkdir -p %s (mode 0700)", path)
            continue
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
