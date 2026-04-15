"""GitHub CLI wrapper.

Auth is via the `$GITHUB_TOKEN` env var on each invocation rather than
`gh auth login` — that keeps the token out of the on-disk gh config store
and lets the `secrets.ephemeral_secrets` context manager own the token lifecycle.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Git author/committer identity, derived from the authenticated GitHub user."""

    name: str
    email: str


def _env_with_token(token: str) -> dict[str, str]:
    env = dict(os.environ)
    env["GITHUB_TOKEN"] = token
    return env


async def ssh_key_registered(token: str, pubkey_path: Path) -> bool:
    """Return True iff the public key at `pubkey_path` is already registered.

    Checks by **content**, not by title. Matching on the
    `bootstrap:<host>:<YYYY-MM-DD>` title caused a silent-divergence bug:
    if a re-run regenerates `~/.ssh/id_ed25519` (e.g. after a path-
    convention change), the old GitHub title is still there pointing at the
    *old* pubkey, so we'd skip the upload and silently leave a mismatch —
    local SSH auth then fails with `Permission denied (publickey)` the next
    time anything touches git over ssh.

    GitHub's API returns each key as `<algo> <base64>` (no comment). We
    match on those two leading fields from the local file, so an
    algo+base64 hit is considered "already registered" regardless of the
    stored title or the comment stripped by GitHub.
    """
    local_head = _pubkey_head(pubkey_path)
    if local_head is None:
        return False
    result = await sh.run(
        ["gh", "api", "/user/keys", "--jq", ".[].key"],
        env=_env_with_token(token),
        destructive=False,
    )
    for line in result.stdout.splitlines():
        remote = line.strip()
        if not remote:
            continue
        remote_parts = remote.split(maxsplit=2)
        if len(remote_parts) < 2:
            continue
        if " ".join(remote_parts[:2]) == local_head:
            return True
    return False


def _pubkey_head(pubkey_path: Path) -> str | None:
    """Return `<algo> <base64>` from a public-key file, or None if unparsable."""
    try:
        content = pubkey_path.read_text()
    except OSError:
        return None
    parts = content.strip().split(maxsplit=2)
    if len(parts) < 2:
        return None
    return " ".join(parts[:2])


async def ssh_key_add(
    token: str,
    pubkey_path: Path,
    title: str,
    *,
    dry_run: bool = False,
) -> None:
    """Upload a public SSH key to the authenticated user's account.

    Title format should include the hostname + date so re-runs on a
    reinstalled machine don't collide with the old entry:
    `bootstrap:<hostname>:<YYYY-MM-DD>`.
    """
    _log.info("uploading SSH key to GitHub: title=%s", title)
    await sh.run(
        [
            "gh",
            "ssh-key",
            "add",
            str(pubkey_path),
            "--title",
            title,
            "--type",
            "authentication",
        ],
        env=_env_with_token(token),
        dry_run=dry_run,
        destructive=True,
    )


async def get_git_identity(token: str) -> GitIdentity:
    """Return the authenticated user's git author/committer identity.

    Fetches `gh api user` and derives `(name, email)` with safe fallbacks:
    - `name` defaults to `login` if the user hasn't set a display name
    - `email` defaults to GitHub's `<id>+<login>@users.noreply.github.com`
      format if the profile email is null (which it is when the user has
      the "Keep my email address private" setting enabled).

    The bootstrap uses this to set `GIT_{AUTHOR,COMMITTER}_{NAME,EMAIL}`
    on the register-phase commit, since fresh machines don't yet have
    `git config --global user.name/email` set.
    """
    result = await sh.run(
        ["gh", "api", "user"],
        env=_env_with_token(token),
        destructive=False,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"gh api user returned non-JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BootstrapError(f"gh api user returned {type(data).__name__}, expected object")

    login = data.get("login")
    user_id = data.get("id")
    if not isinstance(login, str) or not isinstance(user_id, int):
        raise BootstrapError(f"gh api user response missing login/id: {data!r}")

    raw_name = data.get("name")
    name = raw_name if isinstance(raw_name, str) and raw_name else login

    raw_email = data.get("email")
    email = (
        raw_email
        if isinstance(raw_email, str) and raw_email
        else f"{user_id}+{login}@users.noreply.github.com"
    )

    return GitIdentity(name=name, email=email)
