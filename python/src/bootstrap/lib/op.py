"""1Password CLI wrapper.

All calls go through `op` with `--format=json` where available, so we get
structured output instead of parsing ad-hoc text. The phase that uses this
assumes `op` has been signed in to at least one account — the `ephemeral_secrets`
phase polls `signin_wait` to block until that's true.

On macOS the 1Password desktop app verifies CLI authenticity via XPC code
signature checks (Developer ID `2BUA8C4S2C`, AgileBits Inc.). The Nix-
packaged `_1password-cli` is not AgileBits-signed and is rejected by the
desktop app's XPC server, so every `op` call returns "account is not
signed in" — even with "Integrate with 1Password CLI" enabled. Use the
Homebrew-installed binary instead, which ships the official signed `op`.
See https://developer.1password.com/docs/cli/app-integration-security/.
"""

from __future__ import annotations

import json
import logging
import os
import time

from bootstrap.lib import sh
from bootstrap.lib.errors import PrereqMissing
from bootstrap.platform import Platform, detect

_log = logging.getLogger(__name__)

# Canonical locations the Homebrew `1password-cli` cask drops the
# AgileBits-signed binary on macOS. Apple Silicon writes to /opt/homebrew,
# Intel to /usr/local; the .pkg-based cask may also place it under
# /usr/local on Apple Silicon, so check both.
_DARWIN_OP_PATHS = ("/opt/homebrew/bin/op", "/usr/local/bin/op")


def _op_binary() -> str:
    """Return the best `op` binary path for the current platform.

    On Darwin, prefer the Homebrew-installed signed binary so the desktop
    app accepts XPC connections from it. On other platforms, fall back to
    PATH (Nix's wrapper-injected `op` for the bootstrap process).
    """
    if detect() is Platform.DARWIN:
        for candidate in _DARWIN_OP_PATHS:
            if os.path.exists(candidate):
                return candidate
    return "op"


def _whoami_result() -> sh.Result:
    return sh.run(
        [_op_binary(), "whoami", "--format=json"],
        check=False,
        destructive=False,
    )


def _parse_whoami(result: sh.Result) -> bool:
    if not result.ok():
        return False
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and "user_uuid" in parsed


def whoami() -> bool:
    """Return True if `op` is signed in to at least one account."""
    return _parse_whoami(_whoami_result())


def read(path: str) -> str:
    """Read a secret via `op read op://vault/item/field`.

    Returns the raw value with the trailing newline stripped (op always
    appends one). Raises `ShellError` on failure.
    """
    result = sh.run([_op_binary(), "read", path], destructive=False)
    return result.stdout.rstrip("\n")


def signin_wait(timeout_s: float = 180.0, poll_interval_s: float = 2.0) -> None:
    """Poll `op whoami` until it succeeds, or raise after `timeout_s`.

    Emits a progress line every ~10 seconds while waiting, including the
    most recent stderr from `op whoami` so the user can see *what* is
    failing (approval pending, GUI locked, no account, etc.) instead of
    an opaque timeout.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    attempts = 0
    last_stderr = ""
    while time.monotonic() < deadline:
        result = _whoami_result()
        if _parse_whoami(result):
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
        time.sleep(poll_interval_s)
    raise PrereqMissing(
        "1Password CLI sign-in",
        where=f"timed out after {timeout_s:.0f}s; last op error: {last_stderr or 'none'}",
    )
