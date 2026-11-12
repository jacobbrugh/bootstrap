"""secret_zero decides "done" from its artifact — the bootstrap token on disk —
and treats a completed host (token and bootstrap key both gone) as nothing to
do, not as an error; whether anything else is needed is register's call."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from bootstrap.lib import flake_splice
from bootstrap.lib.runtime import Context
from bootstrap.phases.nixos import secret_zero
from bootstrap.platform import Platform


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    token: str | None,
    key_present: bool,
) -> list[str]:
    events: list[str] = []
    token_path = tmp_path / "bootstrap-github-token"
    if token is not None:
        token_path.write_text(token)
    key_path = tmp_path / "age-key"
    if key_present:
        key_path.write_text("AGE-SECRET-KEY-1X\n")
    monkeypatch.setattr(secret_zero, "_PLAINTEXT_TOKEN_PATH", token_path)
    monkeypatch.setattr(secret_zero, "_AGE_KEY_PATH", key_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/run/current-system/sw/bin/nix")
    monkeypatch.setattr(flake_splice, "flake_ref", lambda: "path:/etc/bootstrap-flake")

    async def fake_activate(ctx: Context, *, flake_ref: str) -> None:
        events.append(f"activate:{flake_ref}")

    monkeypatch.setattr(secret_zero, "_activate_bare_metal", fake_activate)
    return events


def _ctx(tmp_path: Path) -> Context:
    return Context(platform=Platform.NIXOS, hostname="rtr9", canonical_repo=tmp_path / "dotfiles")


def test_materialized_token_is_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events = _wire(monkeypatch, tmp_path, token="ghp_x", key_present=True)
    asyncio.run(secret_zero.run(_ctx(tmp_path)))
    assert events == []


def test_empty_token_is_not_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events = _wire(monkeypatch, tmp_path, token="", key_present=True)
    asyncio.run(secret_zero.run(_ctx(tmp_path)))
    assert events == ["activate:path:/etc/bootstrap-flake"]


def test_completed_host_has_nothing_to_materialize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """post shredded the key and the dotfiles generation carries no token:
    return, do not raise, do not activate anything."""
    events = _wire(monkeypatch, tmp_path, token=None, key_present=False)
    asyncio.run(secret_zero.run(_ctx(tmp_path)))
    assert events == []


def test_staged_key_without_token_activates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _wire(monkeypatch, tmp_path, token=None, key_present=True)
    asyncio.run(secret_zero.run(_ctx(tmp_path)))
    assert events == ["activate:path:/etc/bootstrap-flake"]
