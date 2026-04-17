# End-to-end bootstrap test on a fresh NixOS VM.
#
# Exposes two `pkgs.nixosTest` checks:
#   - `nixos-e2e-devbox`   — devbox tier; secrets.yaml gets the anchor
#   - `nixos-e2e-sandbox`  — sandbox tier (BOOTSTRAP_SANDBOX=1); the
#                            "sandbox" tag forces skipping secrets.yaml
#                            and the anchor must NOT be added to its
#                            creation_rule.
#
# Each scenario:
#   1. Builds a fresh bootstrap age key + variant-specific encrypted
#      sops file + fixture dotfiles checkout (via fixture.nix).
#   2. Builds `bootstrapForTest` — the real bootstrap package with the
#      test sops file substituted and mock `gh` prepended to PATH.
#   3. Boots a NixOS VM with `bootstrapForTest` on PATH, the bootstrap
#      key staged at `SOPS_AGE_KEY_FILE`, the fixture cloned into a tmp
#      dotfiles checkout, and a local bare-repo origin.
#   4. Runs `bootstrap-register --non-interactive` and asserts:
#        - register phase exits 0 (secrets decrypted, sops updatekeys,
#          commit pushed)
#        - registry.toml in the pushed commit has a `[test-host-…]`
#          entry
#        - .sops.yaml in the pushed commit has a `host_…` anchor
#        - in the sandbox scenario, `secrets.yaml` is NOT in
#          the creation_rule for the new anchor (only bot-secrets.yaml)

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
        inherit bootstrap fixture mockGh variant;
      };
    in
    pkgs.testers.nixosTest {
      name = "bootstrap-e2e-${variant}";

      nodes.machine =
        { ... }:
        {
          # Headless, no GUI, minimal. bootstrapForTest wrapper bundles
          # git/sops/age on PATH already — we include them in
          # environment.systemPackages too so the testScript setup
          # (which runs outside the wrapper) can use them directly.
          environment.systemPackages = with pkgs; [
            bootstrapForTest
            git
            sops
            age
            openssh
          ];

          # A real unprivileged user with passwordless sudo. The
          # register phase doesn't actually call sudo, but the symlink
          # step (_ensure_symlink) does if the target's parent needs
          # mkdir — and we point the symlink at /tmp to dodge that.
          users.users.jacob = {
            isNormalUser = true;
            extraGroups = [ "wheel" ];
            password = "";
          };
          security.sudo.wheelNeedsPassword = false;

          # Needed so `git push` from the test over file:// protocol
          # works without ssh. The register phase uses whatever remote
          # url the fixture git config has, which will be a file://
          # path to the bare repo we set up in the testScript.
          programs.git.enable = true;

          # Headless VM: prevent the systemd-ssh-proxy watchdog from
          # firing during nixosTest boots; nothing here needs ssh in.
          services.openssh.enable = false;

          virtualisation = {
            cores = 2;
            memorySize = 2048;
            diskSize = 4096;
          };
        };

      # `testScript` runs on the HOST as python. `machine.succeed(…)`
      # shells into the VM. All the setup lives here so the
      # nodes.machine module stays focused on "what NixOS looks like"
      # rather than "how the test is driven."
      testScript = ''
        start_all()
        machine.wait_for_unit("multi-user.target")

        # --- Stage age key + fixture checkout + bare origin --------------
        # The fixture derivation is in /nix/store/… (nixosTest auto-
        # shares the store), so we can read it directly but must copy
        # out of it to get a writable dotfiles checkout.
        machine.succeed("mkdir -p /home/jacob/.config/sops/age")
        machine.succeed("cp ${fixture}/bootstrap-age-key.txt /home/jacob/sops-age-key.txt")
        machine.succeed("chown -R jacob:users /home/jacob/.config")
        machine.succeed("chown jacob:users /home/jacob/sops-age-key.txt")
        machine.succeed("chmod 600 /home/jacob/sops-age-key.txt")

        # Writable dotfiles checkout and a co-located bare origin so
        # register phase's `git push` has somewhere to go. Both owned
        # by the `jacob` user.
        machine.succeed("cp -r ${fixture}/checkout /home/jacob/dotfiles")
        machine.succeed("chown -R jacob:users /home/jacob/dotfiles")
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

        # --- Run bootstrap-register --------------------------------------
        # All BOOTSTRAP_* envs mirror scripts/ci-test-register.sh:
        # canonical path + git remote pointed at the fixture, hostname
        # spoofed, OS rename skipped, flake symlink target pointed at
        # /tmp so we don't touch /etc/nixos.
        #
        # SOPS_AGE_KEY_FILE is THE production mechanism introduced by
        # Chunk A — this exercises the headless path end-to-end (no
        # BOOTSTRAP_TEST_GITHUB_TOKEN bypass). The bundled sops file
        # in bootstrapForTest is encrypted to this exact key.
        #
        # BOOTSTRAP_SANDBOX=${sandboxEnv} picks the tier. The sandbox
        # scenario also auto-adds the "sandbox" tag (enforced by
        # register.py) which triggers `_NON_SENSITIVE_TAGS` to skip
        # secrets.yaml.
        hostname = "test-e2e-${variant}"
        machine.succeed(
            "su - jacob -c '"
            "export SOPS_AGE_KEY_FILE=/home/jacob/sops-age-key.txt && "
            "export BOOTSTRAP_CANONICAL_DOTFILES=/home/jacob/dotfiles && "
            "export BOOTSTRAP_DOTFILES_REMOTE=/home/jacob/origin.git && "
            f"export BOOTSTRAP_HOSTNAME={hostname} && "
            "export BOOTSTRAP_SKIP_RENAME=1 && "
            "export BOOTSTRAP_FLAKE_SYMLINK_PATH=/tmp/fake-flake-link && "
            "export BOOTSTRAP_SANDBOX=${sandboxEnv} && "
            "bootstrap-register --non-interactive'"
        )

        # --- Assertions --------------------------------------------------
        # Inspect the pushed bare origin directly so we know the
        # register phase didn't just commit locally but actually pushed.

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
        # We parse the anchor's position in sops_yaml and confirm that
        # the `secrets.yaml$` creation_rule's key_groups doesn't
        # reference it.
        if ${if assertSandboxAnchorSkipped then "True" else "False"}:
            # Split on the `path_regex: 'nix/secrets.yaml$'` rule and
            # inspect just that rule's body. It's enough to check that
            # `*host_<hostname>` doesn't appear in the substring between
            # that header and the next creation_rules entry.
            marker = "path_regex: 'nix/secrets.yaml$'"
            assert marker in sops_yaml, (
                f"expected secrets.yaml creation_rule block, not found in: {sops_yaml!r}"
            )
            # Take everything after the secrets.yaml rule header up to
            # the next `  - path_regex:` (or EOF). That slice is the
            # key_groups block for secrets.yaml.
            after = sops_yaml.split(marker, 1)[1]
            next_rule = after.find("\n  - path_regex:")
            block = after if next_rule < 0 else after[:next_rule]
            assert f"host_{hostname}" not in block, (
                f"sandbox host {hostname} SHOULD NOT be in secrets.yaml creation_rule, "
                f"but was: {block!r}"
            )
        else:
            # Non-sandbox: the anchor MUST appear in the secrets.yaml
            # creation_rule (the host gets access to the privileged
            # secrets file).
            marker = "path_regex: 'nix/secrets.yaml$'"
            after = sops_yaml.split(marker, 1)[1]
            next_rule = after.find("\n  - path_regex:")
            block = after if next_rule < 0 else after[:next_rule]
            assert f"host_{hostname}" in block, (
                f"devbox host {hostname} SHOULD be in secrets.yaml creation_rule, "
                f"but was NOT. block: {block!r}"
            )

        # Also sanity-check that the generated host age key was written
        # to the standard sops path and is decryptable with the newly
        # committed .sops.yaml (roundtrip).
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
