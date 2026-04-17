# The NixOS module the fixture dotfiles' flake.nix evaluates to for the
# e2e test VM's post-switch system. Serves two roles:
#
#   1. **Build-time input** to fixture.nix, which evaluates it through
#      `lib.nixosSystem` and pre-builds the resulting toplevel store
#      path. That path is added to the VM's /nix/store via
#      `virtualisation.additionalPaths`, so `nixos-rebuild switch` has
#      the toplevel locally — no network, no slow build.
#
#   2. **Runtime target** of the fixture's generated `flake.nix`, which
#      points at this same module file. `nixos-rebuild switch` inside
#      the VM re-evaluates it against the pinned nixpkgs and lands on
#      the same store path from step 1 (identical inputs → identical
#      hash), so the switch is a fast activation.
#
# The config keeps only what the test needs post-switch:
#   - `jacob` user (uid 1000) so `su - jacob` still works for assertions.
#   - A filesystem layout matching the nixosTest VM
#     (/dev/disk/by-label/nixos, ext4) so switch-to-configuration's fstab
#     activation doesn't fight running mounts.
#   - `environment.etc."bootstrap-e2e-marker"` — the success proof: the
#     testScript reads it to confirm `nixos-rebuild switch` ran and the
#     fixture's generated config activated.
#   - `nix.settings.experimental-features` with flakes, since the switch
#     phase's `nixos-rebuild` needs them to read the fixture's flake.nix.
#
# Deliberately sparse: no systemd services, no fancy modules. A bigger
# config would (a) make the pre-built closure larger (longer fixture
# build) and (b) risk activation errors that have nothing to do with the
# bootstrap flow we're testing.

{ pkgs, ... }:
{
  system.stateVersion = "25.11";

  # Match the nixosTest VM's hostname (which is "machine" by default for
  # a single-node test, not the BOOTSTRAP_HOSTNAME we pass to register).
  # nixos-rebuild picks `nixosConfigurations.$(hostname)` by default; the
  # flake.nix the fixture generates exposes the config under this name.
  networking.hostName = "machine";

  users.users.jacob = {
    isNormalUser = true;
    uid = 1000;
    extraGroups = [ "wheel" ];
    home = "/home/jacob";
    password = "";
  };
  security.sudo.wheelNeedsPassword = false;

  # The nixosTest VM boots off an ext4 image labeled `nixos`. The
  # post-switch config must declare the same root filesystem or
  # switch-to-configuration regenerates /etc/fstab to reference a
  # device the kernel isn't actually mounting, which breaks boot.
  #
  # The 9p + overlay mounts under /nix/store are added automatically by
  # `virtualisation.qemu-vm`-style config — but we're not importing
  # that module. Declaring only `/` keeps fstab minimal; the running
  # /nix/.ro-store + overlay stay mounted because switch-to-configuration
  # doesn't unmount filesystems that aren't in the new fstab, it just
  # updates /etc/fstab for the next boot. For the single activation in
  # this test, that's fine.
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };
  boot.loader.grub.enable = false;
  boot.loader.systemd-boot.enable = false;

  # Test marker — the whole point of the post-switch verification. The
  # testScript greps /etc/bootstrap-e2e-marker after the full bootstrap
  # finishes; its presence (and content) proves that nixos-rebuild
  # switch ran AND successfully activated this config.
  environment.etc."bootstrap-e2e-marker".text = "bootstrapped";

  # nixos-rebuild switch reads /etc/nixos/flake.nix when evaluating.
  # Without flakes experimental-features enabled, `nix build` refuses
  # the flake. Ensure both nix-command and flakes are on by default.
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  # Tools needed by testScript assertions that run post-switch (sops
  # decrypt roundtrip, git inspection of the bare origin).
  environment.systemPackages = with pkgs; [
    git
    sops
    age
  ];
}
