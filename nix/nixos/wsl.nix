# Phase 0 NixOS WSL override — composed on top of ./default.nix.
#
# Defaults are the bare-metal case. This module:
#   1. Enables NixOS-WSL
#   2. Reroutes git's ssh so it uses the Windows OpenSSH binary (and thus the
#      Windows ssh-agent), avoiding a second credential store inside WSL
#   3. Forces off the bare-metal-only networking bits that don't apply inside WSL
{ lib, ... }:
{
  wsl = {
    enable = true;
    defaultUser = "jacob";
  };

  # Use the Windows SSH binary so WSL shares the host ssh-agent. Anything
  # launched via `git` inside WSL authenticates to GitHub using the Windows
  # side's keys.
  programs.git.enable = true;
  programs.git.config = {
    core.sshCommand = "/mnt/c/Windows/System32/OpenSSH/ssh.exe -o StrictHostKeyChecking=accept-new";
  };

  # ── Disable bare-metal-only options that don't apply inside WSL ─────
  services.getty.autologinUser = lib.mkForce null;
  networking.useDHCP = lib.mkForce false;
  networking.wireless.enable = lib.mkForce false;
}
