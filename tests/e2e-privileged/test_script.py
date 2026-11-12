# ruff: noqa: F821
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from test_driver.machine import QemuMachine

    machine: QemuMachine

    # Injected by the nix-side testScript prelude (tests/e2e-privileged/default.nix).
    OVMF_CODE: str
    OVMF_VARS: str
    QEMU_KVM: str
    TANGD: str
    SOURCE: str

    def start_all() -> None: ...
    def create_machine(
        start_command: str, *, name: str | None = None, keep_machine_state: bool = False
    ) -> QemuMachine: ...


_SHARED_HOST_DIR = "/tmp/bootstrap-e2e-shared/privileged"
_CACHE_HOST_DIR = "/tmp/bootstrap-e2e-shared/cache"
#: Flashed, inspected on disk, and booted: the fleet-default clevis binding,
#: which must unlock with nothing typed while a Tang server answers.
_HOSTNAME = "usb-e2e-tang"
#: Flashed AND booted: no TPM, no reachable Tang, so an unattended boot here is
#: the direct test of the property the router depends on.
_BOOT_HOSTNAME = "usb-e2e-initrd"
_LUKS_PASS = (
    "usb-e2e-passphrase"  # set in the staged secrets.yaml by bootstrap-e2e-stage
)
GH_LOG = "/tmp/bootstrap-e2e-gh-calls.jsonl"
#: The fake dotfiles origin lives on a disk of its own (/dev/vde on the
#: installer, mounted by label on the booted target) so that a re-flash of the
#: target cannot wipe the repository its previous boot registered into. It is
#: seeded under a name register cannot clone and moved into place only after
#: the first register has failed at its clone — the retry that has to heal.
ORIGIN_MOUNT = "/mnt/origin"
ORIGIN = f"{ORIGIN_MOUNT}/dotfiles"
ORIGIN_PENDING = f"{ORIGIN_MOUNT}/dotfiles.pending"

# The spliced host-networking.nix for the flashed system: test
# instrumentation (backdoor + serial console) so the driver can attach to
# the booted artifact, the 9p shares (fixtures + the binary cache), the origin
# disk, the attic cache + netrc, and static resolvers (see
# tests/e2e-sandbox/default.nix for the resolver rationale). `@args` because
# the scaffold generation passes the `bootstrap` package as a specialArg and
# the dotfiles generation does not: the module system hands every specialArg
# to a module declared with `...`, and the block that reads args.bootstrap is
# gated on the scaffold marker.
_EXTRA_NIX = """
{ lib, pkgs, modulesPath, options, ... }@args:
{
  imports = [ (modulesPath + "/testing/test-instrumentation.nix") ];
  # The test drives the booted artifact directly (git fixture ops, sops
  # token extraction) — the minimal secret-zero system carries neither;
  # production needs neither on the system PATH (the CLI wrapper bundles
  # its own tools).
  environment.systemPackages = [ pkgs.git pkgs.sops ];
  networking.resolvconf.enable = lib.mkForce false;
  environment.etc."resolv.conf".text = "nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n";
  nix.settings = {
    # mkDefault: the scaffold's bootstrap-firstboot module pins max-jobs = 1
    # (asserted below) and must win there; the dotfiles generation gets 2.
    max-jobs = lib.mkDefault 2;
    cores = 8;
    extra-substituters = [
      "file:///mnt/cache?priority=5"
      "https://cache.jacobbrugh.net/attic?priority=10"
    ];
    extra-trusted-public-keys = [ "attic:TGN7u8ffZ1H01LvNYlpV4FgyRYRpaoG9CxtSHhOYRgY=" ];
    require-sigs = false;
    netrc-file = lib.mkForce "/var/lib/bootstrap/nix-netrc";
  };
  fileSystems."/mnt/shared" = {
    device = "e2e";
    fsType = "9p";
    options = [ "trans=virtio" "version=9p2000.L" "msize=104857600" "nofail" ];
    neededForBoot = false;
  };
  fileSystems."/mnt/cache" = {
    device = "cache";
    fsType = "9p";
    options = [ "trans=virtio" "version=9p2000.L" "msize=104857600" "nofail" ];
    neededForBoot = false;
  };
  # The fake origin: outside the scaffold-only gate below, so the dotfiles
  # generation mounts it too — a completed host re-runs the bootstrap against
  # it, and the test reads what was pushed there after every leg.
  fileSystems."/mnt/origin" = {
    device = "/dev/disk/by-label/e2e-origin";
    fsType = "ext4";
    options = [ "nofail" ];
    neededForBoot = false;
  };

  # Everything below exists ONLY in the scaffold generation. bootstrap-flash
  # stages this whole module at /etc/nixos/networking.nix and register harvests
  # it into dotfiles config/hosts/<host>/, so it is evaluated a second time in
  # the dotfiles generation the bootstrap switches to. If these units were
  # defined there too, bootstrap-firstboot would be a *changed* unit rather
  # than a *removed* one, and switch-to-configuration stops changed units no
  # matter what X-StopOnRemoval says — killing the bootstrap mid-switch. The
  # marker is the option the unit module declares: present in the scaffold
  # eval, absent in dotfiles.
  # A throwaway test VM must not push its store paths to the fleet cache, and
  # the attic token it holds is a same-shape dummy the cache would reject, so
  # disable attic-watch-store in BOTH the scaffold and the harvested dotfiles
  # generation. mkMerge, not a bare `systemd.services.attic-watch-store.enable`
  # beside the mkIf below: two literal `systemd.services` definitions in one
  # attrset collide ("attribute 'systemd.services' already defined").
  systemd.services = lib.mkMerge [
  { attic-watch-store.enable = lib.mkForce false; }
  (lib.mkIf (lib.hasAttrByPath [ "bootstrap" "firstboot" ] options) {

  # The fixture the first-boot unit needs, staged before it runs: git has to
  # accept the staged bootstrap source, which another uid owns. The origin
  # itself is on its own disk (see fileSystems above); nothing is copied onto
  # this root.
  bootstrap-e2e-fixture = {
    wantedBy = [ "multi-user.target" ];
    requires = [ "mnt-shared.mount" "mnt-origin.mount" ];
    after = [ "mnt-shared.mount" "mnt-origin.mount" ];
    before = [ "bootstrap-firstboot.service" ];
    path = [ "/run/wrappers" pkgs.git ];
    serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
    script = \'\'
      su - jacob -c 'git config --global --add safe.directory /mnt/shared/bootstrap-source'
    \'\';
  };

  # Point the production unit at the fixture: the packaged CLI with mock-gh
  # on its PATH so the ssh phase touches no real GitHub, and the origin disk
  # as the remote. `wants`, not `requires`: the dotfiles switch stops this
  # scaffold-only fixture, and a Requires= would propagate that stop to the
  # running bootstrap. No BOOTSTRAP_SANDBOX — this host is privileged, and
  # the tier comes from the key. Everything else about the unit is exactly
  # what a flashed host runs.
  bootstrap-firstboot = {
    wants = [ "bootstrap-e2e-fixture.service" ];
    after = [ "bootstrap-e2e-fixture.service" ];
    environment = {
      BOOTSTRAP_FLAKE = "git+file:///mnt/shared/bootstrap-source";
      BOOTSTRAP_DOTFILES_REMOTE = "file:///mnt/origin/dotfiles";
      BOOTSTRAP_E2E_GH_LOG = "/tmp/bootstrap-e2e-gh-calls.jsonl";
    };
    serviceConfig.ExecStart = lib.mkForce (
      lib.getExe (
        pkgs.callPackage ./tests/e2e-lib/bootstrap-for-test.nix {
          bootstrap = args.bootstrap;
          mockGh = pkgs.callPackage ./tests/e2e-lib/mock-gh.nix { };
        }
      )
    );
  };

  })
  ];
}
"""


def _now(m: "QemuMachine") -> str:
    """The guest's clock, for journal windows that outlive a unit's invocation."""
    return m.succeed("date +%s").strip()


def _firstboot_journal(m: "QemuMachine", since: str) -> str:
    _, journal = m.execute(
        f"journalctl -u bootstrap-firstboot --no-pager --since=@{since}"
    )
    return journal


def _wait_for_bootstrap(m: "QemuMachine", *, since: str, timeout: int = 3600) -> None:
    """Wait for a bootstrap-firstboot run started at or after `since` to finish.

    Not wait_for_unit: the bootstrap's switch phase re-execs systemd, and
    for a moment `systemctl show` fails with "Failed to connect to system
    scope bus" — wait_for_unit turns any systemctl error into an assertion
    failure. Poll with execute() instead: a transient error is retried,
    `failed` aborts at once, and the post phase's closing line is the
    success signal — the unit file is gone from the new generation by then,
    so the running instance is `not-found` and its properties are not worth
    relying on. The journal is read from `since` so an earlier, deliberately
    failed run of the same unit cannot satisfy or abort this wait.
    """
    deadline = time.monotonic() + timeout
    while True:
        rc, state = m.execute(
            "systemctl show -p ActiveState --value bootstrap-firstboot.service"
        )
        state = state.strip()
        journal = _firstboot_journal(m, since)
        if "bootstrap complete" in journal:
            return
        # Definitive failures the ActiveState poll can miss: a start-timeout
        # SIGTERM flips the unit failed->removed (X-StopOnRemoval) between
        # 15s samples, and a phase exception logs "bootstrap failed:" while
        # the unit lingers active. Abort at once rather than spin to 3600s.
        for marker in ("start operation timed out", "bootstrap failed:"):
            if marker in journal:
                raise AssertionError(
                    f"bootstrap-firstboot hit {marker!r}. Journal tail:\n{journal[-6000:]}"
                )
        if rc == 0 and state == "failed":
            raise AssertionError(
                f"bootstrap-firstboot failed. Journal tail:\n{journal[-6000:]}"
            )
        if time.monotonic() > deadline:
            raise AssertionError(
                f"bootstrap-firstboot did not finish within {timeout}s (last state "
                f"{state!r}). Journal tail:\n{journal[-6000:]}"
            )
        time.sleep(15)


def _wait_for_bootstrap_failure(
    m: "QemuMachine", *, since: str, timeout: int = 1800
) -> str:
    """Wait for the run started at or after `since` to fail; return its journal."""
    deadline = time.monotonic() + timeout
    while True:
        journal = _firstboot_journal(m, since)
        if "bootstrap failed:" in journal:
            return journal
        assert "bootstrap complete" not in journal, (
            f"expected this bootstrap to fail, it completed. Journal tail:\n{journal[-6000:]}"
        )
        if time.monotonic() > deadline:
            raise AssertionError(
                f"bootstrap-firstboot neither failed nor finished within {timeout}s. "
                f"Journal tail:\n{journal[-6000:]}"
            )
        time.sleep(10)


def _exec_start(m: "QemuMachine") -> str:
    """The store path the first-boot unit runs — the unit is scaffold-only and
    gone after the switch; the wrapped CLI it pointed at is not."""
    raw = m.succeed("systemctl show -p ExecStart --value bootstrap-firstboot.service")
    match = re.search(r"path=(\S+?)\s*;", raw)
    assert match is not None, f"no ExecStart path in {raw!r}"
    return match.group(1)


def _stage_target_netrc(m: "QemuMachine", *, mapper: str, mountpoint: str) -> None:
    """netrc for the booted artifact's nix builds (referenced by the spliced
    extra config), staged onto the freshly flashed initrd-key target."""
    m.succeed(
        f"echo -n '{_LUKS_PASS}' | cryptsetup open /dev/vdd2 {mapper} --key-file -"
    )
    m.succeed(f"mkdir -p {mountpoint} && mount /dev/mapper/{mapper} {mountpoint}")
    m.succeed(
        f"install -D -m 0600 /root/nix-netrc {mountpoint}/var/lib/bootstrap/nix-netrc"
    )
    m.succeed(f"umount -R {mountpoint} && cryptsetup close {mapper}")


#: The Tang server flash binds against, run by the driver on the build host so
#: the installer VM (at flash) and a separately booted target (at its initrd
#: unlock) reach the same server: QEMU user networking maps 10.0.2.2 to the
#: host's loopback for every VM.
_TANG_PORT = 7500
_TANG_URL = f"http://10.0.2.2:{_TANG_PORT}"


def _start_host_tang() -> "subprocess.Popen[bytes]":
    """Host-side Tang for the whole run; keys are generated on first start."""
    keys = tempfile.mkdtemp(prefix="e2e-tang-")
    return subprocess.Popen([TANGD, "-l", "-p", str(_TANG_PORT), keys])


def run() -> None:
    start_all()
    machine.wait_for_unit("multi-user.target")

    _, preflight = machine.execute(
        "ip addr; echo ---; cat /etc/resolv.conf; echo ---; lsblk -d -o NAME,SIZE,RM"
    )
    print(f"[net-preflight]\n{preflight}")
    machine.wait_until_succeeds(
        "curl -4 -sSf -m 10 -o /dev/null https://1.1.1.1/", timeout=120
    )
    machine.wait_until_succeeds("getent hosts github.com", timeout=120)
    machine.succeed("test -d /mnt/cache/nar")

    # ── Stage fixtures ────────────────────────────────────────────────────
    # The ephemeral identity bootstrap-e2e-stage minted as this run's
    # privileged key; the staged .sops.yaml anchors it as bootstrap_privileged.
    machine.succeed("install -m 0600 -o root -g root /mnt/shared/age-key /root/age-key")
    machine.succeed(
        "install -m 0600 -o root -g root /mnt/shared/nix-netrc /root/nix-netrc"
    )
    machine.succeed("test -d /mnt/shared/bootstrap-source")
    machine.succeed("test -d /mnt/shared/dotfiles")
    for host in (_HOSTNAME, _BOOT_HOSTNAME):
        machine.succeed(
            f"grep -q '\\[{host}\\]' /mnt/shared/dotfiles/host_registry.toml"
        )

    # ── The fake origin, on its own disk ──────────────────────────────────
    # Local ext4 (git push to a 9p-hosted file:// remote fails in
    # receive-pack — see tests/e2e-sandbox), owned by jacob (uid 1000 on the
    # target too), with the stage tool's symlink deletions restored from the
    # index so updateInstead accepts a push. Seeded under the .pending name:
    # the target's first register must fail to clone it.
    machine.succeed("mkfs.ext4 -q -L e2e-origin /dev/vde")
    machine.succeed(f"mkdir -p {ORIGIN_MOUNT} && mount /dev/vde {ORIGIN_MOUNT}")
    machine.succeed(f"cp -a /mnt/shared/dotfiles {ORIGIN_PENDING}")
    machine.succeed(f"git config --global --add safe.directory {ORIGIN_PENDING}")
    machine.succeed(f"git -C {ORIGIN_PENDING} checkout -- .")
    machine.succeed(
        f"git -C {ORIGIN_PENDING} config receive.denyCurrentBranch updateInstead"
    )
    machine.succeed(f"chown -R 1000:100 {ORIGIN_PENDING}")

    tang = _start_host_tang()
    machine.wait_until_succeeds(f"curl -sSf -o /root/tang-adv.json {_TANG_URL}/adv")

    env = (
        "export BOOTSTRAP_FLAKE=git+file:///mnt/shared/bootstrap-source && "
        "export BOOTSTRAP_DOTFILES_REMOTE=file:///mnt/shared/dotfiles && "
        f"export BOOTSTRAP_TANG_URLS={_TANG_URL} && "
        "export SOPS_AGE_KEY_FILE=/root/age-key && "
        "export BOOTSTRAP_AGE_KEY_FILE=/root/age-key && "
        "export BOOTSTRAP_EXTRA_NIX=/root/extra.nix && "
        "export BOOTSTRAP_FLASH_NO_REBOOT=1 && "
    )

    # ── bootstrap-create-usb against the virtual USB (/dev/vdb) ───────────
    machine.succeed(
        env + "bootstrap-create-usb /dev/vdb",
        timeout=3600,
    )

    # ── Leg 0: create-usb over a mapping a killed run left open ───────────
    # The exact state that broke the real stick: /dev/mapper/usb-crypt still
    # open, and held by a mount so `cryptsetup close` alone reports "still in
    # use". The preflight must clear it (dmsetup remove -f) and the run
    # succeed; the media assertions below then run on this second stick.
    print("[e2e-privileged] create-usb finished; re-running it over a held mapping")
    machine.succeed(f"echo -n '{_LUKS_PASS}' > /root/usb-pass")
    machine.succeed("cryptsetup open --key-file /root/usb-pass /dev/vdb2 usb-crypt")
    machine.succeed("mkdir -p /mnt-stale && mount /dev/mapper/usb-crypt /mnt-stale")
    machine.succeed(
        env + "bootstrap-create-usb /dev/vdb 2>&1 | tee /root/create-usb-2.log",
        timeout=3600,
    )
    machine.succeed("grep -q 'clearing stale usb-crypt mapping' /root/create-usb-2.log")
    machine.fail("test -e /dev/mapper/usb-crypt")

    print("[e2e-privileged] create-usb finished; inspecting the stick")
    gpt = machine.succeed("sgdisk --print /dev/vdb")
    assert "usb-esp" in gpt and "usb-root" in gpt, (
        f"partlabels missing from GPT: {gpt!r}"
    )

    machine.succeed(
        f"echo -n '{_LUKS_PASS}' | cryptsetup open /dev/vdb2 usb-verify --key-file -"
    )
    machine.succeed("mkdir -p /mnt-usb && mount -o ro /dev/mapper/usb-verify /mnt-usb")
    machine.succeed("mkdir -p /mnt-usb-esp && mount -o ro /dev/vdb1 /mnt-usb-esp")
    machine.succeed("test -f /mnt-usb-esp/EFI/BOOT/BOOTX64.EFI")
    # The media carries the privileged (here: ephemeral) identity, and the
    # privileged token file rides the baked flake source.
    machine.succeed("cmp /mnt-usb/etc/installer-age-key /root/age-key")
    usb_key_mode = machine.succeed("stat -c %a /mnt-usb/etc/installer-age-key").strip()
    assert usb_key_mode == "440", f"installer age key mode {usb_key_mode!r} != 440"
    # The baked flake source carries the privileged token file. /etc/bootstrap-flake
    # on the stick is an environment.etc *absolute* symlink into /nix/store, so it
    # must be resolved and read inside the stick's own store — following it through
    # the /mnt-usb mount would resolve against this VM's store instead.
    flake_store = machine.succeed("readlink /mnt-usb/etc/bootstrap-flake").strip()
    machine.succeed(f"test -s /mnt-usb{flake_store}/secrets-privileged.yaml")
    machine.succeed("umount /mnt-usb-esp /mnt-usb && cryptsetup close usb-verify")

    # ── bootstrap-flash onto the --disk target (/dev/vdc) ─────────────────
    machine.succeed(f"cat > /root/extra.nix <<'NIXEOF'\n{_EXTRA_NIX}\nNIXEOF")
    machine.succeed(
        env + f"bootstrap-flash {_HOSTNAME} --disk /dev/vdc",
        timeout=3600,
    )

    print("[e2e-privileged] flash finished; mount-and-inspect of the target")
    machine.succeed(
        f"echo -n '{_LUKS_PASS}' | cryptsetup open /dev/vdc2 target-verify --key-file -"
    )
    machine.succeed(
        "mkdir -p /mnt-target && mount /dev/mapper/target-verify /mnt-target"
    )
    machine.succeed("mkdir -p /mnt-target/boot && mount /dev/vdc1 /mnt-target/boot")

    # systemd-boot on the ESP; the wrapper baked the hostname; the generated
    # hardware config staged for register's harvest. The unattended key lives
    # only in the LUKS2 header: keyslot 0 is the fallback passphrase, keyslot 1
    # the random key, and a clevis token holds its Tang-wrapped copy. No JWE
    # file exists anywhere on the target.
    machine.succeed("test -f /mnt-target/boot/EFI/systemd/systemd-bootx64.efi")
    machine.succeed(
        "grep -q 'fileSystems' /mnt-target/etc/nixos/hardware-configuration.nix"
    )
    machine.fail("test -e /mnt-target/etc/nixos/cryptroot.jwe")
    machine.succeed(
        "grep -q 'clevis-luks-unlock-cryptroot' /mnt-target/etc/nixos/boot.nix"
    )
    luks_dump = machine.succeed("cryptsetup luksDump /dev/vdc2")
    assert re.search(r"^\s+0: luks2$", luks_dump, re.M), (
        f"keyslot 0 (fallback passphrase) missing: {luks_dump!r}"
    )
    assert re.search(r"^\s+1: luks2$", luks_dump, re.M), (
        f"keyslot 1 (the enrolled unlock key) missing: {luks_dump!r}"
    )
    assert re.search(r"^\s+\d+: clevis$", luks_dump, re.M), (
        f"no clevis token in the LUKS2 header: {luks_dump!r}"
    )

    key_mode = machine.succeed(
        "stat -c %a /mnt-target/var/lib/bootstrap/age-key"
    ).strip()
    assert key_mode == "440", f"staged age key mode {key_mode!r} != 440"

    # The system profile must resolve to a store path that exists *in the
    # target's own store* — nixos-install realizes the closure into the
    # chroot store, so resolving against the installer VM's /nix/store
    # (what `readlink -e` would do at the absolute-symlink hop) is wrong.
    system_link = machine.succeed(
        "readlink /mnt-target/nix/var/nix/profiles/system"
    ).strip()
    assert "system-" in system_link, f"no system profile generation: {system_link!r}"
    target_system = machine.succeed(
        f"readlink /mnt-target/nix/var/nix/profiles/{system_link}"
    ).strip()
    assert target_system.startswith("/nix/store/"), (
        f"profile generation points outside the store: {target_system!r}"
    )
    machine.succeed(f"test -d /mnt-target{target_system}")
    machine.succeed(f"test -x /mnt-target{target_system}/init")

    # Chroot activation: sops-nix must decrypt the bootstrap token with the
    # staged age key — and only the bot token; the privileged one is never a
    # sops-nix secret.
    machine.succeed(
        "nixos-enter --root /mnt-target -- test -s /run/secrets/bootstrap-github-token"
    )
    machine.fail(
        "nixos-enter --root /mnt-target -- test -e /run/secrets/github_ssh_key_token"
    )

    # The scaffold is reachable over ssh from its first boot: fleet keys in,
    # passwords out. Read it through nixos-enter — /etc/ssh/authorized_keys.d/jacob
    # is an environment.etc absolute symlink into the store, which resolves only
    # inside the target's own store, not through the /mnt-target mount.
    machine.succeed(
        "nixos-enter --root /mnt-target -- grep -q jacob@pc1 /etc/ssh/authorized_keys.d/jacob"
    )

    # netrc for the booted artifact's nix builds (referenced by the spliced
    # extra config).
    machine.succeed(
        "install -D -m 0600 /root/nix-netrc /mnt-target/var/lib/bootstrap/nix-netrc"
    )

    machine.succeed("umount -R /mnt-target && cryptsetup close target-verify")

    # ── The initrd-key target (/dev/vdd), which is the one that gets booted ──
    machine.succeed(
        env + f"bootstrap-flash {_BOOT_HOSTNAME} --disk /dev/vdd",
        timeout=3600,
    )

    print(
        "[e2e-privileged] second flash finished; mount-and-inspect of the boot target"
    )
    machine.succeed(
        f"echo -n '{_LUKS_PASS}' | cryptsetup open /dev/vdd2 boot-verify --key-file -"
    )
    machine.succeed("mkdir -p /mnt-boot-t && mount /dev/mapper/boot-verify /mnt-boot-t")
    machine.succeed("mkdir -p /mnt-boot-t/boot && mount /dev/vdd1 /mnt-boot-t/boot")

    # Encrypted like every target — only the unlock differs. No clevis JWE, and
    # the passphrase staged root-only for append-initrd-secrets to pick up.
    machine.succeed("test -f /mnt-boot-t/boot/EFI/systemd/systemd-bootx64.efi")
    machine.fail("test -e /mnt-boot-t/etc/nixos/cryptroot.jwe")
    machine.succeed("grep -q 'boot.initrd.secrets' /mnt-boot-t/etc/nixos/boot.nix")
    machine.succeed("grep -q 'systemd-boot' /mnt-boot-t/etc/nixos/boot.nix")
    keyfile_mode = machine.succeed("stat -c %a /mnt-boot-t/etc/luks-initrd-key").strip()
    assert keyfile_mode == "400", f"initrd keyfile mode {keyfile_mode!r} != 400"

    # The flake source flash stages, so the bootstrap run on the booted target
    # resolves path:/etc/bootstrap-flake rather than fetching from GitHub.
    # The env seam below would mask a missing copy, so assert it on disk —
    # the privileged token file included.
    machine.succeed("test -f /mnt-boot-t/etc/bootstrap-flake/flake.nix")
    machine.succeed("test -d /mnt-boot-t/etc/bootstrap-flake/python")
    machine.succeed("test -s /mnt-boot-t/etc/bootstrap-flake/secrets-privileged.yaml")

    machine.succeed(
        "install -D -m 0600 /root/nix-netrc /mnt-boot-t/var/lib/bootstrap/nix-netrc"
    )
    machine.succeed("umount -R /mnt-boot-t && cryptsetup close boot-verify")

    # ── Boot the artifact ────────────────────────────────────────────────
    # The installer VM must release the target disk image (and the origin
    # disk) first. This is the initrd-key host, so it must reach
    # multi-user.target with NO console interaction at all — the in-VM Tang
    # dying with the installer is exactly the post-power-cut condition the
    # router has to survive.
    machine.succeed(f"umount {ORIGIN_MOUNT}")
    state_dir = machine.state_dir
    machine.shutdown()

    # ── Boot the tang-tpm2 target: unattended through the host-side Tang ──
    # Its initrd brings up DHCP, and clevis-luks-unlock-cryptroot runs
    # `clevis luks unlock` from the header token before systemd-cryptsetup,
    # which then finds the volume active. Nothing is typed, so reaching
    # multi-user.target is the assertion.
    tang_vars = f"{state_dir}/OVMF_VARS-tang.fd"
    shutil.copyfile(OVMF_VARS, tang_vars)
    os.chmod(tang_vars, 0o644)
    tang_target = create_machine(
        " ".join(
            [
                QEMU_KVM,
                "-cpu max",
                "-m 4096",
                "-smp 4",
                f"-drive if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
                f"-drive if=pflash,format=raw,file={tang_vars}",
                f"-drive file={state_dir}/empty1.qcow2,werror=report,if=virtio",
                "-device virtio-net-pci,netdev=n1",
                "-netdev user,id=n1",
            ]
        ),
        name=_HOSTNAME,
    )
    tang_target.start()
    try:
        tang_target.wait_for_unit("multi-user.target", timeout=900)
        console = tang_target.get_console_log()
        assert "Starting password query" not in console, (
            "the tang-tpm2 target asked for a LUKS passphrase; its clevis token "
            f"did not open the root. Console:\n{console[-4000:]}"
        )
        tang_target.succeed("test -e /dev/mapper/cryptroot")
        unlock = tang_target.succeed(
            "journalctl -b -o cat -u clevis-luks-unlock-cryptroot"
        )
        assert "Finished" in unlock, f"the initrd unlock unit did not finish:\n{unlock}"

        # With no Tang answering, the unit retries out its 10-minute window and
        # the boot falls back to the keyslot-0 passphrase prompt rather than
        # hanging or failing open.
        tang_target.crash()
        tang.terminate()
        tang.wait(timeout=30)
        tang_target.start()
        # Match systemd's "Starting password query on /dev/ttyS0" journal line,
        # not the prompt itself: the prompt ends without a newline, and the
        # driver reads the console a line at a time, so the prompt is only seen
        # once later output happens to end its line (bootstrap/main #47 reached
        # the prompt at 625 s and timed out waiting to read it).
        tang_target.wait_for_console_text(
            r"Starting password query on /dev/ttyS0", timeout=1200
        )
    finally:
        tang_target.crash()

    vars_copy = f"{state_dir}/OVMF_VARS.fd"
    shutil.copyfile(OVMF_VARS, vars_copy)
    os.chmod(vars_copy, 0o644)

    start_cmd = " ".join(
        [
            QEMU_KVM,
            "-cpu max",
            "-m 16384",
            "-smp 8",
            f"-drive if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
            f"-drive if=pflash,format=raw,file={vars_copy}",
            f"-drive file={state_dir}/empty2.qcow2,werror=report,if=virtio",
            f"-drive file={state_dir}/empty3.qcow2,werror=report,if=virtio",
            f"-virtfs local,path={_SHARED_HOST_DIR},security_model=none,mount_tag=e2e",
            f"-virtfs local,path={_CACHE_HOST_DIR},security_model=none,mount_tag=cache",
            "-device virtio-net-pci,netdev=n1",
            "-netdev user,id=n1",
        ]
    )
    target = create_machine(start_cmd, name=_BOOT_HOSTNAME)
    target.start()
    # The driver's failure cleanup only kills machines it declared —
    # a leaked create_machine() qemu keeps the vm-test build alive for
    # hours after an assertion failure. Guarantee it dies either way.
    try:
        # The whole point of this leg: an encrypted root that opens with no
        # human, no TPM and no network. Nothing is typed at the console — no
        # send_chars anywhere below — so reaching multi-user.target IS the
        # assertion. A regression that put the passphrase prompt back would
        # hang here rather than quietly passing.
        target.wait_for_unit("multi-user.target", timeout=900)
        boot_at = _now(target)
        assert target.succeed("hostname").strip() == _BOOT_HOSTNAME

        console = target.get_console_log()
        assert "Starting password query" not in console, (
            "the target asked for a LUKS passphrase; the initrd keyfile did not "
            f"open the root unattended. Console:\n{console[-4000:]}"
        )
        # It really is encrypted — an unlocked root would also boot silently.
        target.succeed("test -e /dev/mapper/cryptroot")

        # ── The scaffold runs the modules under test ─────────────────────────
        # bootstrap-e2e-fixture (from _EXTRA_NIX above) seeds the git
        # credential; bootstrap-firstboot then runs the full production
        # bootstrap as jacob with nothing typed. The scaffold generation was
        # built from the staged copy of THIS source, so the first-boot module's
        # own settings are observable here: the build bound (serial builds and
        # zram, so a small host cannot OOM its first switch) and the flake
        # source the flash staged for the bootstrap to run.
        target.wait_for_unit("bootstrap-e2e-fixture.service")
        nix_config = target.succeed("nix config show")
        assert "max-jobs = 1" in nix_config, (
            f"the scaffold does not serialize its first build:\n{nix_config}"
        )
        swap = target.succeed("swapon --show --noheadings")
        assert "zram0" in swap, f"the scaffold has no zram swap:\n{swap!r}"
        target.succeed(
            "cmp /etc/bootstrap-flake/flake.nix /mnt/shared/bootstrap-source/flake.nix"
        )
        exec_start = _exec_start(target)
        target.succeed(f"test -x {exec_start}")
        target.succeed("test -d /mnt/origin")

        # ── Leg 1: register fails before its clone; the retry heals ─────────
        # The origin is not where the unit was told it is, so the first run
        # dies at `git clone`. That must leave the host exactly as it found
        # it: the staged host files under a real /etc/nixos (the retry has to
        # harvest them), no host key yet, the bootstrap key still staged, and
        # no flake symlink claiming the host is bootstrapped — the marker the
        # old fast path read, which made every retry skip register and switch
        # onto secrets the host could not decrypt.
        journal = _wait_for_bootstrap_failure(target, since=boot_at)
        print(f"[e2e-privileged] first run failed as arranged:\n{journal[-1500:]}")
        target.succeed("test -d /etc/nixos && test ! -L /etc/nixos")
        target.succeed("test -f /etc/nixos/hardware-configuration.nix")
        target.succeed("test -f /etc/nixos/boot.nix")
        target.fail(f"test -e {HOST_KEY}")
        target.succeed("test -f /var/lib/bootstrap/age-key")

        target.succeed(f"mv {ORIGIN_PENDING} {ORIGIN}")
        retry_at = _now(target)
        target.succeed("systemctl restart bootstrap-firstboot.service")
        _wait_for_bootstrap(target, since=retry_at)

        # Post-phase shred: the staged bootstrap age key must be gone.
        target.fail("test -f /var/lib/bootstrap/age-key")

        # ── The tier: exactly one upload, to jacobbrugh, with the privileged token ──
        # One across BOTH runs: the ssh phase's already-registered check reads
        # the keys mock-gh remembers, so the retry uploads nothing.
        calls = [
            json.loads(line)
            for line in target.succeed(f"cat {GH_LOG}").splitlines()
            if line
        ]
        argvs = [c["argv"] for c in calls]
        print(f"[e2e-privileged] gh calls: {argvs}")
        uploads = [c for c in calls if c["argv"][:2] == ["ssh-key", "add"]]
        assert len(uploads) == 1, f"expected exactly one ssh-key add, got {uploads}"
        upload = uploads[0]
        assert upload["token"] == "privileged", f"upload used the wrong token: {upload}"
        assert upload["argv"][3:] == [
            "--title",
            upload["argv"][4],
            "--type",
            "authentication",
        ], upload
        assert upload["argv"][4].startswith(f"bootstrap:{_BOOT_HOSTNAME}:20"), upload
        # The owner check ran, with the privileged token, before the upload.
        idx = calls.index(upload)
        owner_checks = [c for c in calls[:idx] if c["argv"] == ["api", "user"]]
        assert any(c["token"] == "privileged" for c in owner_checks), (
            f"no `api user` with the privileged token preceded the upload: {calls[:idx]}"
        )
        # The bot token never reached a key endpoint.
        assert all(
            c["token"] == "privileged"
            for c in calls
            if c["argv"][:1] == ["ssh-key"] or c["argv"][:2] == ["api", "/user/keys"]
        ), calls

        target.succeed(f"git config --global --add safe.directory {ORIGIN}")
        subject = target.succeed(f"git -C {ORIGIN} log -1 --format=%s main").strip()
        assert f"harvest+rekey for {_BOOT_HOSTNAME}" in subject, (
            f"unexpected HEAD subject: {subject!r}"
        )
        author = target.succeed(f"git -C {ORIGIN} log -1 --format=%an main").strip()
        assert author == "Jacob Brugh", (
            f"privileged harvest must be authored by the jacobbrugh account, got {author!r}"
        )

        # This host is initrd-key, so its harvested boot.nix carries the
        # initramfs-keyfile stanza and no `boot.initrd.clevis` unlock stanza
        # (its comments do mention clevis — that a TPM could be fitted later —
        # so this must match the stanza, not the bare word). The secret source
        # must be a quoted STRING: an unquoted Nix path would copy the passphrase
        # into the world-readable store instead of reading it off the target.
        harvested_boot = target.succeed(
            f"git -C {ORIGIN} show main:config/hosts/{_BOOT_HOSTNAME}/boot.nix"
        )
        assert (
            'boot.initrd.secrets."/crypt-key" = "/etc/luks-initrd-key";'
            in harvested_boot
        ), (
            f"harvested boot.nix lacks the quoted initrd-secret source: {harvested_boot!r}"
        )
        assert "boot.initrd.clevis" not in harvested_boot, (
            f"initrd-key host harvested a clevis unlock stanza: {harvested_boot!r}"
        )
        hardware_after_leg1 = target.succeed(
            f"git -C {ORIGIN} show main:config/hosts/{_BOOT_HOSTNAME}/hardware.nix"
        )
        # flash staged the extra config at /etc/nixos/networking.nix; register
        # must harvest it so the post-switch dotfiles system keeps the test
        # instrumentation (backdoor + serial-getty@hvc0 disabled).
        harvested_net = target.succeed(
            f"git -C {ORIGIN} show main:config/hosts/{_BOOT_HOSTNAME}/networking.nix"
        )
        assert "test-instrumentation" in harvested_net, (
            "harvested networking.nix lacks the test-instrumentation import"
        )

        sops_yaml = target.succeed(f"git -C {ORIGIN} show main:.sops.yaml")
        assert f"host_{_BOOT_HOSTNAME}" in sops_yaml, (
            f".sops.yaml missing host anchor: {sops_yaml!r}"
        )
        # In every rule the privileged anchor is in — secrets.yaml, bot-secrets,
        # the logscrub literals, the eval fixtures — and in none it is not.
        assert_host_in_exactly_the_tier_rules(
            sops_yaml, f"host_{_BOOT_HOSTNAME}", "bootstrap_privileged"
        )
        # …anchoring exactly the key this disk holds…
        pubkey_after_leg1 = host_anchor(sops_yaml, _BOOT_HOSTNAME)
        assert pubkey_after_leg1 == host_pubkey(target)
        # …and the switched system could actually decrypt what that bought it.
        target.succeed("test -s /run/secrets/logscrub-literals")
        target.succeed("test -s /run/secrets/attic_client_token")
        assert_healthy(target, origin=ORIGIN)
        head_after_leg1 = target.succeed(f"git -C {ORIGIN} rev-parse main").strip()

        # ── Leg 2: a completed host, run again, changes nothing ──────────────
        # The same wrapped CLI the unit ran, as the same user, in the dotfiles
        # generation: post shredded the bootstrap key, so this run has to
        # decide it is done from the host's own key and the clone, and touch
        # neither the origin nor the generation.
        print("[e2e-privileged] re-running the bootstrap on the completed host")
        target.succeed(
            "systemd-run --wait --collect --pipe --uid=jacob --gid=users "
            "-p WorkingDirectory=/home/jacob "
            "-E PATH=/run/wrappers/bin:/run/current-system/sw/bin "
            "-E BOOTSTRAP_FLAKE=git+file:///mnt/shared/bootstrap-source "
            f"-E BOOTSTRAP_DOTFILES_REMOTE=file://{ORIGIN} "
            f"-E BOOTSTRAP_E2E_GH_LOG={GH_LOG} "
            f"{exec_start} 2>&1 | tee /root/rerun.log",
            timeout=1800,
        )
        target.succeed("grep -q 'bootstrap complete' /root/rerun.log")
        assert (
            target.succeed(f"git -C {ORIGIN} rev-parse main").strip() == head_after_leg1
        ), "a re-run of a registered host pushed a commit — register isn't idempotent"
        assert_healthy(target, origin=ORIGIN)

        # ── Leg 3: re-flash the same target from the same media ──────────────
        # The operator action that regenerates a host's identity: the disk is
        # wiped, the origin is not. The fresh disk's register must replace the
        # previous key under this host's anchor, seal the secrets to the new
        # one, re-harvest the regenerated hardware config, and come up healthy.
        print("[e2e-privileged] re-flashing the boot target")
        target.shutdown()
        machine.start()
        machine.wait_for_unit("multi-user.target")
        machine.succeed(
            env + f"bootstrap-flash {_BOOT_HOSTNAME} --disk /dev/vdd",
            timeout=3600,
        )
        _stage_target_netrc(machine, mapper="reflash-verify", mountpoint="/mnt-reflash")
        machine.shutdown()

        target.start()
        target.wait_for_unit("multi-user.target", timeout=900)
        reboot_at = _now(target)
        console = target.get_console_log()
        assert "Starting password query" not in console, (
            "the re-flashed target asked for a LUKS passphrase. Console:\n"
            f"{console[-4000:]}"
        )
        target.wait_for_unit("bootstrap-e2e-fixture.service")
        target.fail(f"test -e {HOST_KEY}")
        _wait_for_bootstrap(target, since=reboot_at)
        target.fail("test -f /var/lib/bootstrap/age-key")

        target.succeed(f"git config --global --add safe.directory {ORIGIN}")
        sops_yaml = target.succeed(f"git -C {ORIGIN} show main:.sops.yaml")
        pubkey_after_reflash = host_anchor(sops_yaml, _BOOT_HOSTNAME)
        assert pubkey_after_reflash != pubkey_after_leg1, (
            "the re-flashed host kept the previous disk's key under its anchor"
        )
        assert pubkey_after_reflash == host_pubkey(target)
        assert_host_in_exactly_the_tier_rules(
            sops_yaml, f"host_{_BOOT_HOSTNAME}", "bootstrap_privileged"
        )
        hardware_after_reflash = target.succeed(
            f"git -C {ORIGIN} show main:config/hosts/{_BOOT_HOSTNAME}/hardware.nix"
        )
        assert hardware_after_reflash != hardware_after_leg1, (
            "the re-flash regenerated the disk but origin still carries the old hardware.nix"
        )
        assert_healthy(target, origin=ORIGIN)

        print("[bootstrap-e2e-privileged] PASSED")
    finally:
        target.crash()


run()
