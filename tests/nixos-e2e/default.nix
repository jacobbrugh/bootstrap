# End-to-end bootstrap test on a fresh NixOS VM. Runs the ENTIRE
# 6-phase flow (prereqs → onepassword → ssh → register → switch → post),
# not just register.
#
# Exposes two `pkgs.testers.nixosTest` checks:
#   - `nixos-e2e-devbox`   — devbox tier; secrets.yaml gets the anchor.
#   - `nixos-e2e-sandbox`  — sandbox tier (BOOTSTRAP_SANDBOX=1); the
#                            "sandbox" tag forces skipping secrets.yaml
#                            and the anchor must NOT land in its
#                            creation_rule.
#
# Each scenario:
#   1. Builds fresh bootstrap age key + variant-specific encrypted sops
#      file + fixture dotfiles with `configuration.nix` + `post-switch-
#      module.nix`. fixture.nix also pre-builds the post-switch toplevel
#      and exposes it as `passthru.postSwitchToplevel`.
#   2. Builds `bootstrapForTest` — production bootstrap with the test
#      sops file substituted and mock `gh` prepended to PATH.
#   3. Boots a NixOS VM with:
#        - bootstrapForTest + runtime tools on PATH
#        - the pre-built post-switch toplevel in /nix/store (via
#          `virtualisation.additionalPaths`) so the switch phase has
#          nothing to actually build — just activate.
#        - `nix.nixPath` pointing at the host nixpkgs source so classic
#          `nixos-rebuild switch` can resolve `<nixpkgs/nixos>`.
#   4. Stages the bootstrap age key + fixture checkout + local bare
#      origin in /home/jacob.
#   5. Runs `bootstrap --non-interactive` (the full orchestrator) and
#      asserts the end state:
#        - register phase committed + pushed (registry.toml, .sops.yaml
#          changes in the bare origin's HEAD).
#        - devbox|sandbox semantics hold (anchor in secrets.yaml iff
#          devbox).
#        - the switch phase actually activated the new system: the
#          post-switch marker file `/etc/bootstrap-e2e-marker` exists
#          with the expected content. This is the "full sandboxed
#          host" proof.

{ pkgs }:

let
  mockGh = pkgs.callPackage ./mock-gh.nix { };

  # Scenario builder. Caller supplies the already-built production
  # `bootstrap` package (so we don't have to re-plumb `self` + flake
  # inputs into this file) plus the tier-specific variant + env flag
  # + expected sandbox-anchor assertion.
  mkTest =
    {
      bootstrap,
      variant,
      sandboxEnv,
      assertSandboxAnchorSkipped,
    }:
    let
      fixture = pkgs.callPackage ./fixture.nix { inherit variant; };
      bootstrapForTest = pkgs.callPackage ./bootstrap-for-test.nix {
        inherit
          bootstrap
          fixture
          mockGh
          variant
          ;
      };
    in
    pkgs.testers.nixosTest {
      name = "bootstrap-e2e-${variant}";

      nodes.machine =
        { ... }:
        {
          environment.systemPackages = with pkgs; [
            bootstrapForTest
            git
            sops
            age
            openssh
          ];

          users.users.jacob = {
            isNormalUser = true;
            extraGroups = [ "wheel" ];
            password = "";
          };
          security.sudo.wheelNeedsPassword = false;

          programs.git.enable = true;
          services.openssh.enable = false;

          # classic nixos-rebuild resolves `<nixpkgs>` via NIX_PATH.
          # Pin it to the exact store path fixture.nix pre-built the
          # toplevel against, so re-evaluation inside the VM hashes to
          # the cached toplevel rather than trying to fetch anything.
          nix.nixPath = [ "nixpkgs=${pkgs.path}" ];
          nix.settings.experimental-features = [
            "nix-command"
            "flakes"
          ];
          # Pure offline evaluation — fail loudly if anything tries to
          # reach the internet instead of silently timing out.
          nix.settings.substituters = pkgs.lib.mkForce [ ];
          nix.settings.trusted-substituters = pkgs.lib.mkForce [ ];

          # Stage everything nixos-rebuild switch will need into the
          # VM's /nix/store:
          #   - the pre-built post-switch toplevel (what switch will
          #     activate to — identical hash means no rebuild)
          #   - the host nixpkgs source (what `<nixpkgs/nixos>`
          #     resolves to for evaluation)
          virtualisation.additionalPaths = [
            fixture.postSwitchToplevel
            pkgs.path
          ];

          virtualisation = {
            cores = 2;
            memorySize = 4096;
            diskSize = 8192;
          };
        };

      testScript = ''
        start_all()
        machine.wait_for_unit("multi-user.target")

        # --- Stage age key + fixture checkout + bare origin --------------
        machine.succeed("mkdir -p /home/jacob/.config/sops/age")
        machine.succeed("cp ${fixture}/bootstrap-age-key.txt /home/jacob/sops-age-key.txt")
        machine.succeed("chown -R jacob:users /home/jacob/.config")
        machine.succeed("chown jacob:users /home/jacob/sops-age-key.txt")
        machine.succeed("chmod 600 /home/jacob/sops-age-key.txt")

        # `cp -r` from /nix/store preserves 0444/0555 read-only perms;
        # chmod -R u+w after chown so jacob can actually write.
        machine.succeed("cp -r ${fixture}/checkout /home/jacob/dotfiles")
        machine.succeed("chown -R jacob:users /home/jacob/dotfiles")
        machine.succeed("chmod -R u+w /home/jacob/dotfiles")
        machine.succeed(
            "su - jacob -c 'cd /home/jacob/dotfiles && "
            "git init --quiet --initial-branch=main && "
            "git config user.email fixture@example.com && "
            "git config user.name  \"Fixture User\" && "
            "git add -A && "
            "git commit --quiet -m \"initial fixture\"'"
        )
        machine.succeed("su - jacob -c 'git clone --quiet --bare /home/jacob/dotfiles /home/jacob/origin.git'")
        machine.succeed("su - jacob -c 'cd /home/jacob/dotfiles && git remote add origin /home/jacob/origin.git'")

        # --- Probe nixos-rebuild directly first (debug aid) --------------
        # nixos-rebuild's full error dump is >400 chars and bootstrap's
        # ShellError truncates stderr at 400 chars, so actual eval
        # failures get hidden behind "… while" mid-trace. Run it
        # manually first with --show-trace, print the whole output, and
        # fail the test WITHOUT the truncation if eval is broken.
        machine.succeed("ln -sfn /home/jacob/dotfiles /etc/nixos-probe")
        probe_cmd = (
            "NIX_PATH=nixpkgs=${pkgs.path}:nixos-config=/etc/nixos-probe/configuration.nix "
            "sudo -E nixos-rebuild build --show-trace 2>&1"
        )
        rc, probe_out = machine.execute(probe_cmd)
        print("=== nixos-rebuild build probe (rc=", rc, ") ===", flush=True)
        print(probe_out, flush=True)
        print("=== end probe ===", flush=True)
        assert rc == 0, f"nixos-rebuild build probe failed (rc={rc}); see output above"

        # --- Run the full bootstrap (all 6 phases) -----------------------
        #
        # No BOOTSTRAP_FLAKE_SYMLINK_PATH override: we WANT register's
        # _ensure_symlink to install /etc/nixos → /home/jacob/dotfiles
        # so the subsequent `sudo nixos-rebuild switch` in the switch
        # phase reads /home/jacob/dotfiles/configuration.nix by default.
        #
        # SOPS_AGE_KEY_FILE drives the headless production path. The
        # bundled bootstrap-secrets-${variant}.sops.yaml in
        # bootstrapForTest is encrypted to this exact key; mock `gh`
        # answers api calls from the ssh + register phases.
        hostname = "test-e2e-${variant}"
        machine.succeed(
            "su - jacob -c '"
            "export SOPS_AGE_KEY_FILE=/home/jacob/sops-age-key.txt && "
            "export BOOTSTRAP_CANONICAL_DOTFILES=/home/jacob/dotfiles && "
            "export BOOTSTRAP_DOTFILES_REMOTE=/home/jacob/origin.git && "
            f"export BOOTSTRAP_HOSTNAME={hostname} && "
            "export BOOTSTRAP_SKIP_RENAME=1 && "
            "export BOOTSTRAP_SANDBOX=${sandboxEnv} && "
            "bootstrap --non-interactive'",
            timeout=600,
        )

        # --- Post-switch sanity: the new system actually activated -------
        # The post-switch-module.nix we pre-built and pre-staged declares
        # `environment.etc."bootstrap-e2e-marker".text = "bootstrapped"`.
        # Its presence is the proof that `sudo nixos-rebuild switch`
        # inside the VM ran AND switch-to-configuration activated the
        # new toplevel — the "full sandboxed host" exit condition.
        marker_content = machine.succeed("cat /etc/bootstrap-e2e-marker").strip()
        assert marker_content == "bootstrapped", (
            f"expected /etc/bootstrap-e2e-marker to contain 'bootstrapped', "
            f"got: {marker_content!r}"
        )

        # --- Register-phase assertions: commit landed in bare origin -----
        log_subject = machine.succeed(
            "su - jacob -c 'git -C /home/jacob/origin.git log -1 --format=%s main'"
        ).strip()
        assert f"register host {hostname}" in log_subject, (
            f"expected 'register host {hostname}' in HEAD subject, got: {log_subject!r}"
        )

        registry = machine.succeed(
            "su - jacob -c 'git -C /home/jacob/origin.git show main:nix/config/hosts/registry.toml'"
        )
        assert f"[{hostname}]" in registry, (
            f"registry.toml missing [{hostname}] entry: {registry!r}"
        )

        sops_yaml = machine.succeed(
            "su - jacob -c 'git -C /home/jacob/origin.git show main:.sops.yaml'"
        )
        assert f"host_{hostname}" in sops_yaml, (
            f".sops.yaml missing host_{hostname} anchor: {sops_yaml!r}"
        )

        # The sandbox tier MUST NOT get its host key added to
        # secrets.yaml's creation_rule — that's the privileged file.
        if ${if assertSandboxAnchorSkipped then "True" else "False"}:
            marker = "path_regex: 'nix/secrets.yaml$'"
            assert marker in sops_yaml, (
                f"expected secrets.yaml creation_rule block, not found in: {sops_yaml!r}"
            )
            after = sops_yaml.split(marker, 1)[1]
            next_rule = after.find("\n  - path_regex:")
            block = after if next_rule < 0 else after[:next_rule]
            assert f"host_{hostname}" not in block, (
                f"sandbox host {hostname} SHOULD NOT be in secrets.yaml creation_rule, "
                f"but was: {block!r}"
            )
        else:
            marker = "path_regex: 'nix/secrets.yaml$'"
            after = sops_yaml.split(marker, 1)[1]
            next_rule = after.find("\n  - path_regex:")
            block = after if next_rule < 0 else after[:next_rule]
            assert f"host_{hostname}" in block, (
                f"devbox host {hostname} SHOULD be in secrets.yaml creation_rule, "
                f"but was NOT. block: {block!r}"
            )

        # Roundtrip: the generated host age key can decrypt what register
        # just re-encrypted (verifies sops updatekeys actually added the
        # host as a recipient, independent of the devbox/sandbox logic).
        machine.succeed(
            "su - jacob -c '"
            "SOPS_AGE_KEY_FILE=/home/jacob/.config/sops/age/keys.txt "
            "sops decrypt /home/jacob/dotfiles/nix/bot-secrets.yaml >/dev/null'"
        )

        print("[bootstrap-e2e-${variant}] PASSED")
      '';
    };
in
{
  inherit mkTest;
}
