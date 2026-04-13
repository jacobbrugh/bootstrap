# Windows migration checklist

This document tracks the migration of `BootstrapUtils.psm1`'s
PowerShell-side orchestration into typed Python phases that run from
inside WSL and drive the Windows host via
`bootstrap.lib.sh.run_powershell`.

The goal is to shrink `bootstrap.ps1` to a minimal "install WSL +
NixOS-WSL and hand off to the Python flake app" script, with all
post-WSL Windows-host configuration owned by typed Python.

## Status

**Not started.** This file is the inventory the next session executes.

## Plumbing already in place

| Piece | Where | Status |
|---|---|---|
| `Platform.NIXOS_WSL` enum variant | `python/src/bootstrap/platform.py` | ✅ |
| `Context.has_windows_host: bool` flag | `python/src/bootstrap/lib/runtime.py` | ✅ (set True when platform is `NIXOS_WSL`) |
| `sh.run_powershell()` helper | `python/src/bootstrap/lib/sh.py` | ✅ (raises `PlatformError` on non-WSL) |
| `phases/windows/` directory | `python/src/bootstrap/phases/windows/` | ✅ (empty placeholder) |
| `windows-bootstrap.nix` (Phase 0) | repo root | ✅ (one stale block commented out — see below) |

## `BootstrapUtils.psm1` function inventory

The functions below currently live in `BootstrapUtils.psm1`. The
migration moves each into a Python phase (or a `lib/<area>.py` helper
that several phases share), invoking PowerShell via `sh.run_powershell`
where the underlying operation is Windows-host state.

### Logging — DROP

`Write-Info`, `Write-Warn`, `Write-Err`, `Write-Step` — already covered
by `bootstrap.lib.log`. No port needed.

### Admin / elevation — DROP

`Test-AdminElevation`, `Test-SudoEnabled`, `sudo`, `Request-AdminElevation`,
`Invoke-SudoPwshSwitch`, `Test-SystemModuleInstalled`, `Install-SystemModule`,
`Invoke-AsNonAdmin` — replaced by the Python orchestrator running inside
WSL with the user's host-side `wsl.exe` calls already running with
appropriate elevation.

### Environment — DROP

`Update-PathEnvironment` — handled by Python re-spawning subprocesses
which inherit the latest registry PATH automatically when invoked via
fresh `wsl.exe`.

### Scoop → `phases/windows/scoop.py` + `lib/scoop.py`

| PS function | Maps to |
|---|---|
| `Test-ScoopInstalled` | `lib/scoop.py::installed()` (probes `scoop` via `run_powershell`) |
| `Install-Scoop` | `lib/scoop.py::install_script()` (run powershell with `irm get.scoop.sh \| iex`) |
| `Install-ScoopBuckets` | `lib/scoop.py::add_bucket(name)` |
| `Install-ScoopPackage` | `lib/scoop.py::install_package(name)` |
| `Invoke-ScoopValidate` | inline check in `phases/windows/scoop.py` |

### OpenSSH (Windows host) → `phases/windows/openssh.py`

| PS function | Maps to |
|---|---|
| `New-SshKey` | `lib/ssh_ops.py::keygen` already exists; the Python phase invokes it (the host-side ssh-keygen via `run_powershell` is needed only when the key has to live on the Windows side, not in WSL) |
| `Add-SshKeyToAgent` | `lib/windows_ssh.py::add_to_agent` (PS: `ssh-add <path>`) |
| `Invoke-SshValidate` | inline check |

### GitHub → reuse `lib/gh.py`

| PS function | Maps to |
|---|---|
| `Get-GitHubToken` | already covered by `lib/secrets.py::ephemeral_secrets` (extracts from 1Password) |
| `Test-GitHubSshAccess` | covered by `lib/git_ops.py::clone_or_pull` (fails loudly if SSH is wrong) |
| `Add-SshKeyToGitHub` | already covered by `lib/gh.py::ssh_key_add` |

### WSL → `phases/windows/wsl.py` + `lib/wsl.py`

These are the bits `bootstrap.ps1` runs BEFORE Python is available, so
some of this work has to stay in PowerShell. The minimum that stays:

- `Get-WslInstanceName` — keep in `bootstrap.ps1`
- `Install-WslPlatform` — keep in `bootstrap.ps1`
- `Install-NixosWsl` — keep in `bootstrap.ps1`

`Invoke-WslValidate` becomes a Python sanity check in the orchestrator's
WSL-host phases.

### PowerShell modules → `phases/windows/psmodules.py`

| PS function | Maps to |
|---|---|
| `Install-PsModules` | `lib/psmodules.py::install_from_manifest(manifest_path)` (run powershell `Install-Module -Name <name>`) |
| `Install-WindowsPowerShellModules` | same, but invokes via `powershell.exe` (PS5.1) instead of `pwsh.exe` (PS7) |

### Tailscale → `phases/windows/tailscale.py`

| PS function | Maps to |
|---|---|
| `Test-TailscaleConnected` | `lib/tailscale.py::connected()` (run powershell `tailscale status`) |
| `Connect-Tailscale` | `lib/tailscale.py::connect(authkey, login_server)` — pulls the auth key from sops bot-secrets |
| `Invoke-TailscaleValidate` | inline check |

## DSC schema drift fix

`windows-bootstrap.nix` has one commented-out block — the old
`win.dsc.services.{sshd,ssh-agent}` API was renamed by nix-win to the
generated `win.dsc.psdsc.service.<name>` submodule with capital-cased
DSC properties (`State = "Running"`, `StartupType = "Automatic"`).
Re-wire the SSH service startup against the new schema as part of this
migration. See the `# TODO (windows-migration)` marker in
`windows-bootstrap.nix`.

## End-to-end target flow

After the migration completes:

```
bootstrap.ps1 (Windows side)
  ├─ install WSL platform
  ├─ install NixOS-WSL distro
  ├─ wsl exec: nix-win switch --flake github:jacobbrugh/bootstrap#bootstrap
  └─ wsl exec: nix run github:jacobbrugh/bootstrap

  inside WSL:
  Python bootstrap detects Platform.NIXOS_WSL
  ├─ runs all the existing nixos-wsl phases (prereqs, secrets, ssh, register, switch)
  └─ runs the new phases/windows/* phases that drive the Windows host:
     - scoop install + bootstrap-critical packages
     - openssh: client/server capability install + service startup + auth key
     - psmodules: install required modules
     - tailscale: connect to the tailnet
     - any nix-win-managed config that has to live in Python rather than in nix-win
```

The user-facing interface is unchanged: one `iex` from PowerShell and
the rest of the bootstrap is hands-off until manual permission gates.
