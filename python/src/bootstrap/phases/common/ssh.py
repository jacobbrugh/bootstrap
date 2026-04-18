"""ssh phase (OS-agnostic portion).

Generates an ed25519 keypair, writes a `github.com` stanza to
`~/.ssh/config` with `StrictHostKeyChecking accept-new` (so the first
outbound SSH to github.com auto-populates `~/.ssh/known_hosts` instead
of bundling a pinned host-key list), then uploads the public half to
GitHub via the bootstrap PAT.

`accept-new` auto-accepts a new host's key on first connection and
writes it to `known_hosts`; subsequent connections verify against that
entry. A MitM presenting a different key still fails. Right security
posture for a freshly-provisioned host.

OS-specific follow-ups live in their own phase modules:
`phases/darwin/keychain.py` adds the key to the macOS keychain-backed
ssh-agent and writes a `UseKeychain` stanza to `~/.ssh/config`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from bootstrap.lib import gh, ssh_ops
from bootstrap.lib.errors import PrereqMissing
from bootstrap.lib.paths import SSH_CONFIG, SSH_KEY
from bootstrap.lib.runtime import Context

NAME = "ssh"

_log = logging.getLogger(__name__)

_GITHUB_SSH_STANZA = """\
Host github.com
  StrictHostKeyChecking accept-new\
"""


async def run(ctx: Context) -> None:
    """Generate key, config accept-new for github.com, upload public key."""
    if not ctx.dry_run and ctx.github_token is None:
        raise PrereqMissing(
            "ctx.github_token",
            where="wrap in `secrets.ephemeral_secrets(ctx)`",
        )

    comment = f"{ctx.hostname}-bootstrap"
    await ssh_ops.keygen(SSH_KEY, comment, dry_run=ctx.dry_run)

    ssh_ops.merge_config_stanza(SSH_CONFIG, _GITHUB_SSH_STANZA, dry_run=ctx.dry_run)

    pubkey_path = SSH_KEY.with_suffix(".pub")
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    title = f"bootstrap:{ctx.hostname}:{today}"

    if ctx.dry_run:
        _log.info(
            "[dry-run] would check GitHub for %s and upload with title %s if missing",
            pubkey_path,
            title,
        )
        return

    # The prereq check above guarantees ctx.github_token is not None at
    # this point — the `if not ctx.dry_run` branch only lets through real
    # runs, and the real-run branch requires the token.
    assert ctx.github_token is not None
    if await gh.ssh_key_registered(ctx.github_token, pubkey_path):
        _log.info("GitHub already has this public key — skipping upload")
    else:
        await gh.ssh_key_add(ctx.github_token, pubkey_path, title, dry_run=ctx.dry_run)
