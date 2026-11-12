from __future__ import annotations

import asyncio
import json
import logging
import time

from bootstrap.lib import sh

_log = logging.getLogger(__name__)

# The privileged bootstrap age key (anchor `bootstrap_privileged` in both
# repos' .sops.yaml). The single 1Password item the bootstrap reads at
# runtime — real-device darwin bootstraps hold its value in memory only.
BOOTSTRAP_KEY_ITEM = "op://Personal/bw2otnlpjhm434grbcbpb6dady/credential"


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
    return _parse_user_me(await _user_me_result())


async def read(path: str) -> str:
    result = await sh.run(["op", "read", path], destructive=False)
    return result.stdout.rstrip("\n")


async def signin_wait(poll_interval_s: float = 2.0) -> None:
    _log.info("waiting for 1Password CLI sign-in — press Ctrl-C to abort")
    start = time.monotonic()
    attempts = 0
    last_stderr = ""
    while True:
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
