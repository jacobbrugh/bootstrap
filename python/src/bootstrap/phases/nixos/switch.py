"""The NixOS switch phase: activate the dotfiles generation, then prove it is up.

`nixos-rebuild switch` exits 4 when some unit failed to start or restart —
and it exits 4 just the same when the activation itself failed first (a
sops-nix decrypt failure is a failed activation snippet, and the units that
need the missing secrets then fail to start), so the exit code cannot tell a
broken host from a hiccup. This phase therefore decides "done" from the
system it produced: the new generation is the current one, every secret
sops-nix declares for it is on disk, the operator's home-manager activation
is active, and no unit is failed. A host that fails that is not switched, and
the first-boot unit keeps retrying — with the bootstrap age key still on disk
— until it is.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError, ShellError
from bootstrap.lib.runtime import Context

NAME = "switch"

_log = logging.getLogger(__name__)

#: The running generation's activation script: it names the sops-nix manifest
#: (`sops-install-secrets /nix/store/…-manifest.json`) whose `secrets[].path`
#: are exactly the files the switch had to produce.
_ACTIVATE_PATH = Path("/run/current-system/activate")
_MANIFEST_RE = re.compile(r"sops-install-secrets\S*\s+(\S+manifest\.json)")

_CURRENT_SYSTEM = "/run/current-system"
_SYSTEM_PROFILE = "/nix/var/nix/profiles/system"


async def run(ctx: Context) -> None:
    _log.info("running `sudo nixos-rebuild switch`")
    if ctx.dry_run:
        await sh.sudo_run(
            ["nixos-rebuild", "switch"],
            dry_run=True,
            destructive=True,
        )
        return
    result = await sh.sudo_run(
        ["nixos-rebuild", "switch"],
        check=False,
        capture=False,
        destructive=True,
    )
    if not result.ok() and result.returncode != 4:
        raise ShellError(list(result.cmd), result.returncode, result.stderr or "")
    if result.returncode == 4:
        _log.warning(
            "nixos-rebuild exited 4 (some units failed to start or restart) — "
            "verifying the activated system before accepting it"
        )
    await verify_activated_system(_current_user())


async def verify_activated_system(user: str) -> None:
    """Raise unless the system the switch produced is the one asked for, and healthy."""
    problems = await activation_problems(user)
    if problems:
        raise BootstrapError("the switch did not produce a working system: " + "; ".join(problems))
    _log.info("switched: generation active, every declared secret present, no failed units")


async def activation_problems(user: str) -> list[str]:
    problems: list[str] = []

    current = await _readlink(_CURRENT_SYSTEM)
    profile = await _readlink(_SYSTEM_PROFILE)
    if not current or current != profile:
        problems.append(
            f"{_CURRENT_SYSTEM} is {current or '(unresolved)'} but the system profile "
            f"is {profile or '(unresolved)'} — the new generation is not active"
        )

    for path in _declared_secret_paths(_ACTIVATE_PATH):
        if not _non_empty_file(path):
            problems.append(f"sops secret {path} is missing or empty")

    hm_unit = f"home-manager-{user}.service"
    state = await _unit_state(hm_unit)
    if state != "active":
        problems.append(f"{hm_unit} is {state}")

    failed = await _failed_units()
    if failed:
        problems.append("failed units: " + ", ".join(failed))
    return problems


def _declared_secret_paths(activate: Path) -> list[Path]:
    """Every secret path the running generation's sops-nix manifest declares."""
    try:
        text = activate.read_text()
    except OSError:
        return []
    match = _MANIFEST_RE.search(text)
    if match is None:
        return []
    try:
        manifest = json.loads(Path(match.group(1)).read_text())
    except (OSError, ValueError):
        return []
    secrets = manifest.get("secrets", []) if isinstance(manifest, dict) else []
    return [
        Path(entry["path"])
        for entry in secrets
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]


def _non_empty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


async def _readlink(path: str) -> str:
    result = await sh.run(["readlink", "-f", path], check=False, destructive=False)
    return result.stdout.strip() if result.ok() else ""


async def _unit_state(unit: str) -> str:
    result = await sh.run(["systemctl", "is-active", unit], check=False, destructive=False)
    return result.stdout.strip() or "unknown"


async def _failed_units() -> list[str]:
    result = await sh.run(
        ["systemctl", "--failed", "--no-legend", "--plain"],
        check=False,
        destructive=False,
    )
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


def _current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name
