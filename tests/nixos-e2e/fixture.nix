# Build-time fixture for the e2e test. Does three things:
#
#   1. Generates a fresh bootstrap age key + encrypts a tier-specific
#      `bootstrap-secrets-{variant}.sops.yaml` with it (consumed by
#      `bootstrap-for-test.nix`).
#
#   2. Lays out a minimal dotfiles checkout — registry.toml + .sops.yaml
#      + bot-secrets.yaml + secrets.yaml + tags/ — encrypted to the
#      same bootstrap key. The register phase mutates these.
#
#   3. Drops a `configuration.nix` + `post-switch-module.nix` into the
#      checkout so `nixos-rebuild switch` (classic, not flake) has
#      something to evaluate. The module is also pre-built here via
#      `eval-config.nix`, and the resulting toplevel store path is
#      exposed via `passthru.postSwitchToplevel`. The VM config adds
#      it to `virtualisation.additionalPaths` so when the switch phase
#      runs inside the VM, the same toplevel is already cached — no
#      network, no full rebuild, just a switch-to-configuration
#      activation.
#
# Classic (non-flake) rather than flake-based: nixos-rebuild resolves
# `<nixpkgs/nixos>` through NIX_PATH (set by `nix.nixPath` on the VM
# side) and reads `/etc/nixos/configuration.nix` by default. The
# register phase's `_ensure_symlink` installs `/etc/nixos` → the
# dotfiles checkout, so that path lands on our `configuration.nix`.
# Skipping flakes sidesteps the chicken-and-egg `flake.lock` problem
# (a pure fixture derivation can't run `nix flake lock`).

{
  pkgs,
  # "devbox" | "sandbox" — selects the bundled sops filename and the
  # fake github_token value the decrypted payload will yield.
  variant,
}:

let
  # Evaluate the post-switch module through nixpkgs' nixos infra to get
  # a real system toplevel. Same module that ends up in the checkout,
  # so `nixos-rebuild switch` inside the VM hashes to this exact path.
  postSwitchEval = import "${pkgs.path}/nixos/lib/eval-config.nix" {
    system = "x86_64-linux";
    modules = [ ./post-switch-module.nix ];
  };
  postSwitchToplevel = postSwitchEval.config.system.build.toplevel;
in

pkgs.runCommand "bootstrap-e2e-fixture-${variant}"
  {
    nativeBuildInputs = [
      pkgs.age
      pkgs.sops
      pkgs.coreutils
    ];
    passthru = {
      # Exposed so tests/nixos-e2e/default.nix can add this toplevel to
      # the VM's /nix/store via `virtualisation.additionalPaths`. Without
      # it, `nixos-rebuild switch` inside the VM would try to build it
      # and fail (no network, and the host nixpkgs source isn't in the
      # VM's store either).
      inherit postSwitchToplevel;
    };
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
    # Minimal shape of the real dotfiles repo plus a `configuration.nix`
    # for `nixos-rebuild switch` to evaluate. Everything encrypted to
    # the bootstrap key; register will generate a fresh HOST key and
    # re-encrypt against both keys.
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

    # --- NixOS config files (classic, not flake) ---------------------
    # `nixos-rebuild switch` with no --flake arg defaults to
    # /etc/nixos/configuration.nix. The register phase's _ensure_symlink
    # points /etc/nixos at this checkout, so configuration.nix here is
    # what gets evaluated. It imports post-switch-module.nix (same file
    # used to pre-build the toplevel above — identical inputs yield
    # the same store path, so the VM's pre-staged toplevel is hit
    # directly instead of rebuilt).
    cat > $out/checkout/configuration.nix <<'NIX'
    { ... }:
    {
      imports = [ ./post-switch-module.nix ];
    }
    NIX
    cp ${./post-switch-module.nix} $out/checkout/post-switch-module.nix

    echo "[fixture] generated at $out (post-switch toplevel: ${postSwitchToplevel})" >&2
  ''
