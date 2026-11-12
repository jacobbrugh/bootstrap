from __future__ import annotations

import logging

from bootstrap.lib import ssh_ops
from bootstrap.lib.errors import PrereqMissing
from bootstrap.lib.paths import SSH_CONFIG, SSH_KEY
from bootstrap.lib.runtime import Context

NAME = "keychain"

_log = logging.getLogger(__name__)

_STANZA = (
    "Host *\n"
    "    IgnoreUnknown UseKeychain\n"
    "    UseKeychain yes\n"
    "    AddKeysToAgent yes\n"
    f"    IdentityFile {SSH_KEY}\n"
)


async def run(ctx: Context) -> None:
    if not SSH_KEY.exists() and not ctx.dry_run:
        raise PrereqMissing(
            str(SSH_KEY),
            where="run the `ssh` phase before `keychain`",
        )

    await ssh_ops.apple_keychain_add(SSH_KEY, dry_run=ctx.dry_run)
    ssh_ops.merge_config_stanza(SSH_CONFIG, _STANZA, dry_run=ctx.dry_run)
