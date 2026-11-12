# The LUKS-encrypted USB installer system. bootstrap-create-usb partitions
# the stick (GPT: usb-esp + usb-root/LUKS — manual sgdisk/cryptsetup, because
# disko's dm-crypt handling causes I/O errors on USB drives) and
# nixos-installs this config onto it. Booted on a target machine, the
# operator runs `bootstrap-flash <hostname>` from the getty prompt (or over
# ssh) — nothing autoruns.
#
# A function, not a bare module: the nixosTest import path can't see flake
# `self`, so the bootstrap CLI package (or the e2e's mock-wrapped variant)
# and the flake source are passed in explicitly.
{
  bootstrap,
  flakeSource ? null,
  firmwareRom ? null,
}:
{ pkgs, lib, ... }:
{
  # The bootstrap flake source, baked into the media: `bootstrap-flash` reads
  # secrets and config from this copy, so flashing needs no network (see
  # lib/flake_splice.py's flake_ref resolution).
  environment.etc."bootstrap-flake" = lib.mkIf (flakeSource != null) {
    source = flakeSource;
  };

  # The Dasharo coreboot image `bootstrap-firmware` writes, baked in so the
  # conversion is offline and byte-pinned. Fetched at build time from the
  # publisher, verified against the sha256 they publish beside it.
  environment.etc."protectli-firmware/vp46xx.rom" = lib.mkIf (firmwareRom != null) {
    source = firmwareRom;
  };

  # flashrom's internal programmer needs raw access to the chipset's SPI
  # controller, and a stock NixOS kernel denies it twice over.
  #
  # `iomem=relaxed` answers CONFIG_IO_STRICT_DEVMEM, which otherwise "prevents
  # even the root user from accessing hardware from user-space if the resource
  # is unknown to the kernel or a conflicting kernel driver reserved it"
  # (flashrom's FAQ; nixpkgs sets STRICT_DEVMEM and IO_STRICT_DEVMEM in its
  # kernel common-config).
  #
  # The blacklist answers the second: flashrom's manpage records that on Intel
  # the kernel's spi-intel driver claims the PCH flash controller, after which
  # flashrom silently falls back to a restricted MTD path instead of talking to
  # the chipset. lpc_ich is the other conflicting driver the FAQ names. This
  # affects the installer only — the flashed host keeps lpc_ich, which backs its
  # iTCO watchdog.
  boot.kernelParams = [ "iomem=relaxed" ];
  boot.blacklistedKernelModules = [
    "spi_intel_pci"
    "spi_intel"
    "lpc_ich"
  ];

  # Partition labels written by bootstrap-create-usb.
  boot.initrd.luks.devices.usb-crypt.device = "/dev/disk/by-partlabel/usb-root";

  fileSystems."/" = {
    device = "/dev/mapper/usb-crypt";
    fsType = "ext4";
  };

  fileSystems."/boot" = {
    device = "/dev/disk/by-partlabel/usb-esp";
    fsType = "vfat";
    options = [ "umask=0077" ];
  };

  # GRUB with efiInstallAsRemovable: bootable on any machine via the
  # \EFI\BOOT\BOOTX64.EFI fallback path, no NVRAM writes.
  boot.loader.grub = {
    enable = true;
    efiSupport = true;
    efiInstallAsRemovable = true;
    device = "nodev";
  };
  boot.loader.efi.canTouchEfiVariables = false;

  # USB storage + LUKS in the initrd so the stick can find its own root.
  boot.initrd.availableKernelModules = [
    "usb_storage"
    "uas"
    "sd_mod"
    "xhci_pci"
    "ehci_pci"
    "ohci_pci"
    "ahci"
    "nvme"
    "dm_mod"
    "dm_crypt"
  ];

  # WiFi firmware (Intel, Atheros, Realtek, …).
  hardware.enableRedistributableFirmware = true;

  networking = {
    hostName = "bootstrap-installer";
    useDHCP = true;
    # bootstrap-create-usb bakes the operator's WiFi credentials (when set in
    # the repo's sops secrets) to /etc/wpa_supplicant/imperative.conf on the
    # LUKS side — exactly the file allowAuxiliaryImperativeNetworks reads.
    wireless = {
      enable = true;
      userControlled = true;
      allowAuxiliaryImperativeNetworks = true;
    };
  };

  environment.systemPackages = with pkgs; [
    bootstrap
    # bootstrap-firmware drives these two: flashrom rewrites the SPI chip,
    # dmidecode identifies the board and reads the installed BIOS version.
    flashrom
    dmidecode
    cryptsetup
    curl
    git
    gptfdisk
    iw
    parted
    pciutils
    util-linux
    vim
  ];

  services.getty.autologinUser = "root";

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "prohibit-password";
    };
  };
  users.users.root.openssh.authorizedKeys.keys = (import ./fleet-ssh-keys.nix).keys;

  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  environment.etc."motd".text = ''

    ===================================================
      bootstrap USB installer (LUKS-encrypted)
    ===================================================

    1. Firmware (Protectli only). Reports first; writes
       the SPI chip only with --apply. Disable BIOS Lock
       and Secure Boot in firmware setup beforehand:

         bootstrap-firmware
         bootstrap-firmware --apply

    2. Install NixOS (host must already exist in the
       dotfiles host_registry.toml — `nix run .#add-host`).
       --disk is only needed when more than one
       non-removable disk is present:

         lsblk -d -o NAME,SIZE,RM
         bootstrap-flash <hostname> [--disk /dev/<name>]

    3. Done. After the reboot the host runs the bootstrap
       itself (bootstrap-firstboot.service) and joins
       the fleet; nothing to type. To watch, ssh in as
       jacob with a fleet key and:

         journalctl -fu bootstrap-firstboot

    WiFi comes up automatically when credentials were
    baked at media-creation time; otherwise plug in
    ethernet.

    ===================================================

  '';

  system.stateVersion = "24.11";
}
