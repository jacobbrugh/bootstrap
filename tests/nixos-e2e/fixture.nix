# Build-time fixture: generates a fresh bootstrap age key, encrypts a
# tier-specific bootstrap-secrets file with it (for the bootstrapForTest
# derivation's package data), and lays out a minimal throwaway dotfiles
# checkout whose .sops.yaml / bot-secrets.yaml / secrets.yaml are
# encrypted to the same bootstrap key. The bootstrap age key, the
# bundled sops file, and the fixture checkout all travel together to
# the VM so the register phase can actually decrypt + re-encrypt
# against them.
#
# One fixture per scenario (devbox vs sandbox). Scenario selection picks
# which filename the bundled sops file ships under
# (`bootstrap-secrets-{variant}.sops.yaml`); nothing else changes.

{
  pkgs,
  # "devbox" | "sandbox" — selects the bundled filename and the fake token
  variant,
}:

pkgs.runCommand "bootstrap-e2e-fixture-${variant}"
  {
    nativeBuildInputs = [
      pkgs.age
      pkgs.sops
      pkgs.coreutils
    ];
    # `sops` needs SOPS_AGE_KEY_FILE in its env to encrypt — we set it
    # from the freshly generated key. Declaring no env here keeps the
    # derivation deterministic for `nix build` reproducibility purposes
    # (new key every rebuild; that's fine since the fixture is ephemeral).
  }
  ''
    set -euo pipefail
    mkdir -p $out
    export HOME=$TMPDIR

    # --- Generate bootstrap age key (ephemeral; lives only in this derivation) -------
    age-keygen -o $out/bootstrap-age-key.txt 2>/dev/null
    chmod 600 $out/bootstrap-age-key.txt
    PUBKEY=$(age-keygen -y $out/bootstrap-age-key.txt)
    echo "[fixture] bootstrap pubkey: $PUBKEY" >&2

    # --- Bundled secrets file (goes into bootstrapForTest's package data) ----
    # Plain YAML with a fake github_token; encrypt in place with sops.
    # The token value is what the mock `gh` on the VM will see in
    # $GITHUB_TOKEN — the mock ignores the value, so any string works.
    cat > $out/bootstrap-secrets-${variant}.sops.yaml <<'YAML'
    github_token: ci-e2e-fake-token-${variant}
    YAML
    SOPS_AGE_RECIPIENTS=$PUBKEY sops -e -i --input-type yaml --output-type yaml $out/bootstrap-secrets-${variant}.sops.yaml

    # --- Fixture dotfiles checkout ------------------------------------
    # Minimal shape of the real dotfiles repo: registry.toml + tags dir
    # + .sops.yaml + bot-secrets.yaml + secrets.yaml. Everything
    # encrypted to the bootstrap key; the register phase will generate
    # a fresh HOST key and re-encrypt against both keys.
    mkdir -p $out/checkout/nix/config/hosts
    mkdir -p $out/checkout/nix/config/tags

    cat > $out/checkout/nix/config/hosts/registry.toml <<'TOML'
    [pre-existing]
    system = "x86_64-linux"
    TOML

    # _select_tags enumerates tag files; `default.nix` is excluded by
    # convention. We need at least one selectable tag for the happy
    # path and a `sandbox` tag so the sandbox scenario can find it.
    touch $out/checkout/nix/config/tags/default.nix
    touch $out/checkout/nix/config/tags/sandbox.nix
    touch $out/checkout/nix/config/tags/work.nix

    # .sops.yaml: bootstrap key is the only recipient for both
    # creation_rules. After the register phase runs, .sops.yaml will
    # have an additional `host_<hostname>` anchor whose pubkey is the
    # host's freshly generated key.
    cat > $out/checkout/.sops.yaml <<SOPS
    keys:
      - &bootstrap $PUBKEY
    creation_rules:
      - path_regex: 'nix/secrets.yaml\$'
        key_groups:
          - age:
              - *bootstrap
      - path_regex: 'nix/bot-secrets.yaml\$'
        key_groups:
          - age:
              - *bootstrap
    SOPS

    cat > $out/checkout/nix/bot-secrets.yaml <<'YAML'
    placeholder: e2e-test-bot-secret
    YAML
    cat > $out/checkout/nix/secrets.yaml <<'YAML'
    placeholder: e2e-test-host-secret
    YAML

    # sops reads `.sops.yaml` from cwd, so encrypt from inside the
    # checkout so the path_regex matches.
    (
      cd $out/checkout
      SOPS_AGE_KEY_FILE=$out/bootstrap-age-key.txt sops -e -i nix/bot-secrets.yaml
      SOPS_AGE_KEY_FILE=$out/bootstrap-age-key.txt sops -e -i nix/secrets.yaml
    )

    echo "[fixture] generated at $out" >&2
  ''
