"""register's tier decisions: remote, author, rule membership, the sandbox-tag guard."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from bootstrap.lib import git_ops, secrets
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.paths import DOTFILES_GIT_REMOTE_HTTPS, DOTFILES_GIT_REMOTE_SSH
from bootstrap.lib.runtime import Context, Tier
from bootstrap.phases.common import register
from bootstrap.platform import Platform
from tests.unit.conftest import ShRecorder


def _ctx(tmp_path: Path, tier: Tier | None) -> Context:
    ctx = Context(platform=Platform.NIXOS, hostname="rtr9", canonical_repo=tmp_path / "dotfiles")
    ctx.tier = tier
    ctx.github_token = "ghp_bot"
    ctx.bootstrap_age_key_file = tmp_path / "age-key"
    return ctx


def test_remote_by_tier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(register, "DOTFILES_GIT_REMOTE_OVERRIDE", None)
    assert asyncio.run(register.dotfiles_remote(_ctx(tmp_path, Tier.PRIVILEGED))) == (
        DOTFILES_GIT_REMOTE_SSH
    )
    assert asyncio.run(register.dotfiles_remote(_ctx(tmp_path, Tier.SANDBOX))) == (
        DOTFILES_GIT_REMOTE_HTTPS
    )
    assert DOTFILES_GIT_REMOTE_SSH.startswith("git@github.com:")
    assert DOTFILES_GIT_REMOTE_HTTPS.startswith("https://github.com/")
    # No token in the https remote: the credential helper supplies it.
    assert "@github.com/" not in DOTFILES_GIT_REMOTE_HTTPS.replace("https://github.com/", "")


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(register, "DOTFILES_GIT_REMOTE_OVERRIDE", "file:///tmp/x")
    assert asyncio.run(register.dotfiles_remote(_ctx(tmp_path, Tier.PRIVILEGED))) == "file:///tmp/x"


def test_unknown_tier_keeps_an_existing_clone_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After post shredded the key the tier is unknown; a re-run must not
    demand a different remote from the clone it already has."""
    monkeypatch.setattr(register, "DOTFILES_GIT_REMOTE_OVERRIDE", None)
    repo = tmp_path / "dotfiles"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", DOTFILES_GIT_REMOTE_SSH], check=True
    )
    assert asyncio.run(register.dotfiles_remote(_ctx(tmp_path, None))) == DOTFILES_GIT_REMOTE_SSH


def test_tier_anchor(tmp_path: Path) -> None:
    assert register.tier_anchor(_ctx(tmp_path, Tier.PRIVILEGED)) == "bootstrap_privileged"
    assert register.tier_anchor(_ctx(tmp_path, Tier.SANDBOX)) == "bootstrap_sandbox"
    # No key loaded (post shredded it): the registry decides — an untagged host
    # is privileged, a sandbox-tagged one (or a BOOTSTRAP_SANDBOX run) is not.
    assert register.tier_anchor(_ctx(tmp_path, None)) == "bootstrap_privileged"
    assert register.tier_anchor(_ctx(tmp_path, None), ["sandbox"]) == "bootstrap_sandbox"
    forced = _ctx(tmp_path, None)
    forced.force_sandbox = True
    assert register.tier_anchor(forced) == "bootstrap_sandbox"


def test_commit_author_by_tier(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_priv_token(_ctx: Context) -> str:
        return "github_pat_priv"

    monkeypatch.setattr(secrets, "privileged_github_token", fake_priv_token)
    sh_recorder.answers[("gh", "api", "user")] = json.dumps(
        {"login": "whoever", "id": 7, "name": "N", "email": "e@x"}
    )

    asyncio.run(register._commit_identity(_ctx(tmp_path, Tier.PRIVILEGED)))
    env = sh_recorder.calls[-1].env
    assert env is not None and env["GITHUB_TOKEN"] == "github_pat_priv"

    asyncio.run(register._commit_identity(_ctx(tmp_path, Tier.SANDBOX)))
    env = sh_recorder.calls[-1].env
    assert env is not None and env["GITHUB_TOKEN"] == "ghp_bot"


def test_files_matching_uses_tracked_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "modules/security/secrets").mkdir(parents=True)
    (repo / "pkgs/x/eval/cases/a").mkdir(parents=True)
    for rel in (
        "secrets.yaml",
        "modules/security/secrets/bot-secrets.yaml",
        "pkgs/x/eval/cases/a/transcript.jsonl.enc",
        "README.md",
    ):
        (repo / rel).write_text("x")
    (repo / "untracked-secrets.yaml").write_text("x")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "add",
            "--",
            *[
                "secrets.yaml",
                "modules/security/secrets/bot-secrets.yaml",
                "pkgs/x/eval/cases/a/transcript.jsonl.enc",
                "README.md",
            ],
        ],
        check=True,
    )

    matched = asyncio.run(
        register._files_matching(
            repo,
            [
                r"^secrets\.yaml$",
                r"^modules/security/secrets/bot-secrets\.yaml$",
                r"^pkgs/x/eval/cases/[^/]+/transcript\.jsonl(\.enc)?$",
            ],
        )
    )
    assert matched == [
        Path("modules/security/secrets/bot-secrets.yaml"),
        Path("pkgs/x/eval/cases/a/transcript.jsonl.enc"),
        Path("secrets.yaml"),
    ]


def test_privileged_key_on_a_sandbox_tagged_host_is_refused_before_any_edit(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "dotfiles"
    (repo / ".git").mkdir(parents=True)
    (repo / "host_registry.toml").write_text(
        '[rtr9]\nsystem = "x86_64-linux"\ntags = ["sandbox"]\n'
    )
    (repo / ".sops.yaml").write_text(
        "keys:\n    - &bootstrap_privileged age1pq1p\ncreation_rules: []\n"
    )

    async def fake_install(_ctx: Context) -> None:
        return None

    async def fake_clone(*_a: object, **_k: object) -> None:
        return None

    entered: list[str] = []

    def fake_transactional_edit(*_a: object, **_k: object) -> object:
        entered.append("transactional_edit")
        raise AssertionError("must not reach the edit")

    monkeypatch.setattr(register, "DOTFILES_GIT_REMOTE_OVERRIDE", "file:///x")
    monkeypatch.setattr(git_ops, "clone_or_pull", fake_clone)
    monkeypatch.setattr(git_ops, "transactional_edit", fake_transactional_edit)
    monkeypatch.setattr(register, "_ensure_symlink", fake_install)

    with pytest.raises(BootstrapError, match="tagged sandbox"):
        asyncio.run(register.run(_ctx(tmp_path, Tier.PRIVILEGED)))
    assert entered == []
    assert sh_recorder.with_prefix("sops") == []
    assert sh_recorder.with_prefix("git") == []
    # A refused register leaves the host as it found it: no flake symlink.
    assert sh_recorder.calls == []
