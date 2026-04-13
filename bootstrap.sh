#!/usr/bin/env bash
# Convenience wrapper: install Nix (if needed) then run the bootstrap flake.
#
# Usage:
#   curl -fsSL https://jacobbrugh.net/bootstrap.sh | bash
#
# Or if Nix is already installed, run the flake directly:
#   nix run github:jacobbrugh/bootstrap

set -euo pipefail

BOOTSTRAP_FLAKE="${BOOTSTRAP_FLAKE:-github:jacobbrugh/bootstrap}"
NIX_INSTALLER="https://nixos.org/nix/install"

_log() { printf '\033[0;32m[INFO]\033[0m %s\n' "$*"; }

if ! command -v nix &>/dev/null; then
  _log "Nix not found — installing..."
  curl --proto '=https' --tlsv1.2 -sSfL "$NIX_INSTALLER" | sh -s -- --daemon

  # Source nix into this shell
  if [[ -e /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]]; then
    # shellcheck disable=SC1091
    . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
  elif [[ -e "$HOME/.nix-profile/etc/profile.d/nix.sh" ]]; then
    # shellcheck disable=SC1091
    . "$HOME/.nix-profile/etc/profile.d/nix.sh"
  fi
fi

_log "Running bootstrap flake: $BOOTSTRAP_FLAKE"
exec nix run \
  --extra-experimental-features "nix-command flakes" \
  "$BOOTSTRAP_FLAKE" -- "$@"
