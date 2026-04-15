"""Async subprocess wrapper.

Every external command goes through `run()`, `sudo_run()`, `prime_sudo()`,
or `run_powershell()`. Never `shell=True`, never a string that gets split —
`cmd` is always a `Sequence[str]` passed to `asyncio.create_subprocess_exec`.

Guarantees:
- typed `Result` dataclass with stdout/stderr/returncode/duration
- dry-run aware: destructive commands become a "would run: …" log with a
  skipped Result; read-only commands (`destructive=False`) execute anyway
  so decision trees are exercised under `--dry-run`
- every invocation is logged at DEBUG with the full command line
- `sudo_run` self-heals when Homebrew (or anything else) wipes the sudo
  credential cache mid-phase: on a "password is required" failure it
  re-runs `prime_sudo` once and retries the real command
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from bootstrap.lib.errors import PlatformError, ShellError
from bootstrap.platform import Platform, detect

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Result:
    """Captured output of a shell command."""

    cmd: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    dry_run_skipped: bool = False

    def ok(self) -> bool:
        return self.returncode == 0


async def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    dry_run: bool = False,
    destructive: bool = True,
) -> Result:
    """Run a command and return a typed `Result`.

    Arguments:
        cmd: command + args as a sequence. No shell expansion.
        check: if True and the command exits non-zero, raise `ShellError`.
        capture: if True, capture stdout/stderr into the Result.
        cwd: working directory for the subprocess.
        env: environment. If None, inherits the parent process env.
        input_text: text piped to stdin. Implies stdin=PIPE.
        dry_run: if True AND `destructive`, skip execution and return a no-op Result.
        destructive: if False, the command runs even in dry-run mode (for read-only ops).
    """
    cmd_tuple = tuple(cmd)
    rendered = shlex.join(cmd_tuple)

    if dry_run and destructive:
        _log.info("would run: %s", rendered)
        return Result(
            cmd=cmd_tuple,
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.0,
            dry_run_skipped=True,
        )

    _log.debug("$ %s", rendered)
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_tuple,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE if capture else None,
            stderr=asyncio.subprocess.PIPE if capture else None,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError as exc:
        raise ShellError(list(cmd_tuple), 127, str(exc)) from exc

    input_bytes = input_text.encode("utf-8") if input_text is not None else None
    stdout_bytes, stderr_bytes = await proc.communicate(input=input_bytes)
    duration = time.monotonic() - start

    result = Result(
        cmd=cmd_tuple,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
        stderr=stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
        duration_s=duration,
    )
    if check and result.returncode != 0:
        raise ShellError(list(cmd_tuple), result.returncode, result.stderr)
    return result


async def sudo_run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    destructive: bool = True,
) -> Result:
    """Run a command under sudo, using the primed credential cache.

    Tries `sudo -n` (non-interactive) first. If the credential cache has
    been invalidated — typically because sudo's default `timestamp_timeout`
    (5 minutes) lapsed during a long-running operation like Homebrew's
    installer — re-prime once (prompting the user) and retry. Any other
    sudo failure propagates unchanged.
    """
    sudo_cmd: tuple[str, ...] = ("sudo", "-n", *cmd)

    async def _invoke() -> Result:
        return await run(
            sudo_cmd,
            check=False,
            capture=capture,
            cwd=cwd,
            env=env,
            dry_run=dry_run,
            destructive=destructive,
        )

    result = await _invoke()
    if result.dry_run_skipped or result.ok():
        return result
    if _sudo_cache_miss(result.stderr):
        _log.info("sudo credential cache was cleared; re-priming (you may be prompted)")
        await prime_sudo(dry_run=dry_run)
        result = await _invoke()
    if check and not result.ok():
        raise ShellError(list(result.cmd), result.returncode, result.stderr)
    return result


def _sudo_cache_miss(stderr: str) -> bool:
    """True if `sudo -n` stderr indicates an empty/stale credential cache.

    `sudo -n` prints `sudo: a password is required` when the cache is
    missing and asks to prompt. Anything else — permission denied, command
    not found, etc. — is not something re-priming can recover from.
    """
    return "password is required" in stderr


async def prime_sudo(*, dry_run: bool = False) -> None:
    """Prime the sudo credential cache by prompting once, interactively.

    Must be called from an interactive context (TTY). `dry_run=True` skips
    the prompt entirely so dry-run flows never touch real sudo state.

    Runs `sudo -v` as a real subprocess with stdin/stdout/stderr inherited
    from the parent, so sudo talks to the user directly on the controlling
    terminal via `/dev/tty` regardless of what fd 0 is.
    """
    if dry_run:
        _log.info("would run: sudo -v")
        return
    _log.info("priming sudo credential cache — you may be prompted for your password")
    proc = await asyncio.create_subprocess_exec(
        "sudo",
        "-v",
        stdin=None,
        stdout=None,
        stderr=None,
    )
    returncode = await proc.wait()
    if returncode != 0:
        raise ShellError(["sudo", "-v"], returncode, "")


async def run_powershell(
    script: str,
    *,
    shell: str = "powershell.exe",
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
    dry_run: bool = False,
    destructive: bool = True,
) -> Result:
    """Run a PowerShell script on the Windows host from inside WSL.

    The script is piped on stdin to avoid argument-quoting hell. Requires
    `Platform.NIXOS_WSL` — on native Darwin/Linux this raises `PlatformError`.

    Unused in this change; the contract is locked in for the Windows
    migration session so call sites can be added under `phases/windows/`
    without changing plumbing.
    """
    platform = detect()
    if platform is not Platform.NIXOS_WSL:
        raise PlatformError(
            f"run_powershell requires Platform.NIXOS_WSL; detected {platform.value}"
        )
    return await run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", "-"],
        check=check,
        capture=capture,
        cwd=cwd,
        input_text=script,
        dry_run=dry_run,
        destructive=destructive,
    )
