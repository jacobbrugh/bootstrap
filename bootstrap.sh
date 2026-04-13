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

# Source the Nix profile script directly, whether or not the surrounding
# shell has the /etc/zshrc hook. This covers two re-run cases:
#   1. Nix was installed by a previous run but this new shell hasn't
#      re-read /etc/zshrc yet
#   2. `prereqs` moved /etc/zshrc aside and the nix-daemon hook is gone
# Without this, `command -v nix` below would return false and re-trigger
# the installer, which then fails trying to re-create an existing APFS
# volume.
_source_nix_profile() {
  if [[ -e /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]]; then
    # shellcheck disable=SC1091
    . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
  elif [[ -e "$HOME/.nix-profile/etc/profile.d/nix.sh" ]]; then
    # shellcheck disable=SC1091
    . "$HOME/.nix-profile/etc/profile.d/nix.sh"
  fi
}

_source_nix_profile

if ! command -v nix &>/dev/null; then
  _log "Nix not found — installing..."
  curl --proto '=https' --tlsv1.2 -sSfL "$NIX_INSTALLER" | sh -s -- --daemon
  _source_nix_profile
fi

_log "Running bootstrap flake: $BOOTSTRAP_FLAKE"
# --refresh forces Nix to fetch the latest commit on main instead of
# returning a cached eval (default TTL: 1 hour). Without it, fixes
# pushed in the last hour wouldn't reach a user re-running this wrapper
# — which is exactly the recovery path after a mid-bootstrap failure.
exec nix run \
  --refresh \
  --extra-experimental-features "nix-command flakes" \
  "$BOOTSTRAP_FLAKE" -- "$@"
