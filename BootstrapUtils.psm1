#Requires -Version 5.1
<#
.SYNOPSIS
    BootstrapUtils - Reusable PowerShell utilities for Windows bootstrap and configuration

.DESCRIPTION
    This module provides common utilities for:
    - Logging with consistent formatting
    - Admin privilege management
    - Scoop package manager operations
    - Winget package manager operations
    - OpenSSH infrastructure setup
    - GitHub SSH key management
    - WSL installation and configuration
    - Tailscale/Headscale connection
    - Environment PATH management

    Used by bootstrap.ps1 (downloaded from gist) and available in PowerShell profile
    after chezmoi apply.

.NOTES
    This file is published in github.com/jacobbrugh/bootstrap alongside bootstrap.ps1 and bootstrap.sh.
    The bootstrap script downloads and imports this module at runtime.
#>

# ============================================================================
# Logging Functions
# ============================================================================

function Write-Info {
    <#
    .SYNOPSIS
        Write an informational message in green
    #>
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Green
}

function Write-Warn {
    <#
    .SYNOPSIS
        Write a warning message in yellow
    #>
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Err {
    <#
    .SYNOPSIS
        Write an error message in red
    #>
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

function Write-Step {
    <#
    .SYNOPSIS
        Write a step progress indicator
    #>
    param(
        [Parameter(Mandatory)][int]$Step,
        [Parameter(Mandatory)][int]$Total,
        [Parameter(Mandatory)][string]$Message
    )
    Write-Host ""
    Write-Host "=== Step $Step/$Total : $Message ===" -ForegroundColor Cyan
}


# ============================================================================
# Admin Functions
# ============================================================================

function Test-AdminElevation {
    <#
    .SYNOPSIS
        Check if the current process is running with Administrator privileges
    .OUTPUTS
        [bool] True if running as Administrator
    #>
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}


function Test-SudoEnabled {
    <#
    .SYNOPSIS
        Check if Windows Sudo for Windows feature is enabled
    #>
    try {
        $val = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Sudo' -Name Enabled -ErrorAction Stop).Enabled
        return $val -ne 0
    } catch {
        return $false
    }
}


$ModSourcePath = $PSScriptRoot
$ModName = Split-Path $ModSourcePath -Leaf
$SystemInstallPath = Join-Path $Env:ProgramFiles "WindowsPowerShell\Modules\$ModName"
$SettingLink = $false
function Test-SystemModuleInstalled {
    $LinkPath = Get-Item -Path $SystemInstallPath -ErrorAction SilentlyContinue
    if ($LinkPath){
        if ($LinkPath.Attributes.HasFlag([System.IO.FileAttributes]::ReparsePoint)) {
            $CurrentTarget = $LinkPath.Target
            if ($CurrentTarget -eq $ModSourcePath) {
                return $true
            }
        }
    }
    return $false
}

function Install-SystemModule {
    if (-not (Test-SystemModuleInstalled)) {
        if (-not $script:SettingLink) {
            $script:SettingLink = $true
            & $script:sudo "pwsh" New-Item -ItemType SymbolicLink -Path $SystemInstallPath -Target $ModSourcePath -Force | Out-Null
            Write-Host "Success! Module '$ModName' is now linked system-wide." -ForegroundColor Green
        }
    }
}


function sudo {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position=0)]
        [string]$__Command,
        [Parameter(ValueFromRemainingArguments, Position=1)]
        [string[]]$__CommandArgs
    )

    Install-SystemModule
    $isElevated = Test-AdminElevation

    if ($isElevated) {
        & $__Command @__CommandArgs
    } else {
        & sudo.exe $__Command @__CommandArgs
    }
}


function Request-AdminElevation {
    <#
    .SYNOPSIS
        Prompt user to relaunch the script as Administrator
    .PARAMETER ScriptPath
        Path to the script to relaunch. If not specified, uses $PSCommandPath from caller.
    .PARAMETER Command
        Command/arguments to pass to the relaunched script
    #>
    param(
        [string]$ScriptPath,
        [string]$Command
    )

    if (-not (Test-AdminElevation)) {
        Write-Warn "Some operations require Administrator privileges:"
        Write-Warn "  - Enabling Windows features (WSL, VirtualMachinePlatform)"
        Write-Warn "  - Modifying system registry settings"
        Write-Host ""

        $response = Read-Host "Would you like to relaunch as Administrator? (y/N)"
        if ($response -eq 'y' -or $response -eq 'Y') {
            if ([string]::IsNullOrEmpty($ScriptPath)) {
                Write-Err "Cannot auto-elevate when running from web. Please run PowerShell as Administrator."
                exit 1
            }

            Start-Process pwsh -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$ScriptPath`" $Command"
            exit 0
        } else {
            Write-Warn "Continuing without elevation. Some features may not be configured."
        }
    }
}

function Invoke-SudoPwshSwitch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('pwsh', 'powershell')]
        [string]$Shell,

        [Parameter(Mandatory)]
        [string]$__Command,

        [Parameter(ValueFromRemainingArguments)]
        [string[]]$__CommandArgs
    )

    $isAdmin = Test-AdminElevation

    $currentShell = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh' } else { 'powershell' }
    if ($isAdmin -and ($currentShell -eq $Shell)) {
        & $__Command @__CommandArgs
        exit $LASTEXITCODE
    }

    # Write-Host $Command
    # $bytes = [System.Text.Encoding]::Unicode.GetBytes($Command)
    # $encoded = [Convert]::ToBase64String($bytes)
    $exe = if ($Shell -eq 'pwsh') { 'pwsh' } else { 'PowerShell.exe' }
    $commandString = (@($__Command) + $__CommandArgs) -join ' '
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($commandString)
    $encodedCommand = [Convert]::ToBase64String($bytes)

    if ($isAdmin) {
        # $p = Start-Process -FilePath $exe -ArgumentList "-NoProfile -EncodedCommand $encoded" -Wait -NoNewWindow -PassThru
        # return $p.ExitCode
        & $exe -EncodedCommand $encodedCommand
    } else {
        Write-Host "Elevating via sudo ($exe)..." -ForegroundColor Yellow
        # sudo $exe -NoProfile -EncodedCommand $encoded
        sudo $exe -EncodedCommand $encodedCommand
    }
}


function Invoke-AsNonAdmin {
    <#
    .SYNOPSIS
        Runs a PowerShell script block with de-elevated (non-admin) privileges.
    .DESCRIPTION
        Useful for installing Scoop or other user-level tools from an Admin script.
        Uses runas.exe with trustlevel:0x20000 to drop privileges.
    .PARAMETER ScriptBlock
        The script block to execute as non-admin
    .EXAMPLE
        Invoke-AsNonAdmin -ScriptBlock { irm get.scoop.sh | iex }
    #>
    param (
        [Parameter(Mandatory)]
        [ScriptBlock]$ScriptBlock
    )

    # Convert the scriptblock to a string and Base64 encode it
    $commandStr = $ScriptBlock.ToString()
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($commandStr)
    $encodedCommand = [Convert]::ToBase64String($bytes)

    # Construct the PowerShell command
    $psCommand = "powershell.exe -EncodedCommand $encodedCommand"

    Write-Host "[Invoke-AsNonAdmin] Launching de-elevated process..." -ForegroundColor Cyan
    # Use runas with the "Basic User" trust level (0x20000)
    Start-Process "runas.exe" -ArgumentList "/machine:amd64 /trustlevel:0x20000 `"$psCommand`"" -NoNewWindow -Wait
}

# ============================================================================
# Environment Functions
# ============================================================================

function Update-PathEnvironment {
    <#
    .SYNOPSIS
        Refresh the PATH environment variable from the registry
    .DESCRIPTION
        Reloads PATH from both Machine and User registry keys without requiring
        a new shell session.
    #>
    $env:PATH = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
}


# ============================================================================
# Scoop Functions
# ============================================================================

function Test-ScoopInstalled {
    <#
    .SYNOPSIS
        Check if Scoop package manager is installed
    .OUTPUTS
        [bool] True if Scoop is available in PATH
    #>
    return [bool](Get-Command scoop -ErrorAction SilentlyContinue)
}

function Install-Scoop {
    <#
    .SYNOPSIS
        Install the Scoop package manager
    .PARAMETER DryRun
        Show what would happen without making changes
    #>
    [CmdletBinding()]
    param([switch]$DryRun)

    if (Test-ScoopInstalled) {
        Write-Info "Scoop is already installed"
        return
    }

    Write-Info "Installing Scoop..."

    if ($DryRun) {
        Write-Info "[DRY RUN] Would install Scoop"
        return
    }

    # Scoop should be installed as non-admin for user-level package management
    if (Test-AdminElevation) {
        Write-Info "Running as admin - installing Scoop via de-elevated process..."
        Invoke-AsNonAdmin -ScriptBlock {
            Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
            Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression
        }
    } else {
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
        Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression
    }

    # Refresh PATH
    Update-PathEnvironment

    if (-not (Test-ScoopInstalled)) {
        Write-Err "Failed to install Scoop"
        exit 1
    }

    Write-Info "Scoop installed successfully"
}

function Install-ScoopBuckets {
    <#
    .SYNOPSIS
        Add standard Scoop buckets (main, extras, nerd-fonts)
    .PARAMETER DryRun
        Show what would happen without making changes
    #>
    [CmdletBinding()]
    param([switch]$DryRun)

    Write-Info "Adding Scoop buckets..."

    if ($DryRun) {
        Write-Info "[DRY RUN] Would add buckets: main, extras, nerd-fonts"
        return
    }

    # Get list of bucket names (scoop bucket list returns objects with Name property)
    $bucketNames = (scoop bucket list 2>$null).Name

    # Main bucket (usually added by default, but ensure it exists)
    if ($bucketNames -notcontains 'main') {
        Write-Info "Adding main bucket..."
        scoop bucket add main
    } else {
        Write-Info "main bucket already added"
    }

    # Add extras bucket (for vscode, etc.)
    if ($bucketNames -notcontains 'extras') {
        Write-Info "Adding extras bucket..."
        scoop bucket add extras
    } else {
        Write-Info "extras bucket already added"
    }

    # Add nerd-fonts bucket
    if ($bucketNames -notcontains 'nerd-fonts') {
        Write-Info "Adding nerd-fonts bucket..."
        scoop bucket add nerd-fonts https://github.com/matthewjberger/scoop-nerd-fonts
    } else {
        Write-Info "nerd-fonts bucket already added"
    }
}

function Install-ScoopPackage {
    <#
    .SYNOPSIS
        Install a package via Scoop
    .PARAMETER Name
        The Scoop package name
    .PARAMETER DisplayName
        Friendly name for logging
    .PARAMETER DryRun
        Show what would happen without making changes
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$DisplayName,
        [switch]$DryRun
    )

    if (-not $DisplayName) { $DisplayName = $Name }

    $installed = scoop list $Name 2>$null
    if ($LASTEXITCODE -eq 0 -and $installed -match $Name) {
        Write-Info "$DisplayName is already installed"
        return
    }

    Write-Info "Installing $DisplayName..."
    if ($DryRun) {
        Write-Info "[DRY RUN] Would run: scoop install $Name"
    } else {
        scoop install $Name
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Failed to install $DisplayName (may need manual installation)"
        }
    }
}

function Invoke-ScoopValidate {
    <#
    .SYNOPSIS
        Validate that Scoop is installed correctly
    .OUTPUTS
        [bool] True if validation passes
    #>
    if (Test-ScoopInstalled) {
        Write-Info "Scoop: OK"
        return $true
    } else {
        Write-Err "Scoop: NOT INSTALLED"
        return $false
    }
}

# ============================================================================
# OpenSSH Functions
# ============================================================================

function New-SshKey {
    <#
    .SYNOPSIS
        Generate a new ED25519 SSH key
    .PARAMETER SshDir
        Directory to store the key (default: ~/.ssh)
    .PARAMETER KeyName
        Name of the key file (default: id_ed25519)
    .PARAMETER Comment
        Comment/email for the key
    .PARAMETER DryRun
        Show what would happen without making changes
    .OUTPUTS
        [bool] True if key exists or was created
    #>
    [CmdletBinding()]
    param(
        [string]$SshDir = (Join-Path $env:USERPROFILE '.ssh'),
        [string]$KeyName = "id_ed25519",
        [string]$Comment,
        [switch]$DryRun
    )

    $keyPath = Join-Path $SshDir $KeyName

    # Create .ssh directory if needed
    if (-not (Test-Path $SshDir)) {
        Write-Info "Creating $SshDir..."
        if (-not $DryRun) {
            New-Item -ItemType Directory -Path $SshDir -Force | Out-Null
        }
    }

    # Check if key already exists
    if (Test-Path $keyPath) {
        Write-Info "SSH key already exists: $keyPath"
        return $true
    }

    Write-Info "Generating SSH key: $keyPath"

    if ($DryRun) {
        Write-Info "[DRY RUN] Would generate SSH key"
        return $true
    }

    if (-not $Comment) {
        $Comment = "$env:USERNAME@$(hostname)"
    }

    ssh-keygen -t ed25519 -C $Comment -f $keyPath -N '""'

    if (-not (Test-Path $keyPath)) {
        Write-Err "Failed to generate SSH key"
        return $false
    }

    Write-Info "SSH key generated successfully"
    return $true
}

function Add-SshKeyToAgent {
    <#
    .SYNOPSIS
        Add an SSH key to the ssh-agent
    .PARAMETER KeyPath
        Path to the private key file
    .PARAMETER DryRun
        Show what would happen without making changes
    .OUTPUTS
        [bool] True if key was added or already present
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$KeyPath,
        [switch]$DryRun
    )

    if ($DryRun) {
        Write-Info "[DRY RUN] Would add key to ssh-agent"
        return $true
    }

    # Get fingerprint of the key file
    $keyFingerprint = ssh-keygen -lf $KeyPath 2>$null
    if (-not $keyFingerprint) {
        Write-Warn "Could not read key fingerprint from $KeyPath"
        return $false
    }
    # Extract just the fingerprint hash (e.g., SHA256:xxxx)
    if ($keyFingerprint -match '(SHA256:\S+)') {
        $fingerprint = $Matches[1]
    } else {
        Write-Warn "Could not parse key fingerprint"
        return $false
    }

    # Check if key is already in agent by fingerprint
    $agentKeys = ssh-add -l 2>&1
    if ($agentKeys -match [regex]::Escape($fingerprint)) {
        Write-Info "SSH key already in agent"
        return $true
    }

    Write-Info "Adding SSH key to agent..."
    ssh-add $KeyPath 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Info "Key added to ssh-agent"
        return $true
    } else {
        Write-Warn "Failed to add key to ssh-agent"
        return $false
    }
}


function Invoke-SshValidate {
    <#
    .SYNOPSIS
        Validate SSH infrastructure
    .PARAMETER SshKeyPath
        Path to the SSH private key to check
    .PARAMETER GitHubUser
        GitHub username to check SSH access for
    .OUTPUTS
        [bool] True if all validations pass
    #>
    [CmdletBinding()]
    param(
        [string]$SshKeyPath,
        [string]$GitHubUser
    )

    $allOk = $true

    # Check OpenSSH Client
    $sshClient = Get-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0' -ErrorAction SilentlyContinue
    if ($sshClient -and $sshClient.State -eq 'Installed') {
        Write-Info "OpenSSH Client: INSTALLED"
    } else {
        Write-Warn "OpenSSH Client: NOT INSTALLED"
        $allOk = $false
    }

    # Check OpenSSH Server
    $sshServer = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction SilentlyContinue
    if ($sshServer -and $sshServer.State -eq 'Installed') {
        Write-Info "OpenSSH Server: INSTALLED"
    } else {
        Write-Warn "OpenSSH Server: NOT INSTALLED"
        $allOk = $false
    }

    # Check ssh-agent
    $sshAgent = Get-Service ssh-agent -ErrorAction SilentlyContinue
    if ($sshAgent -and $sshAgent.Status -eq 'Running') {
        Write-Info "ssh-agent: RUNNING ($($sshAgent.StartType))"
    } else {
        Write-Warn "ssh-agent: NOT RUNNING"
        $allOk = $false
    }

    # Check SSH key
    if ($SshKeyPath -and (Test-Path $SshKeyPath)) {
        Write-Info "SSH key: OK ($SshKeyPath)"
    } elseif ($SshKeyPath) {
        Write-Warn "SSH key: NOT FOUND"
        $allOk = $false
    }

    # Check GitHub access
    if ($GitHubUser -and (Test-GitHubSshAccess -GitHubUser $GitHubUser)) {
        Write-Info "GitHub SSH: AUTHENTICATED"
    } elseif ($GitHubUser) {
        Write-Warn "GitHub SSH: NOT AUTHENTICATED"
        $allOk = $false
    }

    return $allOk
}

# ============================================================================
# GitHub Functions
# ============================================================================

function Get-GitHubToken {
    <#
    .SYNOPSIS
        Get a GitHub token from environment or 1Password
    .PARAMETER OpVault
        1Password vault name
    .PARAMETER OpItem
        1Password item name
    .PARAMETER OpField
        1Password field name
    .OUTPUTS
        [string] The GitHub token, or $null if not found
    #>
    [CmdletBinding()]
    param(
        [string]$OpVault = 'Personal',
        [string]$OpItem = 'GitHub PAT (SSH Key Upload)',
        [string]$OpField = 'credential'
    )

    # Check environment variable first
    if ($env:GITHUB_TOKEN) {
        Write-Info "Using GITHUB_TOKEN from environment"
        return $env:GITHUB_TOKEN
    }

    # Try 1Password
    if (-not (Get-Command op -ErrorAction SilentlyContinue)) {
        Write-Warn "1Password CLI not found and GITHUB_TOKEN not set"
        return $null
    }

    Write-Info "Fetching GitHub token from 1Password..."

    # Check if signed in
    $opAccount = op account get 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Info "Please sign in to 1Password..."
        op signin 2>&1 | Out-Null
    }

    $token = op read "op://$OpVault/$OpItem/$OpField" 2>&1
    if ($LASTEXITCODE -eq 0 -and $token) {
        return $token
    }

    Write-Warn "Failed to retrieve GitHub token from 1Password"
    return $null
}

function Test-GitHubSshAccess {
    <#
    .SYNOPSIS
        Test SSH access to GitHub
    .PARAMETER GitHubHost
        GitHub host (default: github.com)
    .PARAMETER GitHubUser
        Expected GitHub username
    .OUTPUTS
        [bool] True if SSH access is working
    #>
    [CmdletBinding()]
    param(
        [string]$GitHubHost = 'github.com',
        [string]$GitHubUser
    )

    Write-Info "Testing GitHub SSH access..."

    $result = ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new "git@$GitHubHost" 2>&1
    if ($GitHubUser -and $result -match "Hi $GitHubUser") {
        Write-Info "GitHub SSH access confirmed for $GitHubUser"
        return $true
    } elseif ($result -match "Hi \w+") {
        Write-Info "GitHub SSH access working"
        return $true
    }

    return $false
}

function Add-SshKeyToGitHub {
    <#
    .SYNOPSIS
        Upload an SSH public key to GitHub
    .PARAMETER PublicKeyPath
        Path to the public key file
    .PARAMETER KeyTitle
        Title for the key on GitHub (default: key filename)
    .PARAMETER Token
        GitHub personal access token with admin:public_key scope
    .PARAMETER DryRun
        Show what would happen without making changes
    .OUTPUTS
        [bool] True if key was uploaded or already exists
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PublicKeyPath,
        [string]$KeyTitle,
        [Parameter(Mandatory)][string]$Token,
        [switch]$DryRun
    )

    if (-not (Test-Path $PublicKeyPath)) {
        Write-Err "Public key not found: $PublicKeyPath"
        return $false
    }

    if ($DryRun) {
        Write-Info "[DRY RUN] Would upload SSH key to GitHub"
        return $true
    }

    $pubKeyContent = Get-Content $PublicKeyPath -Raw
    if (-not $KeyTitle) {
        $KeyTitle = [System.IO.Path]::GetFileNameWithoutExtension($PublicKeyPath)
    }

    Write-Info "Uploading SSH key to GitHub as '$KeyTitle'..."

    $headers = @{
        'Accept' = 'application/vnd.github+json'
        'Authorization' = "Bearer $Token"
        'X-GitHub-Api-Version' = '2022-11-28'
    }

    $body = @{
        title = $KeyTitle
        key = $pubKeyContent.Trim()
    } | ConvertTo-Json

    try {
        $response = Invoke-RestMethod -Uri 'https://api.github.com/user/keys' `
            -Method Post -Headers $headers -Body $body -ContentType 'application/json'
        Write-Info "SSH key uploaded successfully (ID: $($response.id))"
        return $true
    } catch {
        if ($_.Exception.Response.StatusCode -eq 422) {
            Write-Info "SSH key already exists on GitHub"
            return $true
        }
        Write-Err "Failed to upload SSH key: $_"
        return $false
    }
}

# ============================================================================
# WSL Functions
# ============================================================================

function Get-WslInstanceName {
    <#
    .SYNOPSIS
        Generate a WSL instance name from hostname or environment
    .DESCRIPTION
        If WSL_INSTANCE_NAME env var is set, use that.
        If hostname matches 'pc#', return 'wsl#'.
        Otherwise return 'wsl-<uuid>'.
    .OUTPUTS
        [string] The WSL instance name
    #>
    if ($env:WSL_INSTANCE_NAME) {
        return $env:WSL_INSTANCE_NAME
    }

    $hostname = hostname
    if ($hostname -match '^pc(\d+)$') {
        return "wsl$($Matches[1])"
    }

    # Fallback to UUID-based name
    return "wsl-$([guid]::NewGuid().ToString().Substring(0,8))"
}

function Install-WslPlatform {
    <#
    .SYNOPSIS
        Install WSL platform without a default distribution
    .PARAMETER DryRun
        Show what would happen without making changes
    .OUTPUTS
        [bool] True if WSL is installed or was installed successfully
    #>
    [CmdletBinding()]
    param([switch]$DryRun)

    # Check if WSL is already installed
    $null = wsl --status 2>&1
    $wslInstalled = $LASTEXITCODE -eq 0

    if (-not $wslInstalled) {
        Write-Info "Installing WSL platform..."
        if ($DryRun) {
            Write-Info "[DRY RUN] Would run: wsl --install --no-distribution"
        } else {
            wsl --install --no-distribution
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "WSL installation may require a reboot. Please reboot and re-run."
                return $false
            }
        }
    } else {
        Write-Info "WSL platform already installed"
    }

    return $true
}

function Install-NixosWsl {
    <#
    .SYNOPSIS
        Download and import NixOS-WSL
    .PARAMETER InstanceName
        Name for the WSL instance
    .PARAMETER InstallPath
        Path to install the WSL instance
    .PARAMETER ReleaseUrl
        URL to the NixOS-WSL tarball
    .PARAMETER DryRun
        Show what would happen without making changes
    .OUTPUTS
        [bool] True if installation succeeded
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$InstanceName,
        [Parameter(Mandatory)][string]$InstallPath,
        [string]$ReleaseUrl = 'https://github.com/nix-community/NixOS-WSL/releases/latest/download/nixos-wsl.tar.gz',
        [switch]$DryRun
    )

    # Check if instance already exists
    $distroList = wsl --list --quiet 2>$null
    if ($distroList -contains $InstanceName) {
        Write-Info "WSL instance '$InstanceName' already exists"
        return $true
    }

    Write-Info "Installing NixOS-WSL as '$InstanceName'..."

    if ($DryRun) {
        Write-Info "[DRY RUN] Would download and import NixOS-WSL"
        return $true
    }

    # Create install directory
    if (-not (Test-Path $InstallPath)) {
        New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
    }

    # Download NixOS-WSL image
    $imagePath = Join-Path $env:TEMP 'nixos.wsl'
    Write-Info "Downloading NixOS-WSL from $ReleaseUrl..."

    try {
        Invoke-WebRequest -Uri $ReleaseUrl -OutFile $imagePath -UseBasicParsing
    } catch {
        Write-Err "Failed to download NixOS-WSL: $_"
        return $false
    }

    # Import the distribution
    Write-Info "Importing NixOS-WSL..."
    wsl --import $InstanceName $InstallPath $imagePath

    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to import NixOS-WSL"
        return $false
    }

    # Clean up downloaded image
    Remove-Item $imagePath -Force -ErrorAction SilentlyContinue

    Write-Info "NixOS-WSL installed successfully as '$InstanceName'"
    return $true
}

function Invoke-WslValidate {
    <#
    .SYNOPSIS
        Validate WSL installation
    .PARAMETER InstanceName
        Name of the WSL instance to check
    .OUTPUTS
        [bool] True if all validations pass
    #>
    [CmdletBinding()]
    param([string]$InstanceName)

    $allOk = $true

    # Check WSL platform
    $null = wsl --status 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Info "WSL platform: INSTALLED"
    } else {
        Write-Err "WSL platform: NOT INSTALLED"
        $allOk = $false
    }

    # Check specific instance if provided
    if ($InstanceName) {
        $distros = wsl --list --quiet 2>$null
        if ($distros -match "^$InstanceName$") {
            Write-Info "WSL instance ($InstanceName): INSTALLED"

            # Check if running
            $running = wsl -l -v 2>$null | Where-Object { $_ -match $InstanceName -and $_ -match 'Running' }
            if ($running) {
                Write-Info "WSL instance state: RUNNING"
            } else {
                Write-Info "WSL instance state: STOPPED"
            }
        } else {
            Write-Warn "WSL instance ($InstanceName): NOT INSTALLED"
            $allOk = $false
        }
    }
    return $allOk
}

# ============================================================================
# PSResource / DSC Functions
# ============================================================================

function Install-PsModules {
    <#
    .SYNOPSIS
        Install PowerShell modules declared in psmodules.psd1
    .DESCRIPTION
        Uses Import-PowerShellDataFile (native, safe, supports comments).
        Installs pwsh7_modules via Install-PSResource, windows_powershell_modules
        via Install-Module in a powershell.exe subprocess.
    #>
    [CmdletBinding()]
    param(
        [string]$Path = (Join-Path $env:USERPROFILE '.config\powershell\psmodules.psd1')
    )
    Write-Host "`n=== PowerShell DSC Module Installer ===`n" -ForegroundColor Cyan

    if (-not (Test-Path $Path)) { throw "Config not found: $Path" }
    $config = Import-PowerShellDataFile -Path $Path

    # Install Windows PowerShell modules (shell out to PS 5.1)
    if ($config.windows_powershell_modules) {
        Write-Host "--- Windows PowerShell Modules ---" -ForegroundColor Cyan
        $modArgs = $config.windows_powershell_modules | ForEach-Object { "$($_.Name)=$($_.Version)" }
        $argString = $modArgs -join ','

        Invoke-SudoPwshSwitch 'powershell' Install-WindowsPowerShellModules -ModuleSpecs `"$argString`"
    }

    # Install PowerShell 7 modules (current process)
    if ($config.pwsh7_modules) {
        Write-Host "--- PowerShell 7 Modules ---" -ForegroundColor Cyan

        # Ensure PSGallery is trusted
        Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue

        foreach ($mod in $config.pwsh7_modules) {
            $installed = Get-Module -ListAvailable -Name $mod.Name -ErrorAction SilentlyContinue |
                Where-Object { $_.Version -ge [version]$mod.Version } |
                Select-Object -First 1

            if ($installed) {
                Write-Host "[OK] $($mod.Name) v$($installed.Version)" -ForegroundColor Green
            } else {
                Write-Host "[INSTALL] $($mod.Name) v$($mod.Version)..." -ForegroundColor Yellow
                Install-PSResource -Name $mod.Name -Version $mod.Version -TrustRepository -Scope AllUsers -ErrorAction Stop
                Write-Host "[OK] $($mod.Name) installed" -ForegroundColor Green
            }
        }
    }
    Write-Host "`nPowerShell DSC modules ready." -ForegroundColor Green
}

function Install-WindowsPowerShellModules {
    <#
    .SYNOPSIS
        Install modules for Windows PowerShell (runs in powershell.exe)
    .DESCRIPTION
        Uses Install-Module (PowerShellGet v2) which is available in PS5.1.
    .PARAMETER ModuleSpecs
        Comma-separated Name=Version pairs
    #>
    param([Parameter(Mandatory)][string]$ModuleSpecs)

    # Ensure NuGet is installed silently to avoid blocking prompts
    if (-not (Get-PackageProvider -ListAvailable -Name "NuGet" -ErrorAction SilentlyContinue)) {
        Write-Host "Installing NuGet provider..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Install-PackageProvider -Name "NuGet" -MinimumVersion 2.8.5.201 -Force -Scope AllUsers
    }
    # Ensure PSGallery is trusted
    Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue

    foreach ($spec in ($ModuleSpecs -split ',')) {
        $name, $version = $spec -split '='

        $installed = Get-Module -ListAvailable -Name $name -ErrorAction SilentlyContinue |
            Where-Object { $_.Version -ge [version]$version } |
            Select-Object -First 1

        if ($installed) {
            Write-Host "[OK] $name v$($installed.Version)" -ForegroundColor Green
        } else {
            Write-Host "[INSTALL] $name v$version..." -ForegroundColor Yellow
            Install-Module -Name $name -RequiredVersion $version -Force -AllowClobber -Scope AllUsers -ErrorAction Stop
            Write-Host "[OK] $name installed" -ForegroundColor Green
        }
    }
}

# ============================================================================
# Tailscale Functions
# ============================================================================

function Test-TailscaleConnected {
    <#
    .SYNOPSIS
        Check if connected to a Tailscale tailnet
    .OUTPUTS
        [bool] True if connected to a tailnet
    #>
    [CmdletBinding()]
    param()

    if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
        return $false
    }

    $status = tailscale status 2>&1
    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    # If we get status output without error, we're connected
    # "Tailscale is stopped" or similar errors return non-zero exit code
    return $true
}

function Connect-Tailscale {
    <#
    .SYNOPSIS
        Connect to a Tailscale tailnet
    .PARAMETER LoginServer
        Custom login server URL (e.g., for Headscale)
    .PARAMETER DryRun
        Show what would happen without making changes
    .OUTPUTS
        [bool] True if connection succeeded or already connected
    #>
    [CmdletBinding()]
    param(
        [string]$LoginServer,
        [switch]$DryRun
    )

    if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
        Write-Err "Tailscale is not installed"
        return $false
    }

    if (Test-TailscaleConnected) {
        Write-Info "Already connected to Tailscale tailnet"
        return $true
    }

    Write-Info "Connecting to Tailscale..."

    if ($DryRun) {
        if ($LoginServer) {
            Write-Info "[DRY RUN] Would run: sudo tailscale up --login-server '$LoginServer'"
        } else {
            Write-Info "[DRY RUN] Would run: sudo tailscale up"
        }
        return $true
    }

    if ($LoginServer) {
        sudo tailscale up --login-server $LoginServer
    } else {
        sudo tailscale up
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to connect to Tailscale"
        return $false
    }

    Write-Info "Connected to Tailscale successfully"
    return $true
}

function Invoke-TailscaleValidate {
    <#
    .SYNOPSIS
        Validate Tailscale installation and connection
    .OUTPUTS
        [bool] True if validation passes
    #>
    [CmdletBinding()]
    param()

    $allOk = $true

    if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
        Write-Err "Tailscale: NOT INSTALLED"
        return $false
    }

    Write-Info "Tailscale: INSTALLED"

    if (Test-TailscaleConnected) {
        # Get more details about the connection
        $status = tailscale status --json 2>$null | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($status -and $status.Self) {
            Write-Info "Tailscale: CONNECTED ($($status.Self.HostName))"
        } else {
            Write-Info "Tailscale: CONNECTED"
        }
    } else {
        Write-Warn "Tailscale: NOT CONNECTED"
        $allOk = $false
    }

    return $allOk
}

# ============================================================================
# Export Module Members
# ============================================================================

Export-ModuleMember -Function @(
    # Logging
    'Write-Info'
    'Write-Warn'
    'Write-Err'
    'Write-Step'

    # Environment
    'Update-PathEnvironment'

    # Admin
    'Test-AdminElevation'
    'Test-SudoEnabled'
    'sudo'
    'Request-AdminElevation'
    'Invoke-SudoPwshSwitch'
    'Invoke-AsNonAdmin'

    # Scoop
    'Test-ScoopInstalled'
    'Install-Scoop'
    'Install-ScoopBuckets'
    'Install-ScoopPackage'
    'Invoke-ScoopValidate'

    # OpenSSH
    'New-SshKey'
    'Add-SshKeyToAgent'
    'Invoke-SshValidate'

    # GitHub
    'Get-GitHubToken'
    'Test-GitHubSshAccess'
    'Add-SshKeyToGitHub'

    # WSL
    'Get-WslInstanceName'
    'Install-WslPlatform'
    'Install-NixosWsl'
    'Invoke-WslValidate'

    # PSResource / DSC
    'Install-PsModules'
    'Install-WindowsPowerShellModules'

    # Tailscale
    'Test-TailscaleConnected'
    'Connect-Tailscale'
    'Invoke-TailscaleValidate'
)
