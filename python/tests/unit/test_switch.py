"""The NixOS switch is done when the system it produced is the one asked for
and healthy — never because nixos-rebuild's exit code said so. Exit 4 masked a
failed activation on a real host (sops-nix could not decrypt, the dependent units
failed, and the phase accepted it)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError, ShellError
from bootstrap.lib.runtime import Context
from bootstrap.phases.nixos import switch
from bootstrap.platform import Platform

_GEN = "/nix/store/aaaa-nixos-system-rtr9"


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    rebuild_rc: int,
    current: str = _GEN,
    profile: str = _GEN,
    hm_state: str = "active",
    failed: str = "",
    secret: bytes | None = b"hunter2",
) -> list[tuple[str, ...]]:
    argvs: list[tuple[str, ...]] = []

    secret_path = tmp_path / "run-secrets" / "token"
    secret_path.parent.mkdir()
    if secret is not None:
        secret_path.write_bytes(secret)
    manifest = tmp_path / "abc-manifest.json"
    manifest.write_text(json.dumps({"secrets": [{"name": "token", "path": str(secret_path)}]}))
    activate = tmp_path / "activate"
    activate.write_text(
        f"#!/bin/sh\n/nix/store/x-sops-install-secrets-0.0.1/bin/sops-install-secrets {manifest}\n"
    )
    monkeypatch.setattr(switch, "_ACTIVATE_PATH", activate)
    monkeypatch.setattr(switch, "_current_user", lambda: "jacob")

    async def fake_sudo_run(cmd: Sequence[str], **kw: object) -> SimpleNamespace:
        argv = tuple(cmd)
        argvs.append(argv)
        return SimpleNamespace(
            cmd=argv,
            returncode=rebuild_rc,
            stdout="",
            stderr="",
            dry_run_skipped=False,
            ok=lambda: rebuild_rc == 0,
        )

    async def fake_run(
        cmd: Sequence[str],
        *,
        check: bool = True,
        capture: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        dry_run: bool = False,
        destructive: bool = True,
    ) -> SimpleNamespace:
        argv = tuple(cmd)
        argvs.append(argv)
        if argv[:2] == ("readlink", "-f"):
            out = current if argv[2] == "/run/current-system" else profile
        elif argv[:2] == ("systemctl", "is-active"):
            out = hm_state
        elif argv[:2] == ("systemctl", "--failed"):
            out = failed
        else:
            out = ""
        return SimpleNamespace(
            cmd=argv, returncode=0, stdout=out, stderr="", dry_run_skipped=False, ok=lambda: True
        )

    monkeypatch.setattr(sh, "sudo_run", fake_sudo_run)
    monkeypatch.setattr(sh, "run", fake_run)
    return argvs


def _ctx(tmp_path: Path) -> Context:
    return Context(platform=Platform.NIXOS, hostname="rtr9", canonical_repo=tmp_path / "dotfiles")


def test_healthy_switch_returns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    argvs = _wire(monkeypatch, tmp_path, rebuild_rc=0)
    asyncio.run(switch.run(_ctx(tmp_path)))
    assert ("sudo", "-nH", "nixos-rebuild", "switch") in argvs or (
        "nixos-rebuild",
        "switch",
    ) in argvs
    assert ("systemctl", "--failed", "--no-legend", "--plain") in argvs


def test_exit_4_with_a_failed_unit_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(
        monkeypatch,
        tmp_path,
        rebuild_rc=4,
        failed="home-manager-jacob.service loaded failed failed Home Manager environment for jacob",
        hm_state="failed",
    )
    with pytest.raises(BootstrapError, match=re.escape("failed units: home-manager-jacob.service")):
        asyncio.run(switch.run(_ctx(tmp_path)))


def test_exit_4_with_a_healthy_system_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exit code is not the check: a transient unit-restart hiccup with a
    verified-healthy system is a completed switch."""
    _wire(monkeypatch, tmp_path, rebuild_rc=4)
    asyncio.run(switch.run(_ctx(tmp_path)))


def test_empty_declared_secret_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real host's exact failure: nixos-rebuild "succeeded" and the secret sops-nix
    declares for the generation was never written."""
    _wire(monkeypatch, tmp_path, rebuild_rc=0, secret=b"")
    with pytest.raises(BootstrapError, match="missing or empty"):
        asyncio.run(switch.run(_ctx(tmp_path)))


def test_missing_declared_secret_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(monkeypatch, tmp_path, rebuild_rc=0, secret=None)
    with pytest.raises(BootstrapError, match="missing or empty"):
        asyncio.run(switch.run(_ctx(tmp_path)))


def test_inactive_home_manager_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(monkeypatch, tmp_path, rebuild_rc=0, hm_state="inactive")
    with pytest.raises(BootstrapError, match=re.escape("home-manager-jacob.service is inactive")):
        asyncio.run(switch.run(_ctx(tmp_path)))


def test_generation_not_current_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(monkeypatch, tmp_path, rebuild_rc=0, current="/nix/store/old-system", profile=_GEN)
    with pytest.raises(BootstrapError, match="not active"):
        asyncio.run(switch.run(_ctx(tmp_path)))


def test_other_exit_codes_still_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _wire(monkeypatch, tmp_path, rebuild_rc=1)
    with pytest.raises(ShellError):
        asyncio.run(switch.run(_ctx(tmp_path)))


def test_declared_secret_paths_reads_the_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "zzz-manifest.json"
    manifest.write_text(
        json.dumps({"secrets": [{"path": "/run/secrets/a"}, {"path": "/etc/argocd/token"}]})
    )
    activate = tmp_path / "activate"
    activate.write_text(
        f"foo\n/nix/store/q-sops-install-secrets/bin/sops-install-secrets {manifest}\n"
    )
    assert switch._declared_secret_paths(activate) == [
        Path("/run/secrets/a"),
        Path("/etc/argocd/token"),
    ]
    assert switch._declared_secret_paths(tmp_path / "missing") == []
