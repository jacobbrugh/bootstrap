"""Linux post — no TCC gates; just print a completion banner."""

from __future__ import annotations

from bootstrap.lib import log
from bootstrap.lib.runtime import Context

NAME = "post"

_log = log.get(__name__)


def run(ctx: Context) -> None:
    del ctx
    _log.info("[bold green]bootstrap complete[/] — no manual follow-ups on Linux")
