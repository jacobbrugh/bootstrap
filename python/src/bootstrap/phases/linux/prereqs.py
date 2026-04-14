"""Linux (HM-standalone) prereqs — just ensure dirs exist.

No Homebrew equivalent to install, no /etc conflict resolution (home-manager
doesn't touch /etc). The tools we need at runtime (`git`, `gh`, `sops`, `age`,
`op`, `ssh-keygen`) are already on PATH via the flake's `makeWrapperArgs`.
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
