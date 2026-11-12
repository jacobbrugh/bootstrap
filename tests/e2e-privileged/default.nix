# e2e-privileged — the privileged tier, end to end, through the USB flash flow:
#   bootstrap-create-usb → virtual USB (mount-and-inspect)
#   bootstrap-flash usb-e2e-tang   → tang-tpm2 target disk (mount-and-inspect)
#   bootstrap-flash usb-e2e-initrd → initrd-key target disk, then booted
#   boot the flashed disk as a second UEFI VM (OVMF; the boot-the-artifact
#   pattern from nixos-anywhere's create_test_machine)
#   let its first-boot unit run the production bootstrap, unattended
#
# What the tier means here: the media carries an ephemeral identity standing
# in for the privileged bootstrap key (staged by `bootstrap-e2e-stage
# privileged`; CI never holds the real one), the booted host is untagged, and
# the test asserts the ssh phase uploaded exactly one key to the jacobbrugh
# account with the privileged token, that the harvest is authored as
# jacobbrugh, and that the host joined every dotfiles sops rule the privileged
# anchor is in — the logscrub literals included, which the switched system
# must be able to decrypt.
#
# The single declared node doubles as the create-usb admin host AND the
# booted installer (it imports installer-usb.nix, so bootstrap-flash and
# the installer tooling are exactly what production media carries). The
# virtual USB is never itself booted — its content is asserted by
# mount-and-inspect; boot-the-artifact happens for the flashed TARGET disk,
# where the payoff (bootloader, unattended unlock, secret-zero activation,
# full bootstrap) lives.
{
  pkgs,
  sops-nix,
}:

let
  mockGh = pkgs.callPackage ../e2e-lib/mock-gh.nix { };

  mkTest =
    { bootstrap, src }:
    let
      bootstrapForTest = pkgs.callPackage ../e2e-lib/bootstrap-for-test.nix {
        inherit bootstrap mockGh;
      };
    in
    pkgs.testers.nixosTest {
      name = "bootstrap-e2e-privileged";
      # The driver's default is one hour. The run measured 61 minutes before the
      # Tang-down leg finished its re-flash (bootstrap/main #45): create-usb
      # twice, three flashes, three target boots, and the Tang-down boot alone
      # waits out the initrd unlock unit's 10-minute retry window.
      globalTimeout = 2 * 60 * 60;

      nodes.installer =
        { lib, modulesPath, ... }:
        {
          imports = [
            (import ../../nix/nixos/installer-usb.nix { bootstrap = bootstrapForTest; })
          ];

          # The qemu-vm harness owns the test VM's root; the installer
          # system's LUKS-by-partlabel root would make the initrd wait for
          # a device that doesn't exist until create-usb has run.
          boot.initrd.luks.devices = lib.mkForce { };
          fileSystems = lib.mkForce {
            "/" = {
              device = "/dev/vda";
              fsType = "ext4";
            };
          };
          boot.loader.grub.enable = lib.mkForce false;

          virtualisation.diskSize = 65536;
          virtualisation.memorySize = 16384;
          virtualisation.cores = 8;
          # The host store over 9p with a writable overlay on the root disk
          # (writableStoreUseTmpfs = false) — NOT an erofs store image. This
          # node's create-usb/flash address disks by fixed /dev/vd* names, and
          # useNixStoreImage inserts the store as /dev/vdb, shifting the empty
          # disks to vdc/vdd/vde. The guests substitute the heavy paths from
          # the staged /mnt/cache instead, so the 9p store is only lightly hit.
          virtualisation.writableStoreUseTmpfs = false;
          # empty0 = the virtual USB stick (/dev/vdb), empty1 = the tang-tpm2
          # target (/dev/vdc), empty2 = the initrd-key target (/dev/vdd) that
          # gets booted, empty3 = the fake dotfiles origin (/dev/vde), a disk
          # of its own so that a re-flash of the target cannot destroy the
          # repository it registered into. The test script names each one.
          virtualisation.emptyDiskImages = [
            16384
            61440
            61440
            2048
          ];

          # Static public resolvers — the SLIRP DNS proxy forwards to the CI
          # daemon host's resolver, which doesn't answer guests (see
          # tests/e2e-sandbox/default.nix).
          networking.resolvconf.enable = false;
          environment.etc."resolv.conf".text = ''
            nameserver 1.1.1.1
            nameserver 8.8.8.8
          '';

          environment.systemPackages = [
            pkgs.sops
            pkgs.age
            pkgs.jq
            pkgs.nixos-install-tools
          ];

          nix.settings = {
            experimental-features = [
              "nix-command"
              "flakes"
            ];
            extra-substituters = [
              "file:///mnt/cache?priority=5"
              "https://cache.jacobbrugh.net/attic?priority=10"
            ];
            extra-trusted-public-keys = [ "attic:TGN7u8ffZ1H01LvNYlpV4FgyRYRpaoG9CxtSHhOYRgY=" ];
            require-sigs = false;
            netrc-file = "/root/nix-netrc";
            max-jobs = 2;
            cores = 8;
          };

          virtualisation.sharedDirectories = {
            e2e = {
              source = "/tmp/bootstrap-e2e-shared/privileged";
              target = "/mnt/shared";
            };
            cache = {
              source = "/tmp/bootstrap-e2e-shared/cache";
              target = "/mnt/cache";
            };
          };
        };

      # SOURCE ties the test derivation to the flake source the VMs consume
      # at run time (see flake.nix, the e2e packages).
      testScript = ''
        OVMF_CODE = "${pkgs.OVMF.firmware}"
        OVMF_VARS = "${pkgs.OVMF.variables}"
        QEMU_KVM = "${pkgs.qemu_test}/bin/qemu-kvm"
        TANGD = "${pkgs.tang}/libexec/tangd"
        SOURCE = "${src}"
      ''
      + builtins.readFile ../e2e-lib/sops_rules.py
      + builtins.readFile ../e2e-lib/health.py
      + builtins.readFile ./test_script.py;

      skipTypeCheck = true;
    };
in
{
  inherit mkTest mockGh;
}
