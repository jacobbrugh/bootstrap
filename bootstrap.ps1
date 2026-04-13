<#
.SYNOPSIS
    Windows bootstrap: install WSL2 + NixOS-WSL, apply minimal Windows config,
    then run the Unix bootstrap app inside WSL.

.DESCRIPTION
    1. Install WSL2 + NixOS-WSL
    2. Apply minimal Windows config via nix-win (SSH, age, git — enough to
       reach the private dotfiles repo)
    3. Shell into WSL and run: nix run github:jacobbrugh/bootstrap
       (which handles SSH keygen, GH auth, age key, then dotfiles switch)

.PARAMETER DryRun
    Show what would happen without making changes.

.PARAMETER SkipNixWin
    Skip the nix-win bootstrap step (useful if re-running after a reboot).

.EXAMPLE
    iex ((New-Object System.Net.WebClient).DownloadString('https://jacobbrugh.net/bootstrap.ps1'))

.NOTES
    Set WSL_INSTANCE_NAME env var to override the default instance name.
    Default: wsl<N> for pc<N> hostnames, otherwise 'NixOS'.
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipNixWin
)

$ErrorActionPreference = 'Stop'

$RepoRaw        = 'https://raw.githubusercontent.com/jacobbrugh/bootstrap/main'
$NixosWslUrl    = 'https://github.com/nix-community/NixOS-WSL/releases/latest/download/nixos.wsl'
$BootstrapFlake = 'github:jacobbrugh/bootstrap'

# Derive instance name: pc1 -> wsl1, else 'NixOS'
$WslInstanceName = if ($env:WSL_INSTANCE_NAME) {
    $env:WSL_INSTANCE_NAME
} elseif ((hostname) -match '^pc(\d+)$') {
    "wsl$($Matches[1])"
} else {
    'NixOS'
}
$WslInstallPath = Join-Path $env:USERPROFILE "wsl\$WslInstanceName"

# ── Load BootstrapUtils ───────────────────────────────────────────────────────
Write-Host "Loading BootstrapUtils..." -ForegroundColor Cyan
$modulePath = Join-Path $env:TEMP 'BootstrapUtils.psm1'
Invoke-WebRequest -Uri "$RepoRaw/BootstrapUtils.psm1" -OutFile $modulePath -UseBasicParsing
Import-Module $modulePath -Force -DisableNameChecking

# ── Step 1: WSL platform + NixOS-WSL ─────────────────────────────────────────
Write-Step 1 3 "Install WSL + NixOS-WSL"

if (-not (Install-WslPlatform -DryRun:$DryRun)) {
    Write-Host ""
    Write-Host "WSL requires a reboot. Please reboot and re-run this script." -ForegroundColor Yellow
    exit 1
}

$null = Install-NixosWsl `
    -InstanceName $WslInstanceName `
    -InstallPath $WslInstallPath `
    -ReleaseUrl $NixosWslUrl `
    -DryRun:$DryRun

# ── Step 2: Minimal Windows config via nix-win ───────────────────────────────
# Installs git, 1password-cli, age, and OpenSSH from inside WSL.
# Gives us the tools needed to reach the private dotfiles repo.
Write-Step 2 3 "Apply bootstrap Windows config (nix-win)"

if ($SkipNixWin) {
    Write-Info "Skipping nix-win step (--SkipNixWin)"
} elseif ($DryRun) {
    Write-Info "[DRY RUN] Would run inside WSL: nix-win switch --flake $BootstrapFlake#bootstrap"
} else {
    wsl -d $WslInstanceName -- nix run `
        --extra-experimental-features "nix-command flakes" `
        github:jacobbrugh/nix-win `
        -- switch --flake "$BootstrapFlake#bootstrap"

    if ($LASTEXITCODE -ne 0) {
        Write-Warn "nix-win bootstrap step failed — continuing anyway"
        Write-Warn "Re-run manually: wsl -d $WslInstanceName -- nix run github:jacobbrugh/nix-win -- switch --flake $BootstrapFlake#bootstrap"
    }
}

# ── Step 3: Unix bootstrap inside WSL ────────────────────────────────────────
# Handles: SSH keygen, GitHub auth, age key, dotfiles switch (NixOS + nix-win).
Write-Step 3 3 "Run Unix bootstrap inside WSL"

if ($DryRun) {
    Write-Info "[DRY RUN] Would run inside WSL: nix run $BootstrapFlake"
} else {
    wsl -d $WslInstanceName -- sh -c "nix run --extra-experimental-features 'nix-command flakes' $BootstrapFlake"
}

Write-Host ""
Write-Host "Bootstrap complete." -ForegroundColor Green
Write-Host ""
