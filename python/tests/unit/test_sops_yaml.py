"""Unit tests for the ruamel.yaml-based .sops.yaml editor.

These tests verify the critical YAML-anchor preservation property: adding
a new anchor + alias must not corrupt or reformat existing anchors. They
also verify the structural failure modes (duplicate anchor, missing
creation_rule) raise loudly rather than silently doing the wrong thing.

Fixture content uses entirely synthetic names (host1/host2/bootstrap,
foo.yaml, bar.yaml) so the fixture doesn't mirror any real .sops.yaml
from a downstream consumer of this library.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bootstrap.lib import sops_yaml
from bootstrap.lib.errors import BootstrapError

_FIXTURE = textwrap.dedent(
    """\
    keys:
      - &host1 age1pq1foofoofoofoofoofoofoofoofoofoofoo
      - &host2 age1pq1barbarbarbarbarbarbarbarbarbar
      - &bootstrap age1pq1bazbazbazbazbazbazbaz

    creation_rules:
      - path_regex: 'foo.yaml$'
        key_groups:
          - age:
              - *host1
              - *host2
              - *bootstrap
      - path_regex: 'bar.yaml$'
        key_groups:
          - age:
              - *host1
              - *host2
              - *bootstrap
    """
)


def _write_fixture(tmp_path: Path) -> Path:
    path = tmp_path / ".sops.yaml"
    path.write_text(_FIXTURE)
    return path


def test_has_anchor(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    assert sops_yaml.has_anchor(doc, "host1")
    assert sops_yaml.has_anchor(doc, "host2")
    assert sops_yaml.has_anchor(doc, "bootstrap")
    assert not sops_yaml.has_anchor(doc, "host3")


def test_add_age_key_appears_in_keys_section(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = sops_yaml.load(path)
    sops_yaml.add_age_key(doc, "host3", "age1pq1quxquxquxquxquxquxquxquxquxquxquxqux")
    sops_yaml.save(doc, path)

    text = path.read_text()
    assert "host3" in text
    assert "age1pq1quxquxquxquxquxquxquxquxquxquxquxqux" in text
    # Pre-existing anchors are still there
    assert "host1" in text
    assert "host2" in text
    assert "bootstrap" in text


def test_add_age_key_then_creation_rule(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = sops_yaml.load(path)
    sops_yaml.add_age_key(doc, "host3", "age1pq1quxquxquxqux")
    sops_yaml.add_to_creation_rule(doc, "bar.yaml$", "host3")
    sops_yaml.save(doc, path)

    text = path.read_text()
    # The new alias should appear in the bar.yaml section
    bar_idx = text.index("bar.yaml")
    bar_section = text[bar_idx:]
    assert "*host3" in bar_section
    # And NOT in the foo.yaml section (we didn't add it there)
    foo_idx = text.index("foo.yaml")
    foo_section = text[foo_idx:bar_idx]
    assert "*host3" not in foo_section


def test_add_to_both_creation_rules(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = sops_yaml.load(path)
    sops_yaml.add_age_key(doc, "host3", "age1pq1k")
    sops_yaml.add_to_creation_rule(doc, "bar.yaml$", "host3")
    sops_yaml.add_to_creation_rule(doc, "foo.yaml$", "host3")
    sops_yaml.save(doc, path)

    text = path.read_text()
    foo_idx = text.index("foo.yaml")
    bar_idx = text.index("bar.yaml")
    foo_section = text[foo_idx:bar_idx]
    bar_section = text[bar_idx:]
    assert "*host3" in foo_section
    assert "*host3" in bar_section


def test_add_duplicate_anchor_raises(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    with pytest.raises(BootstrapError, match="already exists"):
        sops_yaml.add_age_key(doc, "host1", "age1pq1other")


def test_add_to_creation_rule_unknown_anchor_raises(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    with pytest.raises(BootstrapError, match="not found"):
        sops_yaml.add_to_creation_rule(doc, "bar.yaml$", "host3")


def test_add_to_creation_rule_unknown_path_raises(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    sops_yaml.add_age_key(doc, "host3", "age1pq1k")
    with pytest.raises(BootstrapError, match="not found"):
        sops_yaml.add_to_creation_rule(doc, "no-such-file$", "host3")


def test_add_to_creation_rule_idempotent(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    sops_yaml.add_age_key(doc, "host3", "age1pq1k")
    sops_yaml.add_to_creation_rule(doc, "bar.yaml$", "host3")
    # Second call is a no-op (alias already in the list)
    sops_yaml.add_to_creation_rule(doc, "bar.yaml$", "host3")


def test_get_registered_pubkey(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    assert (
        sops_yaml.get_registered_pubkey(doc, "host1") == "age1pq1foofoofoofoofoofoofoofoofoofoofoo"
    )
    assert sops_yaml.get_registered_pubkey(doc, "bootstrap") == "age1pq1bazbazbazbazbazbazbaz"
    assert sops_yaml.get_registered_pubkey(doc, "host99") is None


def test_remove_age_key_strips_declaration_and_aliases(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = sops_yaml.load(path)
    sops_yaml.remove_age_key(doc, "host1")
    sops_yaml.save(doc, path)

    text = path.read_text()
    # Anchor declaration is gone from keys:
    assert "&host1" not in text
    # No dangling aliases in either creation_rule
    assert "*host1" not in text
    # Other anchors + aliases survived unchanged
    assert "&host2" in text
    assert "*host2" in text
    assert "&bootstrap" in text
    assert "*bootstrap" in text


def test_remove_age_key_is_idempotent_via_add(tmp_path: Path) -> None:
    """remove followed by add with the same name produces a usable document.

    Critical property: ruamel.yaml round-trip mode tracks anchor/alias
    identity by object `id()`, not by anchor name at dump time. A naive
    "replace scalar in place" implementation leaves `creation_rules[].age[]`
    references dangling — they keep pointing at the old scalar object, and
    on dump ruamel silently inlines them as plain values (or emits them as
    references to an orphaned anchor) rather than as `*host1` aliases.

    This test verifies that remove-then-add produces a document where every
    reference to `host1` in the creation_rules is STILL emitted as an alias
    (`*host1`), not inlined. If ruamel ever inlines them, the count check
    fails and the test catches the regression.
    """
    path = _write_fixture(tmp_path)
    doc = sops_yaml.load(path)
    sops_yaml.remove_age_key(doc, "host1")
    sops_yaml.add_age_key(doc, "host1", "age1pq1replaced")
    sops_yaml.add_to_creation_rule(doc, "foo.yaml$", "host1")
    sops_yaml.add_to_creation_rule(doc, "bar.yaml$", "host1")
    sops_yaml.save(doc, path)

    text = path.read_text()
    # Anchor declaration updated with the new value.
    assert "&host1 age1pq1replaced" in text
    # Aliases still aliases — exactly two `*host1` references, one per
    # creation_rule. If ruamel inlined them, count would be 0.
    assert text.count("*host1") == 2
    # The new value appears exactly once (in the declaration), not inlined
    # anywhere else in the document.
    assert text.count("age1pq1replaced") == 1
    # Old value is gone entirely.
    assert "foofoo" not in text
    # And re-loading the written file gives us a clean document.
    reloaded = sops_yaml.load(path)
    assert sops_yaml.has_anchor(reloaded, "host1")
    assert sops_yaml.get_registered_pubkey(reloaded, "host1") == "age1pq1replaced"


def test_remove_age_key_has_anchor_returns_false(tmp_path: Path) -> None:
    """After remove, `has_anchor` reflects the deletion immediately."""
    doc = sops_yaml.load(_write_fixture(tmp_path))
    assert sops_yaml.has_anchor(doc, "host1")
    sops_yaml.remove_age_key(doc, "host1")
    assert not sops_yaml.has_anchor(doc, "host1")


def test_remove_age_key_unknown_anchor_raises(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    with pytest.raises(BootstrapError, match="not found"):
        sops_yaml.remove_age_key(doc, "host99")
