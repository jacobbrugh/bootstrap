# The first-boot unit a flashed host runs itself with.
#
# bootstrap-flash stages everything the bootstrap needs — the age key at
# /var/lib/bootstrap/age-key, the flake source at /etc/bootstrap-flake, the
# generated hardware config and boot.nix — and reboots into the scaffold
# system. This unit is what turns that scaffold into a fleet member without
# anyone at the console: it runs the full `bootstrap` (prereqs → secret_zero →
# ssh → register → switch → post) as the operator user on the first boot, and
# on every boot after that until it has succeeded.
#
# Imported only by the host-hardware.nix wrapper bootstrap-flash splices in
# (lib/boot_config.py flake_wrapper). It must NOT be part of
# bootstrap-secret-zero-minimal.nix: that module also backs the WSL system, and
# `#bootstrap-secret-zero` is what phases/nixos/secret_zero.py switches an
# already-installed host onto from *inside* a running bootstrap — a unit wanted
# by multi-user.target there would be started by that very switch and launch a
# second, concurrent bootstrap.
{
  bootstrap,
  config,
  lib,
  ...
}:
let
  cfg = config.bootstrap.firstboot;
in
{
  # The option exists as a switch, and as a MARKER: its declaration is present
  # only in an evaluation that imports this module — the scaffold generation —
  # and absent from the dotfiles generation the bootstrap switches to. The usb
  # e2e keys its scaffold-only fixture on `options ? bootstrap.firstboot`, so
  # nothing test-only follows the harvested networking.nix into dotfiles and
  # redefines this unit there (a redefined unit is *changed*, not *removed*,
  # and switch-to-configuration stops a changed unit regardless of
  # X-StopOnRemoval).
  options.bootstrap.firstboot.enable = lib.mkEnableOption "the first-boot bootstrap unit" // {
    default = true;
  };

  config = lib.mkIf cfg.enable {
    # In the closure, so first boot evaluates no flake and builds no Python
    # before the bootstrap can start, and `bootstrap` is on PATH for a manual
    # re-run from the console.
    environment.systemPackages = [ bootstrap ];

    # The first switch builds from source everything in the target's closure
    # not on cache.nixos.org — the flashed scaffold has no fleet attic — and on
    # a small host that is a lot (a bare-metal router with 8 GB compiles its
    # custom Rust/Go/C packages and their nightly-toolchain dep trees). The
    # stock nix.settings — max-jobs=auto (one build per core, each grabbing
    # every thread) with no swap — runs those builds 4-8 wide and OOM-kills the
    # bootstrap (a router did: systemd-oomd at a 14.4 G peak). Serialize the build to
    # one derivation at a time and give it real swap headroom so the first boot
    # COMPLETES rather than dies; a serial build is slower but it is the
    # unattended, retry-until-success first boot, where finishing beats speed.
    #
    # Both are scaffold-only (this whole module is mkIf'd on the scaffold-only
    # bootstrap.firstboot option and removed by the switch it triggers), so
    # neither reaches the dotfiles generation — the host keeps its own
    # nix.settings from then on. Serializing (max-jobs=1) is what actually caps
    # peak memory; zram is compressed in-RAM swap that needs no disk (a fixed
    # swapfile would risk overflowing a small target's or the e2e VM's disk) and
    # carries no plaintext-leak of its own, giving the one running build spike
    # headroom.
    nix.settings.max-jobs = 1;
    zramSwap = {
      enable = lib.mkDefault true;
      memoryPercent = lib.mkDefault 150;
    };

    systemd.services.bootstrap-firstboot = {
      description = "Run the bootstrap on a freshly flashed host";
      wantedBy = [ "multi-user.target" ];
      # After multi-user.target so a long bootstrap does not hold the target
      # (and everything that waits on it) in "starting" for its whole duration;
      # network-online.target is real here — networking.useDHCP gives dhcpcd,
      # which is before= network-online.target and waits for an address.
      after = [
        "multi-user.target"
        "network-online.target"
      ];
      requires = [ "network-online.target" ];
      # /run/wrappers for the setuid sudo the switch and post phases call;
      # /run/current-system/sw for git, ssh, nix.
      path = [
        "/run/wrappers"
        "/run/current-system/sw"
      ];
      restartIfChanged = false;
      unitConfig = {
        # post shreds the key on success; its absence is the "done" signal, so
        # a stale scaffold generation booted from the systemd-boot menu stays
        # inert.
        ConditionPathExists = "/var/lib/bootstrap/age-key";
        # The bootstrap's own switch phase activates the dotfiles generation,
        # which does not define this unit. switch-to-configuration stops every
        # active unit whose file is missing from the new generation — which
        # would SIGTERM this cgroup mid-switch, before post ever ran — unless the
        # unit opts out with this flag. Same idiom as nixpkgs' amazon-init,
        # digital-ocean-init and nixos-upgrade, the other units that run
        # `nixos-rebuild switch` on themselves.
        X-StopOnRemoval = false;
        # A router whose WAN is still coming up must keep retrying. Every phase
        # decides "done" from its own post-condition, never from a marker, so a
        # retry after any failure repairs rather than skips: the e2es prove it by
        # failing register before its clone (privileged) and after its key
        # generation (sandbox) and retrying, re-running a completed host, and
        # re-flashing the same target (tests/e2e-{privileged,sandbox}).
        StartLimitIntervalSec = 0;
      };
      serviceConfig = {
        # Type=exec, not oneshot: the start job must COMPLETE before the
        # switch phase, not span it. A oneshot's start job stays pending for
        # the whole of ExecStart, so it is still pending when the bootstrap's
        # own `nixos-rebuild switch` re-execs systemd (systemctl daemon-reexec).
        # The dotfiles generation this unit switches to does not define it
        # (scaffold-only, by design), so at the re-exec the unit file is already
        # gone and systemd re-derives the start timeout from the 90s default and
        # fires it — SIGTERMing the bootstrap mid-switch. A per-unit
        # TimeoutStartSec cannot help: the file carrying it has been removed.
        # Type=exec completes the start job the instant ExecStart execs, so by
        # switch time there is no pending start job to time out. RemainAfterExit
        # keeps the unit active after the bootstrap exits 0; Restart=on-failure
        # still retries a bootstrap that *exits* nonzero (e.g. WAN still down).
        Type = "exec";
        RemainAfterExit = true;
        # The operator user, never root: register writes this host's own age
        # key to ~/.config/sops/age/keys.txt and the dotfiles sops config reads
        # it from jacob's home. A root run strands it in /root and the switch
        # dies in setupSecrets. User= makes systemd set $HOME, which the CLI's
        # path constants derive from.
        User = "jacob";
        WorkingDirectory = "/home/jacob";
        Restart = "on-failure";
        RestartSec = "5min";
        # Bare `bootstrap`: the hostname resolves from the system hostname the
        # flash wrapper baked in.
        ExecStart = lib.getExe bootstrap;
      };
    };
  };
}
