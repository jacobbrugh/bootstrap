"""Darwin post — auto-open System Settings panes for manual TCC gates.

For each step in `bootstrap.lib.tcc.STEPS`, prints a human-readable
instruction line and opens the corresponding `x-apple.systempreferences:`
URL. This is the irreducibly-manual portion of the bootstrap: macOS TCC
permissions (Accessibility, Input Monitoring) and System Extension
approvals cannot be granted programmatically on a SIP-enabled personal
Mac without MDM.
"""

from __future__ import annotations

from bootstrap.lib import log, sh, tcc
from bootstrap.lib.runtime import Context

NAME = "post"

_log = log.get(__name__)


def run(ctx: Context) -> None:
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
        sh.run(
            ["open", step.pane_url],
            check=False,  # best-effort — never fail the bootstrap here
            dry_run=ctx.dry_run,
            destructive=True,
        )
