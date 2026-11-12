from __future__ import annotations

from pathlib import Path

from bootstrap.lib.runtime import Context
from bootstrap.platform import Platform


def _ctx() -> Context:
    return Context(
        platform=Platform.DARWIN,
        hostname="test-host",
        canonical_repo=Path("/tmp/nowhere"),
    )


def test_sops_env_overlay_empty_without_key() -> None:
    assert _ctx().sops_env_overlay == {}


def test_sops_env_overlay_file_tier() -> None:
    ctx = _ctx()
    ctx.bootstrap_age_key_file = Path("/var/lib/bootstrap/age-key")
    assert ctx.sops_env_overlay == {"SOPS_AGE_KEY_FILE": "/var/lib/bootstrap/age-key"}


def test_sops_env_overlay_memory_tier() -> None:
    ctx = _ctx()
    ctx.bootstrap_age_key = "AGE-SECRET-KEY-TESTONLY"
    assert ctx.sops_env_overlay == {"SOPS_AGE_KEY": "AGE-SECRET-KEY-TESTONLY"}


def test_sops_env_overlay_memory_tier_wins_over_file() -> None:
    # Both set is a programming error upstream, but the memory tier must
    # win deterministically: it is the fresher, deliberately-loaded value.
    ctx = _ctx()
    ctx.bootstrap_age_key_file = Path("/var/lib/bootstrap/age-key")
    ctx.bootstrap_age_key = "AGE-SECRET-KEY-TESTONLY"
    assert ctx.sops_env_overlay == {"SOPS_AGE_KEY": "AGE-SECRET-KEY-TESTONLY"}


def test_age_key_never_in_repr() -> None:
    ctx = _ctx()
    ctx.bootstrap_age_key = "AGE-SECRET-KEY-TESTONLY"
    ctx.github_token = "ghp_testonly"
    rendered = repr(ctx)
    assert "AGE-SECRET-KEY-TESTONLY" not in rendered
    assert "ghp_testonly" not in rendered
