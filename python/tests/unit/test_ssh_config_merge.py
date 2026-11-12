from __future__ import annotations

from pathlib import Path

from bootstrap.lib import ssh_ops

_STANZA = (
    "Host *\n    UseKeychain yes\n    AddKeysToAgent yes\n    IdentityFile ~/.ssh/id_ed25519_test\n"
)


def test_merge_into_empty_file(tmp_path: Path) -> None:
    config = tmp_path / "config"
    ssh_ops.merge_config_stanza(config, _STANZA)
    text = config.read_text()
    assert ssh_ops.STANZA_BEGIN in text
    assert ssh_ops.STANZA_END in text
    assert "UseKeychain yes" in text
    assert "IdentityFile ~/.ssh/id_ed25519_test" in text


def test_merge_into_existing_user_content(tmp_path: Path) -> None:
    config = tmp_path / "config"
    user_content = "Host work\n    HostName work.example.com\n"
    config.write_text(user_content)
    ssh_ops.merge_config_stanza(config, _STANZA)
    text = config.read_text()
    assert "Host work" in text
    assert "HostName work.example.com" in text
    assert "UseKeychain yes" in text


def test_merge_replaces_existing_managed_block(tmp_path: Path) -> None:
    config = tmp_path / "config"
    initial = (
        "Host work\n    HostName work.example.com\n\n"
        f"{ssh_ops.STANZA_BEGIN}\n"
        "Host *\n    IdentityFile ~/.ssh/old_key\n"
        f"{ssh_ops.STANZA_END}\n"
    )
    config.write_text(initial)
    ssh_ops.merge_config_stanza(config, _STANZA)
    text = config.read_text()
    assert "id_ed25519_test" in text
    assert "old_key" not in text
    assert "Host work" in text
    assert text.count(ssh_ops.STANZA_BEGIN) == 1
    assert text.count(ssh_ops.STANZA_END) == 1


def test_merge_idempotent(tmp_path: Path) -> None:
    config = tmp_path / "config"
    ssh_ops.merge_config_stanza(config, _STANZA)
    first = config.read_text()
    ssh_ops.merge_config_stanza(config, _STANZA)
    second = config.read_text()
    assert first == second


def test_merge_preserves_user_content_after_managed_block(tmp_path: Path) -> None:
    config = tmp_path / "config"
    initial = (
        f"{ssh_ops.STANZA_BEGIN}\n"
        "Host *\n    IdentityFile ~/.ssh/old_key\n"
        f"{ssh_ops.STANZA_END}\n"
        "\n"
        "Host home\n    HostName home.example.com\n"
    )
    config.write_text(initial)
    ssh_ops.merge_config_stanza(config, _STANZA)
    text = config.read_text()
    assert "Host home" in text
    assert "id_ed25519_test" in text
    assert "old_key" not in text


def test_unmarked_same_host_block_absorbed(tmp_path: Path) -> None:
    """A pre-existing hand-written `Host github.com` stanza must be replaced,
    not left above the managed block where ssh's first-match would keep it
    winning."""
    config = tmp_path / "config"
    config.write_text(
        "Host github.com\n    IdentityFile ~/.ssh/old_key\n\nHost other.example\n    User someone\n"
    )
    ssh_ops.merge_config_stanza(
        config,
        "Host github.com\n    IdentityFile ~/.ssh/id_ed25519",
    )
    text = config.read_text()
    assert "old_key" not in text
    assert text.count("Host github.com") == 1
    assert "Host other.example" in text
    assert "User someone" in text
    assert ssh_ops.STANZA_BEGIN in text


def test_unmarked_multi_pattern_host_block_kept(tmp_path: Path) -> None:
    """Only an exact-pattern match is absorbed — a multi-pattern block that
    happens to include the host is left alone (conservative)."""
    config = tmp_path / "config"
    config.write_text("Host github.com gist.github.com\n    User git\n")
    ssh_ops.merge_config_stanza(
        config,
        "Host github.com\n    IdentityFile ~/.ssh/id_ed25519",
    )
    text = config.read_text()
    assert "Host github.com gist.github.com" in text
