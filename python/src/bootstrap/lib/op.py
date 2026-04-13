"""1Password CLI wrapper.

All calls go through `op` with `--format=json` where available, so we get
structured output instead of parsing ad-hoc text. The phase that uses this
assumes `op` has been signed in to at least one account — the `ephemeral_secrets`
phase polls `signin_wait` to block until that's true.
"""

from __future__ import annotations

import json
import logging
import time

from bootstrap.lib import sh
from bootstrap.lib.errors import PrereqMissing

_log = logging.getLogger(__name__)


def whoami() -> bool:
    """Return True if `op` is signed in to at least one account."""
    result = sh.run(
        ["op", "whoami", "--format=json"],
        check=False,
        destructive=False,
    )
    if not result.ok():
        return False
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and "user_uuid" in parsed


def read(path: str) -> str:
    """Read a secret via `op read op://vault/item/field`.

    Returns the raw value with the trailing newline stripped (op always
    appends one). Raises `ShellError` on failure.
    """
    result = sh.run(["op", "read", path], destructive=False)
    return result.stdout.rstrip("\n")


def signin_wait(timeout_s: float = 180.0, poll_interval_s: float = 2.0) -> None:
    """Poll `op whoami` until it succeeds, or raise after `timeout_s`.

    Emits a progress line every ~10 seconds while waiting so the user can see
    the bootstrap is still alive while they're typing their master password.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    attempts = 0
    while time.monotonic() < deadline:
        if whoami():
            _log.info("1Password CLI signed in")
            return
        attempts += 1
        if attempts % 5 == 0:
            elapsed = int(time.monotonic() - start)
            _log.info("still waiting for 1Password sign-in (%ds elapsed)", elapsed)
        time.sleep(poll_interval_s)
    raise PrereqMissing(
        "1Password CLI sign-in",
        where=f"timed out after {timeout_s:.0f}s",
    )
