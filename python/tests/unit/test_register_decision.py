"""register decides "done" from its post-condition, not from a marker.

The post-condition: the age key on this disk decrypts every sops file of the
host's tier as they stand in the clone, and the staged host files are
committed. A key that is merely *named* in .sops.yaml proves nothing about what
the encrypted files are sealed to — that happened on a real host: anchored, unable to decrypt,
and skipped on every retry. Each case below drives the whole phase with every
external effect stubbed and asserts exactly which writes it made.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bootstrap.lib import age_ops, git_ops, sops_ops
from bootstrap.lib.errors import ShellError
from bootstrap.lib.runtime import Context, Tier
from bootstrap.phases.common import register
from bootstrap.platform import Platform
from tests.unit.conftest import ShRecorder

HOST_PUB = "age1pq1hosthosthosthosthosthost"
OLD_PUB = "age1pq1oldoldoldoldoldoldoldold"
BOT_FILE = Path("modules/security/secrets/bot-secrets.yaml")
HW_REL = Path("config/hosts/rtr9/hardware.nix")


def _sops_text(*, host_pub: str | None, in_rules: bool) -> str:
    keys = ["  - &bootstrap_sandbox age1pq1sandboxsandboxsandbox"]
    members = ["          - *bootstrap_sandbox"]
    if host_pub is not None:
        keys.append(f"  - &host_rtr9 {host_pub}")
        if in_rules:
            members.append("          - *host_rtr9")
    return (
        "keys:\n"
        + "\n".join(keys)
        + "\ncreation_rules:\n"
        + "  - path_regex: '^modules/security/secrets/bot-secrets\\.yaml$'\n"
        + "    key_groups:\n"
        + "      - age:\n"
        + "\n".join(members)
        + "\n"
    )


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    sh_recorder: ShRecorder,
    tmp_path: Path,
    *,
    sops_text: str,
    key_present: bool,
    decrypts: bool,
    hw_differs: bool,
) -> Context:
    """Wire register.run against a fake clone; return the Context to run it with."""
    repo = tmp_path / "dotfiles"
    (repo / ".git").mkdir(parents=True)
    (repo / "host_registry.toml").write_text(
        '[rtr9]\nsystem = "x86_64-linux"\ntags = ["sandbox"]\n'
    )
    (repo / ".sops.yaml").write_text(sops_text)
    (repo / BOT_FILE).parent.mkdir(parents=True)
    (repo / BOT_FILE).write_text("sops: {}\n")
    (repo / HW_REL).parent.mkdir(parents=True)
    (repo / HW_REL).write_text("{ old = 1; }\n" if hw_differs else "{ new = 1; }\n")

    staged = tmp_path / "etc-nixos" / "hardware-configuration.nix"
    staged.parent.mkdir()
    staged.write_text("{ new = 1; }\n")

    key_file = tmp_path / "keys.txt"
    if key_present:
        key_file.write_text("AGE-SECRET-KEY-1HOST\n")

    ctx = Context(platform=Platform.NIXOS, hostname="rtr9", canonical_repo=repo)
    ctx.tier = Tier.SANDBOX
    ctx.github_token = "ghp_bot"
    ctx.bootstrap_age_key_file = tmp_path / "age-key"

    monkeypatch.setattr(register, "DOTFILES_GIT_REMOTE_OVERRIDE", "file:///x")
    monkeypatch.setattr(register, "SOPS_AGE_KEY_FILE", key_file)
    monkeypatch.setattr(register, "_HARVEST_SOURCES", {"hardware.nix": staged})

    async def noop(*_a: object, **_k: object) -> None:
        return None

    @contextlib.asynccontextmanager
    async def fake_edit(*_a: object, **_k: object) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(git_ops, "clone_or_pull", noop)
    monkeypatch.setattr(git_ops, "transactional_edit", fake_edit)
    monkeypatch.setattr(register, "_ensure_symlink", noop)

    sh_recorder.answers[("git", "-C", str(repo), "ls-files", "-z")] = f"{BOT_FILE.as_posix()}\0"
    sh_recorder.answers[("age-keygen", "-y", str(key_file))] = HOST_PUB
    sh_recorder.answers[("gh", "api", "user")] = json.dumps(
        {"login": "jacobbrugh-bot", "id": 2, "name": "bot", "email": "b@x"}
    )

    async def fake_generate(path: Path, *, dry_run: bool = False) -> str:
        path.write_text("AGE-SECRET-KEY-1NEW\n")
        return HOST_PUB

    monkeypatch.setattr(age_ops, "generate_keypair", fake_generate)

    async def fake_verify(secret_file: Path, *, env_overlay: dict[str, str], repo: Path) -> None:
        # The host key opens a file only after `sops updatekeys` sealed it in
        # (or from the start, when the case says it already decrypts).
        if env_overlay.get("SOPS_AGE_KEY_FILE") != str(key_file) or decrypts:
            return
        if sh_recorder.with_prefix("sops", "updatekeys", "--yes", str(secret_file)):
            return
        raise ShellError(
            ["sops", "--decrypt", str(secret_file)],
            1,
            "Error getting data key: 0 successful groups required, got 0",
        )

    monkeypatch.setattr(sops_ops, "verify_decrypt", fake_verify)
    return ctx


def _commits(sh_recorder: ShRecorder, repo: Path) -> list[tuple[str, ...]]:
    return [c.argv for c in sh_recorder.with_prefix("git", "-C", str(repo), "commit")]


def _pushes(sh_recorder: ShRecorder, repo: Path) -> list[tuple[str, ...]]:
    return [c.argv for c in sh_recorder.with_prefix("git", "-C", str(repo), "push")]


def _updatekeys(sh_recorder: ShRecorder) -> list[tuple[str, ...]]:
    return [c.argv for c in sh_recorder.with_prefix("sops", "updatekeys")]


def test_registered_host_writes_nothing(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = _drive(
        monkeypatch,
        sh_recorder,
        tmp_path,
        sops_text=_sops_text(host_pub=HOST_PUB, in_rules=True),
        key_present=True,
        decrypts=True,
        hw_differs=False,
    )
    before = (ctx.canonical_repo / ".sops.yaml").read_text()

    asyncio.run(register.run(ctx))

    assert _updatekeys(sh_recorder) == []
    assert _commits(sh_recorder, ctx.canonical_repo) == []
    assert _pushes(sh_recorder, ctx.canonical_repo) == []
    assert (ctx.canonical_repo / ".sops.yaml").read_text() == before


def test_anchored_key_that_cannot_decrypt_is_rekeyed(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key is named in .sops.yaml, the files are sealed to another.
    Before the fix register returned at the anchored check and the switch
    activated secrets the host could not open."""
    ctx = _drive(
        monkeypatch,
        sh_recorder,
        tmp_path,
        sops_text=_sops_text(host_pub=HOST_PUB, in_rules=True),
        key_present=True,
        decrypts=False,
        hw_differs=False,
    )

    asyncio.run(register.run(ctx))

    rekeyed = _updatekeys(sh_recorder)
    assert rekeyed == [("sops", "updatekeys", "--yes", str(ctx.canonical_repo / BOT_FILE))]
    assert len(_commits(sh_recorder, ctx.canonical_repo)) == 1
    assert len(_pushes(sh_recorder, ctx.canonical_repo)) == 1
    text = (ctx.canonical_repo / ".sops.yaml").read_text()
    # The keys section is untouched: one anchor, the same pubkey.
    assert text.count("&host_rtr9") == 1
    assert HOST_PUB in text


def test_unanchored_key_replaces_a_stale_anchor_of_this_host(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A register that generated this key and died before its push (or an
    earlier flash) left another key under host_rtr9. Before the fix
    add_age_key raised "anchor already exists" and the host could never
    register on a retry."""
    ctx = _drive(
        monkeypatch,
        sh_recorder,
        tmp_path,
        sops_text=_sops_text(host_pub=OLD_PUB, in_rules=True),
        key_present=True,
        decrypts=False,
        hw_differs=False,
    )

    asyncio.run(register.run(ctx))

    text = (ctx.canonical_repo / ".sops.yaml").read_text()
    assert HOST_PUB in text
    assert OLD_PUB not in text
    assert text.count("&host_rtr9") == 1
    assert len(_updatekeys(sh_recorder)) == 1
    assert len(_commits(sh_recorder, ctx.canonical_repo)) == 1
    assert len(_pushes(sh_recorder, ctx.canonical_repo)) == 1


def test_absent_key_is_generated_and_replaces_the_stale_anchor(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A re-flashed disk: no key, and origin still carries the previous one."""
    ctx = _drive(
        monkeypatch,
        sh_recorder,
        tmp_path,
        sops_text=_sops_text(host_pub=OLD_PUB, in_rules=True),
        key_present=False,
        decrypts=False,
        hw_differs=False,
    )

    asyncio.run(register.run(ctx))

    assert (tmp_path / "keys.txt").exists()
    text = (ctx.canonical_repo / ".sops.yaml").read_text()
    assert HOST_PUB in text
    assert OLD_PUB not in text
    assert len(_updatekeys(sh_recorder)) == 1
    assert len(_commits(sh_recorder, ctx.canonical_repo)) == 1


def test_changed_host_files_are_committed_without_a_rekey(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A re-flash regenerated the disk UUIDs but the key still opens every
    file: harvest and commit, and do not rewrite the encrypted files."""
    ctx = _drive(
        monkeypatch,
        sh_recorder,
        tmp_path,
        sops_text=_sops_text(host_pub=HOST_PUB, in_rules=True),
        key_present=True,
        decrypts=True,
        hw_differs=True,
    )
    before = (ctx.canonical_repo / ".sops.yaml").read_text()

    asyncio.run(register.run(ctx))

    assert _updatekeys(sh_recorder) == []
    assert (ctx.canonical_repo / HW_REL).read_text() == "{ new = 1; }\n"
    adds = [c.argv for c in sh_recorder.with_prefix("git", "-C", str(ctx.canonical_repo), "add")]
    assert adds == [("git", "-C", str(ctx.canonical_repo), "add", "--", HW_REL.as_posix())]
    assert len(_commits(sh_recorder, ctx.canonical_repo)) == 1
    assert len(_pushes(sh_recorder, ctx.canonical_repo)) == 1
    assert (ctx.canonical_repo / ".sops.yaml").read_text() == before


def _age_keygen(path: Path) -> str:
    subprocess.run(["age-keygen", "-o", str(path)], check=True, capture_output=True)
    return subprocess.run(
        ["age-keygen", "-y", str(path)], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_host_key_decrypts_against_real_sops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The decrypt check on real crypto: a file sealed to another identity is a
    real `0 successful groups` failure, and `sops updatekeys` sealing the host
    in turns it into a pass."""
    for tool in ("sops", "age-keygen"):
        assert shutil.which(tool), f"{tool} is not on PATH (flake.nix nativeCheckInputs)"

    host_key = tmp_path / "host.txt"
    other_key = tmp_path / "other.txt"
    host_pub = _age_keygen(host_key)
    other_pub = _age_keygen(other_key)

    repo = tmp_path / "repo"
    (repo / BOT_FILE).parent.mkdir(parents=True)
    plain = tmp_path / "plain.yaml"
    plain.write_text("token: hunter2\n")
    encrypted = subprocess.run(
        [
            "sops",
            "--encrypt",
            "--age",
            other_pub,
            "--input-type",
            "yaml",
            "--output-type",
            "yaml",
            str(plain),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (repo / BOT_FILE).write_text(encrypted)

    monkeypatch.setattr(register, "SOPS_AGE_KEY_FILE", host_key)
    ctx = Context(platform=Platform.NIXOS, hostname="rtr9", canonical_repo=repo)
    assert asyncio.run(register._host_key_decrypts(ctx, [BOT_FILE])) is False

    (repo / ".sops.yaml").write_text(
        "creation_rules:\n"
        "  - path_regex: '.*'\n"
        "    key_groups:\n"
        "      - age:\n"
        f"          - {other_pub}\n"
        f"          - {host_pub}\n"
    )
    subprocess.run(
        ["sops", "updatekeys", "--yes", str(repo / BOT_FILE)],
        check=True,
        capture_output=True,
        cwd=repo,
        env={**os.environ, "SOPS_AGE_KEY_FILE": str(other_key)},
    )
    assert asyncio.run(register._host_key_decrypts(ctx, [BOT_FILE])) is True


def test_missing_key_never_decrypts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(register, "SOPS_AGE_KEY_FILE", tmp_path / "absent.txt")
    ctx = Context(platform=Platform.NIXOS, hostname="rtr9", canonical_repo=tmp_path)
    assert asyncio.run(register._host_key_decrypts(ctx, [BOT_FILE])) is False
