from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path


class Platform(Enum):
    DARWIN = "darwin"
    NIXOS = "nixos"
    NIXOS_WSL = "nixos-wsl"
    LINUX_HM = "linux-hm"
    UNSUPPORTED = "unsupported"


NIXOS_SENTINEL = Path("/etc/NIXOS")
WSL_SENTINEL = Path("/proc/sys/fs/binfmt_misc/WSLInterop")


def detect() -> Platform:
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
