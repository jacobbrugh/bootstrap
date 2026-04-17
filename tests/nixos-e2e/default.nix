# End-to-end bootstrap test. Boots a fresh NixOS VM, runs the entire
# production 6-phase bootstrap flow inside it, and activates a real
# `nixosConfigurations.e2e-sandbox` from the user's actual dotfiles
# flake (jacobbrugh/dotfiles) via `sudo nixos-rebuild switch`. Not a
# fixture mock, not a classic configuration.nix — the real flake.
#
# Intentional divergences from a real operator's bootstrap run:
#   - `gh` is a PATH-prepended writeShellScriptBin stub (no GitHub API).
#   - `BOOTSTRAP_DOTFILES_REMOTE` points at a local bare clone rather
#     than `git@github.com:jacobbrugh/dotfiles.git` — so no real push
#     downstream.
# Everything else is production: sops-nix at Phase 0 decrypts the
# bootstrap github token, register does real sops updatekeys + commit
# + push, nixos-rebuild switch activates a real
# `nixosConfigurations.e2e-sandbox`, sops-nix on the activated system
# decrypts the sandbox tag's bot-secrets.
#
# Runtime inputs (the real dotfiles checkout + the sandbox bootstrap
# age key) arrive via a qemu 9p share mounted at /mnt/shared. The
# hosting GHA workflow populates /tmp/bootstrap-e2e-shared before
# launching the test driver; this file specifies the destination side.

{
  pkgs,
  # sops-nix flake input, passed in from flake.nix so the test VM can
  # import the NixOS module + decrypt `secrets/bootstrap-secrets.sops.yaml`
  # at activation time the same way Phase 0 does.
  sops-nix,
}:

let
  mockGh = pkgs.callPackage ./mock-gh.nix { };

  mkTest =
    { bootstrap }:
    let
      bootstrapForTest = pkgs.callPackage ./bootstrap-for-test.nix {
        inherit bootstrap mockGh;
      };
    in
    pkgs.testers.nixosTest {
      name = "bootstrap-e2e-sandbox";

      nodes.machine =
        { ... }:
        {
          imports = [ sops-nix.nixosModules.sops ];
          # `bootstrap/lib/host_info.py:67-94` detects hostname via
          # `hostname -s` on Linux. nixos-rebuild picks
          # `nixosConfigurations.$(hostname)` by default. Setting this
          # means ctx.hostname and the rebuild's selector both resolve
          # to `e2e-sandbox` — the registry entry + nixosConfiguration
          # that register will add to the real flake during the test.
          networking.hostName = "e2e-sandbox";

          users.users.jacob = {
            isNormalUser = true;
            extraGroups = [ "wheel" ];
            password = "";
          };
          security.sudo.wheelNeedsPassword = false;

          environment.systemPackages = with pkgs; [
            bootstrapForTest
            git
            sops
            age
            openssh
          ];

          # nix.conf verbatim copy of the dotfiles workflow's
          # nixos-build-and-deploy.yml lines 57-67. Attic priority 10
          # (vs. cache.nixos.org's default 40) so the VM hits Attic
          # first for any closure already pushed from a previous build
          # — which is every registered sandbox host's toplevel.
          #
          # The netrc (ATTIC_TOKEN) lives on the shared 9p mount rather
          # than in the nix store; the workflow writes it to
          # /tmp/bootstrap-e2e-shared/nix-netrc before the test runs.
          nix.settings = {
            experimental-features = [
              "nix-command"
              "flakes"
            ];
            extra-substituters = [ "https://cache.kube.jacobbrugh.net/attic?priority=10" ];
            extra-trusted-public-keys = [
              "attic:TGN7u8ffZ1H01LvNYlpV4FgyRYRpaoG9CxtSHhOYRgY="
            ];
            netrc-file = "/mnt/shared/nix-netrc";
            extra-system-features = [ "kvm" ];
            extra-sandbox-paths = [ "/dev/kvm" ];
            trusted-users = [ "@wheel" ];
          };

          # 9p share from the host running the test driver. Source side
          # is a fixed string so nothing in the nix store depends on
          # the VM being invoked with secrets at build time — the
          # workflow populates it at test run time (dotfiles checkout +
          # sandbox-key + nix-netrc) and qemu surfaces it inside the VM.
          # Key name is `e2e` (not `shared`) because nixosTest already
          # claims the `shared` key for its default /tmp/shared.
          virtualisation.sharedDirectories.e2e = {
            source = "/tmp/bootstrap-e2e-shared";
            target = "/mnt/shared";
          };

          # sops-nix: same bundled sops file Phase 0 uses, plus the
          # operator-pre-staged bootstrap age key — which the 9p share
          # surfaces at /mnt/shared/sandbox-key. Pointing sops.age.keyFile
          # directly at the share avoids a copy step + activation-ordering
          # fight with sops-nix's setup-secrets service.
          sops.defaultSopsFile = ../../secrets/bootstrap-secrets.sops.yaml;
          sops.age.keyFile = "/mnt/shared/sandbox-key";
          sops.secrets.bootstrap-github-token = {
            key = "github_token";
            mode = "0440";
            owner = "root";
            group = "wheel";
          };
        };

      testScript = ''
        start_all()
        machine.wait_for_unit("multi-user.target")

        # --- Stage runtime inputs from the shared 9p mount ---------------
        # Dotfiles: `cp -r` from /nix/store-style read-only share into a
        # writable home path so the register phase can commit + push.
        machine.succeed("cp -r /mnt/shared/dotfiles /home/jacob/dotfiles")
        machine.succeed("chown -R jacob:users /home/jacob/dotfiles")
        machine.succeed("chmod -R u+w /home/jacob/dotfiles")

        # The checkout's git config needs user.email/user.name for any
        # register-internal commit that happens before the GIT_AUTHOR_*
        # env overrides kick in (git's safety.directory gate, or
        # anything that reads local config).
        machine.succeed(
            "su - jacob -c 'git -C /home/jacob/dotfiles config user.email fixture@example.com'"
        )
        machine.succeed(
            "su - jacob -c 'git -C /home/jacob/dotfiles config user.name \"E2E Fixture\"'"
        )

        # Local bare clone as the push destination. Drop/replace any
        # pre-existing `origin` remote that the real dotfiles checkout
        # brought along (git@github.com:jacobbrugh/dotfiles.git), so
        # the register phase pushes to our ephemeral bare instead of
        # trying to reach real GitHub.
        machine.succeed(
            "su - jacob -c 'git clone --quiet --bare /home/jacob/dotfiles /home/jacob/origin.git'"
        )
        machine.succeed(
            "su - jacob -c 'git -C /home/jacob/dotfiles remote remove origin || true'"
        )
        machine.succeed(
            "su - jacob -c 'git -C /home/jacob/dotfiles remote add origin /home/jacob/origin.git'"
        )

        # At this point the VM has already activated with sops-nix; the
        # plaintext token is at /run/secrets/bootstrap-github-token
        # (sops-nix read the age key at /mnt/shared/sandbox-key and
        # decrypted secrets/bootstrap-secrets.sops.yaml at boot). The
        # bootstrap CLI reads that file on its own — no env override.
        machine.succeed("test -r /run/secrets/bootstrap-github-token")

        # Snapshot the pre-switch system so we can assert the switch
        # phase actually transitioned to a new toplevel.
        before_system = machine.succeed("readlink /run/current-system").strip()

        # --- Run production bootstrap (all 6 phases) ---------------------
        # No `BOOTSTRAP_HOSTNAME` (VM's `networking.hostName` drives it
        # via `hostname -s`). No `BOOTSTRAP_SKIP_RENAME` (no-op on
        # NixOS). Bootstrap reads the sops-nix-written token from
        # /run/secrets/bootstrap-github-token; mock `gh` answers api
        # calls.
        machine.succeed(
            "su - jacob -c '"
            "export BOOTSTRAP_CANONICAL_DOTFILES=/home/jacob/dotfiles && "
            "export BOOTSTRAP_DOTFILES_REMOTE=/home/jacob/origin.git && "
            "export BOOTSTRAP_SANDBOX=1 && "
            "bootstrap --non-interactive'",
            timeout=1800,
        )

        # --- Register-phase assertions ----------------------------------
        log_subject = machine.succeed(
            "su - jacob -c 'git -C /home/jacob/origin.git log -1 --format=%s main'"
        ).strip()
        assert "register host e2e-sandbox" in log_subject, (
            f"expected 'register host e2e-sandbox' in HEAD subject, got: {log_subject!r}"
        )

        sops_yaml = machine.succeed(
            "su - jacob -c 'git -C /home/jacob/origin.git show main:.sops.yaml'"
        )
        assert "host_e2e-sandbox" in sops_yaml, (
            f".sops.yaml missing host_e2e-sandbox anchor: {sops_yaml!r}"
        )

        # Parse the two creation_rule blocks and confirm sandbox
        # isolation: anchor lands in bot-secrets.yaml but NOT
        # secrets.yaml (Chunk A's _NON_SENSITIVE_TAGS logic).
        def _rule_block(doc: str, marker: str) -> str:
            after = doc.split(marker, 1)[1]
            nxt = after.find("\n  - path_regex:")
            return after if nxt < 0 else after[:nxt]

        bot_block = _rule_block(sops_yaml, "path_regex: 'nix/bot-secrets.yaml$'")
        secrets_block = _rule_block(sops_yaml, "path_regex: 'nix/secrets.yaml$'")
        assert "host_e2e-sandbox" in bot_block, (
            f"host_e2e-sandbox SHOULD be in bot-secrets.yaml creation_rule. "
            f"block:\n{bot_block!r}"
        )
        assert "host_e2e-sandbox" not in secrets_block, (
            f"host_e2e-sandbox MUST NOT be in secrets.yaml creation_rule. "
            f"block:\n{secrets_block!r}"
        )

        registry = machine.succeed(
            "su - jacob -c 'git -C /home/jacob/origin.git show main:nix/config/hosts/registry.toml'"
        )
        assert "[e2e-sandbox]" in registry, (
            f"registry.toml missing [e2e-sandbox] entry: {registry!r}"
        )

        # /etc/nixos symlink is `_ensure_symlink`'s job. The symlink
        # target is the canonical checkout path.
        nixos_target = machine.succeed("readlink /etc/nixos").strip()
        assert nixos_target == "/home/jacob/dotfiles", (
            f"/etc/nixos should point at /home/jacob/dotfiles, got: {nixos_target!r}"
        )

        # --- Switch-phase assertions ------------------------------------
        after_system = machine.succeed("readlink /run/current-system").strip()
        assert after_system != before_system, (
            f"/run/current-system did not change — switch didn't activate a new "
            f"toplevel. still {after_system!r}"
        )

        # Sandbox tag's sops-nix secrets decrypted + placed on disk. The
        # three come from `nix/config/tags/sandbox.nix`:
        #   headscale_preauth_key → /run/secrets/headscale_preauth_key
        #   k3s_kubeconfig_*      → /etc/kubernetes/kubeconfig
        #   argocd_ci_observer_*  → /etc/argocd/token
        machine.succeed("test -s /run/secrets/headscale_preauth_key")
        machine.succeed("test -s /etc/kubernetes/kubeconfig")
        machine.succeed("test -s /etc/argocd/token")

        # Sandbox tag explicitly disables attic-watch-store.service.
        rc, _ = machine.execute("systemctl is-active --quiet attic-watch-store.service")
        assert rc != 0, (
            "attic-watch-store.service should NOT be active on a sandbox host "
            "(the sandbox tag disables it)"
        )

        print("[bootstrap-e2e-sandbox] PASSED")
      '';
    };
in
{
  inherit mkTest;
}
