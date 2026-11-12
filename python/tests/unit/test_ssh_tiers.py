"""The ssh phase, one tier at a time, down to the exact commands it runs.

The privileged path is the only one that talks to GitHub, and it must do so
with the jacobbrugh-owned token — never the bot token — and only after the
token's owner has been checked. The sandbox path runs no `gh` at all and
never reads the privileged secrets file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bootstrap.lib import flake_splice
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Context, Tier
from bootstrap.phases.common import ssh
from bootstrap.platform import Platform
from tests.unit.conftest import ShRecorder

BOT_TOKEN = "ghp_bot_token_value"
PRIV_TOKEN = "github_pat_privileged_value"


def _ctx(tmp_path: Path, tier: Tier, *, force_sandbox: bool = False) -> Context:
    ctx = Context(
        platform=Platform.NIXOS,
        hostname="rtr9",
        canonical_repo=tmp_path / "dotfiles",
        force_sandbox=force_sandbox,
    )
    ctx.tier = tier
    ctx.github_token = BOT_TOKEN
    ctx.bootstrap_age_key_file = tmp_path / "age-key"
    return ctx


def _stage_ssh_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An existing keypair, so keygen is a no-op and the phase goes straight to GitHub."""
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("PRIVATE")
    key.with_suffix(".pub").write_text("ssh-ed25519 AAAAtest rtr9-bootstrap\n")
    monkeypatch.setattr(ssh, "SSH_KEY", key)
    monkeypatch.setattr(ssh, "SSH_CONFIG", ssh_dir / "config")
    return key.with_suffix(".pub")


def _stage_flake_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    src = tmp_path / "flake-src"
    src.mkdir()

    async def fake_source_path(_ref: str) -> Path:
        return src

    monkeypatch.setattr(flake_splice, "flake_ref", lambda: "path:/nowhere")
    monkeypatch.setattr(flake_splice, "source_path", fake_source_path)
    return src


def _answer_privileged(rec: ShRecorder, login: str, token: str = PRIV_TOKEN) -> None:
    rec.answers[("sops", "-d")] = token + "\n"
    rec.answers[("gh", "api", "user")] = json.dumps(
        {"login": login, "id": 1, "name": "Jacob Brugh", "email": None}
    )
    rec.answers[("gh", "api", "/user/keys")] = ""


def test_privileged_uploads_with_the_privileged_token_only(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pub = _stage_ssh_dir(monkeypatch, tmp_path)
    src = _stage_flake_source(monkeypatch, tmp_path)
    _answer_privileged(sh_recorder, "jacobbrugh")

    asyncio.run(ssh.run(_ctx(tmp_path, Tier.PRIVILEGED)))

    argvs = sh_recorder.argvs()
    # The exact sequence: decrypt the token, check its owner, dup-check, upload.
    assert argvs[0][:3] == ("sops", "-d", "--extract")
    assert argvs[0][3] == '["github_ssh_key_token"]'
    assert argvs[0][4] == str(src / "secrets-privileged.yaml")
    assert argvs[1] == ("gh", "api", "user")
    assert argvs[2] == ("gh", "api", "/user/keys", "--jq", ".[].key")
    add = argvs[3]
    assert add[:4] == ("gh", "ssh-key", "add", str(pub))
    assert add[4:6] == ("--title", f"bootstrap:rtr9:{add[5].split(':')[-1]}")
    assert add[5].startswith("bootstrap:rtr9:20")
    assert add[6:] == ("--type", "authentication")
    assert len(argvs) == 4

    # Every gh call authenticates with the privileged token; the bot token
    # reaches none of them.
    for call in sh_recorder.with_prefix("gh"):
        assert call.env is not None
        assert call.env["GITHUB_TOKEN"] == PRIV_TOKEN
        assert BOT_TOKEN not in call.env.values()
    # The age key, not the bot token, is what unlocks the file.
    sops_env = sh_recorder.calls[0].env
    assert sops_env is not None
    assert sops_env["SOPS_AGE_KEY_FILE"] == str(tmp_path / "age-key")


def test_privileged_skips_upload_when_key_already_registered(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage_ssh_dir(monkeypatch, tmp_path)
    _stage_flake_source(monkeypatch, tmp_path)
    _answer_privileged(sh_recorder, "jacobbrugh")
    sh_recorder.answers[("gh", "api", "/user/keys")] = "ssh-ed25519 AAAAtest other-title\n"

    asyncio.run(ssh.run(_ctx(tmp_path, Tier.PRIVILEGED)))

    assert sh_recorder.with_prefix("gh", "ssh-key") == []


def test_privileged_refuses_a_token_owned_by_another_account(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bot-owned token would upload the key to the bot account; refuse first."""
    _stage_ssh_dir(monkeypatch, tmp_path)
    _stage_flake_source(monkeypatch, tmp_path)
    _answer_privileged(sh_recorder, "jacobbrugh-bot")

    with pytest.raises(BootstrapError, match="belongs to 'jacobbrugh-bot'"):
        asyncio.run(ssh.run(_ctx(tmp_path, Tier.PRIVILEGED)))

    assert sh_recorder.with_prefix("gh", "ssh-key") == []
    assert sh_recorder.with_prefix("gh", "api", "/user/keys") == []


def test_privileged_refuses_the_committed_placeholder(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage_ssh_dir(monkeypatch, tmp_path)
    _stage_flake_source(monkeypatch, tmp_path)
    _answer_privileged(sh_recorder, "jacobbrugh", token="unset")

    with pytest.raises(BootstrapError, match="placeholder"):
        asyncio.run(ssh.run(_ctx(tmp_path, Tier.PRIVILEGED)))

    assert sh_recorder.with_prefix("gh") == []


def test_sandbox_runs_no_gh_and_never_reads_the_privileged_file(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ssh_dir = tmp_path / "ssh"
    monkeypatch.setattr(ssh, "SSH_KEY", ssh_dir / "id_ed25519")
    monkeypatch.setattr(ssh, "SSH_CONFIG", ssh_dir / "config")
    _stage_flake_source(monkeypatch, tmp_path)

    asyncio.run(ssh.run(_ctx(tmp_path, Tier.SANDBOX)))

    argvs = sh_recorder.argvs()
    # The key is still generated locally…
    assert argvs[0][:2] == ("ssh-keygen", "-t")
    # …and that is the only command: nothing reaches GitHub, nothing decrypts
    # the privileged file.
    assert len(argvs) == 1
    assert sh_recorder.with_prefix("gh") == []
    assert sh_recorder.with_prefix("sops") == []
    assert (ssh_dir / "config").read_text().count("Host github.com") == 1


def test_forced_sandbox_overrides_a_privileged_key(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BOOTSTRAP_SANDBOX=1 lowers the tier: the e2e harness never uploads."""
    _stage_ssh_dir(monkeypatch, tmp_path)
    _stage_flake_source(monkeypatch, tmp_path)
    _answer_privileged(sh_recorder, "jacobbrugh")

    asyncio.run(ssh.run(_ctx(tmp_path, Tier.PRIVILEGED, force_sandbox=True)))

    assert sh_recorder.calls == []


def test_dry_run_uploads_nothing(
    sh_recorder: ShRecorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stage_ssh_dir(monkeypatch, tmp_path)
    ctx = _ctx(tmp_path, Tier.PRIVILEGED)
    ctx.dry_run = True
    ctx.github_token = None
    ctx.tier = None

    asyncio.run(ssh.run(ctx))

    assert sh_recorder.calls == []
