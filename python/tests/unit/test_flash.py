from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bootstrap.lib import boot_config, flake_splice, git_ops, registry_toml, sh
from bootstrap.lib.errors import BootstrapError
from bootstrap.lib.runtime import Tier
from bootstrap.phases.nixos import flash


def test_disko_config_shape() -> None:
    cfg = flash._disko_config(Path("/dev/nvme0n1"), "/tmp/passfile")
    assert 'device = "/dev/nvme0n1";' in cfg
    assert 'name = "cryptroot";' in cfg
    assert 'passwordFile = "/tmp/passfile";' in cfg
    assert 'format = "vfat";' in cfg
    assert 'mountpoint = "/";' in cfg


def test_resolve_disk_returns_the_disk_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """--disk wins outright: no lsblk probe, no registry lookup."""
    monkeypatch.setattr(Path, "is_block_device", lambda self: True)
    assert asyncio.run(flash._resolve_disk(Path("/dev/nvme0n1"))) == Path("/dev/nvme0n1")


def test_resolve_disk_argument_must_be_a_block_device(tmp_path: Path) -> None:
    """A typo'd --disk aborts before disko is ever invoked, so the wrong device
    cannot be wiped."""
    with pytest.raises(BootstrapError, match="not a block device"):
        asyncio.run(flash._resolve_disk(tmp_path / "nope"))


def test_tang_enrollment_binds_a_header_token_over_every_tang() -> None:
    """clevis luks bind adds a random key in its own keyslot and stores its
    Tang-wrapped copy in the LUKS2 header; threshold 1 lets any one server (or
    the TPM, where present) open it alone."""
    urls = ["http://192.0.2.1:7500", "http://192.0.2.2:7500"]
    argv = flash._enroll_argv(
        "tang-tpm2", luks_device="/dev/vda2", passfile="/run/p", tang_urls=urls
    )
    assert argv is not None
    assert argv[:7] == ["clevis", "luks", "bind", "-y", "-d", "/dev/vda2", "-k"]
    assert argv[7:9] == ["/run/p", "sss"]
    policy = json.loads(argv[9])
    assert policy["t"] == 1
    assert [pin["url"] for pin in policy["pins"]["tang"]] == urls


def test_tpm2_enrollment_is_a_systemd_token_bound_to_no_pcrs() -> None:
    argv = flash._enroll_argv("tpm2", luks_device="/dev/vda2", passfile="/run/p", tang_urls=[])
    assert argv == [
        "systemd-cryptenroll",
        "--tpm2-device=auto",
        "--tpm2-pcrs=",
        "--unlock-key-file=/run/p",
        "/dev/vda2",
    ]


def test_tpm2_token_and_initrd_key_enroll_nothing_at_flash() -> None:
    for method in ("tpm2-token", "initrd-key"):
        assert (
            flash._enroll_argv(method, luks_device="/dev/vda2", passfile="/run/p", tang_urls=[])
            is None
        )


def test_age_key_file_env_seam(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOOTSTRAP_AGE_KEY_FILE", str(tmp_path / "k"))
    assert flash._age_key_file() == tmp_path / "k"
    monkeypatch.delenv("BOOTSTRAP_AGE_KEY_FILE")
    assert flash._age_key_file() == Path("/etc/installer-age-key")


def test_every_unlock_method_renders_a_bootloader() -> None:
    """Whatever the method, the harvested boot.nix must carry the loader: a
    nixos-generate-config hardware.nix declares none, and without one the first
    dotfiles switch fails NixOS's boot.loader.grub.devices assertion."""
    for method in boot_config.LUKS_UNLOCK_MODES:
        rendered = boot_config.boot_nix(method)
        assert "boot.loader.systemd-boot.enable = true;" in rendered
        assert "boot.loader.grub.enable = false;" in rendered


def test_initrd_key_reference_is_a_string_not_a_nix_path() -> None:
    """append-initrd-secrets copies from the live filesystem when the value is a
    string; an unquoted Nix path would instead copy the passphrase into the
    world-readable Nix store."""
    rendered = boot_config.boot_nix("initrd-key")
    assert 'boot.initrd.secrets."/crypt-key" = "/etc/luks-initrd-key";' in rendered
    assert "= /etc/luks-initrd-key;" not in rendered
    code = "\n".join(line for line in rendered.splitlines() if not line.lstrip().startswith("#"))
    assert "boot.initrd.clevis" not in code


def test_flash_wrapper_imports_the_firstboot_unit() -> None:
    """The wrapper bootstrap-flash splices in is what makes a flashed host run
    its own bootstrap; without the import the box boots to a login prompt and
    waits for a human."""
    wrapper = boot_config.flake_wrapper(hostname="rtr9", method="initrd-key")
    assert "./nix/nixos/bootstrap-firstboot.nix" in wrapper


def test_harvested_boot_nix_carries_no_firstboot_unit() -> None:
    """register commits boot_nix() into dotfiles config/hosts/<host>/; the
    first-boot unit must never follow the host there, or every dotfiles switch
    would re-run the bootstrap."""
    for method in boot_config.LUKS_UNLOCK_MODES:
        rendered = boot_config.boot_nix(method)
        assert "bootstrap-firstboot" not in rendered, method
        assert not any(
            line.lstrip().startswith("systemd.services") for line in rendered.splitlines()
        ), method


def test_tang_boot_nix_unlocks_from_the_header_token() -> None:
    """The tang stanza opens the volume with clevis luks unlock from the disk's
    own token before systemd-cryptsetup runs; no secretFile, so nothing beside
    the config holds unlock material."""
    rendered = boot_config.boot_nix("tang-tpm2", luks_name="crypt-root")
    code = "\n".join(line for line in rendered.splitlines() if not line.lstrip().startswith("#"))
    assert "secretFile" not in code
    assert "boot.initrd.clevis.enable = true;" in code
    assert 'boot.initrd.systemd.services."clevis-luks-unlock-crypt-root"' in code
    assert 'environment.LUKS_DEVICE = config.boot.initrd.luks.devices."crypt-root".device;' in code
    assert 'clevis luks unlock -d "$LUKS_DEVICE" -n crypt-root && exit 0' in code
    assert 'systemd-cryptsetup@${utils.escapeSystemdPath "crypt-root"}.service' in code
    assert '"cdc_ncm"' in code


def test_tpm2_boot_nix_is_the_native_systemd_token() -> None:
    rendered = boot_config.boot_nix("tpm2")
    code = "\n".join(line for line in rendered.splitlines() if not line.lstrip().startswith("#"))
    assert (
        'boot.initrd.luks.devices."cryptroot".crypttabExtraOpts = [ "tpm2-device=auto" ];' in code
    )
    assert "boot.initrd.clevis" not in code
    assert "keyFile" not in code


def test_tpm2_token_leaves_field_three_empty() -> None:
    """systemd-cryptsetup consults a LUKS2 TPM2 token only when no key file is
    configured; with one set it treats that file as the sealed blob instead. So
    this mode must declare tpm2-device= and no keyFile at all."""
    rendered = boot_config.boot_nix("tpm2-token")
    assert 'crypttabExtraOpts = [ "tpm2-device=auto" ];' in rendered
    code = "\n".join(line for line in rendered.splitlines() if not line.lstrip().startswith("#"))
    assert "keyFile" not in code
    assert "boot.initrd.secrets" not in code
    assert "boot.initrd.clevis" not in code
    assert "boot.initrd.systemd.enable = true;" in code


def test_tpm2_token_enrolls_no_keyslot_at_flash_time() -> None:
    """Flash cannot seal to PCR 7: the host's own Secure Boot keys are not in the
    firmware yet, so the value to seal against does not exist. The mode stages
    nothing and the box boots to the keyslot-0 passphrase until enrollment."""
    assert (
        flash._enroll_argv("tpm2-token", luks_device="/dev/vda2", passfile="/run/p", tang_urls=[])
        is None
    )
    wrapper = boot_config.flake_wrapper(hostname="rtr9", method="tpm2-token")
    assert "luks-initrd-key" not in wrapper


def test_unknown_unlock_method_refused() -> None:
    with pytest.raises(ValueError, match="unknown luksUnlock method"):
        boot_config.boot_nix("nonsense")


def _drive_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    luks_unlock: str,
    media: Tier = Tier.PRIVILEGED,
    tags: list[str] | None = None,
) -> list[str]:
    """Run flash.run() with every external effect stubbed, returning an ordered
    event log of the commands it issued and the files it staged."""
    events: list[str] = []

    async def fake_media_tier(_src: Path, _key: Path) -> Tier:
        return media

    monkeypatch.setattr(flash, "media_tier", fake_media_tier)
    monkeypatch.setattr(registry_toml, "get_tags", lambda _r, _h: list(tags or []))

    mount = tmp_path / "mnt"
    (mount / "etc/nixos").mkdir(parents=True)
    (mount / "etc/nixos/hardware-configuration.nix").write_text("{ }\n")
    monkeypatch.setattr(flash, "_MOUNT_POINT", mount)

    age_key = tmp_path / "age-key"
    age_key.write_text("AGE-SECRET-KEY-TEST\n")
    monkeypatch.setenv("BOOTSTRAP_AGE_KEY_FILE", str(age_key))
    monkeypatch.setenv("BOOTSTRAP_FLASH_NO_REBOOT", "1")
    monkeypatch.delenv("BOOTSTRAP_EXTRA_NIX", raising=False)

    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(Path, "is_block_device", lambda self: True)

    async def fake_sh_run(argv: list[str], **kwargs: object) -> object:
        events.append(argv[0])
        return SimpleNamespace(stdout="", stderr="", ok=lambda: True)

    async def fake_decrypt(_f: Path, key: str, **kwargs: object) -> str:
        return f"{key}-value"

    async def fake_clone(_token: str) -> Path:
        return tmp_path / "dotfiles"

    async def fake_source_path(_ref: str) -> Path:
        return tmp_path / "src"

    async def fake_prepare_clone(**kwargs: object) -> None:
        events.append("prepare_clone")

    def fake_splice(**kwargs: object) -> None:
        events.append("splice")

    async def fake_enroll(
        method: str, *, passfile: str, tang_urls: list[str], dry_run: bool
    ) -> None:
        events.append(f"enroll:{method}:{Path(passfile).is_file()}")

    async def fake_stage_initrd_key(luks_pass: str) -> None:
        events.append("stage-initrd-key")
        key = mount / boot_config.INITRD_KEY_PATH.relative_to("/")
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text(luks_pass)

    monkeypatch.setattr(sh, "run", fake_sh_run)
    monkeypatch.setattr(flash, "_decrypt", fake_decrypt)
    monkeypatch.setattr(flash, "_clone_dotfiles", fake_clone)
    monkeypatch.setattr(flash, "_enroll_unlock_key", fake_enroll)
    monkeypatch.setattr(flash, "TANG_URLS_OVERRIDE", ("http://192.0.2.1:7500",))
    monkeypatch.setattr(flash, "_stage_initrd_key", fake_stage_initrd_key)
    # flash imports these as modules, so patching the module object patches
    # what flash resolves at call time.
    monkeypatch.setattr(flake_splice, "flake_ref", lambda: "path:/nowhere")
    monkeypatch.setattr(flake_splice, "source_path", fake_source_path)
    monkeypatch.setattr(flake_splice, "prepare_clone", fake_prepare_clone)
    monkeypatch.setattr(flake_splice, "splice", fake_splice)
    monkeypatch.setattr(registry_toml, "load", lambda _p: {})
    monkeypatch.setattr(registry_toml, "has_host", lambda _r, _h: True)
    monkeypatch.setattr(registry_toml, "get_luks_unlock", lambda _r, _h: luks_unlock)

    asyncio.run(flash.run(hostname="h", disk=Path("/dev/sdz")))
    return events


def test_initrd_keyfile_is_staged_before_nixos_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """nixos-install runs the bootloader installer chrooted at the target, and
    for an initrd-key host that installer calls append-initrd-secrets, which
    does a literal `cp -a /etc/luks-initrd-key` inside the chroot. Staged
    afterwards the file does not exist yet and the install dies with "failed to
    create initrd secrets! / Failed to install bootloader"."""
    events = _drive_run(monkeypatch, tmp_path, luks_unlock="initrd-key")
    assert "stage-initrd-key" in events, events
    assert "nixos-install" in events, events
    assert events.index("stage-initrd-key") < events.index("nixos-install"), events


def test_clevis_hosts_stage_no_initrd_keyfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The clevis method unlocks from a keyslot token in the disk header, so no
    keyfile and no JWE belong on the target filesystem."""
    events = _drive_run(monkeypatch, tmp_path, luks_unlock="tang-tpm2")
    assert "stage-initrd-key" not in events, events
    assert not (tmp_path / "mnt/etc/luks-initrd-key").exists()
    assert not (tmp_path / "mnt/etc/nixos/cryptroot.jwe").exists()


def test_unlock_keyslot_is_enrolled_while_the_passfile_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keyslot 0 authorizes the new keyslot, so enrollment runs inside the block
    that holds the passphrase file, right after disko formats the disk."""
    events = _drive_run(monkeypatch, tmp_path, luks_unlock="tang-tpm2")
    assert "enroll:tang-tpm2:True" in events, events
    assert events.index("disko") < events.index("enroll:tang-tpm2:True"), events
    assert events.index("enroll:tang-tpm2:True") < events.index("nixos-install"), events


def test_age_key_is_staged_before_nixos_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The chrooted activation nixos-install performs runs sops-nix, which reads
    this key; staged later it fails with "cannot read keyfile"."""
    events = _drive_run(monkeypatch, tmp_path, luks_unlock="initrd-key")
    assert (tmp_path / "mnt/var/lib/bootstrap/age-key").is_file(), events
    assert events.index("chown") < events.index("nixos-install"), events


def test_multiple_eligible_disks_name_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-detection covers the one-disk case; anything else must be named
    explicitly rather than guessed at."""

    async def fake_run(argv: list[str], **kwargs: object) -> object:
        if "--json" in argv:
            payload = {
                "blockdevices": [
                    {"name": "nvme0n1", "type": "disk", "rm": False, "ro": False, "size": 1},
                    {"name": "mmcblk0", "type": "disk", "rm": False, "ro": False, "size": 1},
                ]
            }
            return SimpleNamespace(stdout=json.dumps(payload), ok=lambda: True)
        return SimpleNamespace(stdout="", ok=lambda: False)

    monkeypatch.setattr(sh, "run", fake_run)
    with pytest.raises(BootstrapError, match=r"--disk /dev/<name>"):
        asyncio.run(flash._resolve_disk(None))


def test_privileged_media_refuses_a_sandbox_tagged_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The invariant the tiers rest on: a sandbox host never holds the
    privileged key. Refused before disko, so nothing is written."""
    with pytest.raises(BootstrapError, match="tagged sandbox"):
        _drive_run(monkeypatch, tmp_path, luks_unlock="initrd-key", tags=["sandbox"])
    assert not (tmp_path / "mnt/var/lib/bootstrap/age-key").exists()


def test_sandbox_media_flashes_a_sandbox_tagged_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = _drive_run(
        monkeypatch, tmp_path, luks_unlock="initrd-key", media=Tier.SANDBOX, tags=["sandbox"]
    )
    assert "nixos-install" in events


def test_registry_clone_keeps_the_token_out_of_the_url() -> None:
    """The token rides git's env credential helper, so it lands in neither the
    clone's .git/config nor sh.run's argv log."""
    env = git_ops.git_token_env("ghp_secret")
    assert env["BOOTSTRAP_GIT_TOKEN"] == "ghp_secret"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert "ghp_secret" not in env["GIT_CONFIG_VALUE_0"]
    assert "$BOOTSTRAP_GIT_TOKEN" in env["GIT_CONFIG_VALUE_0"]
