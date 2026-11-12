from __future__ import annotations

import logging
import platform as stdlib_platform
import re
import sys

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.platform import Platform, detect

_log = logging.getLogger(__name__)

_HOSTNAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_HOSTNAME_DROP_RE = re.compile(r"[^a-z0-9-]")


def validate_hostname(name: str) -> None:
    if not _HOSTNAME_RE.match(name):
        raise BootstrapError(f"invalid hostname {name!r}: must match [a-z][a-z0-9-]*")


def sanitize_hostname_default(name: str) -> str:
    lowered = name.lower()
    stripped = _HOSTNAME_DROP_RE.sub("", lowered)
    trimmed = stripped.lstrip("-")
    return trimmed or "host"


def system_string() -> str:
    machine = stdlib_platform.machine()
    arch_map = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }
    arch = arch_map.get(machine)
    if arch is None:
        raise BootstrapError(f"unsupported machine architecture: {machine!r}")
    current: str = sys.platform
    if current == "darwin":
        return f"{arch}-darwin"
    if current == "linux":
        return f"{arch}-linux"
    raise BootstrapError(f"unsupported sys.platform: {current!r}")


async def detect_hostname() -> str:
    platform = detect()
    if platform is Platform.DARWIN:
        result = await sh.run(["scutil", "--get", "LocalHostName"], destructive=False)
        return result.stdout.strip()
    if platform in (Platform.NIXOS, Platform.NIXOS_WSL, Platform.LINUX_HM):
        result = await sh.run(["hostname", "-s"], destructive=False)
        return result.stdout.strip()
    raise BootstrapError(f"cannot detect hostname on platform {platform.value}")


async def rename_darwin(new_name: str, *, dry_run: bool = False) -> None:
    validate_hostname(new_name)
    for key in ("LocalHostName", "ComputerName", "HostName"):
        await sh.sudo_run(
            ["scutil", "--set", key, new_name],
            dry_run=dry_run,
            destructive=True,
        )
    if dry_run:
        return
    for key in ("LocalHostName", "HostName"):
        result = await sh.run(["scutil", "--get", key], destructive=False)
        actual = result.stdout.strip()
        if actual != new_name:
            raise BootstrapError(
                f"scutil --set {key} didn't stick: expected {new_name!r}, got {actual!r}"
            )
    _log.info("machine renamed to %s", new_name)
