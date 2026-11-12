from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap.lib import flake_splice
from bootstrap.lib.errors import BootstrapError


def _code(rendered: str) -> str:
    """The rendered module minus comment lines.

    The generated stanzas explain themselves at length, and those comments name
    the options they are explaining — so a bare substring check against the whole
    text asserts nothing about what is actually SET.
    """
    return "\n".join(line for line in rendered.splitlines() if not line.lstrip().startswith("#"))


def _clone_fixture(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "flake.nix").write_text("{ outputs = _: { }; }\n")
    return clone


def _hw_config(tmp_path: Path) -> Path:
    hw = tmp_path / "hardware-configuration.nix"
    hw.write_text('{ ... }: { fileSystems."/".device = "/dev/vda"; }\n')
    return hw


def test_standalone_splice_has_no_bootloader_stanza(tmp_path: Path) -> None:
    """The secret_zero path copies hardware config verbatim — an already-
    booted host keeps whatever bootloader it has."""
    clone = _clone_fixture(tmp_path)
    hw = _hw_config(tmp_path)
    flake_splice.splice(clone=clone, hardware_config=hw)

    spliced = (clone / "host-hardware.nix").read_text()
    assert "boot.loader" not in spliced
    assert spliced == hw.read_text()


def test_missing_hardware_config_is_a_hard_error(tmp_path: Path) -> None:
    clone = _clone_fixture(tmp_path)
    with pytest.raises(BootstrapError, match="hardware configuration not found"):
        flake_splice.splice(clone=clone, hardware_config=tmp_path / "nope.nix")


def test_networking_config_spliced_when_present(tmp_path: Path) -> None:
    clone = _clone_fixture(tmp_path)
    hw = _hw_config(tmp_path)
    net = tmp_path / "networking.nix"
    net.write_text("{ ... }: { networking.useDHCP = true; }\n")
    flake_splice.splice(clone=clone, hardware_config=hw, networking_config=net)
    assert (clone / "host-networking.nix").read_text() == net.read_text()


def test_bootloader_wrapper_shape(tmp_path: Path) -> None:
    clone = _clone_fixture(tmp_path)
    hw = _hw_config(tmp_path)
    flake_splice.splice(
        clone=clone,
        hardware_config=hw,
        hostname="nix9",
        inject_bootloader=True,
        luks_unlock="initrd-key",
    )
    wrapper = (clone / "host-hardware.nix").read_text()
    assert "./hardware-configuration.nix" in wrapper
    assert "./nix/nixos/bootstrap-firstboot.nix" in wrapper
    assert "boot.loader.systemd-boot.enable = true;" in wrapper
    assert "boot.loader.grub.enable = false;" in wrapper
    assert 'networking.hostName = "nix9";' in wrapper
    assert "boot.initrd.clevis" not in _code(wrapper)
    assert (clone / "hardware-configuration.nix").read_text() == hw.read_text()


def test_bootloader_wrapper_with_clevis(tmp_path: Path) -> None:
    clone = _clone_fixture(tmp_path)
    hw = _hw_config(tmp_path)
    flake_splice.splice(
        clone=clone,
        hardware_config=hw,
        hostname="nix9",
        inject_bootloader=True,
        luks_unlock="tang-tpm2",
    )
    wrapper = (clone / "host-hardware.nix").read_text()
    code = _code(wrapper)
    assert "boot.initrd.clevis.enable = true;" in code
    assert 'boot.initrd.systemd.services."clevis-luks-unlock-cryptroot"' in code
    assert "secretFile" not in code
    assert not (clone / "cryptroot.jwe").exists()


def test_tpm2_wrapper_never_waits_on_the_network(tmp_path: Path) -> None:
    """The TPM token needs no network, so the tpm2 stanza brings up none and
    orders nothing after network-online.target."""
    clone = _clone_fixture(tmp_path)
    hw = _hw_config(tmp_path)
    flake_splice.splice(
        clone=clone,
        hardware_config=hw,
        hostname="rtr9",
        inject_bootloader=True,
        luks_unlock="tpm2",
    )
    code = _code((clone / "host-hardware.nix").read_text())
    assert 'crypttabExtraOpts = [ "tpm2-device=auto" ];' in code
    assert "network-online" not in code
    assert "boot.initrd.systemd.network" not in code


def test_git_dir_dropped_so_path_flake_sees_splices(tmp_path: Path) -> None:
    clone = _clone_fixture(tmp_path)
    (clone / ".git").mkdir()
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    hw = _hw_config(tmp_path)
    flake_splice.splice(clone=clone, hardware_config=hw)
    assert not (clone / ".git").exists()


def test_bootloader_without_hostname_refused(tmp_path: Path) -> None:
    clone = _clone_fixture(tmp_path)
    hw = _hw_config(tmp_path)
    with pytest.raises(BootstrapError, match="requires a hostname"):
        flake_splice.splice(clone=clone, hardware_config=hw, inject_bootloader=True)


def test_flake_ref_env_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_FLAKE", "git+file:///x")
    assert flake_splice.flake_ref() == "git+file:///x"
    monkeypatch.delenv("BOOTSTRAP_FLAKE")
    # No baked media copy on a dev machine → the public repo.
    assert flake_splice.flake_ref() in (
        "github:jacobbrugh/bootstrap",
        f"path:{Path('/etc/bootstrap-flake').resolve()}",
    )
