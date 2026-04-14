"""ssh phase (OS-agnostic portion).

Generates an ed25519 keypair, uploads the public half to GitHub via the
bootstrap PAT, and idempotently adds pinned github.com host keys to
`~/.ssh/known_hosts`.

OS-specific follow-ups live in their own phase modules: `phases/darwin/keychain.py`
adds the key to the macOS keychain-backed ssh-agent and writes a
`UseKeychain` stanza to `~/.ssh/config`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from importlib import resources

from bootstrap.lib import gh, ssh_ops
from bootstrap.lib.errors import PrereqMissing
from bootstrap.lib.paths import SSH_KEY, SSH_KNOWN_HOSTS
from bootstrap.lib.runtime import Context

NAME = "ssh"

_log = logging.getLogger(__name__)


async def run(ctx: Context) -> None:
    """Generate key, upload to GitHub, pin github.com host keys."""
    if not ctx.dry_run and ctx.github_token is None:
        raise PrereqMissing(
            "ctx.github_token",
            where="wrap in `secrets.ephemeral_secrets(ctx)`",
        )

    comment = f"{ctx.hostname}-bootstrap"
    await ssh_ops.keygen(SSH_KEY, comment, dry_run=ctx.dry_run)

    _pin_github_host_keys(dry_run=ctx.dry_run)

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


def _pin_github_host_keys(*, dry_run: bool) -> None:
    """Copy pinned github.com host keys from package data into ~/.ssh/known_hosts."""
    pinned = resources.files("bootstrap.data") / "github_known_hosts.txt"
    with resources.as_file(pinned) as pinned_path:
        ssh_ops.update_known_hosts(
            "github.com",
            pinned_path,
            known_hosts=SSH_KNOWN_HOSTS,
            dry_run=dry_run,
        )
