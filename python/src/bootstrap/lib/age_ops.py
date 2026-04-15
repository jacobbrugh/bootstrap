"""age-keygen wrapper — generates post-quantum identity files.

Uses `age-keygen -pq` (ML-KEM-768 + X25519 hybrid). The generated file is
an age identity containing both the private key and a comment with the
public key; `age-keygen -y <file>` extracts the public key.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path

from bootstrap.lib import sh

_log = logging.getLogger(__name__)


async def generate_keypair(key_file: Path, *, dry_run: bool = False) -> str:
    """Generate a post-quantum age keypair at `key_file`.

    Returns the public key. The parent directory is created with mode 0700
    and the file with mode 0600. Callers must ensure `key_file` doesn't
    already exist — use `extract_public_key` to read the pubkey from an
    existing file instead of calling this.
    """
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
    """Extract the public key from an existing age identity file.

    `age-keygen -y <file>` reads the identity and prints the corresponding
    recipient (public key) to stdout.
    """
    result = await sh.run(
        ["age-keygen", "-y", str(key_file)],
        destructive=False,
    )
    return result.stdout.strip()
