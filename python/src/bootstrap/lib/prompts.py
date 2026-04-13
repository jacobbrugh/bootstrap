"""Interactive prompt wrappers over questionary.

Every prompt respects the caller's `non_interactive` flag: when True the
wrapper raises `UserAbort` (or returns a default) instead of blocking on
a TTY. Makes every phase testable without mocking stdin/stdout.
"""

from __future__ import annotations

from collections.abc import Sequence

import questionary

from bootstrap.lib.errors import UserAbort


def text(
    message: str,
    *,
    default: str = "",
    non_interactive: bool,
) -> str:
    """Prompt for a text value. Returns the user's input."""
    if non_interactive:
        if default:
            return default
        raise UserAbort(f"text prompt would block in non-interactive mode: {message}")
    answer: object = questionary.text(message, default=default).ask()
    if answer is None:
        raise UserAbort(f"user cancelled prompt: {message}")
    if not isinstance(answer, str):
        raise UserAbort(f"unexpected prompt return type: {type(answer).__name__}")
    return answer


def confirm(
    message: str,
    *,
    default: bool = False,
    non_interactive: bool,
) -> bool:
    """Yes/no confirmation. Returns the user's choice."""
    if non_interactive:
        return default
    answer: object = questionary.confirm(message, default=default).ask()
    if answer is None:
        raise UserAbort(f"user cancelled prompt: {message}")
    if not isinstance(answer, bool):
        raise UserAbort(f"unexpected prompt return type: {type(answer).__name__}")
    return answer


def checkbox(
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
    answer: object = questionary.checkbox(message, choices=q_choices).ask()
    if answer is None:
        raise UserAbort(f"user cancelled prompt: {message}")
    if not isinstance(answer, list):
        raise UserAbort(f"unexpected prompt return type: {type(answer).__name__}")
    return [str(x) for x in answer]
