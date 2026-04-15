"""Hostname detection, architecture → Nix system string, Darwin rename."""

from __future__ import annotations

import logging
import platform as stdlib_platform
import re
import subprocess
import sys

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.platform import Platform, detect

_log = logging.getLogger(__name__)

_HOSTNAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_HOSTNAME_DROP_RE = re.compile(r"[^a-z0-9-]")


def validate_hostname(name: str) -> None:
    """Raise `BootstrapError` if `name` isn't a DNS-safe hostname."""
    if not _HOSTNAME_RE.match(name):
        raise BootstrapError(f"invalid hostname {name!r}: must match [a-z][a-z0-9-]*")


def sanitize_hostname_default(name: str) -> str:
    """Normalize an OS-derived hostname into a DNS-safe bootstrap default.

    macOS `scutil --get LocalHostName` can return names containing
    apostrophes, spaces, or other characters that don't match the bootstrap's
    hostname regex. Lowercasing alone isn't enough — those characters have
    to be dropped too. Leading hyphens are stripped because the regex
    requires a leading letter. If the result is empty (the OS name was
    entirely junk), return "host" as a visible placeholder.
    """
    lowered = name.lower()
    stripped = _HOSTNAME_DROP_RE.sub("", lowered)
    trimmed = stripped.lstrip("-")
    return trimmed or "host"


def system_string() -> str:
    """Return the Nix system string for the current host (e.g. `aarch64-darwin`).

    Used by the register phase to populate the `system = "…"` field of the
    new host's entry in `registry.toml`.
    """
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


def detect_hostname() -> str:
    """Return the current machine's hostname.

    Synchronous because the CLI entry point calls this BEFORE `asyncio.run`
    starts the event loop — the whole point is that no asyncio subprocess
    machinery has fired yet by the time `_go` is reached, so the very first
    interactive prompt inside the event loop runs on a clean slate.

    Darwin: `scutil --get LocalHostName`. Linux / NixOS / WSL: `hostname -s`.
    """
    platform = detect()
    if platform is Platform.DARWIN:
        result = subprocess.run(
            ["scutil", "--get", "LocalHostName"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    if platform in (Platform.NIXOS, Platform.NIXOS_WSL, Platform.LINUX_HM):
        result = subprocess.run(
            ["hostname", "-s"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    raise BootstrapError(f"cannot detect hostname on platform {platform.value}")


async def rename_darwin(new_name: str, *, dry_run: bool = False) -> None:
    """Rename the macOS machine at the OS level.

    Sets `LocalHostName`, `ComputerName`, and `HostName` via `scutil --set`.
    Each invocation requires sudo. After the sets, re-reads both
    `LocalHostName` and `HostName` via `scutil --get` to verify the rename
    actually stuck.
    """
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
