"""A host's registry `dotfilesDir` is where its clone ends up, and where a re-run finds it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bootstrap.lib import symlinks
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context
from bootstrap.phases.common import register
from bootstrap.platform import Platform


def _clone(tmp_path: Path) -> Context:
    repo = tmp_path / "default" / "dotfiles"
    (repo / ".git").mkdir(parents=True)
    (repo / "flake.nix").write_text("{ }\n")
    return Context(platform=Platform.DARWIN, hostname="mac9", canonical_repo=repo)


def test_the_clone_moves_to_the_hosts_dotfiles_dir(tmp_path: Path) -> None:
    ctx = _clone(tmp_path)
    old = ctx.canonical_repo
    wanted = tmp_path / "elsewhere" / "checkout"
    asyncio.run(register._relocate_clone(ctx, wanted))
    assert ctx.canonical_repo == wanted
    assert (wanted / ".git").is_dir() and (wanted / "flake.nix").is_file()
    assert not old.exists()


def test_an_occupied_dotfiles_dir_is_refused(tmp_path: Path) -> None:
    ctx = _clone(tmp_path)
    wanted = tmp_path / "elsewhere"
    wanted.mkdir()
    (wanted / "something").write_text("x")
    with pytest.raises(BootstrapError, match="already holds"):
        asyncio.run(register._relocate_clone(ctx, wanted))
    assert (ctx.canonical_repo / ".git").is_dir()


def test_a_dry_run_moves_nothing(tmp_path: Path) -> None:
    ctx = _clone(tmp_path)
    ctx.dry_run = True
    old = ctx.canonical_repo
    asyncio.run(register._relocate_clone(ctx, tmp_path / "elsewhere"))
    assert ctx.canonical_repo == old and (old / ".git").is_dir()


def test_a_windows_path_is_not_this_machines(tmp_path: Path) -> None:
    ctx = _clone(tmp_path)
    old = ctx.canonical_repo
    asyncio.run(register._relocate_clone(ctx, Path("C:/Users/jacob/dotfiles")))
    assert ctx.canonical_repo == old


def test_a_rerun_finds_the_checkout_through_the_flake_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "elsewhere"
    (checkout / ".git").mkdir(parents=True)
    link = tmp_path / "etc" / "nix-darwin"
    link.parent.mkdir()
    monkeypatch.setattr(symlinks, "DARWIN_FLAKE_SYMLINK", link)
    assert symlinks.linked_checkout(Platform.DARWIN) is None
    link.symlink_to(checkout)
    assert symlinks.linked_checkout(Platform.DARWIN) == checkout
    # A symlink to something that is not a checkout says nothing.
    link.unlink()
    link.symlink_to(tmp_path)
    assert symlinks.linked_checkout(Platform.DARWIN) is None
