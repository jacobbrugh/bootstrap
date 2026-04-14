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

_CANONICAL_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _find_brew_bin_dir() -> str | None:
    for candidate in _CANONICAL_BIN_DIRS:
        if os.path.exists(os.path.join(candidate, "brew")):
            return candidate
    return None


def ensure_on_path() -> None:
    """Prepend Homebrew's bin dir to `os.environ['PATH']` if missing.

    Homebrew's installer does not modify the current shell's PATH,
    `~/.zprofile`, or the parent Python process's environment. Without
    this injection, subsequent `sh.run(["brew", ...])` calls hit
    `FileNotFoundError` even though brew is installed on disk. Idempotent;
    safe to call repeatedly.
    """
    brew_bin_dir = _find_brew_bin_dir()
    if brew_bin_dir is None:
        return
    existing = os.environ.get("PATH", "")
    if brew_bin_dir in existing.split(os.pathsep):
        return
    os.environ["PATH"] = f"{brew_bin_dir}{os.pathsep}{existing}" if existing else brew_bin_dir


def installed() -> bool:
    """Return True if `brew` is on PATH or present at the canonical locations."""
    if shutil.which("brew") is not None:
        return True
    return _find_brew_bin_dir() is not None


async def install_script(*, dry_run: bool = False) -> None:
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
        ensure_on_path()
        return
    _log.info("fetching Homebrew installer script")
    installer = await sh.run(
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
    await sh.run(
        ["bash"],
        env=env,
        input_text=installer.stdout,
        dry_run=dry_run,
        destructive=True,
    )
    ensure_on_path()


async def install_cask(name: str, *, dry_run: bool = False) -> None:
    """Install a cask if not already present."""
    ensure_on_path()
    check = await sh.run(["brew", "list", "--cask", name], check=False, destructive=False)
    if check.ok():
        _log.debug("cask %s already installed", name)
        return
    _log.info("installing cask: %s", name)
    await sh.run(["brew", "install", "--cask", name], dry_run=dry_run, destructive=True)


async def install_formula(name: str, *, dry_run: bool = False) -> None:
    """Install a formula if not already present."""
    ensure_on_path()
    check = await sh.run(["brew", "list", "--formula", name], check=False, destructive=False)
    if check.ok():
        _log.debug("formula %s already installed", name)
        return
    _log.info("installing formula: %s", name)
    await sh.run(["brew", "install", name], dry_run=dry_run, destructive=True)
