from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import WorkingTreeError

_log = logging.getLogger(__name__)

#: A git credential helper that answers `get` from an env var. The token is
#: handed to git through the environment rather than the URL or a file on the
#: machine, so it reaches neither the clone's .git/config, nor sh.run's
#: --verbose argv log, nor anything that outlives the process. This is the
#: only way bootstrap authenticates to github.com over https: the registry
#: clone at flash, and register's clone and push on a sandbox host. Nothing
#: at switch time needs a credential — dotfiles has no private flake input.
_ENV_CREDENTIAL_HELPER = (
    "!f() { case $1 in get) echo username=x-access-token; "
    'echo "password=$BOOTSTRAP_GIT_TOKEN";; esac; }; f'
)


def git_token_env(token: str) -> dict[str, str]:
    """Env for a git invocation that authenticates to github.com with `token`."""
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": _ENV_CREDENTIAL_HELPER,
        "BOOTSTRAP_GIT_TOKEN": token,
    }


async def clone_or_pull(
    remote: str,
    target: Path,
    *,
    branch: str = "main",
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> None:
    if not target.exists():
        _log.info("cloning %s into %s", remote, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        await sh.run(
            ["git", "clone", "--branch", branch, remote, str(target)],
            env=env,
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

    # Force-sync to origin/<branch>, NOT `git pull --ff-only`. The canonical
    # dotfiles clone is machine-managed: register regenerates and re-pushes this
    # host's harvest every run, and the switch builds the host from this clone
    # via /etc/nixos. A plain ff-only pull silently leaves the clone behind
    # whenever it cannot fast-forward — a harvest commit whose push lost a race
    # while origin advanced, an origin rebase, a detached HEAD — and the host
    # then switches from STALE sources (e.g. a router that never picks up a
    # pushed fix and boot-loops on the old one). fetch + reset --hard + clean
    # instead: everything the host needs is on origin (the harvest is
    # deterministic and re-pushed on the next run), so discarding local
    # divergence or dirt is the recovery, not a hazard.
    _log.info("force-syncing %s to origin/%s", target, branch)
    await sh.run(
        ["git", "-C", str(target), "fetch", "origin", branch],
        env=env,
        dry_run=dry_run,
        destructive=True,
    )
    await sh.run(
        ["git", "-C", str(target), "reset", "--hard", f"origin/{branch}"],
        dry_run=dry_run,
        destructive=True,
    )
    await sh.run(
        ["git", "-C", str(target), "clean", "-fd"],
        dry_run=dry_run,
        destructive=True,
    )


async def commit(
    repo: Path,
    paths: list[Path],
    message: str,
    *,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> None:
    await sh.run(
        ["git", "-C", str(repo), "add", "--", *(str(p) for p in paths)],
        dry_run=dry_run,
        destructive=True,
    )
    await sh.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        env=env,
        dry_run=dry_run,
        destructive=True,
    )


async def push(
    repo: Path,
    *,
    branch: str = "main",
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> None:
    await sh.run(
        ["git", "-C", str(repo), "push", "origin", branch],
        env=env,
        dry_run=dry_run,
        destructive=True,
    )


async def remote_url(repo: Path, *, remote: str = "origin") -> str:
    result = await sh.run(
        ["git", "-C", str(repo), "remote", "get-url", remote],
        destructive=False,
    )
    return result.stdout.strip()


async def _rev_parse_head(repo: Path) -> str:
    result = await sh.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        destructive=False,
    )
    return result.stdout.strip()


@contextlib.asynccontextmanager
async def transactional_edit(
    repo: Path,
    *,
    dry_run: bool = False,
) -> AsyncIterator[None]:
    if dry_run:
        _log.info("would record initial HEAD of %s for transactional rollback", repo)
        try:
            yield
        finally:
            _log.info("would roll back %s on failure (dry-run)", repo)
        return

    initial_head = await _rev_parse_head(repo)
    _log.debug("transactional_edit: initial HEAD of %s = %s", repo, initial_head)
    try:
        yield
    except BaseException:
        _log.warning("rolling back %s to %s after failure", repo, initial_head[:12])
        await sh.run(
            ["git", "-C", str(repo), "reset", "--hard", initial_head],
            check=False,
            destructive=True,
        )
        raise
