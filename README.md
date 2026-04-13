# bootstrap

Public Nix-flake entry point for bootstrapping a fresh machine into the
`jacobbrugh/dotfiles` configuration. The flake exposes a typed Python CLI
as its default app — running it takes a fresh Mac, NixOS, Linux, or
WSL/Windows host from zero to a fully applied system rebuild.

## Usage

The intended invocation on any unix host with Nix already installed:

```sh
nix run github:jacobbrugh/bootstrap
```

For a fresh Mac that doesn't have Nix yet, the convenience wrapper
installs Nix first, then exec's the flake app:

```sh
curl -fsSL https://jacobbrugh.net/bootstrap.sh | bash
```

For Windows: `bootstrap.ps1` installs WSL + NixOS-WSL, then runs the
unix flake app inside the WSL distro.

## Phases

The bootstrap is split into independent, idempotent phases. Running
`bootstrap` with no arguments executes the full OS-appropriate sequence;
running `bootstrap <phase>` executes a single phase for recovery /
debugging. Each phase is also exposed as its own flake app, so you can
run a single one without going through the full CLI:

```sh
nix run github:jacobbrugh/bootstrap#prereqs
nix run github:jacobbrugh/bootstrap#register
```

Phase order on Darwin:

| # | Phase         | What it does                                                                |
|---|---------------|-----------------------------------------------------------------------------|
| 1 | `prereqs`     | Install Homebrew; create `~/.ssh`, `~/.config/sops/age`; resolve `/etc/{nix.conf,zshrc,bashrc}` conflicts. |
| 2 | `onepassword` | Install 1Password GUI via cask, launch it, poll `op whoami` until signed in. |
| 3 | `ssh`         | Generate `id_ed25519_<host>`; upload to GitHub via `gh ssh-key add`; pin github.com host keys; on Darwin, add to keychain via `ssh-add --apple-use-keychain` and write `~/.ssh/config` stanza. |
| 4 | `register`    | Clone the canonical dotfiles repo; prompt hostname + tags; generate post-quantum age key if missing; edit `registry.toml` + `.sops.yaml`; `sops updatekeys` + verify decrypt; commit + push; symlink `/etc/nix-darwin/flake.nix` to the canonical repo. |
| 5 | `switch`      | Run `darwin-rebuild switch` (or `nixos-rebuild` / `home-manager switch`).   |
| 6 | `post`        | Auto-open System Settings panes for the irreducibly manual TCC gates (Accessibility, Input Monitoring, System Extensions). |

Linux / NixOS / WSL get the same phase list with OS-appropriate
implementations of `prereqs`, `onepassword`, `switch`, and `post`.

## Flags

All commands accept the same flags:

- `--dry-run` — log destructive operations as `would run: …` instead of
  executing them. Read-only commands still execute, so the register
  decision tree can be exercised safely on a real machine.
- `--non-interactive` — fail fast instead of prompting. Intended for CI
  and automation.
- `--verbose` / `-v` — DEBUG-level logging.

## Architecture

- `python/` — typed Python package built via `buildPythonApplication`
  (hatchling backend, src layout, `mypy --strict` enforced at build time).
- `nix/nixos/` — Phase 0 minimal NixOS configs (`nixosConfigurations.bootstrap`
  for bare metal, `wsl-bootstrap` for NixOS-WSL).
- `nix/nixos/secrets/phase0.yaml` — sops-encrypted Phase 0 firstboot secrets
  (Headscale URL, timezone). Decrypted by the `phase0-firstboot` systemd
  unit using an age private key the user places at
  `/var/lib/nixos-bootstrap/age-key` at nixos-install time.
- `python/src/bootstrap/data/bootstrap-secrets.sops.yaml` — sops-encrypted
  Python-side runtime secrets (GitHub PAT). Decrypted inside
  `bootstrap.lib.secrets.ephemeral_secrets` using the age key extracted
  from 1Password at runtime.
- `.sops.yaml` — sops recipient config. One `&bootstrap` age anchor
  covers both encrypted files above.
- `windows-bootstrap.nix` — Phase 0 minimal Windows config consumed by
  `winConfigurations.bootstrap` via [`nix-win`](https://github.com/jacobbrugh/nix-win).
- `bootstrap.sh` — pre-Nix bash wrapper that installs Nix and exec's the flake app.
- `bootstrap.ps1` + `BootstrapUtils.psm1` — pre-Nix PowerShell wrapper
  that installs WSL + NixOS-WSL and hands off to the Python bootstrap
  inside WSL.

## Secret-zero model

Exactly **one** secret reference exists at bootstrap runtime: the bootstrap
age private key, stored in 1Password at
`op://Personal/bw2otnlpjhm434grbcbpb6dady/credential`. Running the
bootstrap extracts that key once, then uses it to decrypt everything else
(GitHub PAT, Phase 0 NixOS firstboot config) from sops-encrypted files
committed to this public repo. No other 1Password items are read by the
bootstrap at runtime.

To add a new bootstrap secret, edit the appropriate sops file with `sops`
after exporting `SOPS_AGE_KEY_FILE` pointed at the bootstrap age private
key, then commit.

## Development

```sh
cd python
nix develop          # in the repo root
mypy --strict src/bootstrap
pytest -xvs tests/unit/
```

The pre-commit hook (`nix flake check`) runs `mypy --strict` over the
whole package, plus `ruff` formatting, on every commit.

## Windows migration

The Windows-host orchestration (Scoop, DSC, OpenSSH, Tailscale) currently
lives in `BootstrapUtils.psm1`. The next migration session moves it into
typed Python phases under `python/src/bootstrap/phases/windows/` that
drive the host via `bootstrap.lib.sh.run_powershell`. See
[`docs/windows-migration.md`](docs/windows-migration.md) for the
function-by-function migration checklist.
