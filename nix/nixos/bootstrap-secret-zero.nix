{ ... }:
{
  imports = [
    ./bootstrap-secret-zero-minimal.nix
    ../../host-hardware.nix
    (if builtins.pathExists ../../host-networking.nix then ../../host-networking.nix else { })
  ];
}
