"""Tier detection: the loaded age key, matched against the .sops.yaml anchors."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bootstrap.lib import age_ops, flake_splice, secrets
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context, Tier
from bootstrap.platform import Platform

PRIV_PUB = "age1pq1privprivprivprivprivpriv"
SANDBOX_PUB = "age1pq1sandsandsandsandsandsand"
ANCHORS = {"bootstrap_privileged": PRIV_PUB, "bootstrap_sandbox": SANDBOX_PUB}


def test_tier_for_pubkey() -> None:
    assert secrets.tier_for_pubkey(PRIV_PUB, ANCHORS) is Tier.PRIVILEGED
    assert secrets.tier_for_pubkey(SANDBOX_PUB, ANCHORS) is Tier.SANDBOX


def test_unknown_key_is_refused() -> None:
    """A rotated or foreign key must fail here, not at the first GitHub call."""
    with pytest.raises(BootstrapError, match="neither the bootstrap_privileged nor"):
        secrets.tier_for_pubkey("age1pq1someoneelse", ANCHORS)


def test_missing_anchor_is_refused() -> None:
    with pytest.raises(BootstrapError, match="bootstrap_sandbox"):
        secrets.tier_for_pubkey(PRIV_PUB, {"bootstrap_privileged": PRIV_PUB})


def _stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pubkey: str) -> Context:
    src = tmp_path / "flake-src"
    src.mkdir()
    (src / ".sops.yaml").write_text(
        f"keys:\n    - &bootstrap_sandbox {SANDBOX_PUB}\n    - &bootstrap_privileged {PRIV_PUB}\n"
    )

    async def fake_source_path(_ref: str) -> Path:
        return src

    async def fake_pubkey_of_file(_key_file: Path) -> str:
        return pubkey

    async def fake_pubkey_of_identity(_secret: str) -> str:
        return pubkey

    monkeypatch.setattr(flake_splice, "flake_ref", lambda: "path:/nowhere")
    monkeypatch.setattr(flake_splice, "source_path", fake_source_path)
    monkeypatch.setattr(age_ops, "extract_public_key", fake_pubkey_of_file)
    monkeypatch.setattr(age_ops, "public_key_of_identity", fake_pubkey_of_identity)
    return Context(platform=Platform.NIXOS, hostname="h", canonical_repo=tmp_path / "dotfiles")


def test_detect_tier_file_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ctx = _stage(monkeypatch, tmp_path, PRIV_PUB)
    ctx.bootstrap_age_key_file = tmp_path / "age-key"
    assert asyncio.run(secrets.detect_tier(ctx)) is Tier.PRIVILEGED


def test_detect_tier_memory_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ctx = _stage(monkeypatch, tmp_path, SANDBOX_PUB)
    ctx.bootstrap_age_key = "AGE-SECRET-KEY-TEST"
    assert asyncio.run(secrets.detect_tier(ctx)) is Tier.SANDBOX


def test_ephemeral_secrets_sets_the_tier_on_nixos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _stage(monkeypatch, tmp_path, PRIV_PUB)
    token = tmp_path / "token"
    token.write_text("ghp_bot\n")
    key = tmp_path / "age-key"
    key.write_text("AGE-SECRET-KEY-TEST\n")
    monkeypatch.setenv("BOOTSTRAP_GITHUB_TOKEN_FILE", str(token))
    monkeypatch.setenv("BOOTSTRAP_AGE_KEY_FILE", str(key))

    async def go() -> tuple[Tier | None, bool, bool]:
        async with secrets.ephemeral_secrets(ctx):
            return ctx.tier, ctx.is_privileged, ctx.github_token == "ghp_bot"

    tier, privileged, token_loaded = asyncio.run(go())
    assert tier is Tier.PRIVILEGED
    assert privileged
    assert token_loaded
    # Cleared on exit, token included.
    assert ctx.tier is None
    assert ctx.github_token is None


def test_forced_sandbox_lowers_a_privileged_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _stage(monkeypatch, tmp_path, PRIV_PUB)
    ctx.force_sandbox = True
    ctx.tier = Tier.PRIVILEGED
    assert ctx.is_sandbox
    assert not ctx.is_privileged


def test_no_key_is_sandbox() -> None:
    """Dry-run and post-shred re-runs load no key; nothing privileged happens."""
    ctx = Context(platform=Platform.NIXOS, hostname="h", canonical_repo=Path("/nowhere"))
    assert ctx.tier is None
    assert ctx.is_sandbox


def test_privileged_token_needs_a_loaded_key(tmp_path: Path) -> None:
    ctx = Context(platform=Platform.NIXOS, hostname="h", canonical_repo=tmp_path)
    with pytest.raises(BootstrapError, match="needs the bootstrap age key"):
        asyncio.run(secrets.privileged_github_token(ctx))
