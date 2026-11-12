from __future__ import annotations

import logging

from bootstrap.lib.runtime import Context

NAME = "secret_zero"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    del ctx
    _log.info(
        "secret_zero phase is a no-op on Linux-HM "
        "(use BOOTSTRAP_GITHUB_TOKEN_FILE to point at a pre-staged PAT)"
    )
