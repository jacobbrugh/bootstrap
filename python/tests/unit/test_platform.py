"""Unit tests for `bootstrap.platform.detect`."""

from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap import platform as platform_module
from bootstrap.platform import Platform, detect


def test_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    assert detect() is Platform.DARWIN


def test_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    assert detect() is Platform.UNSUPPORTED


def test_linux_hm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(platform_module, "WSL_SENTINEL", tmp_path / "no-wsl")
    monkeypatch.setattr(platform_module, "NIXOS_SENTINEL", tmp_path / "no-nixos")
    assert detect() is Platform.LINUX_HM


def test_nixos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    nixos = tmp_path / "NIXOS"
    nixos.touch()
    monkeypatch.setattr(platform_module, "NIXOS_SENTINEL", nixos)
    monkeypatch.setattr(platform_module, "WSL_SENTINEL", tmp_path / "no-wsl")
    assert detect() is Platform.NIXOS


def test_nixos_wsl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    nixos = tmp_path / "NIXOS"
    wsl = tmp_path / "WSLInterop"
    nixos.touch()
    wsl.touch()
    monkeypatch.setattr(platform_module, "NIXOS_SENTINEL", nixos)
    monkeypatch.setattr(platform_module, "WSL_SENTINEL", wsl)
    assert detect() is Platform.NIXOS_WSL
