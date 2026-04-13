# Minimal Windows bootstrap config.
# Gets a fresh Windows machine to a state where the full dotfiles
# winConfigurations.pc<N> switch can run.
#
# Evaluated inside WSL via:
#   nix-win switch --flake github:jacobbrugh/bootstrap#bootstrap
{
  lib,
  pkgs,
  czData,
  ...
}:
{
  win.user.name = czData.username;

  # Bootstrap-critical tools — full package set comes from dotfiles#pc<N>
  win.scoop = {
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

  win.dsc = {
    enable = true;

    # Install OpenSSH Client + Server Windows capabilities
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

    # TODO (windows-migration): nix-win renamed `win.dsc.services.<name>` to
    # the generated `win.dsc.psdsc.service.<name>` submodule with DSC's
    # capital-cased property names (`StartupType`, `State`, …). Rewire
    # sshd + ssh-agent startup against the new schema as part of the
    # Windows migration task. Commented out for now so the public flake
    # evaluates cleanly.
    #
    # services = {
    #   sshd = { state = "Running"; startupType = "Automatic"; };
    #   ssh-agent = { state = "Running"; startupType = "Automatic"; };
    # };

    # Minimal sshd config + authorized key for inbound access
    ssh = {
      authorizedKeys = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIASYCo8dKnRJ0Gc01yKMWRm4Afw7nXNASVtd5g8XV+vW"
      ];
      sshdConfig = ''
        AuthorizedKeysFile      .ssh/authorized_keys
        PasswordAuthentication no
        Subsystem       sftp    sftp-server.exe

        Match Group administrators
              AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
      '';
    };
  };
}
