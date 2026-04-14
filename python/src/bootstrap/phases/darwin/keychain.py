"""Darwin keychain — add SSH key to the macOS keychain-backed ssh-agent.

Runs AFTER `phases/common/ssh.py` has generated the keypair and uploaded
the public half to GitHub. Adds the private key to the macOS keychain via
`ssh-add --apple-use-keychain` and writes a `Host *` stanza to
`~/.ssh/config` so future ssh invocations unlock the key automatically.
"""

from __future__ import annotations

from bootstrap.lib import log, ssh_ops
from bootstrap.lib.errors import PrereqMissing
from bootstrap.lib.paths import SSH_CONFIG, SSH_KEY
from bootstrap.lib.runtime import Context

NAME = "keychain"

_log = log.get(__name__)

_STANZA = (
    "Host *\n"
    "    UseKeychain yes\n"
    "    AddKeysToAgent yes\n"
    f"    IdentityFile {SSH_KEY}\n"
)


def run(ctx: Context) -> None:
    if not SSH_KEY.exists() and not ctx.dry_run:
        raise PrereqMissing(
            str(SSH_KEY),
            where="run the `ssh` phase before `keychain`",
        )

    ssh_ops.apple_keychain_add(SSH_KEY, dry_run=ctx.dry_run)
    ssh_ops.merge_config_stanza(SSH_CONFIG, _STANZA, dry_run=ctx.dry_run)
