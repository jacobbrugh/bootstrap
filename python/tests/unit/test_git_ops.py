from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bootstrap.lib import git_ops
from bootstrap.lib.errors import WorkingTreeError
from tests.unit.conftest import ShRecorder

_REMOTE = "git@github.com:jacobbrugh/dotfiles.git"


def _make_clone(tmp_path: Path, sh_recorder: ShRecorder, *, remote: str = _REMOTE) -> Path:
    """A directory that looks like a git clone of `remote`."""
    target = tmp_path / "clone"
    (target / ".git").mkdir(parents=True)
    sh_recorder.answers[("git", "-C", str(target), "remote", "get-url", "origin")] = remote + "\n"
    return target


def test_clone_or_pull_clones_when_absent(sh_recorder: ShRecorder, tmp_path: Path) -> None:
    target = tmp_path / "sub" / "clone"
    asyncio.run(git_ops.clone_or_pull(_REMOTE, target))
    argvs = sh_recorder.argvs()
    assert ("git", "clone", "--branch", "main", _REMOTE, str(target)) in argvs
    # a fresh clone is already at the tip; it must not force-reset
    assert all("reset" not in a for a in argvs)


def test_clone_or_pull_force_syncs_existing(sh_recorder: ShRecorder, tmp_path: Path) -> None:
    """An existing clone is hard-synced to origin/main, never left behind by a
    non-fast-forwardable pull (which is how a host silently switched from a
    stale clone)."""
    target = _make_clone(tmp_path, sh_recorder)
    asyncio.run(git_ops.clone_or_pull(_REMOTE, target))
    argvs = sh_recorder.argvs()
    assert ("git", "-C", str(target), "fetch", "origin", "main") in argvs
    assert ("git", "-C", str(target), "reset", "--hard", "origin/main") in argvs
    assert ("git", "-C", str(target), "clean", "-fd") in argvs
    # the failure mode this replaces: a plain ff-only pull that can silently no-op
    assert all(a[3:5] != ("pull", "--ff-only") for a in argvs)


def test_clone_or_pull_honors_branch(sh_recorder: ShRecorder, tmp_path: Path) -> None:
    target = _make_clone(tmp_path, sh_recorder)
    asyncio.run(git_ops.clone_or_pull(_REMOTE, target, branch="release"))
    argvs = sh_recorder.argvs()
    assert ("git", "-C", str(target), "fetch", "origin", "release") in argvs
    assert ("git", "-C", str(target), "reset", "--hard", "origin/release") in argvs


def test_clone_or_pull_rejects_wrong_remote(sh_recorder: ShRecorder, tmp_path: Path) -> None:
    target = _make_clone(tmp_path, sh_recorder, remote="git@github.com:someone/else.git")
    with pytest.raises(WorkingTreeError, match="expected remote"):
        asyncio.run(git_ops.clone_or_pull(_REMOTE, target))
    # never touch a clone whose origin is unexpected
    assert all("reset" not in a for a in sh_recorder.argvs())


def test_clone_or_pull_rejects_non_repo(sh_recorder: ShRecorder, tmp_path: Path) -> None:
    target = tmp_path / "clone"
    target.mkdir()
    with pytest.raises(WorkingTreeError, match="not a git repo"):
        asyncio.run(git_ops.clone_or_pull(_REMOTE, target))


def test_clone_or_pull_dry_run_syncs(sh_recorder: ShRecorder, tmp_path: Path) -> None:
    target = _make_clone(tmp_path, sh_recorder)
    asyncio.run(git_ops.clone_or_pull(_REMOTE, target, dry_run=True))
    # dry_run threads through to sh.run (which is what skips execution); the
    # command sequence is still the force-sync one.
    argvs = sh_recorder.argvs()
    assert ("git", "-C", str(target), "reset", "--hard", "origin/main") in argvs


def test_clone_or_pull_passes_the_token_env_to_git(sh_recorder: ShRecorder, tmp_path: Path) -> None:
    """An https clone authenticates per process: the env carries the helper,
    the argv carries no token and nothing is written anywhere else."""
    target = tmp_path / "clone"
    env = git_ops.git_token_env("ghp_secret")
    asyncio.run(
        git_ops.clone_or_pull("https://github.com/jacobbrugh/dotfiles.git", target, env=env)
    )
    (call,) = sh_recorder.with_prefix("git", "clone")
    assert call.env is not None
    assert call.env["BOOTSTRAP_GIT_TOKEN"] == "ghp_secret"
    assert call.env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert "ghp_secret" not in " ".join(call.argv)


def test_push_passes_the_token_env_to_git(sh_recorder: ShRecorder, tmp_path: Path) -> None:
    target = _make_clone(tmp_path, sh_recorder)
    asyncio.run(git_ops.push(target, env=git_ops.git_token_env("ghp_secret")))
    (call,) = sh_recorder.with_prefix("git", "-C", str(target), "push")
    assert call.env is not None
    assert call.env["BOOTSTRAP_GIT_TOKEN"] == "ghp_secret"


def test_token_env_keeps_the_token_out_of_the_helper() -> None:
    env = git_ops.git_token_env("ghp_secret")
    assert "ghp_secret" not in env["GIT_CONFIG_VALUE_0"]
    assert "$BOOTSTRAP_GIT_TOKEN" in env["GIT_CONFIG_VALUE_0"]
