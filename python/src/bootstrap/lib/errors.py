from __future__ import annotations

from pathlib import Path


class BootstrapError(Exception):
    pass


class PrereqMissing(BootstrapError):
    def __init__(self, name: str, where: str | None = None) -> None:
        if where:
            super().__init__(f"prerequisite missing: {name} (checked: {where})")
        else:
            super().__init__(f"prerequisite missing: {name}")
        self.name = name
        self.where = where


class PlatformError(BootstrapError):
    pass


class ShellError(BootstrapError):
    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        snippet = stderr.strip()[:400]
        super().__init__(f"command {cmd!r} exited {returncode}: {snippet}")
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


class WorkingTreeError(BootstrapError):
    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message
