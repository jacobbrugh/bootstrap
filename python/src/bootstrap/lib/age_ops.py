from __future__ import annotations

import logging
import stat
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError

_log = logging.getLogger(__name__)


async def generate_keypair(key_file: Path, *, dry_run: bool = False) -> str:
    if not dry_run and key_file.exists():
        raise BootstrapError(
            f"refusing to overwrite existing age key at {key_file}. "
            f"Callers that need the pubkey from an existing file should "
            f"call extract_public_key directly."
        )
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.chmod(stat.S_IRWXU)

    await sh.run(
        ["age-keygen", "-pq", "-o", str(key_file)],
        dry_run=dry_run,
        destructive=True,
    )
    if dry_run:
        return "age1pq1DRYRUN"
    key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return await extract_public_key(key_file)


async def extract_public_key(key_file: Path) -> str:
    result = await sh.run(
        ["age-keygen", "-y", str(key_file)],
        destructive=False,
    )
    return result.stdout.strip()


async def public_key_of_identity(secret: str) -> str:
    """The public key of an identity held in memory (never written to disk)."""
    result = await sh.run(
        ["age-keygen", "-y"],
        input_text=secret.rstrip("\n") + "\n",
        destructive=False,
    )
    return result.stdout.strip()
