# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

The public Nix-flake entry point for bootstrapping a fresh machine into the **private** `jacobbrugh/dotfiles` repo. The flake's `apps.default` is a typed Python CLI (`bootstrap`) that takes a fresh Mac, NixOS, Linux, or WSL/Windows host through a sequence of independent, idempotent phases ending in `darwin-rebuild switch` / `nixos-rebuild switch` / `home-manager switch`. See `README.md` for the user-facing tour and phase table.

The companion repo this flake registers hosts into is at `git@github.com:jacobbrugh/dotfiles.git`, cloned to the canonical local path `~/repos/jacobbrugh/nix-config/nix-config/` by the `register` phase. The user has signalled an intent to rename `dotfiles` → `nix-config` later — that rename is OUT OF SCOPE here.

## The validation gate (READ THIS BEFORE CHANGING ANYTHING)

`nix build .#default` is the single source of truth for "did I break anything". It runs, in order:

1. `pythonImportsCheck` — every wrapped binary actually imports `bootstrap` and `bootstrap.cli`
2. `pytestCheckHook` — full unit test suite under `python/tests/unit/`
3. `mypy --strict --config-file pyproject.toml src/bootstrap tests`

A green `nix build .#default` means strict types, passing tests, and a working wheel. A green `nix flake check` additionally runs `nixfmt`, `ruff format`, `ruff check`, and the same `mypy --strict` over a pre-commit harness — use it before committing. **Never declare a change complete based on `nix eval`** — Nix's lazy evaluation means a successful eval doesn't mean the package builds, the tests pass, or the types check. The dotfiles repo's CLAUDE.md `Validation-First Mindset` rule applies here too.

The dev loop, fastest-to-slowest:

```sh
# Fastest: invoke ruff/mypy/pytest directly inside the devshell.
nix develop
ruff format python/ && ruff check python/
mypy --strict --config-file python/pyproject.toml python/src/bootstrap python/tests
pytest -xvs python/tests/unit/

# Full validation (re-runs every phase under buildPythonApplication).
nix build .#default

# Sanity check the wrapped binaries:
./result/bin/bootstrap --help
./result/bin/bootstrap-prereqs --help
./result/bin/bootstrap --dry-run     # safe to run on a real Mac

# Pre-commit harness — runs every hook (nixfmt + ruff + mypy + pytest):
nix flake check
```

## Architecture rules

These are non-obvious constraints that several modules cooperate to enforce. Breaking them creates subtle regressions you won't catch from a single-file diff.

### 1. `bootstrap.sh` is the only allowed pre-Nix shell

`bootstrap.sh` (34 lines) installs Nix and exec's the flake app. That's it. It's the only bash in the repo, and it's bash because it has to run before Nix exists. **Every other piece of bootstrap logic lives in typed Python** under `python/src/bootstrap/`. Don't add new shell scripts. Don't widen `bootstrap.sh`.

`bootstrap.ps1` and `BootstrapUtils.psm1` are the same exception for Windows: PowerShell because Nix can't run on the Windows host before WSL is installed. The Windows migration (next session) shrinks `bootstrap.ps1` to a minimal "install WSL + hand off" script and moves the rest into typed Python phases under `phases/windows/`. See `docs/windows-migration.md`.

### 2. Phase modules are OS-grouped, not OS-conditional

```
python/src/bootstrap/phases/
├── common/   — OS-agnostic phases (ssh, register)
├── darwin/   — Homebrew, 1Password GUI, keychain, darwin-rebuild, TCC pane open
├── linux/    — HM-standalone Linux
├── nixos/    — NixOS native
└── windows/  — empty placeholder for the Windows migration
```

The orchestrator (`bootstrap/orchestrator.py`) dispatches via `match ctx.platform` to the right module. **No phase ever does its own OS detection.** If you find yourself writing `if sys.platform == "darwin":` inside a phase, that means the phase belongs in `phases/darwin/` instead.

Every phase module exports two things:
- `NAME: str` — short identifier used for log markers and (eventually) state files
- `run(ctx: Context) -> None` — does the work; raises `BootstrapError` on failure

There is no ABC. `phases/base.py` is just a docstring describing the contract.

### 3. `lib/` is OS-agnostic; phases are OS-specific

The boundary between `bootstrap/lib/` and `bootstrap/phases/` is **strict**:

- `lib/` has zero platform-specific code paths. `git_ops`, `sops_ops`, `age_ops`, `op`, `gh`, `registry_toml`, `sops_yaml`, `secrets`, `sh`, `log`, `paths`, `runtime`, `errors`, `symlinks`, `prompts`, `tcc`, `state` — none of them branch on `Platform`.
- **One explicit carve-out:** `lib/host_info.py` does branch on `Platform` (`detect_hostname` picks `scutil --get LocalHostName` on Darwin vs `hostname -s` on Linux; `rename_darwin` is Darwin-only). Hostname detection and OS-level renaming are inherently platform-specific — there's no OS-neutral primitive that would let us move them into `phases/<os>/` without forcing `phases/common/register.py` to cross-import from `phases/darwin/`, which would violate the stronger boundary below. `host_info` stays in `lib/`; every OTHER `lib/` module must remain `Platform`-free.
- `phases/<os>/*.py` is where OS-specific logic lives. `ssh-add --apple-use-keychain` lives in `phases/darwin/keychain.py`, NOT in `lib/ssh_ops.py`. `lib/ssh_ops.py` only has primitives (`keygen`, `merge_config_stanza`, `update_known_hosts`).
- **`phases/common/*.py` must not import from `phases/<os>/*.py`**. That cross-import is a harder architectural violation than the `lib/`-no-`Platform` rule — `phases/common/` is by definition the OS-agnostic subset, and a common phase reaching into a Darwin-specific phase module couples the two in ways that break the Windows migration plan.

This ruleset exists primarily for the **Windows migration**: when Windows phases get added, they reuse `lib/git_ops`, `lib/sops_ops`, `lib/registry_toml`, etc. unchanged, while their OS-specific work happens via `lib/sh.run_powershell` calls inside `phases/windows/*.py`. If you put platform branches into any `lib/` module other than `host_info`, that future migration breaks.

### 4. Cleanup uses `@contextlib.contextmanager` + `try/finally`

Two key context managers own the lifecycle of resources that need cleanup on failure:

- **`bootstrap.lib.secrets.ephemeral_secrets(ctx)`** — scope primitive, not a decrypt flow. Python never touches sops or 1Password; it just reads plaintext written by sops-nix. On entry:
  1. Read plaintext `github_token` from `/run/secrets/bootstrap-github-token` (overridable via `BOOTSTRAP_GITHUB_TOKEN_FILE`). Write to `ctx.github_token`.
  2. Stat `/var/lib/nixos-bootstrap/age-key` (overridable via `BOOTSTRAP_AGE_KEY_FILE`). Write the path to `ctx.bootstrap_age_key_file` for the register phase's `sops updatekeys`.

  On exit (or on any exception), both context fields are cleared. The orchestrator wraps `ssh` + `register` in this so the token is readable for exactly the span that needs it; standalone phase entry points (`nix run .#ssh`, `nix run .#register`) open the same context themselves.

  Where the plaintext comes from:
  - **NixOS**: sops-nix nixosModule decrypts at activation. Operator pre-stages `/var/lib/nixos-bootstrap/age-key` before first boot; the `phase0-firstboot` systemd service `shred -u`'s it after activation so it doesn't persist.
  - **Darwin**: `phases/darwin/onepassword.py` fetches the age key from 1Password (`op://Personal/bw2otnlpjhm434grbcbpb6dady/credential` for devbox, `TODO-sandbox-…` for sandbox), `sudo tee`'s it to the same path, and activates a minimal nix-darwin Phase 0 config so sops-nix-darwin decrypts plaintext to `/run/secrets/bootstrap-github-token`. `phases/darwin/post.py` shreds the age key at the end of bootstrap, mirroring the NixOS pattern.

  **There is only one 1Password item referenced on Darwin** (the age key). The GH PAT and any other bootstrap secrets live in `secrets/bootstrap-secrets.sops.yaml`, decryptable offline given the age key. Adding a new bootstrap secret is `sops secrets/bootstrap-secrets.sops.yaml` (with the age private key configured), then wiring the Nix-side `sops.secrets.<name>` declaration in `nix/nixos/default.nix` / `nix/darwin/default.nix`. Not another 1Password item.

- **`bootstrap.lib.git_ops.transactional_edit(repo)`** — captures HEAD on entry, runs `git reset --hard <initial-HEAD>` in `finally` on any exception. Wraps the destructive section of `register.py` so a failed `git push` (or anything else) leaves both the working tree AND any partial commits rolled back to a clean state. Re-running the bootstrap just works.

**Do not add `atexit.register` or signal handlers for cleanup.** The user explicitly rejected that pattern. Use `@contextlib.contextmanager` + a `try: yield finally: <cleanup>` body.

### 5. `Context` is the only interface phases see

`bootstrap.lib.runtime.Context` is built once by the orchestrator (or by a standalone CLI subcommand) and threaded through every phase. Phases **never read environment variables**, never call `os.environ`, never call `platform.detect()` themselves. Anything they need lives on `ctx`:

- `ctx.platform` — `Platform` enum value from runtime detection
- `ctx.hostname` — current system hostname (post-rename if Darwin scutil rename happened)
- `ctx.canonical_repo` — `~/repos/jacobbrugh/nix-config/nix-config/`
- `ctx.dry_run`, `ctx.non_interactive`
- `ctx.has_windows_host` — True when `Platform.NIXOS_WSL`; reserved for Windows migration
- `ctx.bootstrap_age_key_file`, `ctx.github_token` — populated by `ephemeral_secrets`, None outside that span

This makes every phase trivially testable: build a `Context` in a fixture, call `run(ctx)`, inspect side effects.

### 6. `sh.run` and `sh.sudo_run` are the ONLY way to spawn subprocesses

Under no circumstances should phases import `subprocess` directly. Everything goes through `bootstrap.lib.sh`:

- `sh.run(cmd, *, dry_run=False, destructive=True, …)` — typed wrapper returning `Result`. In dry-run mode, `destructive=True` calls become no-ops that log `would run: …`; `destructive=False` (read-only ops like `git status`, `op user get --me`) still execute so decision trees are testable on a real machine.
- `sh.sudo_run(cmd, …)` — prefixes `sudo -n`. Relies on `sh.prime_sudo()` having been called once earlier.
- `sh.prime_sudo(*, dry_run=False)` — interactive `sudo -v` prompt. Skipped in dry-run.
- `sh.run_powershell(script, …)` — **stub for the Windows migration**. Raises `PlatformError` on non-WSL today. The contract is locked in so future call sites don't need new plumbing.

`shell=True` is forbidden. `subprocess.run` directly is forbidden. The dry-run + destructive distinction is load-bearing for safe local iteration with `bootstrap --dry-run`.

### 7. `registry.toml` and `.sops.yaml` go through round-trip editors only

`bootstrap.lib.registry_toml` (tomlkit) and `bootstrap.lib.sops_yaml` (ruamel.yaml `typ='rt'`) are the **only** way to mutate `registry.toml` and `.sops.yaml` in the dotfiles repo. They preserve comments, whitespace, key order, and YAML anchors across round-trips. **Never edit these files via text manipulation, regex, sed, or any other approach.** The golden-file tests in `tests/unit/test_registry_toml.py` and `tests/unit/test_sops_yaml.py` are the regression guard — keep them green.

The register phase's full decision tree (`phases/common/register.py`) handles all four cases:
- A: host not in registry → register new
- B: host in registry, local age key matches → skip
- C: host in registry, local age key missing → prompt to regenerate + replace
- D: host in registry, local age key mismatches → hard fail with manual recovery instructions

Register modifications are wrapped in `git_ops.transactional_edit` so a failed `sops updatekeys` or `git push` rolls back to the entry HEAD.

## Things to know about the flake

- **`config.allowUnfree = true`** in `pkgsFor` — blanket allow, not a per-package predicate. `_1password-cli` is the immediate need (it's unfree), but the project policy is to allow unfree generally rather than maintain an allowlist that drifts every time a new dependency comes in. Don't replace this with `allowUnfreePredicate`.
- **Two committed sops-encrypted files**, both under the repo-root `secrets/` directory and decryptable only with the bootstrap age key (public half in `.sops.yaml:6`, private half at `op://Personal/bw2otnlpjhm434grbcbpb6dady/credential`):
  - `secrets/bootstrap-secrets.sops.yaml` — runtime secrets (currently just `github_token`). Decrypted at Phase 0 activation by sops-nix (NixOS nixosModule) or sops-nix-darwin (Darwin darwinModule); plaintext lands at `/run/secrets/bootstrap-github-token` and `ephemeral_secrets` reads it directly. Python never decrypts.
  - `secrets/phase0.yaml` — Phase 0 NixOS firstboot secrets (`headscale_login_server`, `timezone`). Decrypted inside the `phase0-firstboot` systemd service using `SOPS_AGE_KEY_FILE=/var/lib/nixos-bootstrap/age-key`, where the user places the age private key once at nixos-install time alongside a single-use Tailscale auth key. After firstboot succeeds, both the age key and the auth key are `shred -u`'d so they don't persist on the installed system. Darwin's equivalent shred happens in `phases/darwin/post.py`.
- **`.sops.yaml` creation_rules** cover the single regex `secrets/.*\.ya?ml$`. The recipient is a single `&bootstrap` age anchor. Re-encrypting (`sops updatekeys`) isn't needed unless you add/remove recipients, which you shouldn't.
- **`nixosConfigurations.bootstrap` is conditionally exposed** — only when `./host-hardware.nix` exists. Otherwise `nix flake check` would trip on the missing module. `nixosConfigurations.wsl-bootstrap` always exists. Adding `host-hardware.nix` (via `cp /etc/nixos/hardware-configuration.nix host-hardware.nix`) is the affirmative step that makes the bare-metal config available.
- **`nix/nixos/default.nix` IS the bare-metal config.** `nix/nixos/wsl.nix` is an override module that uses `lib.mkForce` to disable the bare-metal-only options (`networking.wireless`, `networking.useDHCP`, `services.getty.autologinUser`) and adds WSL-specific config. There is no `bare-metal.nix` and there is no shared `user.nix` — the defaults model means WSL inherits everything that isn't explicitly overridden.
- **`windows-bootstrap.nix` has a `TODO (windows-migration)` block.** nix-win renamed `win.dsc.services.<name>` to the generated `win.dsc.psdsc.service.<name>` submodule with capital-cased DSC properties. The SSH service startup is commented out until the Windows migration session re-wires it.
- **The `phase_*` shims in `cli.py` forward `sys.argv[1:]`** so `bootstrap-prereqs --dry-run` and `bootstrap-prereqs --help` work the same as `bootstrap prereqs --dry-run` / `bootstrap prereqs --help`.

## When mypy or ruff complains

Both run at build time via `nix build .#default`. The fastest fix loop:

```sh
nix shell nixpkgs#ruff -c ruff format python/      # auto-format
nix shell nixpkgs#ruff -c ruff check --fix python/ # auto-fix lint
nix shell nixpkgs#nixfmt -c nixfmt flake.nix nix/nixos/*.nix
```

mypy strict gotchas that have come up:
- `sys.platform` is typed as a `Literal` in typeshed; checking `sys.platform == "darwin"` makes mypy mark the linux branch unreachable on a Darwin build host. Use a typed local: `current: str = sys.platform`.
- `json.loads` returns `Any`; narrow with `isinstance(raw, dict)` checks before reading fields. See `lib/state.py::PhaseState.from_json` for the pattern.
- `getattr(module, "X")` returns `Any`. Avoid dynamic module lookup in hot paths — phases are imported by name and accessed as `module.NAME` / `module.run`, which mypy resolves to the declared types.
- ruamel.yaml's anchor API (`scalar.yaml_set_anchor(name, always_dump=True)`) IS in the stubs — don't add `# type: ignore[attr-defined]` to it.

## Tests

Every meaningful test lives under `python/tests/unit/`. The high-value ones are:

- **`test_registry_toml.py`** — golden-file round-trip for tomlkit. Verifies `add_host` preserves existing entries + comments.
- **`test_sops_yaml.py`** — golden-file round-trip for ruamel.yaml. Verifies anchor preservation, alias insertion in the right `creation_rule`, and idempotency.
- **`test_ssh_config_merge.py`** — verifies the `# managed-by: bootstrap` marker pattern is idempotent and preserves user-authored content above and below the managed block.
- **`test_platform.py`** — fixture-filesystem tests for `Platform.detect()` covering all 5 enum values.
- **`test_smoke.py`** — minimum tripwire: every `[project.scripts]` entry resolves to a callable.

There is no decision-tree integration test for `register.py` yet — that's deferred until there's a fake-FS + fake-sh harness worth building. Until then, the dry-run end-to-end (`./result/bin/bootstrap --dry-run`) is the manual smoke test.

## Out of scope

- **Renaming the dotfiles GitHub repo to `nix-config`.** The user is doing this separately. The clone URL stays `git@github.com:jacobbrugh/dotfiles.git` until told otherwise.
- **Implementing `phases/windows/` content.** The next session migrates `BootstrapUtils.psm1`'s Scoop/DSC/SSH/Tailscale/WSL functions into typed Python phases that drive the Windows host via `sh.run_powershell`. See `docs/windows-migration.md` for the function-by-function checklist and the architectural decisions made in this codebase to ease that migration.
- **Anything that requires `nix flake check` to also build downstream k3s/argocd manifests in the dotfiles repo.** Those have their own validation chain.
