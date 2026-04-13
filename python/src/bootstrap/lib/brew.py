"""Homebrew wrapper — used by the Darwin `prereqs` phase.

Only called from Darwin phases. The caller is already OS-gated, so no
platform check here.
"""

from __future__ import annotations

import logging
import os
import shutil

from bootstrap.lib import sh

_log = logging.getLogger(__name__)

_INSTALL_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"


def installed() -> bool:
    """Return True if `brew` is on PATH or present at the canonical locations."""
    if shutil.which("brew") is not None:
        return True
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.path.exists(candidate):
            return True
    return False


def install_script(*, dry_run: bool = False) -> None:
    """Run the Homebrew installer non-interactively.

    Split into two explicit subprocess calls so the shell-boundary rule
    holds literally: (1) curl fetches the installer script, capturing
    stdout through `sh.run`; (2) bash reads that stdout from stdin via
    `input_text=`, with `NONINTERACTIVE=1` in its env.

    No `bash -c <string>` — no command substitution, no shell expansion
    on interpolated Python values. Requires `sh.prime_sudo()` to have
    been called earlier in the phase, since the installer invokes `sudo`
    internally for `/opt/homebrew` creation.
    """
    if installed():
        _log.info("Homebrew already installed")
        return
    _log.info("fetching Homebrew installer script")
    installer = sh.run(
        ["curl", "-fsSL", _INSTALL_URL],
        dry_run=dry_run,
        destructive=True,
    )
    if dry_run:
        _log.info(
            "[dry-run] would pipe %s into bash with NONINTERACTIVE=1",
            _INSTALL_URL,
        )
        return
    _log.info("running Homebrew installer (non-interactive)")
    env = {**os.environ, "NONINTERACTIVE": "1"}
    sh.run(
        ["bash"],
        env=env,
        input_text=installer.stdout,
        dry_run=dry_run,
        destructive=True,
    )


def install_cask(name: str, *, dry_run: bool = False) -> None:
    """Install a cask if not already present."""
    check = sh.run(["brew", "list", "--cask", name], check=False, destructive=False)
    if check.ok():
        _log.debug("cask %s already installed", name)
        return
    _log.info("installing cask: %s", name)
    sh.run(["brew", "install", "--cask", name], dry_run=dry_run, destructive=True)


def install_formula(name: str, *, dry_run: bool = False) -> None:
    """Install a formula if not already present."""
    check = sh.run(["brew", "list", "--formula", name], check=False, destructive=False)
    if check.ok():
        _log.debug("formula %s already installed", name)
        return
    _log.info("installing formula: %s", name)
    sh.run(["brew", "install", name], dry_run=dry_run, destructive=True)
