"""tomlkit-based editor for `nix/config/hosts/registry.toml`.

tomlkit preserves comments, whitespace, key order, and array formatting
across round-trips. The register phase edits the registry through this
module — never via text manipulation — so new host entries don't disturb
the existing entries' hand-written comments.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import tomlkit
from tomlkit.toml_document import TOMLDocument

from bootstrap.lib.errors import BootstrapError


def load(path: Path) -> TOMLDocument:
    """Parse a TOML file in round-trip mode."""
    return tomlkit.parse(path.read_text())


def save(doc: TOMLDocument, path: Path) -> None:
    """Dump a TOML document back to disk. Idempotent on a load/save round-trip."""
    path.write_text(tomlkit.dumps(doc))


def has_host(doc: TOMLDocument, hostname: str) -> bool:
    """True if `[<hostname>]` is already a top-level table in the document."""
    return hostname in doc


def get_tags(doc: TOMLDocument, hostname: str) -> list[str]:
    """Return the tags list for `hostname`, or `[]` if host/`tags` is missing.

    Used by the register phase to reuse existing tags on re-registration
    rather than re-prompting the user when the host is already in
    `registry.toml`.
    """
    table = doc.get(hostname)
    if table is None:
        return []
    tags = table.get("tags")
    if tags is None:
        return []
    return [str(t) for t in tags]


def add_host(
    doc: TOMLDocument,
    hostname: str,
    *,
    system: str,
    tags: Sequence[str] = (),
    username: str | None = None,
) -> None:
    """Add a new `[<hostname>]` top-level table.

    Matches the existing registry style:
    - `system = "…"` first
    - `tags = [...]` second if non-empty
    - `username = "…"` only if overriding the default

    Raises `BootstrapError` if the hostname is already registered.
    """
    if has_host(doc, hostname):
        raise BootstrapError(f"host {hostname!r} already in registry.toml")
    table = tomlkit.table()
    table.add("system", system)
    if tags:
        arr = tomlkit.array()
        for tag in tags:
            arr.append(tag)
        table.add("tags", arr)
    if username:
        table.add("username", username)
    doc[hostname] = table
