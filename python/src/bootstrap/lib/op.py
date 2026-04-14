"""1Password CLI wrapper.

All calls go through `op` with `--format=json` where available, so we get
structured output instead of parsing ad-hoc text.

## Why `op whoami` is the wrong readiness check for integration users

`op whoami` only reports on a persistent sign-in session (the kind you
get from `eval $(op signin)`). Desktop-app integration doesn't use a
persistent session; instead, each command that needs secrets triggers a
GUI unlock on demand and gets a per-command session. `op whoami` never
triggers integration unlock and therefore always reports "account is not
signed in" for integration users, even though `op read` and
`op vault list` work fine in the same shell.

So we poll with `op user get --me --format=json`, which is a lightweight
read that *does* engage integration: the first call triggers unlock, the
user approves, and subsequent calls inside the 30-minute session succeed
without re-prompting. That's the readiness signal that actually maps to
"can we call `op read` on the bootstrap age key".

The `op` binary itself comes from nixpkgs via the bootstrap's wrapper
PATH (`_1password-cli`), same as on the user's other Macs where it works
fine with desktop-app integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from bootstrap.lib import sh
from bootstrap.lib.errors import PrereqMissing

_log = logging.getLogger(__name__)


async def _user_me_result() -> sh.Result:
    return await sh.run(
        ["op", "user", "get", "--me", "--format=json"],
        check=False,
        destructive=False,
    )


def _parse_user_me(result: sh.Result) -> bool:
    if not result.ok():
        return False
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and "id" in parsed


async def is_signed_in() -> bool:
    """Return True if `op` can read account data (i.e. integration is live)."""
    return _parse_user_me(await _user_me_result())


async def read(path: str) -> str:
    """Read a secret via `op read op://vault/item/field`.

    Returns the raw value with the trailing newline stripped (op always
    appends one). Raises `ShellError` on failure.
    """
    result = await sh.run(["op", "read", path], destructive=False)
    return result.stdout.rstrip("\n")


async def signin_wait(timeout_s: float = 180.0, poll_interval_s: float = 2.0) -> None:
    """Poll until `op` can read account data, or raise after `timeout_s`.

    Uses `op user get --me`, not `op whoami`, because the latter doesn't
    engage desktop-app integration. Emits a progress line every ~10
    seconds, including the most recent stderr from `op` so the user can
    see *what* is failing (approval pending, GUI locked, not yet enabled
    in Developer settings, etc.) instead of an opaque timeout.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    attempts = 0
    last_stderr = ""
    while time.monotonic() < deadline:
        result = await _user_me_result()
        if _parse_user_me(result):
            _log.info("1Password CLI signed in")
            return
        stripped = result.stderr.strip()
        if stripped:
            last_stderr = stripped
        attempts += 1
        if attempts % 5 == 0:
            elapsed = int(time.monotonic() - start)
            _log.info("still waiting for 1Password sign-in (%ds elapsed)", elapsed)
            if last_stderr:
                _log.info("  op said: %s", last_stderr)
        await asyncio.sleep(poll_interval_s)
    raise PrereqMissing(
        "1Password CLI sign-in",
        where=f"timed out after {timeout_s:.0f}s; last op error: {last_stderr or 'none'}",
    )
