{ lib, ... }:
{
  wsl = {
    enable = true;
    defaultUser = "jacob";
  };

  programs.git.enable = true;
  programs.git.config = {
    core.sshCommand = "/mnt/c/Windows/System32/OpenSSH/ssh.exe -o StrictHostKeyChecking=accept-new";
  };

  services.getty.autologinUser = lib.mkForce null;
  networking.useDHCP = lib.mkForce false;
  networking.wireless.enable = lib.mkForce false;
}
