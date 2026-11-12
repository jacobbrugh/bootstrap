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
    openssh.authorizedKeys.keys = (import ./fleet-ssh-keys.nix).keys;
  };

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

  i18n.defaultLocale = "en_US.UTF-8";

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  sops.defaultSopsFile = ../../secrets.yaml;
  sops.age.keyFile = "/var/lib/bootstrap/age-key";
  sops.secrets.bootstrap-github-token = {
    key = "github_token";
    mode = "0440";
    owner = "root";
    group = "wheel";
  };

  system.stateVersion = "24.11";

  services.getty.autologinUser = "jacob";

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "prohibit-password";
    };
  };

  networking.useDHCP = true;
}
