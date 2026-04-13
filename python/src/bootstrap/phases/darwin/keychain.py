"""Darwin keychain — add SSH key to the macOS keychain-backed ssh-agent.

Runs AFTER `phases/common/ssh.py` has generated the keypair and uploaded
the public half to GitHub. Adds the private key to the macOS keychain via
`ssh-add --apple-use-keychain` and writes a `Host *` stanza to
`~/.ssh/config` so future ssh invocations unlock the key automatically.
"""

from __future__ import annotations

from pathlib import Path

from bootstrap.lib import log, ssh_ops
from bootstrap.lib.errors import PrereqMissing
from bootstrap.lib.paths import SSH_CONFIG, ssh_key_path
from bootstrap.lib.runtime import Context

NAME = "keychain"

_log = log.get(__name__)


def run(ctx: Context) -> None:
    key_path = ssh_key_path(ctx.hostname)
    if not key_path.exists() and not ctx.dry_run:
        raise PrereqMissing(
            str(key_path),
            where="run the `ssh` phase before `keychain`",
        )

    ssh_ops.apple_keychain_add(key_path, dry_run=ctx.dry_run)
    ssh_ops.merge_config_stanza(
        SSH_CONFIG,
        _stanza(key_path),
        dry_run=ctx.dry_run,
    )


def _stanza(key_path: Path) -> str:
    return f"Host *\n    UseKeychain yes\n    AddKeysToAgent yes\n    IdentityFile {key_path}\n"
