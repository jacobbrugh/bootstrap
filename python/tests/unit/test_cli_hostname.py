"""Hostname resolution: an explicit name everywhere, detection everywhere but darwin."""

from __future__ import annotations

import asyncio

import pytest

from bootstrap import cli
from bootstrap.lib import host_info
from bootstrap.lib.errors import BootstrapError
from bootstrap.platform import Platform


async def _detected() -> str:
    return "Jacobs-Mac-mini"


def test_the_argument_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_HOSTNAME", "from-env")
    assert asyncio.run(cli._resolve_hostname("mac2", platform=Platform.DARWIN)) == "mac2"


def test_the_environment_names_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_HOSTNAME", "mac2")
    assert asyncio.run(cli._resolve_hostname(None, platform=Platform.DARWIN)) == "mac2"


def test_a_mac_with_no_name_given_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOOTSTRAP_HOSTNAME", raising=False)
    monkeypatch.setattr(host_info, "detect_hostname", _detected)
    with pytest.raises(BootstrapError, match="no hostname given"):
        asyncio.run(cli._resolve_hostname(None, platform=Platform.DARWIN))


def test_nixos_falls_back_to_the_detected_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOTSTRAP_HOSTNAME", raising=False)

    async def detected() -> str:
        return "nixhost"

    monkeypatch.setattr(host_info, "detect_hostname", detected)
    assert asyncio.run(cli._resolve_hostname(None, platform=Platform.NIXOS)) == "nixhost"
