"""Git operations for the register phase.

Every function takes the repo path explicitly and invokes `git -C <repo>`,
so callers never have to juggle cwd.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import WorkingTreeError

_log = logging.getLogger(__name__)


async def clone_or_pull(
    remote: str,
    target: Path,
    *,
    branch: str = "main",
    dry_run: bool = False,
) -> None:
    """Ensure `target` is a clean, up-to-date checkout of `remote`.

    - If absent: clone with `--branch <branch>`.
    - If present: verify remote URL matches, working tree is clean, then
      `git pull --ff-only` (never merge).
    """
    if not target.exists():
        _log.info("cloning %s into %s", remote, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        await sh.run(
            ["git", "clone", "--branch", branch, remote, str(target)],
            dry_run=dry_run,
            destructive=True,
        )
        return

    if not (target / ".git").exists():
        raise WorkingTreeError(target, "exists but is not a git repo")

    existing = await remote_url(target)
    if existing != remote:
        raise WorkingTreeError(
            target,
            f"expected remote {remote!r}, found {existing!r}",
        )

    dirty = await working_tree_status(target)
    if dirty:
        raise WorkingTreeError(
            target,
            f"uncommitted changes present: {dirty}",
        )

    _log.info("pulling latest changes in %s", target)
    await sh.run(
        ["git", "-C", str(target), "pull", "--ff-only", "origin", branch],
        dry_run=dry_run,
        destructive=True,
    )


async def working_tree_status(repo: Path) -> list[str]:
    """Return porcelain-v1 status entries, one per modified path."""
    result = await sh.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1"],
        destructive=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


async def commit(
    repo: Path,
    paths: list[Path],
    message: str,
    *,
    dry_run: bool = False,
) -> None:
    """Stage the given paths and create a commit.

    Uses `git add -- <path>...` with explicit paths — never `git add -A`.
    That's also the reason we don't need a separate "scope check" pass
    before committing: by construction, only the listed paths can enter
    the commit.
    """
    await sh.run(
        ["git", "-C", str(repo), "add", "--", *(str(p) for p in paths)],
        dry_run=dry_run,
        destructive=True,
    )
    await sh.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        dry_run=dry_run,
        destructive=True,
    )


async def push(repo: Path, *, branch: str = "main", dry_run: bool = False) -> None:
    """Push to origin. Aborts the phase if the push fails."""
    await sh.run(
        ["git", "-C", str(repo), "push", "origin", branch],
        dry_run=dry_run,
        destructive=True,
    )


async def remote_url(repo: Path, *, remote: str = "origin") -> str:
    """Return the configured URL for the given remote."""
    result = await sh.run(
        ["git", "-C", str(repo), "remote", "get-url", remote],
        destructive=False,
    )
    return result.stdout.strip()
