"""Typed subprocess wrapper for the bootstrap.

Every shell interaction goes through `run`, `sudo_run`, `prime_sudo`, or
`run_powershell`. Guarantees:

- never `shell=True` (no injection surface; all args explicit)
- typed return value (`Result` dataclass with stdout, stderr, returncode, duration)
- dry-run aware: destructive commands become no-ops that log "would run: …";
  read-only commands (`destructive=False`) still execute so decision trees
  can be exercised safely in dry-run mode
- all commands logged at DEBUG via stdlib logging (Rich handler installed
  by `bootstrap.lib.log`)

`run_powershell` is designed but unused in this change — it locks in the
contract for the Windows migration session so call sites can be added
later without changing plumbing.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
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


def run(
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
        input_text: text piped to stdin (implies `text=True`).
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
        completed = subprocess.run(
            cmd_tuple,
            check=False,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=input_text,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError as exc:
        raise ShellError(list(cmd_tuple), 127, str(exc)) from exc
    duration = time.monotonic() - start

    result = Result(
        cmd=cmd_tuple,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_s=duration,
    )
    if check and result.returncode != 0:
        raise ShellError(list(cmd_tuple), result.returncode, result.stderr)
    return result


def sudo_run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    destructive: bool = True,
) -> Result:
    """Run a command under sudo, non-interactively.

    Prefixes with `sudo -n`, so a password prompt never appears mid-phase.
    Relies on `prime_sudo()` having been called earlier in the phase to
    populate the credential cache.
    """
    return run(
        ["sudo", "-n", *cmd],
        check=check,
        capture=capture,
        cwd=cwd,
        env=env,
        dry_run=dry_run,
        destructive=destructive,
    )


def prime_sudo(*, dry_run: bool = False) -> None:
    """Prime the sudo credential cache by prompting once, interactively.

    Must be called from an interactive context (TTY). `dry_run=True` skips
    the prompt entirely so dry-run flows never touch real sudo state.
    """
    if dry_run:
        _log.info("would run: sudo -v")
        return
    _log.info("priming sudo credential cache — you may be prompted for your password")
    subprocess.run(["sudo", "-v"], check=True)


def run_powershell(
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
    return run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", "-"],
        check=check,
        capture=capture,
        cwd=cwd,
        input_text=script,
        dry_run=dry_run,
        destructive=destructive,
    )
