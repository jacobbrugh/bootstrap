"""NixOS post — no TCC gates; print a completion banner."""

from __future__ import annotations

import logging

from bootstrap.lib.runtime import Context

NAME = "post"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    del ctx
    _log.info("[bold green]bootstrap complete[/] — no manual follow-ups on NixOS")
