"""Rich-based logging setup.

Usage:
    from bootstrap.lib import log
    log.setup(verbose=True)
    logger = log.get(__name__)
    with log.phase("register"):
        logger.info("...")
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


def get(name: str) -> logging.Logger:
    """Get a child logger by name (conventionally `__name__`)."""
    return logging.getLogger(name)


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Emit start/end markers for a phase, and a failure marker on exception."""
    logger = get(f"bootstrap.phases.{name}")
    logger.info("[bold cyan]\u25b6 phase: %s[/]", name)
    try:
        yield
    except Exception:
        logger.exception("[bold red]\u2717 phase failed: %s[/]", name)
        raise
    else:
        logger.info("[bold green]\u2713 phase: %s[/]", name)
