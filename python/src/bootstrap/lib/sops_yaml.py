from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import PlainScalarString

from bootstrap.lib.errors import BootstrapError


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 1 << 20
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load(path: Path) -> CommentedMap:
    yaml = _yaml()
    with path.open("r") as fh:
        doc = yaml.load(fh)
    if not isinstance(doc, CommentedMap):
        raise BootstrapError(f"expected top-level mapping in {path}, got {type(doc).__name__}")
    return doc


def save(doc: CommentedMap, path: Path) -> None:
    yaml = _yaml()
    with path.open("w") as fh:
        yaml.dump(doc, fh)


def _anchor_name_of(item: object) -> str | None:
    anchor_attr = getattr(item, "anchor", None)
    if anchor_attr is None:
        return None
    value = getattr(anchor_attr, "value", None)
    if isinstance(value, str):
        return value
    return None


def has_anchor(doc: CommentedMap, anchor_name: str) -> bool:
    keys = doc.get("keys")
    if not isinstance(keys, CommentedSeq):
        return False
    return any(_anchor_name_of(item) == anchor_name for item in keys)


def _find_anchored_scalar(doc: CommentedMap, anchor_name: str) -> PlainScalarString | None:
    keys = doc.get("keys")
    if not isinstance(keys, CommentedSeq):
        return None
    for item in keys:
        if _anchor_name_of(item) != anchor_name:
            continue
        if isinstance(item, PlainScalarString):
            return item
        return None
    return None


def get_registered_pubkey(doc: CommentedMap, anchor_name: str) -> str | None:
    scalar = _find_anchored_scalar(doc, anchor_name)
    if scalar is None:
        return None
    return str(scalar)


def find_anchor_by_pubkey(doc: CommentedMap, pubkey: str) -> str | None:
    keys = doc.get("keys")
    if not isinstance(keys, CommentedSeq):
        return None
    for item in keys:
        if not isinstance(item, PlainScalarString):
            continue
        if str(item) != pubkey:
            continue
        return _anchor_name_of(item)
    return None


def add_age_key(
    doc: CommentedMap,
    anchor_name: str,
    age_pubkey: str,
) -> None:
    if has_anchor(doc, anchor_name):
        raise BootstrapError(f"anchor {anchor_name!r} already exists in .sops.yaml")
    keys = doc.get("keys")
    if not isinstance(keys, CommentedSeq):
        raise BootstrapError("top-level `keys` sequence missing from .sops.yaml")

    scalar = PlainScalarString(age_pubkey)
    scalar.yaml_set_anchor(anchor_name, always_dump=True)
    keys.append(scalar)


def add_to_creation_rule(
    doc: CommentedMap,
    rel_path: str,
    anchor_name: str,
) -> None:
    """Add `anchor_name` as a recipient of the creation_rule covering `rel_path`.

    The rule is located the way sops itself resolves one: the first rule whose
    `path_regex` matches the repo-relative file path (unanchored search, like
    Go's regexp.MatchString). Matching by path — not by comparing regex strings
    — means the existing rule is extended regardless of how its regex is
    spelled, instead of a duplicate rule being appended.
    """
    scalar = _find_anchored_scalar(doc, anchor_name)
    if scalar is None:
        raise BootstrapError(f"anchor {anchor_name!r} not found in .sops.yaml keys section")
    rules = doc.get("creation_rules")
    if not isinstance(rules, CommentedSeq):
        raise BootstrapError("creation_rules sequence missing from .sops.yaml")

    for rule in rules:
        if not isinstance(rule, CommentedMap):
            continue
        path_regex = rule.get("path_regex")
        if not isinstance(path_regex, str) or re.search(path_regex, rel_path) is None:
            continue
        key_groups = rule.get("key_groups")
        if not isinstance(key_groups, CommentedSeq) or not key_groups:
            raise BootstrapError(f"creation_rule for {path_regex!r} has no key_groups")
        group = key_groups[0]
        if not isinstance(group, CommentedMap):
            raise BootstrapError(f"creation_rule for {path_regex!r} key_groups[0] is not a mapping")
        age_list = group.get("age")
        if not isinstance(age_list, CommentedSeq):
            raise BootstrapError(f"creation_rule for {path_regex!r} has no `age` list")
        for existing in age_list:
            if _anchor_name_of(existing) == anchor_name:
                return
        age_list.append(scalar)
        return

    raise BootstrapError(f"no creation_rule in .sops.yaml matches path {rel_path!r}")


def _rule_age_lists(doc: CommentedMap) -> list[tuple[str, CommentedSeq]]:
    """(path_regex, key_groups[0].age) for every well-formed creation rule."""
    rules = doc.get("creation_rules")
    if not isinstance(rules, CommentedSeq):
        raise BootstrapError("creation_rules sequence missing from .sops.yaml")
    out: list[tuple[str, CommentedSeq]] = []
    for rule in rules:
        if not isinstance(rule, CommentedMap):
            continue
        path_regex = rule.get("path_regex")
        key_groups = rule.get("key_groups")
        if not isinstance(path_regex, str) or not isinstance(key_groups, CommentedSeq):
            continue
        if not key_groups or not isinstance(key_groups[0], CommentedMap):
            continue
        age_list = key_groups[0].get("age")
        if isinstance(age_list, CommentedSeq):
            out.append((path_regex, age_list))
    return out


def rules_with_anchor(doc: CommentedMap, anchor_name: str) -> list[str]:
    """path_regex of every creation rule whose recipients include the anchor."""
    return [
        path_regex
        for path_regex, age_list in _rule_age_lists(doc)
        if any(_anchor_name_of(item) == anchor_name for item in age_list)
    ]


def tier_rules_for_host(
    doc: CommentedMap,
    tier_anchor: str,
    host_anchor: str,
) -> list[str] | None:
    """The tier's rules if the host is a recipient of every one of them, else None.

    A host of a tier decrypts exactly the files its tier's bootstrap anchor
    decrypts, so "registered" means the host anchor is in every rule the tier
    anchor is in. A host in none of them is unregistered; a host in only some is
    a partial registration — the shape that once left a privileged host unable
    to decrypt the logscrub literals at its first switch — and counts as
    unregistered too, so register repairs it instead of skipping it.
    """
    tier_rules = rules_with_anchor(doc, tier_anchor)
    if not tier_rules:
        return None
    host_rules = set(rules_with_anchor(doc, host_anchor))
    if all(path_regex in host_rules for path_regex in tier_rules):
        return tier_rules
    return None


def add_to_rules_with_anchor(
    doc: CommentedMap,
    tier_anchor: str,
    host_anchor: str,
) -> list[str]:
    """Add the host as a recipient of every rule the tier anchor is in.

    A host of a tier decrypts exactly the files its bootstrap identity decrypts
    — that is what the tier anchors in the dotfiles .sops.yaml encode — so this
    is the whole membership decision, with no hard-coded file list. Idempotent
    per list object, which matters when several rules alias one `&fleet`
    list: ruamel round-trips the alias as one shared sequence, and the second
    rule finds the host already present. Returns the path_regex of every rule
    the host is now in.
    """
    scalar = _find_anchored_scalar(doc, host_anchor)
    if scalar is None:
        raise BootstrapError(f"anchor {host_anchor!r} not found in .sops.yaml keys section")
    touched: list[str] = []
    for path_regex, age_list in _rule_age_lists(doc):
        if not any(_anchor_name_of(item) == tier_anchor for item in age_list):
            continue
        if not any(_anchor_name_of(item) == host_anchor for item in age_list):
            age_list.append(scalar)
        touched.append(path_regex)
    if not touched:
        raise BootstrapError(
            f"no creation_rule in .sops.yaml lists the tier anchor {tier_anchor!r}"
        )
    return touched


def set_anchored_pubkey(doc: CommentedMap, anchor_name: str, pubkey: str) -> None:
    """Replace the public key behind an anchor, keeping every alias to it.

    The declaration and each `*alias` are one ruamel object; every occurrence
    is swapped by identity, so the dumped file keeps `&anchor` on the first and
    `*anchor` on the rest.
    """
    old = _find_anchored_scalar(doc, anchor_name)
    if old is None:
        raise BootstrapError(f"anchor {anchor_name!r} not found in .sops.yaml keys section")
    fresh = PlainScalarString(pubkey)
    fresh.yaml_set_anchor(anchor_name, always_dump=True)
    keys = doc.get("keys")
    if isinstance(keys, CommentedSeq):
        for idx, item in enumerate(keys):
            if item is old:
                keys[idx] = fresh
    for _path_regex, age_list in _rule_age_lists(doc):
        for idx, item in enumerate(age_list):
            if item is old:
                age_list[idx] = fresh


def remove_age_key(doc: CommentedMap, anchor_name: str) -> None:
    scalar = _find_anchored_scalar(doc, anchor_name)
    if scalar is None:
        raise BootstrapError(f"anchor {anchor_name!r} not found in .sops.yaml keys section")

    rules = doc.get("creation_rules")
    if isinstance(rules, CommentedSeq):
        for rule in rules:
            if not isinstance(rule, CommentedMap):
                continue
            key_groups = rule.get("key_groups")
            if not isinstance(key_groups, CommentedSeq):
                continue
            for group in key_groups:
                if not isinstance(group, CommentedMap):
                    continue
                age_list = group.get("age")
                if not isinstance(age_list, CommentedSeq):
                    continue
                to_remove = [i for i, item in enumerate(age_list) if item is scalar]
                for idx in reversed(to_remove):
                    del age_list[idx]

    keys = doc.get("keys")
    if isinstance(keys, CommentedSeq):
        to_remove = [i for i, item in enumerate(keys) if item is scalar]
        for idx in reversed(to_remove):
            del keys[idx]
