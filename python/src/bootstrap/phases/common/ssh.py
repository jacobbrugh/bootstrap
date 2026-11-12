"""The ssh phase: a host key, and — for a privileged host only — its upload.

Every host gets an ed25519 key and a `Host github.com` stanza. What happens
next is decided by the tier the loaded bootstrap age key proved
(lib/secrets.py):

* sandbox — nothing. No SSH key of a sandbox host is uploaded to any GitHub
  account; register reaches dotfiles over https with the bot token instead.
* privileged — the key is uploaded to the `jacobbrugh` account, with the
  token only the privileged age key can decrypt (secrets-privileged.yaml).
  The token's owner is checked first, so a token minted under the bot or
  any other account is refused before anything is uploaded.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from bootstrap.lib import gh, secrets, ssh_ops
from bootstrap.lib.errors import BootstrapError, PrereqMissing
from bootstrap.lib.paths import SSH_CONFIG, SSH_KEY
from bootstrap.lib.runtime import Context

NAME = "ssh"

_log = logging.getLogger(__name__)

_GITHUB_SSH_STANZA = """\
Host github.com
  StrictHostKeyChecking accept-new\
"""


async def run(ctx: Context) -> None:
    if not ctx.dry_run and ctx.github_token is None:
        raise PrereqMissing(
            "ctx.github_token",
            where="wrap in `secrets.ephemeral_secrets(ctx)`",
        )

    comment = f"{ctx.hostname}-bootstrap"
    await ssh_ops.keygen(SSH_KEY, comment, dry_run=ctx.dry_run)

    ssh_ops.merge_config_stanza(SSH_CONFIG, _GITHUB_SSH_STANZA, dry_run=ctx.dry_run)

    if ctx.dry_run:
        _log.info(
            "[dry-run] no key loaded, so the tier is unknown: a privileged host "
            "would upload %s to the %s GitHub account, a sandbox host uploads nothing",
            SSH_KEY.with_suffix(".pub"),
            gh.PRIVILEGED_LOGIN,
        )
        return

    if ctx.is_sandbox:
        _log.info("sandbox tier — no GitHub SSH key upload")
        return

    pubkey_path = SSH_KEY.with_suffix(".pub")
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    title = f"bootstrap:{ctx.hostname}:{today}"

    token = await secrets.privileged_github_token(ctx)
    owner = await gh.get_git_identity(token)
    if owner.login != gh.PRIVILEGED_LOGIN:
        raise BootstrapError(
            f"the privileged GitHub token belongs to {owner.login!r}, not "
            f"{gh.PRIVILEGED_LOGIN!r} — refusing to upload this host's SSH key "
            "anywhere but the privileged account"
        )
    if await gh.ssh_key_registered(token, pubkey_path):
        _log.info("GitHub (%s) already has this public key — skipping upload", owner.login)
    else:
        await gh.ssh_key_add(token, pubkey_path, title, dry_run=ctx.dry_run)
