"""Interactive prompts via questionary, run on a worker thread.

Questionary exposes two APIs: sync `.ask()` and async `.ask_async()`.
The async API runs prompt_toolkit on the current event loop. The sync
API spins up a fresh event loop just for the prompt.

The bootstrap's orchestrator drives many `asyncio.create_subprocess_exec`
calls on the main event loop before the first interactive prompt fires.
Running the prompt on that same main loop via `ask_async()` is brittle:
any edge case in my subprocess pipe handling accumulates on that loop
and can break a later `add_reader` call that questionary needs.

So instead the wrappers below call sync `.ask()` on a worker thread via
`asyncio.to_thread`. The worker thread has no running event loop at
entry, so questionary's `Application.run()` is free to create a fresh
one for the duration of the prompt. Zero sharing with the main loop.

Every prompt respects the caller's `non_interactive` flag: when True the
wrapper returns `default` (or raises `UserAbort` if no usable default).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import questionary

from bootstrap.lib.errors import UserAbort


async def text(
    message: str,
    *,
    default: str = "",
    non_interactive: bool,
) -> str:
    """Prompt for a free-form string."""
    if non_interactive:
        if default:
            return default
        raise UserAbort(f"text prompt would block in non-interactive mode: {message}")

    def _ask() -> object:
        return questionary.text(message, default=default).ask()

    answer: object = await asyncio.to_thread(_ask)
    if answer is None:
        raise UserAbort(f"user cancelled prompt: {message}")
    if not isinstance(answer, str):
        raise UserAbort(f"unexpected prompt return type: {type(answer).__name__}")
    return answer


async def confirm(
    message: str,
    *,
    default: bool = False,
    non_interactive: bool,
) -> bool:
    """Yes/no confirmation."""
    if non_interactive:
        return default

    def _ask() -> object:
        return questionary.confirm(message, default=default).ask()

    answer: object = await asyncio.to_thread(_ask)
    if answer is None:
        raise UserAbort(f"user cancelled prompt: {message}")
    if not isinstance(answer, bool):
        raise UserAbort(f"unexpected prompt return type: {type(answer).__name__}")
    return answer


async def checkbox(
    message: str,
    *,
    choices: Sequence[str],
    defaults: Sequence[str] = (),
    non_interactive: bool,
) -> list[str]:
    """Multi-select checkbox. Returns the selected values in user-picked order."""
    if non_interactive:
        return list(defaults)
    q_choices = [questionary.Choice(c, checked=c in defaults) for c in choices]

    def _ask() -> object:
        return questionary.checkbox(message, choices=q_choices).ask()

    answer: object = await asyncio.to_thread(_ask)
    if answer is None:
        raise UserAbort(f"user cancelled prompt: {message}")
    if not isinstance(answer, list):
        raise UserAbort(f"unexpected prompt return type: {type(answer).__name__}")
    return [str(x) for x in answer]
