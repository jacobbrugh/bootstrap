"""Unit tests for the tomlkit-based registry editor.

These are golden-file round-trip tests: load the fixture, mutate it, save
it, and assert both the new content AND the preservation of pre-existing
entries + comments.

Fixture content uses entirely synthetic names (host1/group1/group2/etc.)
so it doesn't mirror any real registry.toml.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bootstrap.lib import registry_toml
from bootstrap.lib.errors import BootstrapError

_FIXTURE = textwrap.dedent(
    """\
    # Host registry — template definitions expanded by nix/modules/hosts.nix.

    # ── single hosts ──────────────────────────────────────────────────────
    [host1]
    system = "aarch64-darwin"

    # ── indexed group: generates group1_1..group1_5 ──────────────────────
    [group1]
    system = "x86_64-linux"
    startIndex = 1
    endIndex = 5

    [group2]
    tags = ["tag1"]
    system = "x86_64-linux"
    endIndex = 2
    """
)


def _write_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "registry.toml"
    path.write_text(_FIXTURE)
    return path


def test_load_and_has_host(tmp_path: Path) -> None:
    doc = registry_toml.load(_write_fixture(tmp_path))
    assert registry_toml.has_host(doc, "host1")
    assert registry_toml.has_host(doc, "group1")
    assert registry_toml.has_host(doc, "group2")
    assert not registry_toml.has_host(doc, "host2")


def test_add_host_simple(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = registry_toml.load(path)
    registry_toml.add_host(doc, "host2", system="aarch64-darwin")
    registry_toml.save(doc, path)

    text = path.read_text()
    assert "[host2]" in text
    assert 'system = "aarch64-darwin"' in text
    # Pre-existing entries are still there
    assert "[host1]" in text
    assert "[group1]" in text
    assert "[group2]" in text
    # And the header comment is preserved
    assert "# Host registry" in text


def test_add_host_with_tags(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = registry_toml.load(path)
    registry_toml.add_host(
        doc,
        "host3",
        system="x86_64-linux",
        tags=["tag2"],
    )
    registry_toml.save(doc, path)
    text = path.read_text()
    assert "[host3]" in text
    assert 'tags = ["tag2"]' in text


def test_add_host_with_username_override(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = registry_toml.load(path)
    registry_toml.add_host(
        doc,
        "host4",
        system="x86_64-linux",
        tags=["tag1", "tag3"],
        username="alt_user",
    )
    registry_toml.save(doc, path)
    text = path.read_text()
    assert "[host4]" in text
    assert 'username = "alt_user"' in text


def test_add_host_duplicate_raises(tmp_path: Path) -> None:
    doc = registry_toml.load(_write_fixture(tmp_path))
    with pytest.raises(BootstrapError, match="already in registry"):
        registry_toml.add_host(doc, "host1", system="aarch64-darwin")


def test_add_then_query(tmp_path: Path) -> None:
    """`has_host` should immediately reflect adds without re-loading."""
    doc = registry_toml.load(_write_fixture(tmp_path))
    assert not registry_toml.has_host(doc, "host99")
    registry_toml.add_host(doc, "host99", system="aarch64-darwin")
    assert registry_toml.has_host(doc, "host99")
