# bootstrap

Nix-flake entry point for bootstrapping a fresh machine
into the `jacobbrugh/dotfiles` configuration. The flake exposes a typed
Python CLI as its default app — running it takes a fresh Mac, NixOS,
Linux, or WSL/Windows host from zero to a fully applied system rebuild.

## Usage

The intended invocation on any unix host with Nix already installed
(the host must already exist in the dotfiles `host_registry.toml`):

```sh
nix run 'github:jacobbrugh/bootstrap' [hostname]
```

## Bare-metal flash flow

Three declarative commands take a machine from blank disk to fleet
member; each takes at most one argument:

> A flashed host carries the flake source at `/etc/bootstrap-flake` and
> bootstraps with no GitHub access at all; everything else fetches this
> repo's `github:` ref, which is also the CLI's own default, so
> `BOOTSTRAP_FLAKE` is never needed.

1. **Media** (a Linux admin host holding the bootstrap age key):
   `sudo nix run 'github:jacobbrugh/bootstrap#create-usb' -- /dev/sdX` —
   wipes the stick and produces LUKS-encrypted boot media carrying the
   `installer-usb` system, the bootstrap age key, the flake source
   (`/etc/bootstrap-flake`, so flash needs no network), and baked WiFi
   credentials when a `wifi_wpa_conf` key is added to the sops secrets
   (none is set today). The LUKS passphrase is the `luks_passphrase` sops
   secret.
2. **Firmware** (Protectli only, target booted from that USB):
   `bootstrap-firmware` reports the board, the firmware on the chip and
   whether the flash is unlocked, writing nothing; `bootstrap-firmware
   --apply` backs the current image up and converts the board to Dasharo
   coreboot. Disable BIOS Lock and Secure Boot in firmware setup first —
   the tool prints the right menu path for whichever firmware is running.
3. **Flash** (same USB, same boot):
   `bootstrap-flash <hostname> [--disk /dev/<name>]` — resolves the target
   disk (the `--disk` argument, else the sole eligible disk),
   disko-partitions (ESP + LUKS `cryptroot`), sets up the unlock
   the registry's `luksUnlock` asks for, installs `#bootstrap-secret-zero`
   with systemd-boot + the hostname + that unlock + the first-boot unit,
   stages the age key, `boot.nix` and the flake source, and reboots.

   Every target is encrypted; `luksUnlock` picks how it opens.
   `tang-tpm2` (default): flash runs `clevis luks bind`, adding a random key
   in its own keyslot whose Tang-wrapped copy (SSS t=1 over both home-LAN
   Tang servers, plus TPM2 where present) lives as a clevis token in the
   LUKS2 header; the initrd runs `clevis luks unlock` from it, retrying for
   10 minutes. `tpm2`: flash runs `systemd-cryptenroll`, sealing a random
   key to the TPM, opened natively with no network in the initrd.
   `tpm2-token` is enrolled later, after Secure Boot. `initrd-key` puts the
   passphrase in the initramfs, for hardware with neither TPM nor Tang. The
   sops passphrase stays LUKS keyslot 0 in every method as the console
   recovery path, and no unlock material leaves the disk.
4. **Bootstrap** — nothing to run. The reboot lands in the scaffold
   system, whose `bootstrap-firstboot.service` (`nix/nixos/bootstrap-firstboot.nix`,
   imported by the wrapper flash splices in) runs the full `bootstrap` as
   `jacob`, retrying every five minutes until it succeeds. Its `switch` phase
   activates the dotfiles config, which does not define the unit; the unit
   sets `X-StopOnRemoval=false` so that switch does not kill it mid-run, and
   `post` then shreds the age key it conditions on. The scaffold authorizes
   the fleet SSH keys (`nix/nixos/fleet-ssh-keys.nix`) and takes no
   passwords, so `ssh jacob@<host>` works from the first boot; watch with
   `journalctl -fu bootstrap-firstboot`.

## Two tiers

Which bootstrap age key a host was handed decides what it may do at GitHub
(`lib/secrets.py` reads the key's public half against the `.sops.yaml`
anchors; `lib/runtime.py` `Tier`):

| | sandbox (`bootstrap_sandbox`) | privileged (`bootstrap_privileged`) |
|---|---|---|
| who | CI, e2e and sandbox VMs (`sandbox` tag) | real devices, from media built with the 1Password key |
| `ssh` phase | generates a key, uploads it **nowhere** | uploads it to the `jacobbrugh` account |
| upload token | — | `github_ssh_key_token` in `secrets-privileged.yaml`, decryptable by this key only; owner checked (`gh api user` must be `jacobbrugh`) before `gh ssh-key add` |
| `register` remote | `https://github.com/jacobbrugh/dotfiles.git`, the bot token on register's own git processes (never written to the machine) | `git@github.com:jacobbrugh/dotfiles.git`, the key just uploaded |
| harvest author | `jacobbrugh-bot` | `jacobbrugh` |
| dotfiles sops rules joined | every rule listing `*bootstrap_sandbox` | every rule listing `*bootstrap_privileged` (secrets.yaml, gate-secrets, bot-secrets, the logscrub literals, the eval fixtures) |

The two never mix: `bootstrap-flash` refuses privileged media for a
`sandbox`-tagged host, `register` refuses the same combination before any
edit, and `BOOTSTRAP_SANDBOX=1` can only lower a tier, never raise it.

The privileged token is a fine-grained PAT owned by `jacobbrugh` with the
single account permission **Git SSH keys: Read and write** (covers
`GET /user/keys` and `POST /user/keys`), repository access "Public
repositories" (it touches none), no expiration. It lives in
`secrets-privileged.yaml`, committed with the placeholder `unset` that the
`ssh` phase refuses; set it from a machine holding the privileged key:

```sh
SOPS_AGE_KEY=$(op read op://Personal/bw2otnlpjhm434grbcbpb6dady/credential) sops set secrets-privileged.yaml '["github_ssh_key_token"]' '"github_pat_…"'
```

For a fresh Mac that doesn't have Nix yet, the convenience wrapper
installs Nix first, then exec's the flake app:

```sh
curl -fsSL https://jacobbrugh.net/bootstrap.sh | bash -s -- <host>
```

`<host>` is the machine's name in the dotfiles `host_registry.toml`. On a Mac
it is required: the name macOS gave the machine is not a registry name.

For Windows: `bootstrap.ps1` installs WSL + NixOS-WSL, then runs the
unix flake app inside the WSL distro.

## Phases

The bootstrap is split into independent, idempotent phases. Running
`bootstrap` with no arguments executes the full OS-appropriate sequence;
running `bootstrap <phase>` executes a single phase for recovery /
debugging. Each phase is also exposed as its own flake app, so you can
run a single one without going through the full CLI:

```sh
nix run 'github:jacobbrugh/bootstrap#prereqs'
nix run 'github:jacobbrugh/bootstrap#register'
```

Phase order on Darwin:

| # | Phase         | What it does                                                                |
|---|---------------|-----------------------------------------------------------------------------|
| 1 | `prereqs`     | Install Homebrew; create `~/.ssh`, `~/.config/sops/age`; resolve `/etc/{nix.conf,zshrc,bashrc}` conflicts. |
| 2 | `secret-zero` | Make the bootstrap secrets reachable. On Darwin: install the 1Password GUI via cask and wait for CLI sign-in — the age key is later read straight into memory (never disk) and the PAT decrypted directly from the flake's `secrets.yaml`. On NixOS: clone the bootstrap flake, splice host hardware config, run `nixos-rebuild switch` against `bootstrap-secret-zero`, which sops-nix-materializes the PAT at `/run/secrets/bootstrap-github-token` from the age key staged at `/var/lib/bootstrap/age-key`. |
| 3 | `ssh`         | Generate `~/.ssh/id_ed25519`; `Host github.com` with `StrictHostKeyChecking accept-new`; on a privileged host, upload the key to the `jacobbrugh` account with the privileged token (owner checked first); on Darwin, add to keychain via `/usr/bin/ssh-add --apple-use-keychain` and write `~/.ssh/config` stanza. |
| 4 | `register`    | Clone the canonical dotfiles repo (SSH for privileged, https + bot token for sandbox); verify the host is already in `host_registry.toml` (else error pointing at `nix run path:<repo>#add-host`); generate post-quantum age key if missing; add it to every `.sops.yaml` rule the tier's anchor is in; `sops updatekeys` + verify decrypt each file; harvest `/etc/nixos/{hardware-configuration,networking,boot}.nix` (overwriting a changed one — a re-flash renames the disk); commit + push; symlink the `/etc/nix-darwin` directory to the canonical repo. |
| 5 | `switch`      | Run `darwin-rebuild switch` (or `nixos-rebuild` / `home-manager switch`).   |
| 6 | `post`        | Auto-open System Settings panes for the irreducibly manual TCC gates (Accessibility, Input Monitoring, System Extensions). |

Linux / NixOS / WSL get the same phase list with OS-appropriate
implementations of `prereqs`, `secret-zero`, `switch`, and `post`.

## Flags

The hostname may be passed as the single positional argument
(`nix run 'github:jacobbrugh/bootstrap' myhost`). It is required on a Mac
and optional elsewhere: a host flashed by `bootstrap-flash` already carries
its hostname, and the detected system hostname is used otherwise. All commands also accept:

- `--hostname HOST` — explicit override (same as the positional form).
  Resolution order: positional/flag, then `$BOOTSTRAP_HOSTNAME` (a test
  seam), then the system-detected hostname.
- `--dry-run` — log destructive operations as `would run: …` instead of
  executing them. Read-only commands still execute, so the register
  decision tree can be exercised safely on a real machine.
- `--verbose` / `-v` — DEBUG-level logging.

The CLI is fully non-interactive: host shape (tags, sandbox, indexing)
is read from the dotfiles `host_registry.toml` (index-range templates
and per-host override sections are expanded the same way the dotfiles
`lib/hosts/fromRegistry.nix` expands them). Add a host first with
`nix run path:<dotfiles>#add-host` if it isn't already registered.

## Architecture

- `python/` — typed Python package built via `buildPythonApplication`
  (hatchling backend, src layout, `mypy --strict` enforced at build time).
- `nix/nixos/` — minimal NixOS install configs: `bootstrap-secret-zero`
  (the flashed/bare-metal secret-zero target), `wsl-bootstrap` +
  `bootstrap-secret-zero-wsl` for NixOS-WSL, and `installer-usb.nix`
  (the LUKS USB installer system `bootstrap-create-usb` flashes).
- `.sops.yaml` — sops recipient config. Two delivery-agnostic tiers:
  `&bootstrap_privileged` (real-device bootstraps; the key in 1Password,
  also a recipient of the dotfiles fleet secrets) and
  `&bootstrap_sandbox` (CI runners and e2e/sandbox VMs — never a
  recipient of privileged dotfiles secrets). `secrets.yaml` is encrypted
  to both; `secrets-privileged.yaml` to the privileged key alone.
- `tests/` — the two end-to-end tests, one per tier
  (`e2e-sandbox`, `e2e-privileged`), their shared `e2e-lib/` (the logging
  `mock-gh`, the mock-wrapped CLI, the sops-rule assertion), and
  `python/src/bootstrap/ci/e2e_stage.py`, the CI tool that stages each
  tier's fixtures and the guests' binary cache.
- `windows-bootstrap.nix` — minimal Windows install config consumed by
  `winConfigurations.bootstrap` via [`nix-win`](https://github.com/jacobbrugh/nix-win).
- `bootstrap.sh` — pre-Nix bash wrapper that installs Nix and exec's the flake app.
- `bootstrap.ps1` + `BootstrapUtils.psm1` — pre-Nix PowerShell wrapper
  that installs WSL + NixOS-WSL and hands off to the Python bootstrap
  inside WSL.

## Secret-zero model

Exactly **one** secret reference exists at bootstrap runtime: the
privileged bootstrap age key, stored in 1Password at
`op://Personal/bw2otnlpjhm434grbcbpb6dady/credential`. Running the
bootstrap extracts that key once — on Darwin it is held in process
memory only and never written to disk; on a NixOS flash it rides the
LUKS-encrypted installer media and is shredded after registration — then
uses it to decrypt everything else (the bot GitHub PAT, LUKS passphrase
— and, for a privileged host, the `jacobbrugh`
SSH-key token in `secrets-privileged.yaml`) from sops-encrypted files
committed to this repo. No other 1Password items are read by the
bootstrap at runtime. CI and sandbox VMs use the separate
`bootstrap_sandbox` key (the Jenkins `bootstrap-age-key` credential /
test fixtures) via the `BOOTSTRAP_AGE_KEY_FILE` seam; it cannot decrypt
the privileged token, and the privileged e2e stands up an ephemeral
identity in its place (`bootstrap-e2e-stage privileged`).

## Development

```sh
nix develop          # in the repo root
cd python
mypy --strict src/bootstrap
pytest -xvs tests/unit/
```

The pre-commit hook (`nix flake check`) runs `mypy --strict` over the
whole package, plus `ruff` formatting, on every commit.

CI (`Jenkinsfile`) runs `flake-check`, then the two e2e tiers in parallel
pods: each stage is `nix run .#e2e-stage -- <tier>` (stage fixtures and
the guests' `file://` binary cache on the daemon host) followed by a build
of `.#e2e-<tier>`. The guests substitute the dotfiles CI closure, both
flakes' inputs and the installer toplevel from that cache instead of
downloading them over qemu SLIRP, and use an erofs store image rather than
a 9p-backed overlay, which is what keeps a green run to a fraction of the
sequential hour it used to take.

## Windows migration

The Windows-host orchestration (Scoop, DSC, OpenSSH, Tailscale) currently
lives in `BootstrapUtils.psm1`. The next migration session moves it into
typed Python phases under `python/src/bootstrap/phases/windows/` that
drive the host via `bootstrap.lib.sh.run_powershell`. See
[`docs/windows-migration.md`](docs/windows-migration.md) for the
function-by-function migration checklist.
