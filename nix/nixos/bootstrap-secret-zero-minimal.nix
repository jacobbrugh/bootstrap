{
  pkgs,
  lib,
  ...
}:
{
  users.users.jacob = {
    isNormalUser = true;
    home = "/home/jacob";
    shell = pkgs.zsh;
    extraGroups = [ "wheel" ];
    initialPassword = "";
    # The fleet keys, so a flashed host is reachable over ssh from its first
    # boot: sshd refuses the empty console password (PermitEmptyPasswords
    # defaults to no), and a first-boot bootstrap that fails is otherwise
    # debuggable only at the physical console.
    openssh.authorizedKeys.keys = (import ./fleet-ssh-keys.nix).keys;
  };

  security.sudo.wheelNeedsPassword = false;
  programs.zsh.enable = true;

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "prohibit-password";
    };
  };

  networking.useDHCP = lib.mkDefault true;

  # WiFi for freshly flashed hosts: bootstrap-flash copies the baked WiFi
  # credentials (when present on the installer media) to
  # /etc/wpa_supplicant/imperative.conf on the target — the file
  # allowAuxiliaryImperativeNetworks reads. Hosts without wifi hardware just
  # run an idle wpa_supplicant.
  networking.wireless = {
    enable = lib.mkDefault true;
    userControlled = true;
    allowAuxiliaryImperativeNetworks = true;
  };

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  # The bot token every tier reads (secrets.yaml). The privileged token in
  # secrets-privileged.yaml is deliberately NOT declared here: sops-nix would
  # fail activation on a sandbox-keyed scaffold that cannot decrypt it, and
  # it must never sit in /run/secrets — the ssh phase decrypts it into memory
  # only (lib/secrets.py).
  sops.defaultSopsFile = ../../secrets.yaml;
  sops.age.keyFile = "/var/lib/bootstrap/age-key";
  sops.secrets.bootstrap-github-token = {
    key = "github_token";
    mode = "0440";
    owner = "root";
    group = "wheel";
  };

  system.stateVersion = "24.11";
}
