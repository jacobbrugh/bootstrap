"""Post for NixOS + Linux-HM — print completion banner, nothing else.

Darwin's `phases/darwin/post.py` does real work (shreds the bootstrap
age key, opens System Settings panes for TCC gates).
"""

from __future__ import annotations

import logging

from bootstrap.lib.runtime import Context

NAME = "post"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    del ctx
    _log.info("[bold green]bootstrap complete[/] — no manual follow-ups")
