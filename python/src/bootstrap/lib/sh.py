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
    sudo_cmd: tuple[str, ...] = ("sudo", "-nH", *cmd)

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
    return "password is required" in stderr


async def prime_sudo(*, dry_run: bool = False) -> None:
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
