from __future__ import annotations

import logging
import os
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.paths import (
    DARWIN_FLAKE_SYMLINK,
    HM_FLAKE_SYMLINK,
    NIXOS_FLAKE_SYMLINK,
)
from bootstrap.platform import Platform

_log = logging.getLogger(__name__)


def _flake_path_for(platform: Platform) -> Path | None:
    if platform is Platform.DARWIN:
        return DARWIN_FLAKE_SYMLINK
    if platform in (Platform.NIXOS, Platform.NIXOS_WSL):
        return NIXOS_FLAKE_SYMLINK
    if platform is Platform.LINUX_HM:
        return HM_FLAKE_SYMLINK
    return None


def _needs_sudo(dir_path: Path) -> bool:
    return dir_path.exists() and not os.access(dir_path, os.W_OK)


async def _mkdir(path: Path, *, dry_run: bool) -> None:
    if path.exists():
        return
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    if os.access(ancestor, os.W_OK):
        if not dry_run:
            path.mkdir(parents=True, exist_ok=True)
        else:
            _log.info("would mkdir -p %s", path)
    else:
        await sh.sudo_run(["mkdir", "-p", str(path)], dry_run=dry_run, destructive=True)


async def _move(src: Path, dst: Path, *, dry_run: bool) -> None:
    if _needs_sudo(src.parent) or _needs_sudo(dst.parent):
        await sh.sudo_run(["mv", str(src), str(dst)], dry_run=dry_run, destructive=True)
    else:
        if not dry_run:
            src.rename(dst)
        else:
            _log.info("would mv %s → %s", src, dst)


async def _symlink(target: Path, link: Path, *, dry_run: bool) -> None:
    args = ["ln", "-sfn", str(target), str(link)]
    if _needs_sudo(link.parent):
        await sh.sudo_run(args, dry_run=dry_run, destructive=True)
    else:
        await sh.run(args, dry_run=dry_run, destructive=True)


def linked_checkout(platform: Platform) -> Path | None:
    """The dotfiles checkout the platform's flake symlink points at, if it is one.

    A host whose registry entry sets `dotfilesDir` has its clone there, not at
    the default path, and the symlink register installed is what remembers
    where: a re-run finds the checkout through it.
    """
    link_path = _flake_path_for(platform)
    if link_path is None or not link_path.is_symlink():
        return None
    target = link_path.resolve()
    return target if (target / ".git").exists() else None


async def install_flake_symlink(platform: Platform, target: Path, *, dry_run: bool = False) -> None:
    link_path = _flake_path_for(platform)
    if link_path is None:
        raise BootstrapError(f"no default flake path for platform {platform.value}")

    if link_path.is_symlink():
        current = link_path.resolve()
        if current == target.resolve():
            _log.debug("%s already points at %s", link_path, target)
            return
        _log.info("%s points at %s — replacing with %s", link_path, current, target)
    elif link_path.exists():
        backup = link_path.with_suffix(link_path.suffix + ".before-bootstrap")
        _log.info("backing up existing %s to %s", link_path, backup)
        await _move(link_path, backup, dry_run=dry_run)

    await _mkdir(link_path.parent, dry_run=dry_run)
    _log.info("symlinking %s → %s", link_path, target)
    await _symlink(target, link_path, dry_run=dry_run)
