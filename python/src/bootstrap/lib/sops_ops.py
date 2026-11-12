from __future__ import annotations

import logging
import os
from pathlib import Path

from bootstrap.lib import sh

_log = logging.getLogger(__name__)


def _env(overlay: dict[str, str]) -> dict[str, str]:
    return {**os.environ, **overlay}


async def update_keys(
    secret_file: Path,
    *,
    env_overlay: dict[str, str],
    repo: Path,
    dry_run: bool = False,
) -> None:
    _log.info("sops updatekeys: %s", secret_file)
    await sh.run(
        ["sops", "updatekeys", "--yes", str(secret_file)],
        env=_env(env_overlay),
        cwd=repo,
        dry_run=dry_run,
        destructive=True,
    )


async def verify_decrypt(
    secret_file: Path,
    *,
    env_overlay: dict[str, str],
    repo: Path,
) -> None:
    await sh.run(
        ["sops", "--decrypt", str(secret_file)],
        env=_env(env_overlay),
        cwd=repo,
        destructive=False,
    )
