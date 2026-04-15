"""ruamel.yaml-based editor for `.sops.yaml`.

`.sops.yaml` uses YAML anchors heavily: every recipient age key is declared
once in the top-level `keys:` sequence with an anchor (`- &pc_jacobmac
age1pq…`) and referenced by alias everywhere else (`- *pc_jacobmac` inside
each `creation_rules[*].key_groups[0].age` list).

ruamel.yaml's round-trip mode is the only YAML library that preserves
anchors, aliases, comments, and whitespace. All edits to `.sops.yaml` go
through this module — never via text manipulation.

Adding a new host is two calls: `add_age_key` adds the anchor declaration
in `keys`, then `add_to_creation_rule` inserts an alias reference into each
relevant creation_rule. Removing a host is one call (`remove_age_key`),
which strips both the declaration and every alias reference to it in a
single pass so the document never has dangling aliases mid-operation.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import PlainScalarString

from bootstrap.lib.errors import BootstrapError


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    # PQ age keys are ~2 KB single-line strings. Default line width would
    # wrap them, which changes the file's structure on round-trip. Disable.
    yaml.width = 1 << 20
    # The existing file uses 2-space indent; round-trip preserves it but
    # being explicit hurts nothing.
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load(path: Path) -> CommentedMap:
    """Load `.sops.yaml` in round-trip mode."""
    yaml = _yaml()
    with path.open("r") as fh:
        doc = yaml.load(fh)
    if not isinstance(doc, CommentedMap):
        raise BootstrapError(f"expected top-level mapping in {path}, got {type(doc).__name__}")
    return doc


def save(doc: CommentedMap, path: Path) -> None:
    """Dump `.sops.yaml` back to disk."""
    yaml = _yaml()
    with path.open("w") as fh:
        yaml.dump(doc, fh)


def _anchor_name_of(item: object) -> str | None:
    """Return the anchor name on a YAML node, if any."""
    anchor_attr = getattr(item, "anchor", None)
    if anchor_attr is None:
        return None
    value = getattr(anchor_attr, "value", None)
    if isinstance(value, str):
        return value
    return None


def has_anchor(doc: CommentedMap, anchor_name: str) -> bool:
    """True if `anchor_name` is already declared in the top-level `keys` sequence."""
    keys = doc.get("keys")
    if not isinstance(keys, CommentedSeq):
        return False
    return any(_anchor_name_of(item) == anchor_name for item in keys)


def _find_anchored_scalar(doc: CommentedMap, anchor_name: str) -> PlainScalarString | None:
    """Look up the scalar string that carries `anchor_name` in `keys`."""
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
    """Return the public-key string for `anchor_name` if present, else None.

    Public sibling to `has_anchor`: instead of just "is this anchor declared?"
    this returns the actual value string so callers can compare against a
    freshly-generated pubkey without reaching into the anchored scalar
    object directly.
    """
    scalar = _find_anchored_scalar(doc, anchor_name)
    if scalar is None:
        return None
    return str(scalar)


def find_anchor_by_pubkey(doc: CommentedMap, pubkey: str) -> str | None:
    """Return the anchor name whose declared value equals `pubkey`, else None.

    Existing `.sops.yaml` files often use ad-hoc anchor names that don't
    match the bootstrap's `host_<hostname>` convention — e.g. `pc_jacobmac`
    for the primary Mac, `server_nixN` for NixOS servers, `server_wsl1`,
    `server_lima1`, etc. When the bootstrap re-runs on one of those hosts
    and finds an existing local age key, matching by content (the pubkey
    string) lets us re-use the existing anchor instead of appending a
    duplicate `host_<hostname>` anchor for the same key.
    """
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
    """Declare a new anchored age key in the top-level `keys` sequence.

    Raises `BootstrapError` if `anchor_name` already exists.
    """
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
    path_regex: str,
    anchor_name: str,
) -> None:
    """Append an alias-to-`anchor_name` into the `age:` list of the matching creation_rule.

    Idempotent: if the alias is already present in the target rule, this is
    a no-op. Raises `BootstrapError` if the anchor isn't declared yet (call
    `add_age_key` first), or if no creation_rule matches `path_regex`.
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
        if rule.get("path_regex") != path_regex:
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
                return  # already aliased in this rule
        age_list.append(scalar)
        return

    raise BootstrapError(f"creation_rule with path_regex {path_regex!r} not found in .sops.yaml")


def remove_age_key(doc: CommentedMap, anchor_name: str) -> None:
    """Remove an age key anchor AND all its alias references in one pass.

    Deletes:
      1. Every alias-to-`anchor_name` from every
         `creation_rules[*].key_groups[*].age` list (first, so the document
         never carries dangling aliases mid-operation), then
      2. The `- &anchor_name <pubkey>` entry from the top-level `keys`
         sequence.

    Alias matching uses Python object identity — in ruamel.yaml round-trip
    mode, `*anchor_name` references are the same object as the declared
    scalar, so an `item is scalar` check finds every reference regardless
    of where it lives in the document.

    Raises `BootstrapError` if the anchor isn't declared.
    """
    scalar = _find_anchored_scalar(doc, anchor_name)
    if scalar is None:
        raise BootstrapError(f"anchor {anchor_name!r} not found in .sops.yaml keys section")

    # Strip aliases from creation_rules first, so no dangling alias ever
    # exists in a transiently-inconsistent intermediate document state.
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
                # Collect then delete in reverse so indices stay valid
                # across in-place `CommentedSeq` mutation.
                to_remove = [i for i, item in enumerate(age_list) if item is scalar]
                for idx in reversed(to_remove):
                    del age_list[idx]

    # Now remove the declaration from `keys:`.
    keys = doc.get("keys")
    if isinstance(keys, CommentedSeq):
        to_remove = [i for i, item in enumerate(keys) if item is scalar]
        for idx in reversed(to_remove):
            del keys[idx]
