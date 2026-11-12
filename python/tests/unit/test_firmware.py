from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bootstrap.lib.errors import BootstrapError
from bootstrap.phases.nixos import firmware


def test_ami_is_not_dasharo() -> None:
    """The unit ships with AMI, so this is the branch that decides the whole run."""
    assert not firmware.is_dasharo("CML6A 2.50 x64")
    assert not firmware.is_target_version("CML6A 2.50 x64")


def test_dasharo_target_version_recognised() -> None:
    version = "Dasharo (coreboot+UEFI) v1.2.0"
    assert firmware.is_dasharo(version)
    assert firmware.is_target_version(version)


def test_older_dasharo_is_not_the_target() -> None:
    assert firmware.is_dasharo("Dasharo (coreboot+UEFI) v1.1.0")
    assert not firmware.is_target_version("Dasharo (coreboot+UEFI) v1.1.0")


def test_lockdown_markers_detected() -> None:
    """A locked chip must abort before the write, not during it: a partial write
    is the failure mode that actually bricks a board."""
    assert (
        firmware.find_lockdown("...\nSPI Configuration is locked down\n...")
        == "SPI Configuration is locked down"
    )
    assert firmware.find_lockdown("PR0: Warning: 0x1000-0x2000 is read-only") == "PR0:"
    assert firmware.find_lockdown("Found chipset. Everything is writable.") is None


def test_rom_path_env_seam(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOOTSTRAP_FIRMWARE_ROM", str(tmp_path / "x.rom"))
    assert firmware.rom_path() == tmp_path / "x.rom"
    monkeypatch.delenv("BOOTSTRAP_FIRMWARE_ROM")
    assert firmware.rom_path() == Path("/etc/protectli-firmware/vp46xx.rom")


def test_missing_rom_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setenv("BOOTSTRAP_FIRMWARE_ROM", str(tmp_path / "absent.rom"))
    with pytest.raises(BootstrapError, match="firmware image not found"):
        asyncio.run(firmware.run())


def test_wrong_sized_rom_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A truncated download must never reach the chip."""
    rom = tmp_path / "short.rom"
    rom.write_bytes(b"\xff" * 1024)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setenv("BOOTSTRAP_FIRMWARE_ROM", str(rom))
    with pytest.raises(BootstrapError, match="refusing to write"):
        asyncio.run(firmware.run())


def test_run_refuses_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(BootstrapError, match="must run as root"):
        asyncio.run(firmware.run())
