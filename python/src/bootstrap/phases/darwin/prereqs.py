"""Darwin prereqs — Homebrew install + /etc conflict resolution.

Must run before any Darwin phase that expects `brew` on PATH, and before
`darwin-rebuild switch` can succeed — nix-darwin's activation fails on
first run if `/etc/nix/nix.conf`, `/etc/zshrc`, or `/etc/bashrc` are
regular files (the Nix installer creates them as regular files; nix-darwin
replaces them with symlinks into `/etc/static/`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from bootstrap.lib import brew, sh
from bootstrap.lib.paths import SOPS_AGE_DIR, SSH_DIR
from bootstrap.lib.runtime import Context

NAME = "prereqs"

_log = logging.getLogger(__name__)

# Files created by the Nix installer that nix-darwin wants to manage itself.
# Move them to a `.before-nix-darwin` suffix so activation can proceed.
_NIX_DARWIN_CONFLICTS: tuple[Path, ...] = (
    Path("/etc/nix/nix.conf"),
    Path("/etc/zshrc"),
    Path("/etc/bashrc"),
)


async def run(ctx: Context) -> None:
    _log.info("priming sudo (you may be prompted once for your password)")
    await sh.prime_sudo(dry_run=ctx.dry_run)

    await brew.install_script(dry_run=ctx.dry_run)

    _ensure_dir(SSH_DIR, dry_run=ctx.dry_run)
    _ensure_dir(SOPS_AGE_DIR, dry_run=ctx.dry_run)

    for conflict in _NIX_DARWIN_CONFLICTS:
        await _resolve_nix_darwin_conflict(conflict, dry_run=ctx.dry_run)


def _ensure_dir(path: Path, *, dry_run: bool) -> None:
    if path.exists():
        return
    if dry_run:
        _log.info("would mkdir -p %s (mode 0700)", path)
        return
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


async def _resolve_nix_darwin_conflict(path: Path, *, dry_run: bool) -> None:
    """Move a regular file at `path` aside so nix-darwin can manage it."""
    if not path.exists() or path.is_symlink():
        return
    backup = path.parent / (path.name + ".before-nix-darwin")
    if backup.exists():
        _log.debug("%s already backed up at %s", path, backup)
        return
    _log.info("moving %s aside to %s", path, backup)
    await sh.sudo_run(
        ["mv", str(path), str(backup)],
        dry_run=dry_run,
        destructive=True,
    )
