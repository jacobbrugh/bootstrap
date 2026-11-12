from __future__ import annotations

import logging

from bootstrap.lib import sh, tcc
from bootstrap.lib.runtime import Context

NAME = "post"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    # No secret cleanup on darwin: the bootstrap age key lives only in
    # process memory (lib/secrets.py) and dies with the run.
    _log.info(
        "[bold green]bootstrap complete[/] — opening System Settings panes "
        "for the manual TCC gates",
    )
    for step in tcc.STEPS:
        _log.info(
            "[bold]%s[/] — needed by: %s",
            step.name,
            ", ".join(step.required_by),
        )
        _log.info("    %s", step.instructions)
        await sh.run(
            ["open", step.pane_url],
            check=False,
            dry_run=ctx.dry_run,
            destructive=True,
        )
