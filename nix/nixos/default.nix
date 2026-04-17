# Phase 0 NixOS default module — the "bare-metal" path.
#
# This is the base config for `nixosConfigurations.bootstrap`. The WSL variant
# composes this + ./wsl.nix, where wsl.nix overrides the handful of options
# that don't apply inside WSL (wireless, DHCP, getty autologin).
#
# hardware-configuration.nix / host-networking.nix are composed at the
# flake.nix level, not here — they're per-host and orthogonal to bare-metal
# vs WSL.
{ pkgs, ... }:
{
  users.users.jacob = {
    isNormalUser = true;
    home = "/home/jacob";
    shell = pkgs.zsh;
    extraGroups = [
      "wheel"
      "docker"
      "systemd-journal"
    ];
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIASYCo8dKnRJ0Gc01yKMWRm4Afw7nXNASVtd5g8XV+vW"
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDibOr+tge8OHe8sDZ+Hlhn83vN1P4Xcat1f4MJuYytM"
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILWh7JO+95XPqc41Up5gBCRkENckIj5/6zIIXZTobTzV"
    ];
  };

  # Phase 0 is a 5-minute transient install that exists only long enough
  # for the dotfiles `nixos-rebuild switch` to take over with real security
  # policy. LUKS protects data-at-rest; the autologin-without-password path
  # below assumes the installing user already has physical access to the
  # machine. Both this and `services.openssh.PermitRootLogin` below are
  # overridden by the full dotfiles configuration immediately after first
  # boot — do NOT "harden" them here.
  security.sudo.wheelNeedsPassword = false;
  programs.zsh.enable = true;

  environment.systemPackages = with pkgs; [
    age
    curl
    delta
    gh
    git
    vim
  ];

  # time.timeZone is intentionally NOT set at Phase 0 — the real timezone is
  # configured by the full dotfiles `nixos-rebuild switch` that happens a few
  # minutes later. UTC (NixOS default) is fine for the interim, and omitting
  # it here avoids leaking geolocation.
  i18n.defaultLocale = "en_US.UTF-8";

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  system.stateVersion = "24.11";

  # ── Bare-metal specifics (overridden by ./wsl.nix for the WSL variant) ─

  # Console autologin — disk is LUKS-encrypted so no password needed.
  services.getty.autologinUser = "jacob";

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "prohibit-password";
    };
  };

  networking.useDHCP = true;
  networking.wireless = {
    enable = true;
    userControlled = true;
    allowAuxiliaryImperativeNetworks = true;
  };

  services.tailscale.enable = true;

  # First-boot: install baked WiFi creds if present, then delete them.
  systemd.services.wifi-firstboot = {
    description = "First-boot WiFi configuration";
    before = [ "wpa_supplicant.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      WIFI_CONF="/var/lib/nixos-bootstrap/wifi.conf"
      [ -f "$WIFI_CONF" ] || exit 0
      mkdir -p /etc/wpa_supplicant
      cp "$WIFI_CONF" /etc/wpa_supplicant/imperative.conf
      chmod 600 /etc/wpa_supplicant/imperative.conf
      rm -f "$WIFI_CONF"
    '';
  };

  # First-boot: set timezone + authenticate Tailscale.
  #
  # The user places two files in /var/lib/nixos-bootstrap/ before running
  # the NixOS installer:
  #   - age-key              → the bootstrap age private key (for sops)
  #   - tailscale-auth-key   → a single-use Tailscale/Headscale preauth key
  #
  # With those present, this script decrypts the top-level
  # `secrets/phase0.yaml` (committed in the public bootstrap repo,
  # encrypted to the bootstrap age pubkey) to get the Headscale login-
  # server URL and the timezone, then runs `timedatectl` + `tailscale
  # up`. On success all three runtime files are shredded so the key
  # material doesn't persist after firstboot.
  systemd.services.phase0-firstboot = {
    description = "Phase 0 firstboot: timezone + Tailscale auth via sops";
    after = [
      "tailscaled.service"
      "network-online.target"
    ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = [
      pkgs.sops
      pkgs.tailscale
      pkgs.systemd
      pkgs.coreutils
    ];
    script = ''
      set -eu
      AGE_KEY_FILE="/var/lib/nixos-bootstrap/age-key"
      AUTH_KEY_FILE="/var/lib/nixos-bootstrap/tailscale-auth-key"
      SOPS_FILE="${../../secrets/phase0.yaml}"

      if [ ! -f "$AGE_KEY_FILE" ] || [ ! -f "$AUTH_KEY_FILE" ]; then
        echo "phase0-firstboot: age-key or tailscale-auth-key missing; skipping"
        exit 0
      fi

      export SOPS_AGE_KEY_FILE="$AGE_KEY_FILE"

      LOGIN_SERVER="$(sops decrypt --extract '["headscale_login_server"]' "$SOPS_FILE")"
      TZ_VALUE="$(sops decrypt --extract '["timezone"]' "$SOPS_FILE")"

      timedatectl set-timezone "$TZ_VALUE" || true

      for i in $(seq 1 30); do
        tailscale status >/dev/null 2>&1 && break
        sleep 1
      done

      if tailscale up \
          --login-server="$LOGIN_SERVER" \
          --auth-key="$(cat "$AUTH_KEY_FILE")"; then
        shred -u "$AUTH_KEY_FILE" "$AGE_KEY_FILE" 2>/dev/null || rm -f "$AUTH_KEY_FILE" "$AGE_KEY_FILE"
      fi
    '';
  };
}
