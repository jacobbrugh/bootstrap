"""Interactive prompt wrappers using stdlib `input()`.

Previous implementation used `questionary`, which wraps `prompt_toolkit`.
`prompt_toolkit` runs its own asyncio loop and registers stdin as an
async reader via kqueue on macOS; for reasons that are somewhere between
"upstream Python asyncio bug" and "kqueue doesn't love /dev/tty", that
registration fails with `OSError: [Errno 22] Invalid argument` the moment
stdin is the controlling tty. The whole bootstrap then dies at the first
prompt.

`input()` does a plain blocking `read(0, …)` with no asyncio anywhere in
sight. It works on piped stdin, on a tty, on `/dev/tty`, everywhere. We
sacrifice the fancy arrow-key UX (which a once-per-machine bootstrap
doesn't need) and get a prompt layer that actually works.

Every prompt still respects the caller's `non_interactive` flag.
"""

from __future__ import annotations

from collections.abc import Sequence

from bootstrap.lib.errors import UserAbort


def text(
    message: str,
    *,
    default: str = "",
    non_interactive: bool,
) -> str:
    """Prompt for a text value. Empty response returns `default`."""
    if non_interactive:
        if default:
            return default
        raise UserAbort(f"text prompt would block in non-interactive mode: {message}")
    prompt = f"{message} [{default}] " if default else f"{message} "
    try:
        answer = input(prompt).strip()
    except EOFError as exc:
        raise UserAbort(f"stdin closed before answer: {message}") from exc
    return answer or default


def confirm(
    message: str,
    *,
    default: bool = False,
    non_interactive: bool,
) -> bool:
    """Yes/no confirmation. Empty response returns `default`."""
    if non_interactive:
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{message} {suffix} ").strip().lower()
    except EOFError as exc:
        raise UserAbort(f"stdin closed before answer: {message}") from exc
    if not answer:
        return default
    if answer in ("y", "yes"):
        return True
    if answer in ("n", "no"):
        return False
    raise UserAbort(f"unrecognized yes/no response: {answer!r}")


def checkbox(
    message: str,
    *,
    choices: Sequence[str],
    defaults: Sequence[str] = (),
    non_interactive: bool,
) -> list[str]:
    """Multi-select numbered list. Empty response accepts the defaults.

    Renders as:

        message
          [x] 1. foo
          [ ] 2. bar
          [x] 3. baz
          numbers (space-separated) or empty for defaults:

    Responses are 1-indexed numbers separated by spaces (or commas). Any
    out-of-range or non-numeric token raises `UserAbort`.
    """
    if non_interactive:
        return list(defaults)
    defaults_set = set(defaults)
    print(message)
    for i, choice in enumerate(choices, start=1):
        marker = "x" if choice in defaults_set else " "
        print(f"  [{marker}] {i}. {choice}")
    try:
        raw = input("  numbers (space-separated) or empty for defaults: ").strip()
    except EOFError as exc:
        raise UserAbort(f"stdin closed before answer: {message}") from exc
    if not raw:
        return list(defaults)
    selected: list[str] = []
    tokens = raw.replace(",", " ").split()
    for tok in tokens:
        try:
            idx = int(tok) - 1
        except ValueError as exc:
            raise UserAbort(f"invalid selection {tok!r}") from exc
        if idx < 0 or idx >= len(choices):
            raise UserAbort(f"selection {tok} out of range 1..{len(choices)}")
        choice = choices[idx]
        if choice not in selected:
            selected.append(choice)
    return selected
