# Phase 0 Darwin minimal config.
#
# Activated transiently from `phases/darwin/onepassword.py` via
# `nix run nix-darwin -- switch --flake <bootstrap>#bootstrap`. Sole
# purpose: sops-nix at activation reads the age key the onepassword
# phase just staged at `/var/lib/nixos-bootstrap/age-key`, decrypts
# `secrets/bootstrap-secrets.sops.yaml`, and writes plaintext to
# `/run/secrets/bootstrap-github-token`. The bootstrap CLI reads that
# plaintext for the `ssh` + `register` phases — no Python decryption.
#
# Replaced by the user's dotfiles darwinConfiguration at the `switch`
# phase. The `post` phase shreds `/var/lib/nixos-bootstrap/age-key` so
# it doesn't persist on the installed system — mirrors the NixOS
# `phase0-firstboot` systemd service's `shred -u` step.
{ ... }:
{
  sops.defaultSopsFile = ../../secrets/bootstrap-secrets.sops.yaml;
  sops.age.keyFile = "/var/lib/nixos-bootstrap/age-key";
  sops.secrets.bootstrap-github-token = {
    key = "github_token";
    path = "/run/secrets/bootstrap-github-token";
    mode = "0440";
    owner = "root";
    group = "admin";
  };

  system.stateVersion = 5;
}
