"""bootstrap-flash <hostname> — flash this machine from the USB installer.

Run on the target, booted from the media ``bootstrap-create-usb`` produced.
Takes the target hostname, plus ``--disk`` on a machine with more than one
eligible disk (omit it and the sole eligible disk is used). Everything else
is declarative: the host's registry entry (``luksUnlock``, tags) comes from
the dotfiles ``host_registry.toml``, the LUKS passphrase and GitHub token
come from the bootstrap sops secrets (decrypted with the age key the media
carries), and the final host configuration arrives later via the normal
``bootstrap`` run on the installed system.

The disk is an argument rather than a registry field because it is read off
the machine being flashed, minutes before it is used, and nothing consumes
it afterwards — device enumeration is a property of the booted installer,
not a durable fact about the host worth committing to git.

Produces a fully flashed, auto-unlocking, minimal NixOS system: GPT via
disko (ESP + LUKS ``cryptroot`` ext4 root), and the
``#bootstrap-secret-zero`` system installed with a systemd-boot + hostname
+ unlock wrapper spliced via ``lib.flake_splice``.

Every target is encrypted; the registry's ``luksUnlock`` picks how it opens
(``lib.boot_config``). ``tang-tpm2`` and ``tpm2`` get a random key enrolled into
its own keyslot and reachable only through the LUKS2 header (a clevis token, or a
systemd-tpm2 token), so nothing that unlocks the disk leaves the machine.
``tpm2-token`` is enrolled later, after Secure Boot; ``initrd-key`` carries the
passphrase in the initramfs for hardware with neither TPM nor Tang. The sops
passphrase stays LUKS keyslot 0 in every method as the console recovery path.

Re-running after a failure restarts from scratch (disko re-wipes) — the
right idempotency model for a destructive installer.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from bootstrap.lib import (
    age_ops,
    boot_config,
    flake_splice,
    registry_toml,
    secrets,
    sh,
    sops_anchors,
)
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.git_ops import git_token_env
from bootstrap.lib.paths import (
    BOOTSTRAP_STATE_DIR,
    DOTFILES_GIT_REMOTE_HTTPS,
    DOTFILES_REGISTRY_REL,
    TANG_URLS_OVERRIDE,
)
from bootstrap.lib.runtime import Tier

NAME = "flash"

_log = logging.getLogger(__name__)

_LUKS_NAME = "cryptroot"
# A dedicated directory, not /mnt itself: mounting the new root over /mnt
# would shadow anything already mounted beneath it (the e2e's 9p share
# lives at /mnt/shared), and the final `umount -R` would take those down
# with it.
_MOUNT_POINT = Path("/mnt/bootstrap-flash")

_INSTALLER_AGE_KEY = Path("/etc/installer-age-key")
_INSTALLER_WIFI_CONF = Path("/etc/wpa_supplicant/imperative.conf")

_DOTFILES_CLONE = Path("/tmp/bootstrap-flash-dotfiles")
_FLAKE_CLONE = Path("/tmp/bootstrap-flash-flake")

_TPM_DEVICE = Path("/dev/tpmrm0")


def _age_key_file() -> Path:
    return Path(os.environ.get("BOOTSTRAP_AGE_KEY_FILE", _INSTALLER_AGE_KEY))


async def _decrypt(secrets_file: Path, key: str, *, age_key_file: Path) -> str:
    result = await sh.run(
        ["sops", "--decrypt", "--extract", f'["{key}"]', str(secrets_file)],
        env={**os.environ, "SOPS_AGE_KEY_FILE": str(age_key_file)},
        destructive=False,
        check=False,
    )
    if not result.ok():
        raise BootstrapError(
            f"could not decrypt {key!r} from {secrets_file} with "
            f"{age_key_file}: {result.stderr.strip()}"
        )
    return result.stdout


async def _clone_dotfiles(token: str) -> Path:
    """Shallow-clone the dotfiles repo for the registry lookup."""
    remote = os.environ.get("BOOTSTRAP_DOTFILES_REMOTE", DOTFILES_GIT_REMOTE_HTTPS)
    if _DOTFILES_CLONE.exists():
        shutil.rmtree(_DOTFILES_CLONE)
    await sh.run(
        ["git", "clone", "--depth", "1", remote, str(_DOTFILES_CLONE)],
        env=git_token_env(token),
        destructive=True,
    )
    return _DOTFILES_CLONE


async def media_tier(flake_src: Path, age_key: Path) -> Tier:
    """Which bootstrap identity this installer media carries."""
    pubkey = await age_ops.extract_public_key(age_key)
    return secrets.tier_for_pubkey(pubkey, sops_anchors.anchor_pubkeys(flake_src / ".sops.yaml"))


async def _resolve_disk(declared: Path | None) -> Path:
    """The target disk: the --disk argument, else the sole eligible disk."""
    if declared is not None:
        if not declared.is_block_device():
            raise BootstrapError(f"--disk {str(declared)!r} is not a block device on this machine")
        return declared

    result = await sh.run(
        ["lsblk", "--json", "-d", "-b", "-o", "NAME,TYPE,RM,RO,SIZE"],
        destructive=False,
    )
    devices = json.loads(result.stdout).get("blockdevices", [])

    boot_parent = ""
    parent = await sh.run(
        ["lsblk", "-no", "pkname", "/dev/disk/by-partlabel/usb-root"],
        destructive=False,
        check=False,
    )
    if parent.ok():
        boot_parent = parent.stdout.strip()

    eligible: list[str] = [
        str(d["name"])
        for d in devices
        if d.get("type") == "disk"
        and not d.get("rm")
        and not d.get("ro")
        and d.get("size", 0) > 0
        and d["name"] != boot_parent
    ]
    if len(eligible) == 1:
        return Path("/dev") / eligible[0]
    if not eligible:
        raise BootstrapError("no eligible target disk found (all disks are removable/boot media)")
    raise BootstrapError(
        f"multiple eligible target disks: {', '.join(eligible)} — "
        "name the one to wipe with --disk /dev/<name>"
    )


def _disko_config(disk: Path, password_file: str) -> str:
    return f"""{{
  disko.devices.disk.main = {{
    device = "{disk}";
    type = "disk";
    content = {{
      type = "gpt";
      partitions = {{
        ESP = {{
          size = "1G";
          type = "EF00";
          content = {{
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
            mountOptions = [ "umask=0077" ];
          }};
        }};
        luks = {{
          size = "100%";
          content = {{
            type = "luks";
            name = "{_LUKS_NAME}";
            passwordFile = "{password_file}";
            settings.allowDiscards = true;
            content = {{
              type = "filesystem";
              format = "ext4";
              mountpoint = "/";
            }};
          }};
        }};
      }};
    }};
  }};
}}
"""


def _tang_policy(tang_urls: list[str]) -> str:
    """The clevis SSS policy for tang-tpm2: threshold 1 over every Tang server,
    plus a TPM2 pin when this machine has one, so any single pin opens it."""
    pins: dict[str, object] = {"tang": [{"url": url} for url in tang_urls]}
    if _TPM_DEVICE.exists():
        _log.info("TPM2 present at %s — adding tpm2 pin", _TPM_DEVICE)
        pins["tpm2"] = {}
    else:
        _log.info("no TPM2 device — tang-only clevis policy")
    return json.dumps({"t": 1, "pins": pins})


def _enroll_argv(
    method: str, *, luks_device: str, passfile: str, tang_urls: list[str]
) -> list[str] | None:
    """The command that enrolls a random unlock key into its own keyslot, or
    None for the methods flash enrolls nothing for.

    Both authenticate with the keyslot-0 passphrase in ``passfile`` and store the
    new key's unlock material in the LUKS2 header itself: a clevis token for
    tang-tpm2, a systemd-tpm2 token (bound to no PCRs) for tpm2.
    """
    if method == "tang-tpm2":
        return [
            "clevis",
            "luks",
            "bind",
            "-y",
            "-d",
            luks_device,
            "-k",
            passfile,
            "sss",
            _tang_policy(tang_urls),
        ]
    if method == "tpm2":
        return [
            "systemd-cryptenroll",
            "--tpm2-device=auto",
            "--tpm2-pcrs=",
            f"--unlock-key-file={passfile}",
            luks_device,
        ]
    return None


async def _luks_device() -> str:
    """The partition behind the open ``cryptroot`` mapping."""
    result = await sh.run(["cryptsetup", "status", _LUKS_NAME], destructive=False)
    for line in result.stdout.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "device":
            return value.strip()
    raise BootstrapError(f"`cryptsetup status {_LUKS_NAME}` reported no backing device")


async def _enroll_unlock_key(
    method: str, *, passfile: str, tang_urls: list[str], dry_run: bool
) -> None:
    """Enroll the method's unattended-unlock keyslot on the freshly formatted disk."""
    if dry_run:
        argv = _enroll_argv(
            method, luks_device="<cryptroot device>", passfile=passfile, tang_urls=tang_urls
        )
        if argv is not None:
            _log.info("would run %s", " ".join(argv))
        return
    argv = _enroll_argv(
        method, luks_device=await _luks_device(), passfile=passfile, tang_urls=tang_urls
    )
    if argv is not None:
        await sh.run(argv, destructive=True)


async def _stage_age_key(age_key: Path) -> None:
    """Put the bootstrap age key on the target, root:wheel 0440.

    Staged before nixos-install so the chrooted sops-nix activation can read
    it; the booted system then uses the same copy until post shreds it.
    """
    key_dir = _MOUNT_POINT / BOOTSTRAP_STATE_DIR.relative_to("/")
    key_dir.mkdir(parents=True, exist_ok=True)
    key_dir.chmod(0o710)
    await sh.run(["chown", "root:wheel", str(key_dir)], destructive=True)
    key_dst = key_dir / "age-key"
    shutil.copyfile(age_key, key_dst)
    key_dst.chmod(0o440)
    await sh.run(["chown", "root:wheel", str(key_dst)], destructive=True)


async def _stage_initrd_key(luks_pass: str) -> None:
    """Put the LUKS passphrase where append-initrd-secrets will copy it from.

    Root-only, on the encrypted root; the initramfs copy on the ESP is what
    the boot actually reads.
    """
    key_path = _MOUNT_POINT / boot_config.INITRD_KEY_PATH.relative_to("/")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(luks_pass)
    key_path.chmod(0o400)
    await sh.run(["chown", "root:root", str(key_path)], destructive=True)


async def run(*, hostname: str, disk: Path | None = None, dry_run: bool = False) -> None:
    if os.geteuid() != 0 and not dry_run:
        raise BootstrapError("bootstrap-flash must run as root (it wipes a disk)")

    age_key = _age_key_file()
    if not age_key.is_file() and not dry_run:
        raise BootstrapError(
            f"bootstrap age key not found at {age_key} — is this the "
            "bootstrap-create-usb installer environment?"
        )

    flake_ref = flake_splice.flake_ref()
    flake_src = await flake_splice.source_path(flake_ref)
    secrets_file = flake_src / "secrets.yaml"

    luks_pass = await _decrypt(secrets_file, "luks_passphrase", age_key_file=age_key)
    github_token = await _decrypt(secrets_file, "github_token", age_key_file=age_key)

    # 1. Registry resolution — the host must already be declared.
    dotfiles = await _clone_dotfiles(github_token)
    registry = registry_toml.load(dotfiles / DOTFILES_REGISTRY_REL)
    if not registry_toml.has_host(registry, hostname):
        raise BootstrapError(
            f"host {hostname!r} is not in host_registry.toml — add it with "
            "`nix run .#add-host` from any dotfiles checkout, push, and re-run"
        )
    tier = await media_tier(flake_src, age_key)
    tags = registry_toml.get_tags(registry, hostname)
    if tier is Tier.PRIVILEGED and "sandbox" in tags:
        raise BootstrapError(
            f"host {hostname!r} is tagged sandbox in host_registry.toml, but this "
            "media carries the privileged bootstrap age key. A sandbox host is "
            "flashed from media built with the sandbox key; nothing was written."
        )
    _log.info("media tier: %s (tags %s)", tier.value, tags or "(none)")
    disk = await _resolve_disk(disk)
    luks_unlock = registry_toml.get_luks_unlock(registry, hostname)
    tang_urls = list(TANG_URLS_OVERRIDE) or registry_toml.tang_urls(registry)
    if luks_unlock == "tang-tpm2" and not tang_urls:
        raise BootstrapError(
            f"host {hostname!r} declares luksUnlock='tang-tpm2' but no host in "
            "host_registry.toml is tagged tang"
        )
    if luks_unlock in ("tpm2", "tpm2-token") and not _TPM_DEVICE.exists() and not dry_run:
        raise BootstrapError(
            f"host {hostname!r} declares luksUnlock={luks_unlock!r} but no TPM is "
            f"present at {_TPM_DEVICE}. Flashing it would produce a host nothing "
            "can unlock unattended, leaving the console passphrase as the only "
            'way in. Fit the module, or set luksUnlock = "initrd-key" for an '
            "unattended boot without one."
        )
    _log.info("flashing %s onto %s (luks unlock: %s)", hostname, disk, luks_unlock)

    # 2. Network is assumed up (ethernet or the media's baked WiFi) — the
    # registry clone above already proved it for the token path.

    # 3. Partition + mount via disko; the passphrase file exists only for
    # the format step (disko forwards only device+settings into the
    # installed config, never the passwordFile).
    with tempfile.NamedTemporaryFile("w", prefix="flash-luks-pass-") as pass_file:
        pass_file.write(luks_pass)
        pass_file.flush()
        os.chmod(pass_file.name, 0o600)

        disko_cfg = Path(tempfile.mkstemp(prefix="disko-", suffix=".nix")[1])
        disko_cfg.write_text(_disko_config(disk, pass_file.name))
        if not dry_run:
            _MOUNT_POINT.mkdir(parents=True, exist_ok=True)
        _log.info("=== disko partition/format/mount %s ===", disk)
        await sh.run(
            [
                "disko",
                "--mode",
                "disko",
                "--root-mountpoint",
                str(_MOUNT_POINT),
                str(disko_cfg),
            ],
            dry_run=dry_run,
            capture=False,
        )

        # The unattended-unlock keyslot, enrolled while the passphrase file
        # still exists (keyslot 0 authorizes it, and stays the console path).
        _log.info("=== enrolling the %s unlock keyslot ===", luks_unlock)
        await _enroll_unlock_key(
            luks_unlock, passfile=pass_file.name, tang_urls=tang_urls, dry_run=dry_run
        )

    # 4. Hardware config for the freshly partitioned machine.
    _log.info("=== nixos-generate-config ===")
    await sh.run(
        ["nixos-generate-config", "--root", str(_MOUNT_POINT)],
        dry_run=dry_run,
    )

    # 5. Clone + splice the bootstrap flake.
    extra_nix = os.environ.get("BOOTSTRAP_EXTRA_NIX")
    hardware_config = _MOUNT_POINT / "etc/nixos/hardware-configuration.nix"
    if dry_run and not hardware_config.is_file():
        _log.info("[dry-run] would splice %s into the flake clone", hardware_config)
    else:
        await flake_splice.prepare_clone(flake_ref=flake_ref, clone=_FLAKE_CLONE)
        flake_splice.splice(
            clone=_FLAKE_CLONE,
            hardware_config=hardware_config,
            networking_config=Path(extra_nix) if extra_nix else None,
            hostname=hostname,
            inject_bootloader=True,
            luks_unlock=luks_unlock,
        )

    # 6. Stage what the install itself reads off the target filesystem. This
    #    MUST precede nixos-install, not follow it.
    #
    #    nixos-install runs `switch-to-configuration boot` under nixos-enter,
    #    so the bootloader installer executes chrooted at _MOUNT_POINT. For an
    #    `initrd-key` host that installer calls append-initrd-secrets, which
    #    does a literal `cp -a /etc/luks-initrd-key` — resolving inside the
    #    chroot. Staged afterwards the file does not exist yet and the install
    #    dies with "cp: cannot stat '/etc/luks-initrd-key'" -> "failed to
    #    create initrd secrets!" -> "Failed to install bootloader", exit 1.
    #
    #    The age key rides along for the same reason one layer up: the
    #    chrooted activation runs sops-nix, which reads it at
    #    BOOTSTRAP_STATE_DIR.
    _log.info("=== staging install inputs ===")
    if dry_run:
        _log.info(
            "would stage the age key%s before nixos-install",
            " and the initrd keyfile" if luks_unlock == "initrd-key" else "",
        )
    else:
        await _stage_age_key(age_key)
        if luks_unlock == "initrd-key":
            await _stage_initrd_key(luks_pass)

    # 7. Install the minimal secret-zero system.
    _log.info("=== nixos-install #bootstrap-secret-zero ===")
    await sh.run(
        [
            "nixos-install",
            "--no-root-password",
            "--root",
            str(_MOUNT_POINT),
            "--flake",
            f"path:{_FLAKE_CLONE}#bootstrap-secret-zero",
        ],
        dry_run=dry_run,
        capture=False,
    )

    # 8. Stage the rest of the post-boot payload.
    _log.info("=== staging target payload ===")
    if dry_run:
        _log.info("would stage wifi conf, boot.nix and the flake source onto the target")
    else:
        if _INSTALLER_WIFI_CONF.is_file():
            wifi_dst = _MOUNT_POINT / "etc/wpa_supplicant/imperative.conf"
            wifi_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_INSTALLER_WIFI_CONF, wifi_dst)
            wifi_dst.chmod(0o600)

        # register's harvest picks these out of /etc/nixos on the booted
        # system: hardware-configuration.nix is already there from
        # nixos-generate-config; boot.nix goes beside it.
        (_MOUNT_POINT / "etc/nixos/boot.nix").write_text(boot_config.boot_nix(luks_unlock))

        # The flake source, so the bootstrap run on the booted target resolves
        # `path:/etc/bootstrap-flake` with no network. flake_splice.flake_ref()
        # prefers this path, so every reference the CLI resolves internally
        # follows.
        await flake_splice.prepare_clone(
            flake_ref=flake_ref,
            clone=_MOUNT_POINT / "etc/bootstrap-flake",
        )

        # The spliced extra config must also reach the harvest location —
        # register commits /etc/nixos/networking.nix into the dotfiles host
        # dir, and without it the post-switch system drops whatever the
        # extra config provided (the e2e's test-instrumentation backdoor
        # dies mid-switch and a getty spawns on its console).
        if extra_nix:
            shutil.copyfile(extra_nix, _MOUNT_POINT / "etc/nixos/networking.nix")

    # 9. Detach and reboot into the flashed system.
    _log.info("=== unmounting ===")
    await sh.run(["umount", "-R", str(_MOUNT_POINT)], dry_run=dry_run, check=False)
    await sh.run(["cryptsetup", "close", _LUKS_NAME], dry_run=dry_run, check=False)

    unlock_summary = {
        "tang-tpm2": "a clevis keyslot over "
        + ", ".join(tang_urls)
        + (" + TPM2" if _TPM_DEVICE.exists() else " (no TPM present)"),
        "tpm2": "a systemd-tpm2 keyslot sealed to this machine's TPM2",
        "tpm2-token": (
            "a LUKS2 TPM2 token — NOT yet enrolled, so this host boots to the "
            "keyslot-0 passphrase until systemd-cryptenroll is run against it "
            "with the host's own Secure Boot keys in the firmware"
        ),
        "initrd-key": "a keyfile inside the initramfs",
    }[luks_unlock]
    _log.info(
        "[bold green]%s flashed onto %s[/] — LUKS unlock: %s. It bootstraps "
        "itself on first boot (bootstrap-firstboot.service; watch with "
        "`journalctl -fu bootstrap-firstboot` on the target).",
        hostname,
        disk,
        unlock_summary,
    )
    if os.environ.get("BOOTSTRAP_FLASH_NO_REBOOT"):
        _log.info("BOOTSTRAP_FLASH_NO_REBOOT set — skipping reboot (test seam)")
        return
    await sh.run(["reboot"], dry_run=dry_run, destructive=True)
