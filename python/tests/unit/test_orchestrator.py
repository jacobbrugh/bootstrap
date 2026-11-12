"""run_full decides from register's post-condition, never from a marker.

The wedge this guards: run_full used to skip ssh+register once /etc/nixos pointed
at the canonical clone — a symlink register installed even when it failed, and
which resolves even when dangling — so a host that never registered, or whose
key drifted, switched onto secrets it could not decrypt on every retry forever
(a real host did). Now the only question is whether the key on this disk opens the host's
secrets in a clone force-synced to origin/main; a host that passes goes
straight to switch + post, a host that fails registers again.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import pytest
from ruamel.yaml.comments import CommentedMap

from bootstrap import orchestrator
from bootstrap.lib import git_ops, secrets
from bootstrap.lib.errors import ShellError
from bootstrap.lib.runtime import Context
from bootstrap.phases.common import register
from bootstrap.platform import Platform

_REMOTE = "git@github.com:jacobbrugh/dotfiles.git"


def _ctx(tmp_path: Path) -> Context:
    return Context(platform=Platform.NIXOS, hostname="rtr9", canonical_repo=tmp_path / "dotfiles")


def _seed_clone(repo: Path) -> None:
    (repo / ".git").mkdir(parents=True)
    (repo / "host_registry.toml").write_text('[rtr9]\nsystem = "x86_64-linux"\n')
    (repo / ".sops.yaml").write_text(
        "keys:\n    - &bootstrap_privileged age1pq1p\ncreation_rules: []\n"
    )


def _wire(monkeypatch: pytest.MonkeyPatch, events: list[str], *, registered: bool) -> None:
    async def fake_prereqs(ctx: Context) -> None:
        events.append("prereqs")

    async def fake_remote(ctx: Context) -> str:
        return _REMOTE

    async def fake_clone_or_pull(remote: str, target: Path, **kw: object) -> None:
        events.append(f"clone_or_pull:{remote}")

    async def fake_is_registered(ctx: Context, sops_doc: CommentedMap, tags: Iterable[str]) -> bool:
        events.append("is_registered")
        return registered

    async def fake_switch(ctx: Context) -> None:
        events.append("switch")

    async def fake_post(ctx: Context) -> None:
        events.append("post")

    async def fake_secret_zero(ctx: Context) -> None:
        events.append("secret_zero")

    async def fake_ssh(ctx: Context) -> None:
        events.append("ssh")

    async def fake_register(ctx: Context) -> None:
        events.append("register")

    @contextlib.asynccontextmanager
    async def fake_ephemeral(ctx: Context) -> AsyncIterator[None]:
        events.append("ephemeral_secrets")
        yield

    monkeypatch.setattr(orchestrator, "_dispatch_prereqs", fake_prereqs)
    monkeypatch.setattr(register, "dotfiles_remote", fake_remote)
    monkeypatch.setattr(register, "is_registered", fake_is_registered)
    monkeypatch.setattr(git_ops, "clone_or_pull", fake_clone_or_pull)
    monkeypatch.setattr(orchestrator, "_dispatch_switch", fake_switch)
    monkeypatch.setattr(orchestrator, "_dispatch_post", fake_post)
    monkeypatch.setattr(orchestrator, "_dispatch_secret_zero", fake_secret_zero)
    monkeypatch.setattr(orchestrator, "_run_ssh_inner", fake_ssh)
    monkeypatch.setattr(orchestrator, "_run_register_inner", fake_register)
    monkeypatch.setattr(secrets, "ephemeral_secrets", fake_ephemeral)


def test_no_symlink_gate() -> None:
    """The flake symlink decides nothing: the gate that read it is gone."""
    assert not hasattr(orchestrator, "_already_bootstrapped")


def test_registered_host_refreshes_clone_then_switches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _seed_clone(tmp_path / "dotfiles")
    _wire(monkeypatch, events, registered=True)

    asyncio.run(orchestrator.run_full(_ctx(tmp_path)))

    assert events == [
        "prereqs",
        f"clone_or_pull:{_REMOTE}",
        "is_registered",
        "switch",
        "post",
    ], events
    # The clone must be refreshed strictly before the check and the switch.
    assert events.index(f"clone_or_pull:{_REMOTE}") < events.index("switch")
    # A healthy re-run never needs the bootstrap secrets (on darwin: no 1Password).
    assert "ephemeral_secrets" not in events


def test_unregistered_host_with_a_clone_registers_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shape a real host was left in: a clone exists (register ran once and failed, or the key
    drifted), so the old fast path would have skipped register; now the real
    check fails and the host registers."""
    events: list[str] = []
    _seed_clone(tmp_path / "dotfiles")
    _wire(monkeypatch, events, registered=False)

    asyncio.run(orchestrator.run_full(_ctx(tmp_path)))

    assert events == [
        "prereqs",
        f"clone_or_pull:{_REMOTE}",
        "is_registered",
        "secret_zero",
        "ephemeral_secrets",
        "ssh",
        "register",
        "switch",
        "post",
    ], events


def test_first_run_does_not_double_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A first run has no clone: register does its own clone_or_pull, and the
    # refresh + check must not also fire (nothing to check, and the clone would
    # be synced twice).
    events: list[str] = []
    _wire(monkeypatch, events, registered=True)

    asyncio.run(orchestrator.run_full(_ctx(tmp_path)))

    assert f"clone_or_pull:{_REMOTE}" not in events
    assert "is_registered" not in events
    assert events == [
        "prereqs",
        "secret_zero",
        "ephemeral_secrets",
        "ssh",
        "register",
        "switch",
        "post",
    ], events


def test_clone_for_an_unknown_host_is_not_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clone whose registry does not carry this host proves nothing: register
    runs and raises its own clear error."""
    events: list[str] = []
    repo = tmp_path / "dotfiles"
    _seed_clone(repo)
    (repo / "host_registry.toml").write_text('[other]\nsystem = "x86_64-linux"\n')
    _wire(monkeypatch, events, registered=True)

    asyncio.run(orchestrator.run_full(_ctx(tmp_path)))

    assert "is_registered" not in events
    assert "register" in events


def test_a_sandbox_retry_whose_https_fetch_is_refused_registers_with_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sandbox host cloned over https and failed before its first switch: no
    key on any account and no system git credential yet, so the refresh that
    runs before any secret is loaded cannot fetch the private repo. That used
    to abort the run, identically on every retry; now it means "not registered
    yet" and the full path, which holds the token, runs."""
    events: list[str] = []
    _seed_clone(tmp_path / "dotfiles")
    _wire(monkeypatch, events, registered=True)
    https = "https://github.com/jacobbrugh/dotfiles.git"

    async def https_remote(ctx: Context) -> str:
        return https

    async def refused(remote: str, target: Path, **kw: object) -> None:
        env = kw.get("env")
        assert isinstance(env, dict) and env.get("GIT_TERMINAL_PROMPT") == "0"
        events.append("fetch-refused")
        raise ShellError(["git", "fetch"], 128, "could not read Username")

    monkeypatch.setattr(register, "dotfiles_remote", https_remote)
    monkeypatch.setattr(git_ops, "clone_or_pull", refused)

    asyncio.run(orchestrator.run_full(_ctx(tmp_path)))

    assert events == [
        "prereqs",
        "fetch-refused",
        "secret_zero",
        "ephemeral_secrets",
        "ssh",
        "register",
        "switch",
        "post",
    ], events


def test_an_ssh_fetch_failure_is_still_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _seed_clone(tmp_path / "dotfiles")
    _wire(monkeypatch, events, registered=True)

    async def unreachable(remote: str, target: Path, **kw: object) -> None:
        raise ShellError(["git", "fetch"], 128, "Could not resolve hostname")

    monkeypatch.setattr(git_ops, "clone_or_pull", unreachable)
    with pytest.raises(ShellError):
        asyncio.run(orchestrator.run_full(_ctx(tmp_path)))
    assert "switch" not in events
