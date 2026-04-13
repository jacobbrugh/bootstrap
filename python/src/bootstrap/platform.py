"""Runtime OS detection for phase-list selection.

The orchestrator maps the detected platform to an OS-specific phase list.
Detection is a pure function — testable with fixture filesystems by
monkey-patching the sentinel Path constants.
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path


class Platform(Enum):
    """Supported target platforms for the bootstrap."""

    DARWIN = "darwin"
    NIXOS = "nixos"
    NIXOS_WSL = "nixos-wsl"
    LINUX_HM = "linux-hm"
    UNSUPPORTED = "unsupported"


# Sentinel files used for detection. Module-level so tests can monkey-patch.
NIXOS_SENTINEL = Path("/etc/NIXOS")
WSL_SENTINEL = Path("/proc/sys/fs/binfmt_misc/WSLInterop")


def detect() -> Platform:
    """Detect the current platform via sys.platform + sentinel files."""
    # Widen from the Literal that typeshed gives sys.platform — otherwise
    # mypy's static narrowing marks whichever branch isn't the build host's
    # as unreachable and the whole function fails --warn-unreachable.
    current: str = sys.platform
    if current == "darwin":
        return Platform.DARWIN
    if current != "linux":
        return Platform.UNSUPPORTED
    is_wsl = WSL_SENTINEL.exists()
    is_nixos = NIXOS_SENTINEL.exists()
    if is_nixos and is_wsl:
        return Platform.NIXOS_WSL
    if is_nixos:
        return Platform.NIXOS
    return Platform.LINUX_HM
