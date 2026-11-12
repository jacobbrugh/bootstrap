"""The CI staging tool's pure parts: fixtures, recipient parsing, dummies, pruning."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bootstrap.ci import e2e_stage
from bootstrap.lib.runtime import Tier


def test_registry_fixture_tiers() -> None:
    sandbox = e2e_stage.registry_fixture(Tier.SANDBOX)
    assert "[e2e]" in sandbox and "endIndex = 0" in sandbox
    assert '[e2e0]\ntags = ["sandbox"]' in sandbox
    privileged = e2e_stage.registry_fixture(Tier.PRIVILEGED)
    # Untagged on purpose: a privileged host. luksUnlock picks the booted one.
    assert "sandbox" not in privileged
    assert "[usb-e2e-tang]" in privileged
    assert '[usb-e2e-initrd]\nsystem = "x86_64-linux"\nluksUnlock = "initrd-key"' in privileged


def test_nix_conf_lines_appended_once(tmp_path: Path) -> None:
    conf = tmp_path / "nix.conf"
    e2e_stage.ensure_nix_conf(conf)
    e2e_stage.ensure_nix_conf(conf)
    text = conf.read_text()
    assert text.count("sandbox = relaxed") == 1
    assert text.count("extra-sandbox-paths = /tmp/bootstrap-e2e-shared") == 1


def test_delete_symlinks(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("x")
    (tmp_path / "sub").mkdir()
    os.symlink("a", tmp_path / "link")
    os.symlink("../a", tmp_path / "sub/link2")
    assert e2e_stage.delete_symlinks(tmp_path) == 2
    assert (tmp_path / "a").exists()
    assert not (tmp_path / "link").exists()


def test_recipients_of_yaml_and_json(tmp_path: Path) -> None:
    y = tmp_path / "s.yaml"
    y.write_text(
        "k: ENC[AES256_GCM,data:x]\n"
        "sops:\n"
        "    age:\n"
        "        - recipient: age1aaa\n"
        "          enc: |\n            X\n"
        "        - recipient: age1bbb\n"
        "          enc: |\n            Y\n"
    )
    assert e2e_stage.recipients_of(y) == {"age1aaa", "age1bbb"}
    j = tmp_path / "t.jsonl.enc"
    meta = {"age": [{"recipient": "age1ccc", "enc": "Z"}]}
    j.write_text(json.dumps({"data": "ENC[x]", "sops": meta}))
    assert e2e_stage.recipients_of(j) == {"age1ccc"}


def test_dummy_keeps_shape_and_drops_values() -> None:
    """A SopsSecret-shaped literals file: metadata survives, values and the
    sops block go — what sops-nix and logscrubd read is the shape."""
    ciphertext = (
        "apiVersion: isindir.github.com/v1alpha3\n"
        "kind: SopsSecret\n"
        "metadata:\n  name: logscrub-literals\n"
        "spec:\n  secretTemplates:\n    - name: logscrub-literals\n"
        "      stringData:\n        lscrub-canary: ENC[AES256_GCM,data:abc]\n"
        "sops:\n  age:\n    - recipient: age1aaa\n      enc: X\n  mac: ENC[AES256_GCM,data:m]\n"
    )
    plain = e2e_stage.dummy_plaintext(ciphertext)
    assert "kind: SopsSecret" in plain
    assert "lscrub-canary: e2e-dummy" in plain
    assert "ENC[" not in plain
    assert "sops:" not in plain
    flat = e2e_stage.dummy_plaintext(
        "attic_client_token: ENC[AES256_GCM,data:a]\n"
        "psk: ENC[AES256_GCM,data:b]\n"
        "sops:\n  version: 3\n"
    )
    assert flat == "attic_client_token: e2e-dummy\npsk: e2e-dummy\n"
    binary = e2e_stage.dummy_plaintext(json.dumps({"data": "ENC[x]", "sops": {"age": []}}))
    assert json.loads(binary) == {"data": "e2e-dummy"}


def test_prune_cache_by_age(tmp_path: Path) -> None:
    old = tmp_path / "old.narinfo"
    new = tmp_path / "nar" / "new.nar.zst"
    new.parent.mkdir()
    old.write_text("x")
    new.write_text("y")
    stale = time.time() - 20 * 24 * 3600
    os.utime(old, (stale, stale))
    assert e2e_stage.prune_cache(tmp_path, now=time.time()) == 1
    assert not old.exists()
    assert new.exists()
    assert e2e_stage.prune_cache(tmp_path / "absent", now=time.time()) == 0


def test_tracked_files_matching(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    (repo / "d").mkdir(parents=True)
    (repo / "secrets.yaml").write_text("x")
    (repo / "d" / "secrets.yaml").write_text("x")
    (repo / "untracked.yaml").write_text("x")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "secrets.yaml", "d/secrets.yaml"], check=True)
    assert asyncio.run(e2e_stage.tracked_files_matching(repo, [r"^secrets\.yaml$"])) == [
        Path("secrets.yaml")
    ]


def test_cli_entry_point_and_tier_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console script imports `main`; a bad tier and a missing key fail
    before anything is staged."""
    assert callable(e2e_stage.main)
    runner = CliRunner()
    assert runner.invoke(e2e_stage.app, ["--help"]).exit_code == 0
    assert runner.invoke(e2e_stage.app, ["nonsense"]).exit_code != 0
    monkeypatch.delenv("SOPS_AGE_KEY", raising=False)
    assert runner.invoke(e2e_stage.app, ["sandbox"]).exit_code == 1
