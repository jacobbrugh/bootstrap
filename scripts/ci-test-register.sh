#!/usr/bin/env bash
# End-to-end test for the `register` phase. Stands up a throwaway
# dotfiles fixture (age key + sops-encrypted bot-secrets/secrets +
# registry + tags + git history + local bare remote) and runs
# `./result/bin/bootstrap register --non-interactive` against it.
#
# Designed to run both locally (for pre-push verification) and in the
# `test-register` GHA job. In both environments, the bootstrap's
# secrets layer is bypassed via BOOTSTRAP_TEST_* env vars so the test
# doesn't need a real 1Password session or GitHub token.
#
# Requires `age-keygen`, `sops`, and `git` on PATH. Locally, invoke as:
#   nix shell nixpkgs#age nixpkgs#sops nixpkgs#git -c ./scripts/ci-test-register.sh
# In CI, the workflow does the equivalent `nix shell ... -c bash ...`.

set -euo pipefail

cd "$(dirname "$0")/.."
BOOTSTRAP_DIR=$(pwd)

if [[ ! -x "$BOOTSTRAP_DIR/result/bin/bootstrap" ]]; then
    echo "[ci-test-register] building ./result/bin/bootstrap"
    nix build .#default 2>&1 | tail -3
fi

TEST_ROOT=$(mktemp -d -t bootstrap-ci-test-register-XXXXXX)
trap 'rm -rf "$TEST_ROOT"' EXIT

CHECKOUT="$TEST_ROOT/checkout"
ORIGIN="$TEST_ROOT/origin.git"
AGE_KEY_FILE="$TEST_ROOT/bootstrap-age-key.txt"
FAKE_HOSTNAME="test-ci-$(date +%s)"

# Throwaway path for the flake symlink that register's _ensure_symlink
# installs. Without this override, the test would clobber the real
# /etc/nix-darwin/flake.nix or /etc/nixos/flake.nix on the developer's
# machine — ask me how I know.
FAKE_SYMLINK="$TEST_ROOT/fake-flake-symlink"

echo "[ci-test-register] test root: $TEST_ROOT"
echo "[ci-test-register] fake hostname: $FAKE_HOSTNAME"

# --- Pre-flight safety check ------------------------------------------
# This test writes a fresh age key to $HOME/.config/sops/age/keys.txt.
# In CI that's a throwaway path, but if someone runs this locally by
# mistake it would destroy the real host's sops decryption key. Refuse
# to run if the destination already exists — the cost of a clear error
# is nothing compared to the cost of losing a private key.
HOST_AGE_KEY_DEST="$HOME/.config/sops/age/keys.txt"
if [[ -e "$HOST_AGE_KEY_DEST" ]]; then
    echo "[ci-test-register] REFUSING to run: $HOST_AGE_KEY_DEST already exists." >&2
    echo "[ci-test-register] This script writes a fresh key to that path." >&2
    echo "[ci-test-register] Run it only in CI or a fully throwaway sandbox." >&2
    exit 1
fi

# --- Generate throwaway age keys --------------------------------------
# Two separate keys so the test exercises the real "new host" path:
#   1. $AGE_KEY_FILE — the "bootstrap" key (exported as SOPS_AGE_KEY_FILE).
#      This is the only recipient in the fixture's .sops.yaml, so sops
#      can decrypt the fixture's bot-secrets / secrets files initially.
#   2. $HOST_AGE_KEY_DEST — the host's own key, a FRESH keypair whose
#      pubkey is NOT in the fixture's .sops.yaml. When the register
#      phase runs, `_ensure_age_key` finds the existing file,
#      `find_anchor_by_pubkey` returns None, and the phase falls
#      through to the "declare new anchor" path — adding
#      host_<FAKE_HOSTNAME> to .sops.yaml, which is what the final
#      assertions check for.
age-keygen -o "$AGE_KEY_FILE" 2>/dev/null
chmod 600 "$AGE_KEY_FILE"
PUBKEY=$(age-keygen -y "$AGE_KEY_FILE")
echo "[ci-test-register] bootstrap pubkey: ${PUBKEY:0:48}…"

mkdir -p "$(dirname "$HOST_AGE_KEY_DEST")"
age-keygen -o "$HOST_AGE_KEY_DEST" 2>/dev/null
chmod 600 "$HOST_AGE_KEY_DEST"
HOST_PUBKEY=$(age-keygen -y "$HOST_AGE_KEY_DEST")
echo "[ci-test-register] host pubkey:      ${HOST_PUBKEY:0:48}…"

# --- Build the fixture dotfiles ---------------------------------------
mkdir -p "$CHECKOUT/nix/config/hosts" "$CHECKOUT/nix/config/tags"

# One pre-existing host in the registry so the file isn't empty and
# `registry_toml.add_host` has something to append to.
cat > "$CHECKOUT/nix/config/hosts/registry.toml" <<'TOML'
[pre-existing]
system = "x86_64-linux"
TOML

# At least one tag file so `_select_tags` doesn't raise on an empty
# directory. The file name is what matters (it becomes a choice); the
# content can be empty since this is not a real Nix config.
touch "$CHECKOUT/nix/config/tags/sandbox.nix"
touch "$CHECKOUT/nix/config/tags/default.nix"  # excluded from choices by _select_tags

# Minimal .sops.yaml using the test age pubkey as the only recipient
# for both creation_rules. Mirrors the shape of the real dotfiles
# .sops.yaml — top-level `keys:` with anchors, then `creation_rules`
# with path_regex + key_groups + age aliases.
cat > "$CHECKOUT/.sops.yaml" <<SOPS
keys:
  - &test_host $PUBKEY
creation_rules:
  - path_regex: 'nix/secrets.yaml\$'
    key_groups:
      - age:
          - *test_host
  - path_regex: 'nix/bot-secrets.yaml\$'
    key_groups:
      - age:
          - *test_host
SOPS

# Plaintext placeholders for the two encrypted files, then sops encrypt
# them in place. sops -e -i writes the encrypted form back to the same
# path; after this `bot-secrets.yaml` and `secrets.yaml` are valid sops
# files decryptable with AGE_KEY_FILE.
cat > "$CHECKOUT/nix/bot-secrets.yaml" <<'YAML'
placeholder: ci-test
YAML
cat > "$CHECKOUT/nix/secrets.yaml" <<'YAML'
placeholder: ci-test
YAML

# Run sops from inside $CHECKOUT so (a) .sops.yaml is in cwd where
# sops expects it, and (b) the target path is relative and matches
# the `nix/bot-secrets.yaml$` regex.
cd "$CHECKOUT"
SOPS_AGE_KEY_FILE="$AGE_KEY_FILE" sops -e -i nix/bot-secrets.yaml
SOPS_AGE_KEY_FILE="$AGE_KEY_FILE" sops -e -i nix/secrets.yaml

# --- Initialize git + bare origin -------------------------------------
git init --quiet --initial-branch=main
git config user.email "fixture@example.com"
git config user.name "Fixture User"
git add -A
git commit --quiet -m "initial fixture"
cd "$TEST_ROOT"
git clone --quiet --bare "$CHECKOUT" "$ORIGIN"
cd "$CHECKOUT"
# `git init` doesn't create any remote — we have to hand-add origin
# after cloning the bare repo out. `remote add`, not `set-url`.
git remote add origin "$ORIGIN"

# --- Run the bootstrap ------------------------------------------------
cd "$BOOTSTRAP_DIR"
echo "[ci-test-register] running ./result/bin/bootstrap register --non-interactive"
BOOTSTRAP_CANONICAL_DOTFILES="$CHECKOUT" \
BOOTSTRAP_DOTFILES_REMOTE="$ORIGIN" \
BOOTSTRAP_HOSTNAME="$FAKE_HOSTNAME" \
BOOTSTRAP_SKIP_RENAME=1 \
BOOTSTRAP_FLAKE_SYMLINK_PATH="$FAKE_SYMLINK" \
SOPS_AGE_KEY_FILE="$AGE_KEY_FILE" \
BOOTSTRAP_TEST_GITHUB_TOKEN="ci-dummy-token" \
BOOTSTRAP_TEST_GIT_AUTHOR_NAME="CI Test User" \
BOOTSTRAP_TEST_GIT_AUTHOR_EMAIL="ci-test@example.com" \
"$BOOTSTRAP_DIR/result/bin/bootstrap" register --non-interactive

# --- Assertions -------------------------------------------------------
echo "[ci-test-register] verifying commit landed in bare origin"
LAST_MSG=$(git -C "$ORIGIN" log -1 --format='%s' main)
echo "  HEAD subject: $LAST_MSG"
if [[ "$LAST_MSG" != *"register host $FAKE_HOSTNAME"* ]]; then
    echo "FAIL: expected 'register host $FAKE_HOSTNAME' at HEAD, got:" >&2
    echo "  $LAST_MSG" >&2
    exit 1
fi

COMMIT_AUTHOR=$(git -C "$ORIGIN" log -1 --format='%an <%ae>' main)
echo "  author: $COMMIT_AUTHOR"
if [[ "$COMMIT_AUTHOR" != "CI Test User <ci-test@example.com>" ]]; then
    echo "FAIL: expected 'CI Test User <ci-test@example.com>', got:" >&2
    echo "  $COMMIT_AUTHOR" >&2
    exit 1
fi

echo "[ci-test-register] verifying registry.toml picked up the new host"
git -C "$ORIGIN" show "main:nix/config/hosts/registry.toml" | grep -q "\\[$FAKE_HOSTNAME\\]" || {
    echo "FAIL: registry.toml in the pushed commit doesn't have [$FAKE_HOSTNAME]" >&2
    exit 1
}

echo "[ci-test-register] verifying .sops.yaml picked up the new anchor"
git -C "$ORIGIN" show "main:.sops.yaml" | grep -q "host_$FAKE_HOSTNAME" || {
    echo "FAIL: .sops.yaml in the pushed commit doesn't have host_$FAKE_HOSTNAME anchor" >&2
    exit 1
}

echo "[ci-test-register] ALL CHECKS PASSED"
