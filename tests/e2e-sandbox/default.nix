# e2e-sandbox — the sandbox tier, end to end, on an already-installed NixOS VM:
#   the full production bootstrap (prereqs → secret-zero → ssh → register →
#   switch → post) as jacob, with the sandbox bootstrap age key and a
#   sandbox-tagged registry entry; then a second run for idempotency.
#
# What the tier means here: mock-gh logs every GitHub call, and the test
# asserts the ssh phase uploaded NO key to any account and that the host was
# added to exactly the dotfiles sops rules the sandbox anchor is in.
#
# Fixtures come from /tmp/bootstrap-e2e-shared/sandbox, staged by
# `bootstrap-e2e-stage sandbox` on the CI daemon host (python/src/bootstrap/ci).
{
  pkgs,
  sops-nix,
}:

let
  mockGh = pkgs.callPackage ../e2e-lib/mock-gh.nix { };

  fakeHardwareConfig = ''
    { lib, ... }:
    {
      fileSystems."/" = {
        device = "/dev/vda";
        fsType = "ext4";
      };
      boot.loader.grub = {
        enable = true;
        devices = [ "nodev" ];
      };
    }
  '';

  mkTest =
    { bootstrap, src }:
    let
      bootstrapForTest = pkgs.callPackage ../e2e-lib/bootstrap-for-test.nix {
        inherit bootstrap mockGh;
      };
    in
    pkgs.testers.nixosTest {
      name = "bootstrap-e2e-sandbox";

      nodes.machine =
        { lib, ... }:
        {
          # e2e0, not a literal name: the registry fixture registers this host
          # via an indexed template ([e2e] endIndex = 0) plus an [e2e0] tag
          # override, so the e2e exercises the same template-expansion path
          # fromRegistry.nix uses for the real fleet's indexed hosts.
          networking.hostName = "e2e0";

          virtualisation.diskSize = 65536;
          virtualisation.memorySize = 16384;
          virtualisation.cores = 8;
          # The store as an erofs image plus a writable layer on the root
          # disk, not an overlay over the host store on 9p: every small-file
          # store write (a nixpkgs unpack, a leaf build) then costs a disk
          # write, not a 9p round-trip. writableStore defaults to
          # mountHostNixStore, which this turns off, so it is set explicitly.
          virtualisation.useNixStoreImage = true;
          virtualisation.writableStore = true;
          virtualisation.writableStoreUseTmpfs = false;

          # Outbound internet rides eth0's qemu SLIRP NAT. Do NOT rely on the
          # SLIRP DNS proxy (10.0.2.3): it forwards to the build host's
          # resolver, which on the CI daemon stopped answering guest
          # queries (first seen 2026-07-20; green through 2026-06-09). Static
          # public resolvers go straight out the NAT as ordinary UDP, keeping
          # the test independent of host resolver state.
          networking.resolvconf.enable = false;
          environment.etc."resolv.conf".text = ''
            nameserver 1.1.1.1
            nameserver 8.8.8.8
          '';

          users.users.jacob = {
            isNormalUser = true;
            extraGroups = [ "wheel" ];
            password = "";
          };
          security.sudo.wheelNeedsPassword = false;

          environment.systemPackages = with pkgs; [
            # The mock-wrapped CLI is invoked by absolute store path from the
            # test script (testScript prelude), not via PATH, so it need not be
            # installed here — but git/sops/age are used by the script directly.
            git
            openssh
            sops
            age
            curl
          ];

          nix.settings = {
            experimental-features = [
              "nix-command"
              "flakes"
            ];
            extra-system-features = [ "kvm" ];
            extra-sandbox-paths = [ "/dev/kvm" ];
            trusted-users = [ "@wheel" ];
            # The staged binary cache (bootstrap-e2e-stage): the dotfiles CI
            # closure, both flakes' inputs, the installer toplevel. Consulted
            # before attic and cache.nixos.org; unsigned, hence require-sigs.
            extra-substituters = [ "file:///mnt/cache?priority=5" ];
            require-sigs = false;
            max-jobs = 2;
            cores = 8;
          };

          virtualisation.sharedDirectories = {
            e2e = {
              # Per-tier subdirectory of the hostPath the CI pod and daemon share.
              source = "/tmp/bootstrap-e2e-shared/sandbox";
              target = "/mnt/shared";
            };
            cache = {
              source = "/tmp/bootstrap-e2e-shared/cache";
              target = "/mnt/cache";
            };
          };

          system.activationScripts.fakeNixosInstallArtifacts = {
            deps = [ ];
            text = ''
              install -d -m 0755 /etc/nixos
              cat > /etc/nixos/hardware-configuration.nix <<'NIXEOF'
              ${fakeHardwareConfig}
              NIXEOF
              cat > /etc/nixos/networking.nix <<'NIXEOF'
              { lib, modulesPath, ... }:
              {
                imports = [ (modulesPath + "/testing/test-instrumentation.nix") ];
                # Same static-resolver override as the pre-switch node config
                # (see nodes.machine above): this file is harvested into the
                # dotfiles host config, so the post-switch system keeps DNS
                # that bypasses the broken SLIRP DNS proxy — and the staged
                # binary cache, so the switch substitutes from it too.
                networking.resolvconf.enable = lib.mkForce false;
                environment.etc."resolv.conf".text = "nameserver 1.1.1.1\nnameserver 8.8.8.8\n";
                nix.settings = {
                  max-jobs = 2;
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
                  options = [ "trans=virtio" "version=9p2000.L" "msize=104857600" ];
                  neededForBoot = false;
                };
                fileSystems."/mnt/cache" = {
                  device = "cache";
                  fsType = "9p";
                  options = [ "trans=virtio" "version=9p2000.L" "msize=104857600" "nofail" ];
                  neededForBoot = false;
                };
              }
              NIXEOF
            '';
          };

          systemd.tmpfiles.rules = [
            "d /var/lib/bootstrap 0710 root wheel - -"
          ];
        };

      # SOURCE ties the test derivation to the flake source the VM consumes
      # at run time (see flake.nix, the e2e packages).
      testScript = ''
        BOOTSTRAP = "${bootstrapForTest}/bin/bootstrap"
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
