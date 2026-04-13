"""Filesystem path constants.

Every path in the bootstrap comes from this module. No literal strings in
phase code, no hardcoded `/Users/...` or `/home/...` paths — everything is
derived from `Path.home()` + environment variables so the same code works
on Darwin, Linux, NixOS, and inside WSL (where `~` expands differently).
"""

from __future__ import annotations

import os
from pathlib import Path

HOME: Path = Path.home()

# ── Canonical dotfiles checkout ─────────────────────────────────────────
# Symlinks at /etc/nix-darwin/flake.nix, /etc/nixos/flake.nix, and
# $XDG_CONFIG_HOME/home-manager/flake.nix all resolve to this directory.
CANONICAL_DOTFILES: Path = HOME / "repos" / "jacobbrugh" / "nix-config" / "nix-config"

# Remote to clone from. Renaming the repo to `nix-config` happens separately
# from bootstrap work — this stays `dotfiles` until the user renames it.
DOTFILES_GIT_REMOTE: str = "git@github.com:jacobpbrugh/dotfiles.git"

# ── XDG base directories ────────────────────────────────────────────────
XDG_CONFIG_HOME: Path = Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config")))
XDG_STATE_HOME: Path = Path(os.environ.get("XDG_STATE_HOME", str(HOME / ".local" / "state")))

# ── Bootstrap state ─────────────────────────────────────────────────────
STATE_DIR: Path = XDG_STATE_HOME / "bootstrap"
PHASE_STATE_DIR: Path = STATE_DIR / "phases"

# ── sops / age ──────────────────────────────────────────────────────────
SOPS_AGE_DIR: Path = XDG_CONFIG_HOME / "sops" / "age"
SOPS_AGE_KEY_FILE: Path = SOPS_AGE_DIR / "keys.txt"

# ── SSH ─────────────────────────────────────────────────────────────────
SSH_DIR: Path = HOME / ".ssh"
SSH_CONFIG: Path = SSH_DIR / "config"
SSH_KNOWN_HOSTS: Path = SSH_DIR / "known_hosts"

# ── Default flake symlink targets per-platform ──────────────────────────
DARWIN_FLAKE_SYMLINK: Path = Path("/etc/nix-darwin/flake.nix")
NIXOS_FLAKE_SYMLINK: Path = Path("/etc/nixos/flake.nix")
HM_FLAKE_SYMLINK: Path = XDG_CONFIG_HOME / "home-manager" / "flake.nix"


def ssh_key_path(hostname: str) -> Path:
    """Private ed25519 key file for a given host."""
    return SSH_DIR / f"id_ed25519_{hostname}"
