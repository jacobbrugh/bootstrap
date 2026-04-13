"""ssh phase (OS-agnostic portion).

Generates an ed25519 keypair, uploads the public half to GitHub via the
bootstrap PAT, and idempotently adds pinned github.com host keys to
`~/.ssh/known_hosts`.

OS-specific follow-ups live in their own phase modules: `phases/darwin/keychain.py`
adds the key to the macOS keychain-backed ssh-agent and writes a
`UseKeychain` stanza to `~/.ssh/config`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import resources

from bootstrap.lib import gh, log, ssh_ops
from bootstrap.lib.errors import PrereqMissing
from bootstrap.lib.paths import SSH_KNOWN_HOSTS, ssh_key_path
from bootstrap.lib.runtime import Context

NAME = "ssh"

_log = log.get(__name__)


def run(ctx: Context) -> None:
    """Generate key, upload to GitHub, pin github.com host keys."""
    if ctx.github_token is None:
        raise PrereqMissing(
            "ctx.github_token",
            where="wrap in `secrets.ephemeral_secrets(ctx)`",
        )

    key_path = ssh_key_path(ctx.hostname)
    comment = f"{ctx.hostname}-bootstrap"
    ssh_ops.keygen(key_path, comment, dry_run=ctx.dry_run)

    _pin_github_host_keys(dry_run=ctx.dry_run)

    pubkey_path = key_path.with_suffix(".pub")
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    title = f"bootstrap:{ctx.hostname}:{today}"

    # The `gh api /user/keys` idempotency check is `destructive=False` so it
    # runs even in dry-run — but with the dry-run fake token from
    # `ephemeral_secrets`, it would fail 401 Unauthorized. Short-circuit the
    # whole GitHub interaction here. `gh.ssh_key_titles`'s destructive marker
    # stays False because real runs SHOULD hit the API for the idempotency
    # check; only the dry-run-with-fake-token combination is broken.
    if ctx.dry_run:
        _log.info(
            "[dry-run] would check GitHub for SSH key titled %s and upload %s if missing",
            title,
            pubkey_path,
        )
        return

    existing_titles = gh.ssh_key_titles(ctx.github_token)
    if title in existing_titles:
        _log.info("GitHub already has SSH key with title %s — skipping upload", title)
    else:
        gh.ssh_key_add(ctx.github_token, pubkey_path, title, dry_run=ctx.dry_run)


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
