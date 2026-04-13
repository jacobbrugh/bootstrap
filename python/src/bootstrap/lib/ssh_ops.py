"""SSH key generation, keychain handling, and config-file merging."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import WorkingTreeError

_log = logging.getLogger(__name__)

# Marker comments bracket the bootstrap-managed stanza in ~/.ssh/config so
# re-runs can replace the block without touching user-authored content.
STANZA_BEGIN = "# managed-by: bootstrap begin"
STANZA_END = "# managed-by: bootstrap end"


def keygen(path: Path, comment: str, *, dry_run: bool = False) -> None:
    """Generate an ed25519 SSH keypair at `path` if it doesn't already exist."""
    if path.exists():
        _log.debug("SSH key already exists at %s", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(stat.S_IRWXU)
    sh.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-C", comment, "-N", ""],
        dry_run=dry_run,
        destructive=True,
    )


def apple_keychain_add(key_path: Path, *, dry_run: bool = False) -> None:
    """Add an SSH key to the macOS keychain-backed ssh-agent.

    Darwin-only. The caller is OS-gated (only Darwin phases invoke this).
    """
    sh.run(
        ["ssh-add", "--apple-use-keychain", str(key_path)],
        dry_run=dry_run,
        destructive=True,
    )


def merge_config_stanza(
    config_path: Path,
    stanza: str,
    *,
    dry_run: bool = False,
) -> None:
    """Idempotently add or replace a managed stanza in `~/.ssh/config`.

    The stanza is wrapped in `# managed-by: bootstrap begin` / `end` marker
    comments. On re-runs, the existing marked block is replaced; user-authored
    content outside the markers is untouched.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.parent.chmod(stat.S_IRWXU)

    existing = config_path.read_text() if config_path.exists() else ""
    new_block = f"\n{STANZA_BEGIN}\n{stanza.strip()}\n{STANZA_END}\n"

    if STANZA_BEGIN in existing and STANZA_END in existing:
        before, _, rest = existing.partition(STANZA_BEGIN)
        _, _, after = rest.partition(STANZA_END)
        new_content = before.rstrip() + new_block + after.lstrip("\n")
    else:
        new_content = existing.rstrip() + new_block

    if dry_run:
        _log.info("would update SSH config stanza at %s", config_path)
        return
    config_path.write_text(new_content)
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def update_known_hosts(
    hostname: str,
    pinned_keys_file: Path,
    *,
    known_hosts: Path,
    dry_run: bool = False,
) -> None:
    """Ensure `hostname`'s pinned host keys are present in `known_hosts`.

    `pinned_keys_file` is bundled package data — a file with known_hosts-style
    lines (`<hostname> <alg> <key>`). We filter by `hostname`, deduplicate
    against the existing `known_hosts`, and append whatever's new. We NEVER
    call `ssh-keyscan` — all keys come from the pinned file, which is
    updated out-of-band from `https://api.github.com/meta`.
    """
    if not pinned_keys_file.exists():
        raise WorkingTreeError(
            pinned_keys_file,
            "pinned known-hosts file missing from package data",
        )

    pinned_lines: list[str] = []
    for raw in pinned_keys_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) >= 2 and parts[0] == hostname:
            pinned_lines.append(line)

    if not pinned_lines:
        raise WorkingTreeError(
            pinned_keys_file,
            f"no pinned keys found for hostname {hostname!r}",
        )

    existing_lines: set[str] = set()
    if known_hosts.exists():
        existing_lines = {
            line.strip() for line in known_hosts.read_text().splitlines() if line.strip()
        }
    to_add = [line for line in pinned_lines if line not in existing_lines]

    if not to_add:
        _log.debug("%s host keys already present in %s", hostname, known_hosts)
        return

    if dry_run:
        _log.info(
            "would append %d host key line(s) for %s to %s",
            len(to_add),
            hostname,
            known_hosts,
        )
        return

    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.parent.chmod(stat.S_IRWXU)
    with known_hosts.open("a") as fh:
        if known_hosts.stat().st_size > 0:
            fh.write("\n")
        fh.write("\n".join(to_add) + "\n")
    known_hosts.chmod(stat.S_IRUSR | stat.S_IWUSR)
