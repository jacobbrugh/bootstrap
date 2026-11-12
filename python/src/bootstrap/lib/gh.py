from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError

_log = logging.getLogger(__name__)

#: The only account a privileged host's SSH key may be uploaded to. The ssh
#: phase checks `gh api user` against this before `gh ssh-key add`, so a token
#: minted under any other account is refused rather than used.
PRIVILEGED_LOGIN = "jacobbrugh"


@dataclass(frozen=True, slots=True)
class GitIdentity:
    login: str
    name: str
    email: str


def _env_with_token(token: str) -> dict[str, str]:
    env = dict(os.environ)
    env["GITHUB_TOKEN"] = token
    return env


async def ssh_key_registered(token: str, pubkey_path: Path) -> bool:
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

    return GitIdentity(login=login, name=name, email=email)
