"""The boot configuration bootstrap-flash generates for a target.

Every flashed host gets a LUKS root; only the way it opens varies, and that
choice comes from the registry's ``luksUnlock`` field. This module renders that
choice — bootloader plus unlock — exactly once, because the same text is needed
in two places and they must not drift:

* spliced into the flake clone as ``host-hardware.nix``, so the minimal
  secret-zero system flash installs boots unattended on its own; and
* staged at ``/etc/nixos/boot.nix`` on the target, where register's harvest
  picks it up into ``config/hosts/<host>/boot.nix`` and the dotfiles flake
  imports it.

Why the bootloader lives here at all: ``nixos-generate-config`` writes bootloader
settings only into ``configuration.nix``, which nothing imports, so a harvested
``hardware.nix`` declares none. Without a loader stanza ``nixos-install`` runs
NixOS's warn-and-exit-0 no-bootloader stub and the disk does not boot, and the
first dotfiles switch fails the ``boot.loader.grub.devices`` assertion.

Every unattended method unlocks with a random key of its own, held in its own
LUKS keyslot and reachable only through the disk's header, never through a file
beside this config. Flash enrolls it (``flash._enroll_unlock_key``):

* ``tang-tpm2``: ``clevis luks bind`` stores the key Tang-wrapped (plus a TPM2
  pin where the hardware has one) as a ``clevis`` token in the LUKS2 header. The
  initrd unit rendered below runs ``clevis luks unlock`` from that token.
* ``tpm2``: ``systemd-cryptenroll`` seals the key to the TPM as a
  ``systemd-tpm2`` token, which systemd-cryptsetup opens natively.

``tpm2-token`` uses the same systemd token sealed to PCR 7, enrolled only after
the host's own Secure Boot keys are in the firmware, so a freshly flashed host
in that mode boots to the keyslot-0 passphrase prompt until then. ``initrd-key``
carries a copy of the passphrase inside the initramfs. The sops passphrase stays
keyslot 0 in every method, as the console recovery path.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal, get_args

#: dm-crypt mapper name. Matches the disko layout bootstrap-flash formats with
#: and the device name the dotfiles-side config refers to.
LUKS_NAME: Final = "cryptroot"

#: Unlock methods, mirroring the luksUnlock enum in the dotfiles
#: lib/hosts/fromRegistry.nix. The first is that option's default.
LuksUnlock = Literal["tang-tpm2", "tpm2", "tpm2-token", "initrd-key"]
LUKS_UNLOCK_MODES: Final[tuple[str, ...]] = get_args(LuksUnlock)
LUKS_UNLOCK_DEFAULT: Final = "tang-tpm2"

#: Where flash stages the plaintext passphrase for the `initrd-key` method. It
#: sits on the encrypted root; the initramfs copy on the ESP is what the boot
#: actually reads.
INITRD_KEY_PATH: Final = Path("/etc/luks-initrd-key")

#: Path the keyfile takes inside the initramfs.
INITRD_KEY_DEST: Final = "/crypt-key"

# NIC drivers for initrd DHCP, needed only when Tang has to be reachable from
# the initrd — nixos-generate-config never emits network modules. The USB
# Ethernet drivers cover laptops whose only wired port is a dongle (two of the fleet's
# laptops enumerate theirs under cdc_ncm).
_INITRD_NIC_MODULES = (
    '"r8169" "e1000e" "igc" "igb" "virtio_net" "virtio_pci" "usbnet" "cdc_ether" "cdc_ncm" "r8152"'
)

_BOOTLOADER = """  boot.loader.systemd-boot.enable = true;
  boot.loader.grub.enable = false;
  boot.loader.efi.canTouchEfiVariables = true;
"""


def _tang_tpm2(name: str) -> str:
    return f"""
  # Clevis unlock from the binding flash enrolled into this disk's LUKS2 header:
  # a random key in its own keyslot, Tang-wrapped over both home-LAN Tang servers
  # (plus a TPM2 pin where the hardware has one), SSS threshold 1. The unit below
  # opens the volume before systemd-cryptsetup runs, which then finds it active
  # and returns success. It retries for 10 minutes, so a whole-house power cut
  # that brings this machine up before the Tang hosts still unlocks unattended;
  # past that, the console passphrase (keyslot 0) is the fallback.
  #
  # boot.initrd.clevis.enable with no devices supplies the clevis, jose, curl and
  # cryptsetup binaries to the initrd and generates no unit of its own.
  boot.initrd.systemd.enable = true;
  boot.initrd.clevis.enable = true;
  boot.initrd.systemd.network.enable = true;
  boot.initrd.systemd.network.networks."10-dhcp" = {{
    matchConfig.Name = "en*";
    networkConfig.DHCP = "yes";
  }};
  boot.initrd.availableKernelModules = [ {_INITRD_NIC_MODULES} ];
  boot.initrd.systemd.services."clevis-luks-unlock-{name}" = {{
    description = "Unlock {name} from its clevis LUKS2 token";
    wantedBy = [ "systemd-cryptsetup@${{utils.escapeSystemdPath "{name}"}}.service" ];
    before = [
      "systemd-cryptsetup@${{utils.escapeSystemdPath "{name}"}}.service"
      "initrd-switch-root.target"
      "shutdown.target"
    ];
    wants = [ "network-online.target" ];
    after = [
      "network-online.target"
      "systemd-modules-load.service"
      "tpm2.target"
    ];
    conflicts = [
      "initrd-switch-root.target"
      "shutdown.target"
    ];
    unitConfig.DefaultDependencies = false;
    serviceConfig = {{
      Type = "oneshot";
      RemainAfterExit = true;
      TimeoutStartSec = "11min";
    }};
    environment.LUKS_DEVICE = config.boot.initrd.luks.devices."{name}".device;
    script = ''
      for _ in $(seq 120); do
        clevis luks unlock -d "$LUKS_DEVICE" -n {name} && exit 0
        sleep 5
      done
    '';
  }};
"""


def _tpm2(name: str) -> str:
    return f"""
  # TPM2 unlock from the systemd-tpm2 token flash enrolled into this disk's LUKS2
  # header (systemd-cryptenroll, no PCR binding): a random key in its own
  # keyslot, opened natively by systemd-cryptsetup with no network wait. The
  # console passphrase (keyslot 0) remains the recovery path.
  #
  # crypttab field 3 stays "-" (no keyFile): with a key file configured,
  # systemd-cryptsetup treats that file as a sealed blob and never consults the
  # token.
  boot.initrd.systemd.enable = true;
  boot.initrd.luks.devices."{name}".crypttabExtraOpts = [ "tpm2-device=auto" ];
"""


def _initrd_key(name: str) -> str:
    return f"""
  # Unattended unlock with no TPM and no network: a copy of the LUKS passphrase
  # rides inside the initramfs. boot.initrd.secrets appends it when the
  # bootloader installs a generation, and the initrd-nixos-copy-secrets service
  # is ordered before cryptsetup-pre.target, so it is in place before any unlock
  # is attempted.
  #
  # The value is a quoted STRING, not a Nix path literal. append-initrd-secrets
  # copies from that path on the live filesystem at bootloader-install time; an
  # unquoted path would instead copy the passphrase into the world-readable Nix
  # store. systemd-boot sets supportsInitrdSecrets = true, so the string form is
  # both allowed and correct here.
  #
  # This puts the key on the unencrypted ESP beside the ciphertext it opens, so
  # it resists nothing physical. Its value is that the volume is already LUKS:
  # fitting a TPM later is an enrollment plus a new boot.nix, with no reinstall.
  boot.initrd.systemd.enable = true;
  boot.initrd.secrets."{INITRD_KEY_DEST}" = "{INITRD_KEY_PATH}";
  boot.initrd.luks.devices."{name}".keyFile = "{INITRD_KEY_DEST}";
"""


def _tpm2_token(name: str) -> str:
    return f"""
  # TPM2 unlock through a LUKS2 token in the LUKS header, added by
  # systemd-cryptenroll after the host's own Secure Boot keys are enrolled.
  # Nothing is enrolled at flash time, because PCR 7 does not hold its final
  # value until those keys are in the firmware, which is after the first switch.
  #
  # crypttab field 3 stays "-" (no keyFile), which is what lets
  # systemd-cryptsetup reach the token at all: with a key file configured it
  # treats that file as the sealed blob and never consults the token.
  # tpm2-device= is crypttab(5)'s documented way to declare the intent.
  boot.initrd.systemd.enable = true;
  boot.initrd.luks.devices."{name}".crypttabExtraOpts = [ "tpm2-device=auto" ];
"""


_UNLOCK_BLOCKS: Final[dict[str, Callable[[str], str]]] = {
    "tang-tpm2": _tang_tpm2,
    "tpm2": _tpm2,
    "tpm2-token": _tpm2_token,
    "initrd-key": _initrd_key,
}


def _stanza(method: str, luks_name: str) -> str:
    try:
        render = _UNLOCK_BLOCKS[method]
    except KeyError:
        raise ValueError(
            f"unknown luksUnlock method {method!r}; expected one of {', '.join(LUKS_UNLOCK_MODES)}"
        ) from None
    return _BOOTLOADER + render(luks_name)


def boot_nix(method: str, *, luks_name: str = LUKS_NAME) -> str:
    """The standalone module staged at /etc/nixos/boot.nix for register's harvest.

    ``luks_name`` is the ``boot.initrd.luks.devices`` entry the host's hardware
    config declares: ``cryptroot`` for every flashed host, another name only for
    a host installed before flash existed.
    """
    return f"""# Generated by bootstrap-flash. Harvested by `bootstrap register` into
# config/hosts/<host>/boot.nix, where the dotfiles flake imports it.
#
# Carries this host's bootloader and its LUKS unlock method. The unattended
# methods unlock with a random key enrolled into the disk's own LUKS header, so
# nothing beside this file holds unlock material.
{{ config, utils, ... }}:
{{
{_stanza(method, luks_name)}}}
"""


def flake_wrapper(*, hostname: str, method: str) -> str:
    """The host-hardware.nix wrapper spliced into the flake clone.

    Same boot stanza as boot_nix, plus the generated hardware config, the
    hostname, and the first-boot unit — so the minimal system flash installs
    boots unattended AND then runs the bootstrap itself, with nobody at the
    console. The unit rides only this wrapper, never the harvested boot.nix.
    """
    return f"""{{ config, utils, ... }}:
{{
  imports = [
    ./hardware-configuration.nix
    # The first-boot unit that runs the bootstrap unattended. Flash-only:
    # the harvested boot.nix (boot_nix below) never carries it.
    ./nix/nixos/bootstrap-firstboot.nix
  ];

{_stanza(method, LUKS_NAME)}
  networking.hostName = "{hostname}";
}}
"""
