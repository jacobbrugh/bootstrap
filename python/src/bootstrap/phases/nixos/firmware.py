"""bootstrap-firmware — convert a Protectli VP4650 to Dasharo coreboot.

Run on the target, booted from the media ``bootstrap-create-usb`` produced.
Reports and exits by default; ``--apply`` writes the flash chip.

The BIOS/UEFI is not on a disk. It lives on a 16 MiB socketed SPI chip on the
motherboard, and ``flashrom`` is an ordinary userspace program that rewrites it
through the chipset's SPI controller while Linux runs. So this needs no special
boot medium — the installer USB is already a Linux with ``flashrom`` on it, and
the same stick then runs ``bootstrap-flash``. Nothing is wiped in between.

This is Dasharo's *initial deployment* path, not its update path: the unit ships
with AMI, the stock proprietary firmware. Dasharo's update page redirects that
case ("If your device is currently flashed with the proprietary firmware please
refer to the Initial deployment documentation"), and initial deployment
prescribes a backup read followed by a whole-chip write that includes the Intel
ME. That is the same command Protectli's own flasher issues for a vp4650
(``flashli/configurations.py``: ``vendor/flashrom -p internal -w {0}``), so the
write is identical either way; what differs is which firmware-setup menus clear
the write protection beforehand, and the reset afterwards.

Safety shape: a wrong environment makes flashrom fail *before* writing —
Protectli documents its own returns as "2: /dev/mem cannot be opened" and
"3: mmap() failed". The mode that bricks a board is an interrupted write. So the
worst case here is a clean abort, with Protectli's Flashli-on-Ubuntu path still
available afterwards.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from bootstrap.lib import sh
from bootstrap.lib.errors import BootstrapError

NAME = "firmware"

_log = logging.getLogger(__name__)

#: Baked onto the installer by nix/nixos/installer-usb.nix. The env var is a
#: test seam; production always reads the media copy.
_ROM_PATH = Path("/etc/protectli-firmware/vp46xx.rom")

#: Boards this tool will touch. The ROM is the VP4600-series image, which covers
#: the VP4630/VP4650/VP4670 variants, but only the VP4650 is validated here.
_SUPPORTED_PRODUCTS = ("VP4650",)

#: Target version, as Dasharo reports it in the SMBIOS BIOS version string.
_TARGET_VERSION = "v1.2.0"

#: Expected size of the VP4600-series flash image.
_ROM_BYTES = 16777216

_BACKUP_DIR = Path("/root")

# flashrom prints these when the chipset refuses writes. Either means the BIOS
# lock is still set in firmware setup — a write attempted anyway is the
# partial-write case that actually bricks boards.
_LOCKDOWN_MARKERS = (
    "SPI Configuration is locked down",
    "SPI Programming Register Lock",
    "PR0:",
    "Warning: BIOS region SMM protection is enabled",
)

_AMI_UNLOCK = (
    "AMI firmware setup (hold DEL at boot):\n"
    "  Chipset -> PCH-IO Configuration -> BIOS Lock  : Disabled\n"
    "  Security -> Secure Boot                       : Disabled\n"
    "  F4 to save and reboot, then run this again."
)

_DASHARO_UNLOCK = (
    "Dasharo firmware setup (hold DEL at boot):\n"
    "  Dasharo System Features -> Dasharo Security Options\n"
    "    -> clear 'Lock the BIOS boot medium'\n"
    "  Device Manager -> Secure Boot Configuration   : Disabled\n"
    "  F10 to save, reset, then run this again."
)

_RESET_INSTRUCTIONS = (
    "Flash complete. Restart the device now.\n"
    "\n"
    "Protectli's own flasher prints 'Please restart your device' for a vp4650 —\n"
    "the models needing a full power-pull are a separate list this board is not\n"
    "on. If the board does NOT come back up, Dasharo's initial-deployment page\n"
    "prescribes a hard reset after flashing over proprietary firmware:\n"
    "  1. Power off (it may not power off fully, due to the freshly flashed ME).\n"
    "  2. Disconnect the power supply once the screen goes dark.\n"
    "  3. Open the four lid screws and disconnect the CR2032 coin cell\n"
    "     (2-pin connector, underside of the board) for a couple of seconds.\n"
    "  4. Reconnect the cell, then the power supply.\n"
    "\n"
    "Then re-enter setup and confirm Secure Boot is still disabled (v1.2.0\n"
    "enrolls default keys on first boot, and our bootloader is unsigned), and\n"
    "that Power Management Options -> Power State After Power/AC Loss reads\n"
    "'Powered On'."
)


def rom_path() -> Path:
    return Path(os.environ.get("BOOTSTRAP_FIRMWARE_ROM", _ROM_PATH))


async def _dmi(field: str) -> str:
    result = await sh.run(["dmidecode", "-s", field], destructive=False, check=False)
    if not result.ok():
        raise BootstrapError(f"could not read DMI field {field!r}: {result.stderr.strip()}")
    return result.stdout.strip()


def is_dasharo(bios_version: str) -> bool:
    """Whether an SMBIOS BIOS version string is Dasharo rather than AMI."""
    return "dasharo" in bios_version.lower()


def is_target_version(bios_version: str) -> bool:
    """Whether the chip already carries the version this tool would write."""
    return is_dasharo(bios_version) and _TARGET_VERSION in bios_version


def find_lockdown(probe_output: str) -> str | None:
    """The first lockdown marker present in flashrom's probe output, if any."""
    for marker in _LOCKDOWN_MARKERS:
        if marker in probe_output:
            return marker
    return None


def _backup_path() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return _BACKUP_DIR / f"firmware-backup-{stamp}.rom"


async def run(*, apply: bool = False, dry_run: bool = False) -> None:
    if os.geteuid() != 0 and not dry_run:
        raise BootstrapError(
            "bootstrap-firmware must run as root (it reads and writes the SPI flash chip)"
        )

    rom = rom_path()
    if not rom.is_file():
        raise BootstrapError(
            f"firmware image not found at {rom} — this tool expects the "
            "bootstrap-create-usb installer environment, which bakes it in"
        )
    size = rom.stat().st_size
    if size != _ROM_BYTES:
        raise BootstrapError(
            f"firmware image {rom} is {size} bytes, expected {_ROM_BYTES} — "
            "refusing to write a truncated or wrong-board image"
        )

    # 1. Board identity. The write is whole-chip and board-specific; a mismatch
    #    is exactly the case worth refusing rather than recovering from.
    product = await _dmi("system-product-name")
    if product not in _SUPPORTED_PRODUCTS:
        raise BootstrapError(
            f"this board reports system-product-name={product!r}; "
            f"bootstrap-firmware only handles {', '.join(_SUPPORTED_PRODUCTS)}"
        )

    # 2. What is on the chip now.
    bios_version = await _dmi("bios-version")
    _log.info("board: %s", product)
    _log.info("installed firmware: %s", bios_version)
    _log.info("target firmware: Dasharo (coreboot+UEFI) %s", _TARGET_VERSION)

    if is_target_version(bios_version):
        _log.info(
            "[bold green]already running Dasharo %s[/] — nothing to do",
            _TARGET_VERSION,
        )
        return

    on_ami = not is_dasharo(bios_version)
    unlock_help = _AMI_UNLOCK if on_ami else _DASHARO_UNLOCK
    _log.info(
        "this is %s",
        "an AMI -> Dasharo conversion (initial deployment)"
        if on_ami
        else "a Dasharo version change",
    )

    # 3. Read-only probe: does the chipset actually allow a write right now.
    probe = await sh.run(["flashrom", "-p", "internal"], destructive=False, check=False)
    probe_output = probe.stdout + probe.stderr
    if not probe.ok():
        raise BootstrapError(
            "flashrom could not probe the flash chip, so nothing was written.\n"
            f"{probe_output.strip()}\n\n"
            "On a NixOS installer this is usually the kernel blocking raw "
            "access: the media boots with iomem=relaxed and blacklists "
            "spi_intel/spi_intel_pci/lpc_ich for exactly this reason."
        )
    marker = find_lockdown(probe_output)
    if marker is not None:
        raise BootstrapError(
            f"the flash chip is write-protected (flashrom reported {marker!r}), "
            "so nothing was written. Clear it first:\n\n" + unlock_help
        )
    _log.info("flash chip is probeable and unlocked")

    if not apply:
        _log.info(
            "[bold]report only[/] — re-run with --apply to write the chip. "
            "Make sure BIOS Lock and Secure Boot are disabled first:\n%s",
            unlock_help,
        )
        return

    if dry_run:
        _log.info("would back up the current firmware, then run: flashrom -p internal -w %s", rom)
        return

    # 4. Backup. On an AMI board this file is the only copy of the unit's
    #    factory image.
    backup = _backup_path()
    _log.info("=== reading current firmware -> %s ===", backup)
    await sh.run(
        ["flashrom", "-p", "internal", "-r", str(backup)],
        destructive=True,
        capture=False,
    )
    if not backup.is_file() or backup.stat().st_size != _ROM_BYTES:
        raise BootstrapError(
            f"backup read to {backup} did not produce a {_ROM_BYTES}-byte image; refusing to write"
        )
    _log.info("backup saved (%d bytes)", backup.stat().st_size)

    # 5. Whole-chip write, including the Intel ME. No --ifd, no --fmap.
    _log.info("=== writing %s — DO NOT interrupt or power off ===", rom)
    await sh.run(
        ["flashrom", "-p", "internal", "-w", str(rom)],
        destructive=True,
        capture=False,
    )

    _log.info("[bold green]%s flashed to Dasharo %s[/]", product, _TARGET_VERSION)
    for line in _RESET_INSTRUCTIONS.splitlines():
        _log.info("%s", line)
