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

# `UseKeychain` is an Apple-specific patch on macOS's OpenSSH. Any other
# ssh on the machine (notably the Nix-provided one the bootstrap wrapper
# puts first on PATH) will abort at config-parse time with
# "Bad configuration option: usekeychain". `IgnoreUnknown UseKeychain`
# is an upstream directive that tells any parser to silently skip the
# option if it doesn't recognize it. Apple's ssh sees and uses
# `UseKeychain yes` normally; everyone else skips it.
_STANZA = (
    "Host *\n"
    "    IgnoreUnknown UseKeychain\n"
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
