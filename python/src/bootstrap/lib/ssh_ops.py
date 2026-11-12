from __future__ import annotations

import logging
import stat
from pathlib import Path

from bootstrap.lib import sh

_log = logging.getLogger(__name__)

STANZA_BEGIN = "# managed-by: bootstrap begin"
STANZA_END = "# managed-by: bootstrap end"


async def keygen(path: Path, comment: str, *, dry_run: bool = False) -> None:
    if path.exists():
        _log.debug("SSH key already exists at %s", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(stat.S_IRWXU)
    await sh.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-C", comment, "-N", ""],
        dry_run=dry_run,
        destructive=True,
    )


async def apple_keychain_add(key_path: Path, *, dry_run: bool = False) -> None:
    await sh.run(
        ["/usr/bin/ssh-add", "--apple-use-keychain", str(key_path)],
        dry_run=dry_run,
        destructive=True,
    )


def _strip_unmarked_host_block(existing: str, host_pattern: str) -> str:
    """Remove a pre-existing unmarkered `Host <pattern>` block from an ssh config.

    Without this, a hand-written `Host github.com` stanza that predates
    bootstrap would sit above the managed block and win ssh's first-match
    resolution, silently overriding the managed settings. Blocks are
    delimited by the next line starting with `Host `/`Match ` (or EOF).
    """
    lines = existing.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        starts_block = stripped.startswith(("Host ", "Match "))
        if starts_block:
            skipping = stripped.startswith("Host ") and stripped.split()[1:] == [host_pattern]
        if not skipping:
            out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def merge_config_stanza(
    config_path: Path,
    stanza: str,
    *,
    dry_run: bool = False,
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.parent.chmod(stat.S_IRWXU)

    existing = config_path.read_text() if config_path.exists() else ""
    new_block = f"\n{STANZA_BEGIN}\n{stanza.strip()}\n{STANZA_END}\n"

    if STANZA_BEGIN in existing and STANZA_END in existing:
        before, _, rest = existing.partition(STANZA_BEGIN)
        _, _, after = rest.partition(STANZA_END)
        new_content = before.rstrip() + new_block + after.lstrip("\n")
    else:
        first_line = stanza.strip().splitlines()[0].strip()
        if first_line.startswith("Host "):
            host_pattern = first_line.split(maxsplit=1)[1].strip()
            existing = _strip_unmarked_host_block(existing, host_pattern)
        new_content = existing.rstrip() + new_block

    if dry_run:
        _log.info("would update SSH config stanza at %s", config_path)
        return
    config_path.write_text(new_content)
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
