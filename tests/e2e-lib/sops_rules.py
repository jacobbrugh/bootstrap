"""Recipient sets of a .sops.yaml's creation rules, from its text.

Shared by both e2e scripts (spliced in by their default.nix preludes). Parsed
from text because the nixos-test-driver's Python has no YAML library: an
`age:` list's members are `*name` aliases, an `age: &fleet` line defines a
reusable list, and `age: *fleet` reuses it — the shapes the dotfiles file uses.
"""

import re

_RULE_RE = re.compile(r"^\s*- path_regex: '([^']*)'\s*$")
_AGE_DEF_RE = re.compile(r"^\s*- age: &([A-Za-z0-9_-]+)\s*$")
_AGE_REF_RE = re.compile(r"^\s*- age: \*([A-Za-z0-9_-]+)\s*$")
_ALIAS_RE = re.compile(r"^\s*- \*([A-Za-z0-9_-]+)\s*$")


def rule_recipients(sops_yaml_text: str) -> dict[str, set[str]]:
    """path_regex -> the anchor names its first key group lists."""
    out: dict[str, set[str]] = {}
    named_lists: dict[str, set[str]] = {}
    current: set[str] | None = None
    for line in sops_yaml_text.splitlines():
        rule = _RULE_RE.match(line)
        if rule:
            current = set()
            out[rule.group(1)] = current
            continue
        if current is None:
            continue
        if _AGE_DEF_RE.match(line):
            named_lists[_AGE_DEF_RE.match(line).group(1)] = current  # type: ignore[union-attr]
            continue
        ref = _AGE_REF_RE.match(line)
        if ref:
            current.update(named_lists[ref.group(1)])
            continue
        alias = _ALIAS_RE.match(line)
        if alias:
            current.add(alias.group(1))
    return out


def assert_host_in_exactly_the_tier_rules(
    sops_yaml_text: str, host_anchor: str, tier_anchor: str
) -> None:
    """The host is a recipient of a rule iff the tier's bootstrap anchor is."""
    rules = rule_recipients(sops_yaml_text)
    assert rules, "no creation rules parsed"
    for path_regex, recipients in rules.items():
        assert (host_anchor in recipients) == (tier_anchor in recipients), (
            f"rule {path_regex!r}: {host_anchor} present={host_anchor in recipients}, "
            f"{tier_anchor} present={tier_anchor in recipients}; recipients={sorted(recipients)}"
        )
    tier_rules = [r for r, recipients in rules.items() if tier_anchor in recipients]
    assert tier_rules, f"{tier_anchor} is in no rule at all"
