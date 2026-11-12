from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import tomlkit
from tomlkit.toml_document import TOMLDocument

from bootstrap.lib import registry_toml
from bootstrap.lib.errors import BootstrapError

# Mirrors the dotfiles host_registry.toml shapes that lib/hosts/fromRegistry.nix
# expands: literal hosts, indexed templates (startIndex/endIndex), and
# per-host override sections (no `system`) merged onto the expansion.
_FIXTURE = textwrap.dedent(
    """\
    # Host registry — template definitions expanded by lib/hosts/fromRegistry.nix.

    # ── literal host ──────────────────────────────────────────────────────
    [mac1]
    tags = ["laptop"]
    system = "aarch64-darwin"

    # ── indexed template: expands to nix1..nix3 ──────────────────────────
    [nix]
    system = "x86_64-linux"
    startIndex = 1
    endIndex = 3

    # Override section (no `system`) — merged onto the [nix] expansion.
    [nix2]
    tags = ["infra"]

    # ── indexed template with default startIndex 0: kube0..kube1 ────────
    [kube]
    tags = ["kubevirt"]
    system = "x86_64-linux"
    endIndex = 1

    [kube1]
    tags = ["sandbox"]
    username = "kube"
    """
)


def _load(tmp_path: Path) -> TOMLDocument:
    path = tmp_path / "host_registry.toml"
    path.write_text(_FIXTURE)
    return registry_toml.load(path)


def test_literal_host(tmp_path: Path) -> None:
    doc = _load(tmp_path)
    assert registry_toml.has_host(doc, "mac1")
    assert registry_toml.get_tags(doc, "mac1") == ["laptop", "desktop", "wireless"]


def test_indexed_expansion(tmp_path: Path) -> None:
    doc = _load(tmp_path)
    assert registry_toml.has_host(doc, "nix1")
    assert registry_toml.has_host(doc, "nix3")
    assert registry_toml.get_tags(doc, "nix1") == []


def test_indexed_default_start_index_zero(tmp_path: Path) -> None:
    doc = _load(tmp_path)
    assert registry_toml.has_host(doc, "kube0")
    assert registry_toml.get_tags(doc, "kube0") == ["kubevirt"]


def test_template_name_is_not_a_host(tmp_path: Path) -> None:
    doc = _load(tmp_path)
    assert not registry_toml.has_host(doc, "nix")
    assert not registry_toml.has_host(doc, "kube")
    assert registry_toml.get_tags(doc, "nix") == []


def test_out_of_range_index_is_not_a_host(tmp_path: Path) -> None:
    doc = _load(tmp_path)
    assert not registry_toml.has_host(doc, "nix0")
    assert not registry_toml.has_host(doc, "nix4")
    assert not registry_toml.has_host(doc, "kube2")


def test_override_merges_tags(tmp_path: Path) -> None:
    doc = _load(tmp_path)
    assert registry_toml.has_host(doc, "nix2")
    assert registry_toml.get_tags(doc, "nix2") == ["infra"]
    assert registry_toml.get_tags(doc, "kube1") == ["kubevirt", "sandbox"]


def test_override_section_is_not_its_own_host(tmp_path: Path) -> None:
    # An override section only refines an expanded host — a registry where an
    # override matches nothing fails the dotfiles eval, so bootstrap just
    # doesn't surface it as a host.
    path = tmp_path / "host_registry.toml"
    path.write_text('[orphan]\ntags = ["sandbox"]\n')
    doc = registry_toml.load(path)
    assert not registry_toml.has_host(doc, "orphan")


def test_unknown_host(tmp_path: Path) -> None:
    doc = _load(tmp_path)
    assert not registry_toml.has_host(doc, "nope")
    assert registry_toml.get_tags(doc, "nope") == []


def test_save_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "host_registry.toml"
    path.write_text(_FIXTURE)
    doc = registry_toml.load(path)
    registry_toml.save(doc, path)
    assert path.read_text() == _FIXTURE


def test_luks_unlock_defaults_when_absent() -> None:
    """An entry that omits luksUnlock means the fleet default, not "unset"."""
    doc = tomlkit.parse('[plainhost]\nsystem = "x86_64-linux"\n')
    assert registry_toml.get_luks_unlock(doc, "plainhost") == "tang-tpm2"


def test_luks_unlock_read_from_entry() -> None:
    doc = tomlkit.parse('[rtr9]\nsystem = "x86_64-linux"\nluksUnlock = "initrd-key"\n')
    assert registry_toml.get_luks_unlock(doc, "rtr9") == "initrd-key"


def test_luks_unlock_flows_through_indexed_expansion() -> None:
    doc = tomlkit.parse('[node]\nsystem = "x86_64-linux"\nendIndex = 1\nluksUnlock = "tpm2"\n')
    assert registry_toml.get_luks_unlock(doc, "node0") == "tpm2"
    assert registry_toml.get_luks_unlock(doc, "node1") == "tpm2"


def test_unknown_luks_unlock_refused() -> None:
    doc = tomlkit.parse('[bad]\nsystem = "x86_64-linux"\nluksUnlock = "nope"\n')
    with pytest.raises(BootstrapError, match="not one of"):
        registry_toml.get_luks_unlock(doc, "bad")


_TAGGED = tomlkit.parse(
    textwrap.dedent(
        """\
        [lap]
        system = "x86_64-linux"
        tags = ["laptop", "tang"]
        [lap.networking.interfaces.ethernet]
        name = "eno1"
        address = "192.0.2.26"

        [srv]
        system = "x86_64-linux"
        tags = ["k3s", "tang"]
        [srv.networking.interfaces.ethernet]
        address = "192.0.2.201"

        [priv]
        system = "x86_64-linux"
        tags = ["kubevirt", "private"]

        [pinned]
        system = "aarch64-darwin"
        dotfilesDir = "/Users/jacob/elsewhere"

        [plain]
        system = "x86_64-linux"
        """
    )
)


def test_private_implies_sandbox() -> None:
    """A private VM is sandbox-tier in dotfiles; reading raw tags made it privileged here."""
    assert "sandbox" in registry_toml.get_tags(_TAGGED, "priv")


def test_laptop_implies_desktop_and_wireless() -> None:
    assert registry_toml.get_tags(_TAGGED, "lap") == ["laptop", "tang", "desktop", "wireless"]


def test_untagged_hosts_gain_no_tags() -> None:
    assert registry_toml.get_tags(_TAGGED, "plain") == []


def test_tang_urls_come_from_the_tang_hosts_own_addresses() -> None:
    assert registry_toml.tang_urls(_TAGGED) == [
        "http://192.0.2.26:7500",
        "http://192.0.2.201:7500",
    ]


def test_a_tang_host_without_an_address_is_refused() -> None:
    doc = tomlkit.parse('[t]\nsystem = "x86_64-linux"\ntags = ["tang"]\n')
    with pytest.raises(BootstrapError, match="declares no"):
        registry_toml.tang_urls(doc)


def test_dotfiles_dir_is_read_only_when_pinned() -> None:
    assert registry_toml.get_dotfiles_dir(_TAGGED, "pinned") == "/Users/jacob/elsewhere"
    assert registry_toml.get_dotfiles_dir(_TAGGED, "plain") is None
