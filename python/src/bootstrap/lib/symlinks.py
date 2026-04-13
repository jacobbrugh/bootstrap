"""Install the OS-specific default flake path as a symlink to the canonical repo.

Each supported target has exactly one default flake location; all three
resolve symlinks before taking the parent directory. So after this runs:

- Darwin:    `darwin-rebuild switch` (no --flake) reads from the canonical repo
- NixOS:     `nixos-rebuild switch` (no --flake) reads from the canonical repo
- Linux HM:  `home-manager switch` (no --flake) reads from the canonical repo

Existing regular files at the default path are backed up to
`<path>.before-bootstrap` before the symlink is created. Parent directories
are created with sudo if they don't exist and we can't write to them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.paths import (
    CANONICAL_DOTFILES,
    DARWIN_FLAKE_SYMLINK,
    HM_FLAKE_SYMLINK,
    NIXOS_FLAKE_SYMLINK,
)
from bootstrap.platform import Platform

_log = logging.getLogger(__name__)


def _flake_path_for(platform: Platform) -> Path | None:
    """Return the default flake-path for `platform`, or None if unsupported."""
    if platform is Platform.DARWIN:
        return DARWIN_FLAKE_SYMLINK
    if platform in (Platform.NIXOS, Platform.NIXOS_WSL):
        return NIXOS_FLAKE_SYMLINK
    if platform is Platform.LINUX_HM:
        return HM_FLAKE_SYMLINK
    return None


def _needs_sudo(dir_path: Path) -> bool:
    """True if the directory exists and we lack write permission on it."""
    return dir_path.exists() and not os.access(dir_path, os.W_OK)


def _mkdir(path: Path, *, dry_run: bool) -> None:
    """mkdir -p, with sudo when necessary."""
    if path.exists():
        return
    # Walk up to find the first existing ancestor and test writability there.
    ancestor = path
    while not ancestor.exists():
        ancestor = ancestor.parent
    if os.access(ancestor, os.W_OK):
        if not dry_run:
            path.mkdir(parents=True, exist_ok=True)
        else:
            _log.info("would mkdir -p %s", path)
    else:
        sh.sudo_run(["mkdir", "-p", str(path)], dry_run=dry_run, destructive=True)


def _move(src: Path, dst: Path, *, dry_run: bool) -> None:
    """mv, with sudo when necessary."""
    if _needs_sudo(src.parent) or _needs_sudo(dst.parent):
        sh.sudo_run(["mv", str(src), str(dst)], dry_run=dry_run, destructive=True)
    else:
        if not dry_run:
            src.rename(dst)
        else:
            _log.info("would mv %s → %s", src, dst)


def _symlink(target: Path, link: Path, *, dry_run: bool) -> None:
    """ln -sfn, with sudo when necessary. Always atomic (ln -sfn replaces)."""
    args = ["ln", "-sfn", str(target), str(link)]
    if _needs_sudo(link.parent):
        sh.sudo_run(args, dry_run=dry_run, destructive=True)
    else:
        sh.run(args, dry_run=dry_run, destructive=True)


def install_flake_symlink(platform: Platform, *, dry_run: bool = False) -> None:
    """Install the default-flake-path symlink for `platform`.

    Idempotent: re-runs check the existing symlink target and no-op if it
    already points at `CANONICAL_DOTFILES/flake.nix`.
    """
    link_path = _flake_path_for(platform)
    if link_path is None:
        raise BootstrapError(f"no default flake path for platform {platform.value}")
    target = CANONICAL_DOTFILES / "flake.nix"

    if link_path.is_symlink():
        current = link_path.resolve()
        if current == target.resolve():
            _log.debug("%s already points at %s", link_path, target)
            return
        _log.info("%s points at %s — replacing with %s", link_path, current, target)
    elif link_path.exists():
        backup = link_path.with_suffix(link_path.suffix + ".before-bootstrap")
        _log.info("backing up existing %s to %s", link_path, backup)
        _move(link_path, backup, dry_run=dry_run)

    _mkdir(link_path.parent, dry_run=dry_run)
    _log.info("symlinking %s → %s", link_path, target)
    _symlink(target, link_path, dry_run=dry_run)
