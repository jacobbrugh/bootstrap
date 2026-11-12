{
  lib,
  pkgs,
  czData,
  ...
}:
{
  system.primaryUser = czData.username;

  scoop = {
    enable = true;
    buckets = {
      main = "https://github.com/ScoopInstaller/Main";
    };
    packages = {
      git.bucket = "main";
      "1password-cli".bucket = "main";
      age.bucket = "main";
    };
  };

  dsc = {
    enable = true;

    extraResources = [
      {
        name = "OpenSSH Client Capability";
        type = "Microsoft.Windows/WindowsPowerShell";
        properties.resources = [
          {
            name = "OpenSSH Client Inner";
            type = "PSDesiredStateConfiguration/Script";
            properties = {
              GetScript = "return @{ Result = (Get-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0').State }";
              TestScript = "(Get-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0').State -eq 'Installed'";
              SetScript = "Add-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0'";
            };
          }
        ];
      }
      {
        name = "OpenSSH Server Capability";
        type = "Microsoft.Windows/WindowsPowerShell";
        properties.resources = [
          {
            name = "OpenSSH Server Inner";
            type = "PSDesiredStateConfiguration/Script";
            properties = {
              GetScript = "return @{ Result = (Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0').State }";
              TestScript = "(Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0').State -eq 'Installed'";
              SetScript = "Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'";
            };
          }
        ];
      }
    ];
  };

  # Two files under %ProgramData%\ssh; the same sshd settings the fleet's
  # Windows hosts run once dotfiles takes over.
  programs.openssh = {
    authorizedKeys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIASYCo8dKnRJ0Gc01yKMWRm4Afw7nXNASVtd5g8XV+vW"
    ];
    extraConfig = ''
      AuthorizedKeysFile      .ssh/authorized_keys
      PasswordAuthentication no
      Subsystem       sftp    sftp-server.exe

      Match Group administrators
            AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
    '';
  };
}
