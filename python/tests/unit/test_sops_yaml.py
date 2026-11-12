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
      - path_regex: '^foo\\.yaml$'
        key_groups:
          - age:
              - *host1
              - *host2
              - *bootstrap
      - path_regex: '^modules/security/secrets/bar-secrets\\.yaml$'
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
    assert "host1" in text
    assert "host2" in text
    assert "bootstrap" in text


def test_add_age_key_then_creation_rule(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = sops_yaml.load(path)
    sops_yaml.add_age_key(doc, "host3", "age1pq1quxquxquxqux")
    sops_yaml.add_to_creation_rule(doc, "modules/security/secrets/bar-secrets.yaml", "host3")
    sops_yaml.save(doc, path)

    text = path.read_text()
    bar_idx = text.index("bar-secrets")
    bar_section = text[bar_idx:]
    assert "*host3" in bar_section
    foo_idx = text.index("foo\\.yaml")
    foo_section = text[foo_idx:bar_idx]
    assert "*host3" not in foo_section


def test_add_to_both_creation_rules(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = sops_yaml.load(path)
    sops_yaml.add_age_key(doc, "host3", "age1pq1k")
    sops_yaml.add_to_creation_rule(doc, "modules/security/secrets/bar-secrets.yaml", "host3")
    sops_yaml.add_to_creation_rule(doc, "foo.yaml", "host3")
    sops_yaml.save(doc, path)

    text = path.read_text()
    foo_idx = text.index("foo\\.yaml")
    bar_idx = text.index("bar-secrets")
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
        sops_yaml.add_to_creation_rule(doc, "modules/security/secrets/bar-secrets.yaml", "host3")


def test_add_to_creation_rule_unknown_path_raises(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    sops_yaml.add_age_key(doc, "host3", "age1pq1k")
    with pytest.raises(BootstrapError, match="no creation_rule"):
        sops_yaml.add_to_creation_rule(doc, "no-such-file.yaml", "host3")


def test_add_to_creation_rule_idempotent(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    sops_yaml.add_age_key(doc, "host3", "age1pq1k")
    sops_yaml.add_to_creation_rule(doc, "modules/security/secrets/bar-secrets.yaml", "host3")
    sops_yaml.add_to_creation_rule(doc, "modules/security/secrets/bar-secrets.yaml", "host3")


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
    assert "&host1" not in text
    assert "*host1" not in text
    assert "&host2" in text
    assert "*host2" in text
    assert "&bootstrap" in text
    assert "*bootstrap" in text


def test_remove_age_key_is_idempotent_via_add(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    doc = sops_yaml.load(path)
    sops_yaml.remove_age_key(doc, "host1")
    sops_yaml.add_age_key(doc, "host1", "age1pq1replaced")
    sops_yaml.add_to_creation_rule(doc, "foo.yaml", "host1")
    sops_yaml.add_to_creation_rule(doc, "modules/security/secrets/bar-secrets.yaml", "host1")
    sops_yaml.save(doc, path)

    text = path.read_text()
    assert "&host1 age1pq1replaced" in text
    assert text.count("*host1") == 2
    assert text.count("age1pq1replaced") == 1
    assert "foofoo" not in text
    reloaded = sops_yaml.load(path)
    assert sops_yaml.has_anchor(reloaded, "host1")
    assert sops_yaml.get_registered_pubkey(reloaded, "host1") == "age1pq1replaced"


def test_remove_age_key_has_anchor_returns_false(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    assert sops_yaml.has_anchor(doc, "host1")
    sops_yaml.remove_age_key(doc, "host1")
    assert not sops_yaml.has_anchor(doc, "host1")


def test_remove_age_key_unknown_anchor_raises(tmp_path: Path) -> None:
    doc = sops_yaml.load(_write_fixture(tmp_path))
    with pytest.raises(BootstrapError, match="not found"):
        sops_yaml.remove_age_key(doc, "host99")


def test_rule_located_by_path_match_not_regex_string(tmp_path: Path) -> None:
    # "modules/security/secrets/bar-secrets.yaml" must resolve the anchored,
    # escaped rule even though a constructed-regex string comparison would
    # never match its spelling; and the bare filename must NOT resolve that
    # rule (the ^ anchor rejects it).
    doc = sops_yaml.load(_write_fixture(tmp_path))
    sops_yaml.add_age_key(doc, "host3", "age1pq1k")
    with pytest.raises(BootstrapError, match="no creation_rule"):
        sops_yaml.add_to_creation_rule(doc, "bar-secrets.yaml", "host3")
    sops_yaml.add_to_creation_rule(doc, "modules/security/secrets/bar-secrets.yaml", "host3")


# The dotfiles .sops.yaml shape: a `&fleet` alias on one rule's age list is
# reused by a second rule, and a codesign-style rule lists the host without
# any bootstrap keys. This mirrors the stale-anchor rekey path register
# walks when a pre-registered host is re-bootstrapped on fresh
# hardware: the old anchor must vanish from *every* rule (no dangling
# aliases), and the fresh key must propagate through the fleet alias.
_STALE_FIXTURE = textwrap.dedent(
    """\
    keys:
      - &host1 age1pq1foofoofoofoofoofoofoofoofoofoofoo
      - &host_mac2 age1pq1stalestalestalestalestale
      - &bootstrap_privileged age1pq1bazbazbazbazbazbazbaz

    creation_rules:
      - path_regex: '^secrets\\.yaml$'
        key_groups:
          - age:
              - *host1
              - *bootstrap_privileged
              - *host_mac2
      - path_regex: '^modules/security/secrets/bot-secrets\\.yaml$'
        key_groups:
          - age: &fleet
              - *host1
              - *bootstrap_privileged
              - *host_mac2
      - path_regex: '^transcripts/.*$'
        key_groups:
          - age: *fleet
      - path_regex: '^config/system/darwin/codesign\\.yaml$'
        key_groups:
          - age:
              - *host1
              - *host_mac2
    """
)


_PARTIAL_FIXTURE = textwrap.dedent(
    """\
    keys:
      - &host_part age1pq1partpartpartpartpart
      - &bootstrap_privileged age1pq1bazbazbazbazbazbazbaz

    creation_rules:
      - path_regex: '^secrets\\.yaml$'
        key_groups:
          - age:
              - *bootstrap_privileged
      - path_regex: '^modules/security/secrets/bot-secrets\\.yaml$'
        key_groups:
          - age:
              - *host_part
              - *bootstrap_privileged
    """
)


def test_tier_rules_for_host(tmp_path: Path) -> None:
    """Registered means "in every rule the tier anchor is in" — including the
    rule that reaches the host only through the aliased `&fleet` list — and a
    host in only some of them (the partial registration that once left a
    privileged host unable to decrypt the logscrub literals) is not."""
    path = tmp_path / ".sops.yaml"
    path.write_text(_STALE_FIXTURE)
    doc = sops_yaml.load(path)

    assert sops_yaml.tier_rules_for_host(doc, "bootstrap_privileged", "host1") == [
        r"^secrets\.yaml$",
        r"^modules/security/secrets/bot-secrets\.yaml$",
        r"^transcripts/.*$",
    ]
    assert sops_yaml.tier_rules_for_host(doc, "bootstrap_privileged", "host_mac2") == [
        r"^secrets\.yaml$",
        r"^modules/security/secrets/bot-secrets\.yaml$",
        r"^transcripts/.*$",
    ]
    assert sops_yaml.tier_rules_for_host(doc, "bootstrap_privileged", "host_nope") is None
    # A tier anchor that is in no rule proves nothing.
    assert sops_yaml.tier_rules_for_host(doc, "bootstrap_sandbox", "host1") is None

    path.write_text(_PARTIAL_FIXTURE)
    partial = sops_yaml.load(path)
    assert sops_yaml.tier_rules_for_host(partial, "bootstrap_privileged", "host_part") is None


def test_stale_anchor_rekey_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".sops.yaml"
    path.write_text(_STALE_FIXTURE)
    doc = sops_yaml.load(path)

    fresh_pubkey = "age1pq1freshfreshfreshfreshfresh"

    sops_yaml.remove_age_key(doc, "host_mac2")
    sops_yaml.add_age_key(doc, "host_mac2", fresh_pubkey)
    sops_yaml.add_to_creation_rule(doc, "modules/security/secrets/bot-secrets.yaml", "host_mac2")
    sops_yaml.add_to_creation_rule(doc, "secrets.yaml", "host_mac2")
    sops_yaml.save(doc, path)

    text = path.read_text()
    # The stale pubkey is gone everywhere; the fresh one is anchored.
    assert "stalestale" not in text
    assert fresh_pubkey in text

    # Round-trips as valid YAML with no dangling aliases, and the fresh
    # anchor reaches the rules register manages...
    reloaded = sops_yaml.load(path)
    assert sops_yaml.find_anchor_by_pubkey(reloaded, fresh_pubkey) == "host_mac2"
    rules = reloaded["creation_rules"]
    secrets_rule, bot_rule, transcripts_rule, codesign_rule = list(rules)
    assert fresh_pubkey in [str(k) for k in secrets_rule["key_groups"][0]["age"]]
    bot_age = [str(k) for k in bot_rule["key_groups"][0]["age"]]
    assert fresh_pubkey in bot_age
    # ...including through the fleet alias reused by the transcripts rule...
    assert [str(k) for k in transcripts_rule["key_groups"][0]["age"]] == bot_age
    # ...while rules register does not manage simply lose the stale anchor.
    codesign_age = [str(k) for k in codesign_rule["key_groups"][0]["age"]]
    assert fresh_pubkey not in codesign_age
    assert "stalestale" not in "".join(codesign_age)


def test_rules_with_anchor_and_add_to_them(tmp_path: Path) -> None:
    """A privileged host joins every rule the privileged anchor is in — secrets,
    bot-secrets and, through the `&fleet` alias, the transcripts rule — and
    none it is not (codesign)."""
    path = tmp_path / ".sops.yaml"
    path.write_text(_STALE_FIXTURE)
    doc = sops_yaml.load(path)
    assert sops_yaml.rules_with_anchor(doc, "bootstrap_privileged") == [
        "^secrets\\.yaml$",
        "^modules/security/secrets/bot-secrets\\.yaml$",
        "^transcripts/.*$",
    ]
    sops_yaml.add_age_key(doc, "host_new", "age1pq1newnewnew")
    touched = sops_yaml.add_to_rules_with_anchor(doc, "bootstrap_privileged", "host_new")
    assert touched == [
        "^secrets\\.yaml$",
        "^modules/security/secrets/bot-secrets\\.yaml$",
        "^transcripts/.*$",
    ]
    # Idempotent: a second pass touches the same rules and appends nothing.
    assert sops_yaml.add_to_rules_with_anchor(doc, "bootstrap_privileged", "host_new") == touched
    sops_yaml.save(doc, path)
    text = path.read_text()
    assert text.count("&host_new") == 1
    # bot-secrets carries the anchor; the transcripts rule inherits it through
    # *fleet, so it is dumped exactly twice, and never under codesign.
    assert text.count("*host_new") == 2
    codesign = text[text.index("codesign") :]
    assert "host_new" not in codesign
    reloaded = sops_yaml.load(path)
    rules = list(reloaded["creation_rules"])
    assert "age1pq1newnewnew" in [str(k) for k in rules[2]["key_groups"][0]["age"]]


def test_add_to_rules_with_unknown_tier_anchor_raises(tmp_path: Path) -> None:
    path = tmp_path / ".sops.yaml"
    path.write_text(_STALE_FIXTURE)
    doc = sops_yaml.load(path)
    sops_yaml.add_age_key(doc, "host_new", "age1pq1newnewnew")
    with pytest.raises(BootstrapError, match="no creation_rule"):
        sops_yaml.add_to_rules_with_anchor(doc, "bootstrap_sandbox", "host_new")


def test_set_anchored_pubkey_keeps_every_alias(tmp_path: Path) -> None:
    """The CI stage swaps the privileged anchor for an ephemeral key; the
    declaration and all three aliases must follow, through the fleet alias too."""
    path = tmp_path / ".sops.yaml"
    path.write_text(_STALE_FIXTURE)
    doc = sops_yaml.load(path)
    sops_yaml.set_anchored_pubkey(doc, "bootstrap_privileged", "age1pq1ephemeral")
    sops_yaml.save(doc, path)
    text = path.read_text()
    assert "bazbaz" not in text
    assert text.count("&bootstrap_privileged age1pq1ephemeral") == 1
    assert text.count("*bootstrap_privileged") == 2
    reloaded = sops_yaml.load(path)
    assert sops_yaml.get_registered_pubkey(reloaded, "bootstrap_privileged") == "age1pq1ephemeral"
    rules = list(reloaded["creation_rules"])
    for idx in (0, 1, 2):
        assert "age1pq1ephemeral" in [str(k) for k in rules[idx]["key_groups"][0]["age"]]
