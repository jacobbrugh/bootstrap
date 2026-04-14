"""Rich-based logging setup + phase start/end/failure context manager.

`log.setup()` installs a `RichHandler` on the root logger — call it once
from the CLI entry point. `log.phase("name")` is a sync context manager
that emits a cyan "▶ phase: name" before the block, a green "✓ phase:
name" on success, and a red "✗ phase failed: name" with traceback on
exception. Works unchanged inside `async def` functions — sync context
managers compose fine with `async with`-style phase bodies because each
phase runs a sequence of `await`s rather than yielding.

Per-module loggers are obtained directly via `logging.getLogger(__name__)`;
no thin wrapper here. Keeping the module tiny so it's obvious what's doing
work and what isn't.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.logging import RichHandler

_CONSOLE: Console = Console(stderr=True)
_configured: bool = False


def setup(*, verbose: bool = False) -> None:
    """Configure the root logger with a RichHandler. Idempotent."""
    global _configured
    if _configured:
        return
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=_CONSOLE,
                rich_tracebacks=True,
                show_path=False,
                show_time=True,
                markup=True,
            ),
        ],
        force=True,
    )
    _configured = True


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Emit start/end markers for a phase, and a failure marker on exception."""
    logger = logging.getLogger(f"bootstrap.phases.{name}")
    logger.info("[bold cyan]\u25b6 phase: %s[/]", name)
    try:
        yield
    except Exception:
        logger.exception("[bold red]\u2717 phase failed: %s[/]", name)
        raise
    else:
        logger.info("[bold green]\u2713 phase: %s[/]", name)
