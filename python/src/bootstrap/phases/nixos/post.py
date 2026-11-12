from __future__ import annotations

import logging

from bootstrap.lib import sh
from bootstrap.lib.paths import BOOTSTRAP_AGE_KEY
from bootstrap.lib.runtime import Context

NAME = "post"

_log = logging.getLogger(__name__)

_AGE_KEY_PATH = BOOTSTRAP_AGE_KEY


async def run(ctx: Context) -> None:
    await _shred_bootstrap_age_key(ctx)
    _log.info("[bold green]bootstrap complete[/] — no manual follow-ups")


async def _shred_bootstrap_age_key(ctx: Context) -> None:
    if not _AGE_KEY_PATH.exists() and not ctx.dry_run:
        _log.info("bootstrap age key already absent at %s", _AGE_KEY_PATH)
        return
    _log.info("shredding bootstrap age key at %s", _AGE_KEY_PATH)
    await sh.sudo_run(
        ["shred", "-u", str(_AGE_KEY_PATH)],
        check=False,
        dry_run=ctx.dry_run,
        destructive=True,
    )
