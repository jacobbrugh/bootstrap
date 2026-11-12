"""bootstrap-create-usb — flash the LUKS-encrypted USB installer.

`sudo nix run 'github:jacobbrugh/bootstrap#create-usb' -- /dev/sdX`
takes the
one argument that cannot be derived (the USB block device), wipes it, and
produces boot media carrying the ``installer-usb`` NixOS system, the
bootstrap age key, and — when the operator has set ``wifi_wpa_conf`` in the
repo's sops secrets — baked WiFi credentials. Everything else is
declarative: changing what the media contains means changing bootstrap repo
code/secrets and re-running, never per-invocation knobs.

Partitioning is manual sgdisk + cryptsetup + mkfs (GPT: usb-esp ESP +
usb-root LUKS) because disko's dm-crypt handling causes I/O errors on USB
drives. The LUKS passphrase is the ``luks_passphrase`` sops secret — the
same value the operator types once at the installer's boot prompt, and the
same value ``bootstrap-flash`` later enrolls as targets' fallback keyslot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from bootstrap.lib import flake_splice, sh, sops_anchors
from bootstrap.lib.errors import BootstrapError

NAME = "create_usb"

_log = logging.getLogger(__name__)

_LUKS_NAME = "usb-crypt"

_WIFI_DEST_REL = Path("etc/wpa_supplicant/imperative.conf")
_AGE_KEY_DEST_REL = Path("etc/installer-age-key")


def _age_key_file_candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("SOPS_AGE_KEY_FILE")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / ".config" / "sops" / "age" / "keys.txt")
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        candidates.append(Path("/home") / sudo_user / ".config" / "sops" / "age" / "keys.txt")
        candidates.append(Path("/Users") / sudo_user / ".config" / "sops" / "age" / "keys.txt")
    return [c for i, c in enumerate(candidates) if c not in candidates[:i]]


async def _decrypt_secret(
    source: Path, key: str, *, age_key_file: Path, required: bool
) -> str | None:
    result = await sh.run(
        ["sops", "--decrypt", "--extract", f'["{key}"]', str(source)],
        env={**os.environ, "SOPS_AGE_KEY_FILE": str(age_key_file)},
        destructive=False,
        check=False,
    )
    if result.ok():
        return result.stdout
    if required:
        raise BootstrapError(
            f"could not decrypt {key!r} from {source}: {result.stderr.strip()} — "
            "set it with `sops set secrets.yaml '[\"" + key + "\"]' ...` from a "
            "machine holding the bootstrap age key"
        )
    _log.info("optional secret %r not available (%s)", key, result.stderr.strip().splitlines()[-1])
    return None


async def _resolve_secrets(flake_src: Path) -> tuple[str, str | None, Path]:
    """Find a working age key file and decrypt the media secrets with it.

    Returns (luks_passphrase, wifi_wpa_conf | None, age_key_file).
    """
    secrets_file = flake_src / "secrets.yaml"
    errors: list[str] = []
    for key_file in _age_key_file_candidates():
        if not key_file.is_file():
            errors.append(f"{key_file}: not a file")
            continue
        result = await sh.run(
            ["sops", "--decrypt", "--extract", '["luks_passphrase"]', str(secrets_file)],
            env={**os.environ, "SOPS_AGE_KEY_FILE": str(key_file)},
            destructive=False,
            check=False,
        )
        if result.ok():
            wifi = await _decrypt_secret(
                secrets_file, "wifi_wpa_conf", age_key_file=key_file, required=False
            )
            return result.stdout, wifi, key_file
        errors.append(f"{key_file}: {result.stderr.strip().splitlines()[-1]}")
    raise BootstrapError(
        "could not decrypt luks_passphrase from the bootstrap sops secrets "
        f"({secrets_file}) with any local age key. Tried:\n  "
        + "\n  ".join(errors)
        + "\nThe bootstrap age key (1Password: bootstrap age key) must be in "
        "an age keys file (SOPS_AGE_KEY_FILE or ~/.config/sops/age/keys.txt), "
        "and secrets.yaml must carry a luks_passphrase entry."
    )


#: .sops.yaml anchor names, in the order media creation prefers them. Real
#: device media must carry the privileged key: the sandbox tier is explicitly
#: "never a recipient of privileged dotfiles secrets" (.sops.yaml header), so a
#: host flashed with it cannot decrypt fleet secrets. CI holds only the sandbox
#: key and falls through to the second entry.
_ANCHOR_PREFERENCE = (sops_anchors.PRIVILEGED_ANCHOR, sops_anchors.SANDBOX_ANCHOR)


async def _bootstrap_identity(flake_src: Path, key_file: Path) -> str:
    """Extract the identity for the most privileged anchor the operator holds.

    The operator's keys file may hold several identities; only the bootstrap key
    belongs on the media. Anchors are resolved by NAME, not by position in the
    keys list — the repo declares &bootstrap_sandbox first and
    &bootstrap_privileged second, so taking keys[0] silently selected the
    sandbox key for real hardware.
    """
    sops_yaml_path = flake_src / ".sops.yaml"
    anchors = sops_anchors.anchor_pubkeys(sops_yaml_path)
    missing = [name for name in _ANCHOR_PREFERENCE if name not in anchors]
    if missing:
        raise BootstrapError(
            f"{sops_yaml_path} declares no anchor(s) named {', '.join(missing)}; "
            f"found {', '.join(sorted(anchors)) or 'none'}"
        )

    local: dict[str, str] = {}
    for line in key_file.read_text().splitlines():
        line = line.strip()
        if not line.startswith("AGE-SECRET-KEY-"):
            continue
        derived = await sh.run(
            ["age-keygen", "-y"],
            input_text=line + "\n",
            destructive=False,
        )
        local[derived.stdout.strip()] = line

    for name in _ANCHOR_PREFERENCE:
        secret = local.get(anchors[name])
        if secret is not None:
            _log.info("using the %s age identity for this media", name)
            return secret

    wanted = ", ".join(f"{name}={anchors[name][:24]}…" for name in _ANCHOR_PREFERENCE)
    raise BootstrapError(
        f"no identity in {key_file} matches a bootstrap age public key from "
        f"{sops_yaml_path} (looked for {wanted})"
    )


def _partition_paths(device: Path, *, dry_run: bool) -> tuple[Path, Path]:
    nvme_style = (Path(f"{device}p1"), Path(f"{device}p2"))
    scsi_style = (Path(f"{device}1"), Path(f"{device}2"))
    if dry_run:
        return scsi_style
    if nvme_style[0].is_block_device():
        return nvme_style
    if scsi_style[0].is_block_device():
        return scsi_style
    raise BootstrapError(f"could not find partitions on {device} after sgdisk")


def _mapping_exists() -> bool:
    """Whether the ``usb-crypt`` device-mapper node is currently present."""
    return (Path("/dev/mapper") / _LUKS_NAME).exists()


async def _teardown_stale_mapping(*, dry_run: bool) -> None:
    """Clear a leftover ``usb-crypt`` LUKS mapping from an aborted run.

    A create-usb that is hard-killed - SIGKILL, a closed terminal, a yanked
    stick - never reaches the ``finally`` that unmounts and closes the mapping
    it opened, so ``/dev/mapper/usb-crypt`` lingers. The next run then dies at
    ``cryptsetup open ... usb-crypt`` with "Device usb-crypt already exists",
    and a manual ``cryptsetup close`` reports "still in use" because a stale
    mount (not any named process, so ``lsof``/``procs`` by name find nothing)
    still holds it. Unmount every mountpoint it carries (found with ``findmnt``,
    since ``umount -R`` recurses over a mountpoint directory and does nothing
    useful given the device path), close it, and - if a holder still blocks the
    close - force the device-mapper node out with ``dmsetup remove -f``. A node
    that survives all of that is reported here, rather than as the confusing
    "already exists" from the ``cryptsetup open`` that follows.
    """
    if dry_run or not _mapping_exists():
        return
    _log.info("=== clearing stale %s mapping left by an aborted run ===", _LUKS_NAME)
    mapper = str(Path("/dev/mapper") / _LUKS_NAME)
    mounts = await sh.run(
        ["findmnt", "-rn", "-o", "TARGET", "--source", mapper],
        check=False,
        destructive=False,
    )
    # Deepest first, so a mount stacked inside another comes off before its parent.
    for target in sorted(mounts.stdout.split(), key=len, reverse=True):
        await sh.run(["umount", "-R", target], check=False, destructive=True)
    closed = await sh.run(["cryptsetup", "close", _LUKS_NAME], check=False, destructive=True)
    if not closed.ok() and _mapping_exists():
        await sh.run(["dmsetup", "remove", "-f", _LUKS_NAME], check=False, destructive=True)
    if _mapping_exists():
        raise BootstrapError(
            f"{mapper} is still present after unmounting, closing and force-removing it; "
            "something outside this run holds it (see `dmsetup info -c` and `lsblk`)"
        )


async def run(*, device: Path, dry_run: bool = False) -> None:
    if os.geteuid() != 0 and not dry_run:
        raise BootstrapError(
            "bootstrap-create-usb must run as root "
            "(sudo nix run 'github:jacobbrugh/bootstrap#create-usb' -- /dev/sdX)"
        )
    if not device.is_block_device() and not dry_run:
        raise BootstrapError(f"{device} is not a block device")

    # Preflight: a create-usb killed before its cleanup leaves /dev/mapper/
    # usb-crypt open, and this run's `cryptsetup open ... usb-crypt` would then
    # fail "already exists". Clear any such leftover before touching the disk.
    await _teardown_stale_mapping(dry_run=dry_run)

    flake_ref = flake_splice.flake_ref()
    flake_src = await flake_splice.source_path(flake_ref)
    luks_pass, wifi_conf, key_file = await _resolve_secrets(flake_src)
    age_identity = await _bootstrap_identity(flake_src, key_file)

    # nixos-install refuses a root mountpoint with a world-writable ancestor
    # ("Path \"/tmp\" is world-writable or a symlink"), so mount under /run
    # (root-owned 0755). Dry-run may be unprivileged — fall back to $TMPDIR.
    mount_point = Path(
        tempfile.mkdtemp(
            prefix="bootstrap-usb-",
            dir="/run" if os.geteuid() == 0 else None,
        )
    )
    mounted_esp = False
    mounted_root = False
    luks_open = False

    with tempfile.NamedTemporaryFile("w", prefix="usb-luks-pass-") as pass_file:
        pass_file.write(luks_pass)
        pass_file.flush()
        os.chmod(pass_file.name, 0o600)

        try:
            _log.info("=== partitioning %s ===", device)
            await sh.run(["wipefs", "--all", "-f", str(device)], dry_run=dry_run)
            await sh.run(
                [
                    "sgdisk",
                    "--clear",
                    "--new=1:0:+512M",
                    "--typecode=1:EF00",
                    "--change-name=1:usb-esp",
                    "--new=2:0:0",
                    "--typecode=2:8300",
                    "--change-name=2:usb-root",
                    str(device),
                ],
                dry_run=dry_run,
            )
            await sh.run(["partprobe", str(device)], dry_run=dry_run)
            await asyncio.sleep(0 if dry_run else 1)

            part_esp, part_root = _partition_paths(device, dry_run=dry_run)
            _log.info("ESP: %s  root: %s", part_esp, part_root)

            await sh.run(["mkfs.vfat", "-n", "USB-ESP", str(part_esp)], dry_run=dry_run)

            _log.info("=== LUKS format + open ===")
            await sh.run(
                [
                    "cryptsetup",
                    "luksFormat",
                    "--batch-mode",
                    "--sector-size",
                    "512",
                    "--key-file",
                    pass_file.name,
                    str(part_root),
                ],
                dry_run=dry_run,
            )
            await sh.run(
                [
                    "cryptsetup",
                    "open",
                    "--key-file",
                    pass_file.name,
                    str(part_root),
                    _LUKS_NAME,
                ],
                dry_run=dry_run,
            )
            luks_open = not dry_run
            await sh.run(
                ["mkfs.ext4", "-F", "-L", "usb-nixos", f"/dev/mapper/{_LUKS_NAME}"],
                dry_run=dry_run,
            )

            _log.info("=== mounting at %s ===", mount_point)
            await sh.run(["mount", f"/dev/mapper/{_LUKS_NAME}", str(mount_point)], dry_run=dry_run)
            mounted_root = not dry_run
            if not dry_run:
                (mount_point / "boot").mkdir(exist_ok=True)
            await sh.run(["mount", str(part_esp), str(mount_point / "boot")], dry_run=dry_run)
            mounted_esp = not dry_run

            _log.info("=== nixos-install %s#installer-usb ===", flake_ref)
            await sh.run(
                [
                    "nixos-install",
                    "--no-root-password",
                    "--root",
                    str(mount_point),
                    "--flake",
                    f"{flake_ref}#installer-usb",
                ],
                dry_run=dry_run,
                capture=False,
            )

            _log.info("=== staging media payload ===")
            if dry_run:
                _log.info("would write %s (0440)", mount_point / _AGE_KEY_DEST_REL)
                if wifi_conf is not None:
                    _log.info("would write %s (0600)", mount_point / _WIFI_DEST_REL)
            else:
                age_dest = mount_point / _AGE_KEY_DEST_REL
                age_dest.parent.mkdir(parents=True, exist_ok=True)
                age_dest.write_text(age_identity + "\n")
                age_dest.chmod(0o440)
                if wifi_conf is not None:
                    wifi_dest = mount_point / _WIFI_DEST_REL
                    wifi_dest.parent.mkdir(parents=True, exist_ok=True)
                    wifi_dest.write_text(wifi_conf)
                    wifi_dest.chmod(0o600)
        finally:
            if mounted_esp or mounted_root:
                await sh.run(["umount", "-R", str(mount_point)], check=False, destructive=True)
            if luks_open:
                await sh.run(["cryptsetup", "close", _LUKS_NAME], check=False, destructive=True)
            if not any(mount_point.iterdir()):
                mount_point.rmdir()

    _log.info(
        "[bold green]USB installer ready[/] on %s — boot it on the target, "
        "enter the luks_passphrase at the prompt, then run "
        "`bootstrap-flash <hostname>`",
        device,
    )
