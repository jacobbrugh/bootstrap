"""sops wrapper — update_keys + verify_decrypt for the register phase.

`update_keys` re-encrypts an existing sops file with the current recipient
list from `.sops.yaml`. Decryption uses the bootstrap age key (supplied via
`SOPS_AGE_KEY_FILE` env var); re-encryption writes to all recipients listed
in `.sops.yaml` for the file's path regex.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from bootstrap.lib import sh

_log = logging.getLogger(__name__)


def _env_with_age_key(age_key_file: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["SOPS_AGE_KEY_FILE"] = str(age_key_file)
    return env


async def update_keys(
    secret_file: Path,
    *,
    age_key_file: Path,
    repo: Path,
    dry_run: bool = False,
) -> None:
    """Run `sops updatekeys --yes <file>` to re-encrypt with the current recipient list.

    Runs with `cwd=repo` so sops resolves `.sops.yaml` relative to the
    dotfiles repo root, not the caller's cwd.
    """
    _log.info("sops updatekeys: %s", secret_file)
    await sh.run(
        ["sops", "updatekeys", "--yes", str(secret_file)],
        env=_env_with_age_key(age_key_file),
        cwd=repo,
        dry_run=dry_run,
        destructive=True,
    )


async def verify_decrypt(
    secret_file: Path,
    *,
    age_key_file: Path,
    repo: Path,
) -> None:
    """Verify that `secret_file` can be decrypted with `age_key_file`.

    Captures and discards the decrypted output — we only care that the
    exit code is zero. Used after `update_keys` to prove the new key was
    actually added to the recipient list before we commit.
    """
    await sh.run(
        ["sops", "--decrypt", str(secret_file)],
        env=_env_with_age_key(age_key_file),
        cwd=repo,
        destructive=False,
    )
