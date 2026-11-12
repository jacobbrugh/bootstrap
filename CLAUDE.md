# CLAUDE.md

History before 2026-11 is in the private repo `jacobbrugh/archived-bootstrap`.

See [README.md](README.md) for what this repo is, the phase table, command surface, and architectural file layout.

## The validation gate (READ THIS BEFORE CHANGING ANYTHING)

`nix build .#default` is the single source of truth for "did I break anything". It runs, in order:

1. `pythonImportsCheck` — every wrapped binary actually imports `bootstrap` and `bootstrap.cli`
2. `pytestCheckHook` — full unit test suite under `python/tests/unit/`
3. `mypy --strict --config-file pyproject.toml src/bootstrap tests`

A green `nix build .#default` means strict types, passing tests, and a working wheel. The two e2e packages (`e2e-sandbox`, `e2e-privileged` — one per bootstrap tier) are CI-only — each boots real qemu VMs with internet via `__noChroot` + the CI client's `sandbox = relaxed`, and each needs the fixtures `bootstrap-e2e-stage <tier>` writes on the daemon host, so building them locally fails by design; push and let the Jenkins `bootstrap/main` job run them (both tiers in parallel). A green `nix flake check` additionally runs `nixfmt`, `ruff format`, `ruff check`, and the same `mypy --strict` over a pre-commit harness — use it before committing. **Never declare a change complete based on `nix eval`** — Nix's lazy evaluation means a successful eval doesn't mean the package builds, the tests pass, or the types check. The dotfiles repo's CLAUDE.md `Validation-First Mindset` rule applies here too.

See [README.md §Development](README.md#development) for the dev-loop command list.

## Architecture rules

### 1. `bootstrap.sh` is the only allowed pre-Nix shell

`bootstrap.sh` (34 lines) installs Nix and exec's the flake app. That's it. It's the only bash in the repo, and it's bash because it has to run before Nix exists. **Every other piece of bootstrap logic lives in typed Python** under `python/src/bootstrap/`. Don't add new shell scripts. Don't widen `bootstrap.sh`.

`bootstrap.ps1` and `BootstrapUtils.psm1` are the same exception for Windows: PowerShell because Nix can't run on the Windows host before WSL is installed. The Windows migration (not started) shrinks `bootstrap.ps1` to a minimal "install WSL + hand off" script and moves the rest into typed Python phases under `phases/windows/`. See `docs/windows-migration.md`.

### 2. Phase modules are OS-grouped, not OS-conditional

```
python/src/bootstrap/phases/
├── common/   — OS-agnostic phases (prereqs, ssh, register)
├── darwin/   — Homebrew, 1Password GUI, keychain, darwin-rebuild, TCC pane open
├── linux/    — HM-standalone Linux
├── nixos/    — NixOS native
└── windows/  — empty placeholder for the Windows migration
```

The orchestrator (`bootstrap/orchestrator.py`) dispatches via `match ctx.platform` to the right module. **No phase ever does its own OS detection.** If you find yourself writing `if sys.platform == "darwin":` inside a phase, that means the phase belongs in `phases/darwin/` instead.

Every phase module exports two things:
- `NAME: str` — short identifier used for log markers and (eventually) state files
- `run(ctx: Context) -> None` — does the work; raises `BootstrapError` on failure

There is no ABC.

### 3. `lib/` is OS-agnostic; phases are OS-specific

The boundary between `bootstrap/lib/` and `bootstrap/phases/` is **strict**:

- `lib/` has zero platform-specific code paths. `git_ops`, `sops_ops`, `age_ops`, `op`, `gh`, `registry_toml`, `sops_yaml`, `secrets`, `sh`, `log`, `paths`, `runtime`, `errors`, `symlinks`, `tcc` — none of them branch on `Platform`.
- **One explicit carve-out:** `lib/host_info.py` does branch on `Platform` (`detect_hostname` picks `scutil --get LocalHostName` on Darwin vs `hostname -s` on Linux; `rename_darwin` is Darwin-only). Hostname detection and OS-level renaming are inherently platform-specific — there's no OS-neutral primitive that would let us move them into `phases/<os>/` without forcing `phases/common/register.py` to cross-import from `phases/darwin/`, which would violate the stronger boundary below. `host_info` stays in `lib/`; every OTHER `lib/` module must remain `Platform`-free.
- `phases/<os>/*.py` is where OS-specific logic lives. `ssh-add --apple-use-keychain` lives in `phases/darwin/keychain.py`, NOT in `lib/ssh_ops.py`. `lib/ssh_ops.py` only has primitives (`keygen`, `merge_config_stanza`, `update_known_hosts`).
- **`phases/common/*.py` must not import from `phases/<os>/*.py`**. That cross-import is a harder architectural violation than the `lib/`-no-`Platform` rule — `phases/common/` is by definition the OS-agnostic subset, and a common phase reaching into a Darwin-specific phase module couples the two in ways that break the Windows migration plan.

This ruleset exists primarily for the **Windows migration**: when Windows phases get added, they reuse `lib/git_ops`, `lib/sops_ops`, `lib/registry_toml`, etc. unchanged, while their OS-specific work happens via `lib/sh.run_powershell` calls inside `phases/windows/*.py`. If you put platform branches into any `lib/` module other than `host_info`, that future migration breaks.

### 4. Cleanup uses `@contextlib.contextmanager` + `try/finally`

Two key context managers own the lifecycle of resources that need cleanup on failure:

- **`bootstrap.lib.secrets.ephemeral_secrets(ctx)`** — loads the bootstrap key + github token into `ctx` for exactly the span that needs them, in one of two delivery tiers:
  - **NixOS (file tier)**: sops-nix decrypts at activation — read plaintext `github_token` from `/run/secrets/bootstrap-github-token` (overridable via `BOOTSTRAP_GITHUB_TOKEN_FILE`); stat the flash-staged age key at `/var/lib/bootstrap/age-key` (overridable via `BOOTSTRAP_AGE_KEY_FILE`) into `ctx.bootstrap_age_key_file`. A missing age key is a warning, not an error — the `post` phase shreds it after a completed bootstrap, and a registered host's run never needs it (register's edit branch raises clearly if it does).
  - **Darwin (memory tier)**: the age key is `op read` **once** from 1Password (`bootstrap.lib.op.BOOTSTRAP_KEY_ITEM`) into `ctx.bootstrap_age_key` — never written to disk, nothing to shred — and the github token is decrypted directly from the flake-source `secrets.yaml` with `sops -d --extract`. A sandbox/harness darwin run uses the file tier instead (`BOOTSTRAP_AGE_KEY_FILE`, same seam as the NixOS e2e). `phases/darwin/secret_zero.py` only ensures the 1Password app + CLI session exist.

  sops call sites never see the tier: they consume `ctx.sops_env_overlay`, which yields `SOPS_AGE_KEY` (memory) or `SOPS_AGE_KEY_FILE` (file). On exit (or on any exception), all secret context fields are cleared.

  **There is only one 1Password item referenced on Darwin** (the privileged bootstrap age key — anchor `bootstrap_privileged`; the CI/sandbox tier uses the separate `bootstrap_sandbox` key). The GH PAT and any other bootstrap secrets live in `secrets.yaml`, decryptable offline given the age key

  - **`bootstrap.lib.git_ops.transactional_edit(repo)`** — captures HEAD on entry, runs `git reset --hard <initial-HEAD>` in `finally` on any exception. Wraps the destructive section of `register.py` so a failed `git push` (or anything else) leaves both the working tree AND any partial commits rolled back to a clean state. Re-running the bootstrap just works.

**Do not add `atexit.register` or signal handlers for cleanup.** The user explicitly rejected that pattern. Use `@contextlib.contextmanager` + a `try: yield finally: <cleanup>` body.

### 5. `Context` is the only interface phases see

`bootstrap.lib.runtime.Context` is built once by the orchestrator (or by a standalone CLI subcommand) and threaded through every phase. Phases **never read environment variables**, never call `os.environ`, never call `platform.detect()` themselves. Anything they need lives on `ctx`:

- `ctx.platform` — `Platform` enum value from runtime detection
- `ctx.hostname` — current system hostname (post-rename if Darwin scutil rename happened)
- `ctx.canonical_repo` — `~/.local/share/chezmoi/`, the fleet's dotfiles checkout path (`BOOTSTRAP_CANONICAL_DOTFILES` overrides it). A host whose registry entry sets `dotfilesDir` gets its clone moved there by register, and a re-run finds it through the flake symlink (`symlinks.linked_checkout`)
- `ctx.dry_run`, `ctx.is_sandbox`
- `ctx.has_windows_host` — True when `Platform.NIXOS_WSL`; reserved for Windows migration
- `ctx.bootstrap_age_key_file` / `ctx.bootstrap_age_key` (file tier vs memory tier — consume via `ctx.sops_env_overlay`), `ctx.github_token` — populated by `ephemeral_secrets`, None outside that span

This makes every phase trivially testable: build a `Context` in a fixture, call `run(ctx)`, inspect side effects.

### 6. `sh.run` and `sh.sudo_run` are the ONLY way to spawn subprocesses

Under no circumstances should phases import `subprocess` directly. Everything goes through `bootstrap.lib.sh`:

- `sh.run(cmd, *, dry_run=False, destructive=True, …)` — typed wrapper returning `Result`. In dry-run mode, `destructive=True` calls become no-ops that log `would run: …`; `destructive=False` (read-only ops like `git status`, `op user get --me`) still execute so decision trees are testable on a real machine.
- `sh.sudo_run(cmd, …)` — prefixes `sudo -n`. Relies on `sh.prime_sudo()` having been called once earlier.
- `sh.prime_sudo(*, dry_run=False)` — interactive `sudo -v` prompt. Skipped in dry-run.
- `sh.run_powershell(script, …)` — **stub for the Windows migration**. Raises `PlatformError` on non-WSL today. The contract is locked in so future call sites don't need new plumbing.

`shell=True` is forbidden. `subprocess.run` directly is forbidden. The dry-run + destructive distinction is load-bearing for safe local iteration with `bootstrap --dry-run`.

### 7. The tier is the key, and every GitHub decision follows it

`lib/secrets.py` sets `ctx.tier` from the loaded bootstrap age key's public half, matched against the `bootstrap_privileged` / `bootstrap_sandbox` anchors of the flake source's `.sops.yaml`. Phases read `ctx.is_privileged` / `ctx.is_sandbox` and nothing else: the ssh phase uploads a key only when privileged (to `jacobbrugh`, with the token from `secrets-privileged.yaml`, after checking `gh api user`), register picks the SSH or https remote and the harvest author by tier, and the dotfiles sops rules a host joins are `sops_yaml.rules_with_anchor` of the tier's anchor — never a hard-coded file list. `BOOTSTRAP_SANDBOX=1` (read once in `cli.py`) can only lower a tier. Do not add a second way to decide any of this.

### 8. `host_registry.toml` and `.sops.yaml` go through round-trip editors only

`bootstrap.lib.registry_toml` (tomlkit) and `bootstrap.lib.sops_yaml` (ruamel.yaml `typ='rt'`) are the **only** way to read/mutate the dotfiles `host_registry.toml` and `.sops.yaml`. They preserve comments, whitespace, key order, and YAML anchors across round-trips. **Never edit these files via text manipulation, regex, sed, or any other approach.** The golden-file tests in `tests/unit/test_registry_toml.py` and `tests/unit/test_sops_yaml.py` are the regression guard — keep them green.

`registry_toml` mirrors the dotfiles `lib/hosts/fromRegistry.nix` expansion semantics: a section with `system` is a host template (indexed to `base<start>..base<end>` when `endIndex` is set, in which case the template name itself is NOT a host), a section without `system` is a partial override merged onto the same-named expanded host (scalars override-win, `tags` concatenate), and tags carry their implications (`private` implies `sandbox`, `laptop` implies `desktop` and `wireless`). It is also where the Tang server URLs come from: every host tagged `tang`, at its registry ethernet address. `sops_yaml.add_to_creation_rule` locates the rule to extend by matching the repo-relative file path against each rule's `path_regex` (sops' own resolution semantics), never by comparing regex strings.

The register phase's decision tree (`phases/common/register.py`):
- host not in the registry → hard error pointing at `nix run .#add-host` (bootstrap never adds registry entries)
- host in registry, and the key on this disk decrypts every sops file of its tier and its host files are committed → skip (idempotent re-run); a key merely *named* in `.sops.yaml` proves nothing
- host in registry, no local age key → auto-regenerate, replace any stale registered pubkey
- host in registry, local age key present but not anchored → add its pubkey under `host_<hostname>`

Register modifications are wrapped in `git_ops.transactional_edit` so a failed `sops updatekeys` or `git push` rolls back to the entry HEAD.

## Things to know about the flake

- **`config.allowUnfree = true`** in `pkgsFor` — blanket allow, not a per-package predicate. `_1password-cli` is the immediate need (it's unfree), but the project policy is to allow unfree generally rather than maintain an allowlist that drifts every time a new dependency comes in. Don't replace this with `allowUnfreePredicate`.
- **`.sops.yaml` creation_rules** cover two files: `secrets.yaml` (both tier anchors, `&bootstrap_sandbox` and `&bootstrap_privileged`) and `secrets-privileged.yaml` (`&bootstrap_privileged` alone — the jacobbrugh SSH-key token, which CI must never be able to decrypt). Re-encrypting (`sops updatekeys`) isn't needed unless you add/remove recipients; the UPDATEKEYS CI stage can rekey only `secrets.yaml`.
- **`nixosConfigurations.bootstrap-secret-zero` is conditionally exposed** — only when `./host-hardware.nix` exists (the file `lib/flake_splice.py` writes into a local clone; `nix flake check` on the bare repo would otherwise trip on the missing module). `wsl-bootstrap`, `bootstrap-secret-zero-wsl`, and `installer-usb` always exist.
- **`nix/nixos/default.nix` is the wsl-bootstrap base.** `nix/nixos/wsl.nix` overrides the non-WSL options (`networking.useDHCP`, `services.getty.autologinUser`) on top of it. `installer-usb.nix` is a `{ bootstrap, flakeSource ? null, firmwareRom ? null }:` function (the nixosTest import path can't see flake `self`, so the CLI package is passed in explicitly).
- **The `phase_*` shims in `cli.py` forward `sys.argv[1:]`** so `bootstrap-prereqs --dry-run` and `bootstrap-prereqs --help` work the same as `bootstrap prereqs --dry-run` / `bootstrap prereqs --help`.

## Out of scope

- **Implementing `phases/windows/` content.** A later migration moves `BootstrapUtils.psm1`'s Scoop/DSC/SSH/Tailscale/WSL functions into typed Python phases that drive the Windows host via `sh.run_powershell`. See `docs/windows-migration.md` for the function-by-function checklist and the architectural decisions made in this codebase to ease that migration.
- **Anything that requires `nix flake check` to also build downstream k3s/argocd manifests in the dotfiles repo.** Those have their own validation chain.
