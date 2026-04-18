"""Exception hierarchy for the bootstrap.

`cli.main` catches `BootstrapError` at the boundary and prints a rich
traceback. Anything else propagates as an unexpected crash with a full
stack trace.
"""

from __future__ import annotations

from pathlib import Path


class BootstrapError(Exception):
    """Base class for all bootstrap-originated errors."""


class UserAbort(BootstrapError):
    """User declined an interactive prompt or explicitly aborted."""


class PrereqMissing(BootstrapError):
    """A required prerequisite is missing (tool, file, env var, etc.)."""

    def __init__(self, name: str, where: str | None = None) -> None:
        if where:
            super().__init__(f"prerequisite missing: {name} (checked: {where})")
        else:
            super().__init__(f"prerequisite missing: {name}")
        self.name = name
        self.where = where


class PlatformError(BootstrapError):
    """Attempted to do something that doesn't apply on the current platform."""


class ShellError(BootstrapError):
    """A subprocess invocation failed in a way we expected to succeed."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        snippet = stderr.strip()[:400]
        super().__init__(f"command {cmd!r} exited {returncode}: {snippet}")
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


class WorkingTreeError(BootstrapError):
    """Unexpected state in the dotfiles repo working tree."""

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message
