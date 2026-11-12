from __future__ import annotations

import os
from pathlib import Path

HOME: Path = Path.home()

CANONICAL_DOTFILES: Path = Path(
    os.environ.get(
        "BOOTSTRAP_CANONICAL_DOTFILES",
        str(HOME / ".local" / "share" / "chezmoi"),
    )
)

# The canonical dotfiles remote, by tier. A privileged host reaches it over
# SSH with the key the ssh phase just uploaded to the jacobbrugh account; a
# sandbox host has no key on any account and uses https with the bot token,
# handed to register's own git processes and written nowhere.
# BOOTSTRAP_DOTFILES_REMOTE overrides both (the e2e's file:// clone).
DOTFILES_GIT_REMOTE_SSH: str = "git@github.com:jacobbrugh/dotfiles.git"
DOTFILES_GIT_REMOTE_HTTPS: str = "https://github.com/jacobbrugh/dotfiles.git"
DOTFILES_GIT_REMOTE_OVERRIDE: str | None = os.environ.get("BOOTSTRAP_DOTFILES_REMOTE")

# Repo-relative paths inside the canonical dotfiles checkout. These must track
# the dotfiles repo layout — the e2e runs against a dotfiles checkout, so a
# layout move over there turns it red rather than silently breaking fresh
# bootstraps.
DOTFILES_REGISTRY_REL: Path = Path("host_registry.toml")
DOTFILES_SOPS_YAML_REL: Path = Path(".sops.yaml")
DOTFILES_HOSTS_DIR_REL: Path = Path("config/hosts")

# The Tang servers a Tang-unlocked disk is bound to come from the dotfiles
# registry (lib/registry_toml.tang_urls). This comma-separated override exists
# solely for the usb e2e, which runs Tang inside the test VM.
TANG_URLS_OVERRIDE: tuple[str, ...] = tuple(
    url for url in os.environ.get("BOOTSTRAP_TANG_URLS", "").split(",") if url
)

# Transient bootstrap state on a target machine (created by bootstrap-flash,
# consumed by register, shredded by post — nothing here survives a completed
# bootstrap). Platform-neutral: the same layout serves NixOS and any other
# file-tier consumer. Must stay in lockstep with the sops.age.keyFile paths
# in nix/nixos/*.nix.
BOOTSTRAP_STATE_DIR: Path = Path("/var/lib/bootstrap")
BOOTSTRAP_AGE_KEY: Path = BOOTSTRAP_STATE_DIR / "age-key"

XDG_CONFIG_HOME: Path = Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config")))

SOPS_AGE_DIR: Path = XDG_CONFIG_HOME / "sops" / "age"
SOPS_AGE_KEY_FILE: Path = SOPS_AGE_DIR / "keys.txt"

SSH_DIR: Path = HOME / ".ssh"
SSH_CONFIG: Path = SSH_DIR / "config"
SSH_KEY: Path = SSH_DIR / "id_ed25519"

DARWIN_FLAKE_SYMLINK: Path = Path("/etc/nix-darwin")
NIXOS_FLAKE_SYMLINK: Path = Path("/etc/nixos")
HM_FLAKE_SYMLINK: Path = XDG_CONFIG_HOME / "home-manager"
