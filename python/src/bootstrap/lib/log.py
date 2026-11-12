from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.logging import RichHandler

_CONSOLE: Console = Console(stderr=True)
_configured: bool = False


def setup(*, verbose: bool = False) -> None:
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
    logger = logging.getLogger(f"bootstrap.phases.{name}")
    logger.info("[bold cyan]▶ phase: %s[/]", name)
    try:
        yield
    except Exception:
        logger.exception("[bold red]✗ phase failed: %s[/]", name)
        raise
    else:
        logger.info("[bold green]✓ phase: %s[/]", name)
