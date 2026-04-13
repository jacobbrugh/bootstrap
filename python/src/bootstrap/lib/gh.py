"""GitHub CLI wrapper.

Auth is via the `$GITHUB_TOKEN` env var on each invocation rather than
`gh auth login` — that keeps the token out of the on-disk gh config store
and lets the `secrets.ephemeral_secrets` context manager own the token lifecycle.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from bootstrap.lib import sh

_log = logging.getLogger(__name__)


def _env_with_token(token: str) -> dict[str, str]:
    env = dict(os.environ)
    env["GITHUB_TOKEN"] = token
    return env


def ssh_key_titles(token: str) -> list[str]:
    """Return the titles of the authenticated user's SSH keys.

    Uses `gh api /user/keys --jq '.[].title'` so we get structured output
    without parsing `gh ssh-key list`'s tab-separated format.
    """
    result = sh.run(
        ["gh", "api", "/user/keys", "--jq", ".[].title"],
        env=_env_with_token(token),
        destructive=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def ssh_key_add(
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
    sh.run(
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
