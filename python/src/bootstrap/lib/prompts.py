"""Async interactive prompts on top of questionary.

`questionary` wraps `prompt_toolkit`, which is natively async. Both
libraries have two APIs: a sync `.ask()` / `.prompt()` that is intended
for "my whole program is sync and I just want a line of input", and an
async `.ask_async()` / `.prompt_async()` that is intended for "my
program is already running an asyncio event loop and I want to prompt
from inside it." The sync API bridges by spinning up a throwaway
asyncio event loop for each prompt; the async API cooperates with
whatever loop the caller already has.

The bootstrap's whole orchestrator + phase graph runs inside a single
`asyncio.run(async_main())` call, so `ask_async()` is the correct
integration path. Every prompt below is `async def` and awaits
`questionary.<prompt>.ask_async()`.

Every prompt respects the caller's `non_interactive` flag: when True the
wrapper returns `default` (or raises `UserAbort` if no usable default).
"""

from __future__ import annotations

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
    answer: object = await questionary.text(message, default=default).ask_async()
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
    answer: object = await questionary.confirm(message, default=default).ask_async()
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
    answer: object = await questionary.checkbox(message, choices=q_choices).ask_async()
    if answer is None:
        raise UserAbort(f"user cancelled prompt: {message}")
    if not isinstance(answer, list):
        raise UserAbort(f"unexpected prompt return type: {type(answer).__name__}")
    return [str(x) for x in answer]
