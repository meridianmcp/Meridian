# install-windows.ps1 -- install the Meridian standalone binary (meridian.exe) on
# Windows. The Windows counterpart to install.sh: downloads the latest release
# binary into ~/.local/bin and puts that directory on the user PATH so `meridian`
# works in a new terminal.
#
#   irm https://usemeridian.us/install-windows.ps1 | iex
#
# cf8a90ec -- pass -Tray to install meridian-tray.exe instead: the same local
# server as meridian.exe, wrapped in a pystray system-tray icon (Open Dashboard /
# Status / View Logs / Restart / Quit -- see meridian/tray_main.py) rather than a
# bare CLI. Built by the same PyInstaller pipeline, published to the same GitHub
# release, but was never reachable through this documented install path until now.
#   & ([scriptblock]::Create((irm https://usemeridian.us/install-windows.ps1))) -Tray
# Add -Autostart to register the tray companion for this Windows user at sign-in.
# It is opt-in; a normal -Tray install only creates the Start Menu shortcut.
#   & ([scriptblock]::Create((irm https://usemeridian.us/install-windows.ps1))) -Tray -Autostart
#
# The -Tray path also now creates a per-user Start Menu shortcut and registers
# a Programs-and-Features / Settings > Apps entry (HKCU, no admin required --
# same no-admin philosophy as the PATH handling below), and a new -Uninstall
# switch reverses exactly that: removes meridian-tray.exe, the Start Menu
# shortcut, and the registry entry. It does NOT touch the shared
# ~/.local/bin directory or the user PATH -- see the "-Uninstall: reverse
# exactly what the -Tray path installs" comment below for why. Uninstall from
# Windows Settings works even when the
# original install ran via `irm | iex` with no local file: the -Tray path
# saves a runnable copy of this script next to meridian-tray.exe for the
# registered UninstallString to invoke later.
#   & ([scriptblock]::Create((irm https://usemeridian.us/install-windows.ps1))) -Uninstall
# Add -ConfigureZotero to open the optional local-only Zotero setup after
# installing; interactive -Tray installs offer the same choice by default.
#
param(
    [switch]$Tray,
    [switch]$Autostart,
    [switch]$ConfigureZotero,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$TargetRepo = (Get-Location).Path
# Captured once, at top level: $PSCommandPath is only populated when this
# script runs from a saved .ps1 file (-File ...); piped `irm | iex` execution
# leaves it empty. Passed explicitly into Save-MeridianUninstallerCopy below
# rather than re-read inside a function, since behavior must not depend on
# scope-specific automatic-variable resolution.
$ScriptSelfPath = $PSCommandPath

# ---- GUI installer support: Start Menu shortcut + Add/Remove Programs ------
# Shared by the -Tray install path (below) and -Uninstall (see the top-level
# -Uninstall short-circuit further down). Defined at top level, same reason
# Get-MeridianDeviceToken/Get-MeridianCachedToken are top level in install.ps1
# (cee295bd bug fix there): must be reachable regardless of which branch runs.
$MeridianUninstallKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Meridian"
$MeridianRunKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$MeridianAutostartValueName = "MeridianTray"
$MeridianStartMenuShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Meridian.lnk"
$MeridianTrayInstallerSelfUrl = "https://usemeridian.us/install-windows.ps1"

function Enable-MeridianAutostart {
    param([Parameter(Mandatory = $true)][string]$TargetPath)
    try {
        New-Item -Path $MeridianRunKeyPath -Force | Out-Null
        $command = '"{0}"' -f $TargetPath
        New-ItemProperty -Path $MeridianRunKeyPath -Name $MeridianAutostartValueName `
            -Value $command -PropertyType String -Force | Out-Null
        Write-Host "Enabled Meridian tray start at sign-in."
        return $true
    } catch {
        Write-Warning "Could not enable Meridian tray start at sign-in: $($_.Exception.Message)"
        return $false
    }
}

function Remove-MeridianAutostart {
    if (-not (Test-Path -LiteralPath $MeridianRunKeyPath)) {
        return
    }
    $entry = Get-ItemProperty -LiteralPath $MeridianRunKeyPath `
        -Name $MeridianAutostartValueName -ErrorAction SilentlyContinue
    if ($null -eq $entry) {
        return
    }
    try {
        Remove-ItemProperty -LiteralPath $MeridianRunKeyPath -Name $MeridianAutostartValueName `
            -Force -ErrorAction Stop
        Write-Host "Removed Meridian tray start at sign-in."
    } catch {
        Write-Warning "Could not remove Meridian tray start at sign-in: $($_.Exception.Message)"
    }
}

function New-MeridianStartMenuShortcut {
    <#
    .SYNOPSIS
      Create/refresh a Start Menu shortcut to meridian-tray.exe in the
      CURRENT USER's Start Menu Programs folder (never the all-users one --
      matches this script's per-user-only, no-admin philosophy). Uses the
      standard PowerShell-native WScript.Shell COM object, no extra
      dependency. Best-effort: a failure here must never abort the install.
    #>
    param([Parameter(Mandatory = $true)][string]$TargetPath)
    try {
        $startMenuDir = Split-Path $MeridianStartMenuShortcut -Parent
        if (-not (Test-Path -LiteralPath $startMenuDir)) {
            New-Item -ItemType Directory -Force -Path $startMenuDir | Out-Null
        }
        $wshShell = New-Object -ComObject WScript.Shell
        $shortcut = $wshShell.CreateShortcut($MeridianStartMenuShortcut)
        $shortcut.TargetPath = $TargetPath
        $shortcut.WorkingDirectory = Split-Path $TargetPath -Parent
        $shortcut.IconLocation = "$TargetPath,0"
        $shortcut.Description = "Meridian -- local server + tray icon"
        $shortcut.Save()
        Write-Host "Created Start Menu shortcut: $MeridianStartMenuShortcut"
        return $true
    } catch {
        Write-Warning "Could not create the Start Menu shortcut: $($_.Exception.Message)"
        return $false
    }
}

function Remove-MeridianStartMenuShortcut {
    <# Idempotent: safe to call even when the shortcut was never created or
       was already removed by hand. #>
    if (Test-Path -LiteralPath $MeridianStartMenuShortcut) {
        try {
            Remove-Item -LiteralPath $MeridianStartMenuShortcut -Force -ErrorAction Stop
            Write-Host "Removed Start Menu shortcut: $MeridianStartMenuShortcut"
        } catch {
            Write-Warning "Could not remove the Start Menu shortcut: $($_.Exception.Message)"
        }
    } else {
        Write-Host "Start Menu shortcut already absent: $MeridianStartMenuShortcut"
    }
}

function Save-MeridianUninstallerCopy {
    <#
    .SYNOPSIS
      Persist a runnable copy of THIS script next to meridian-tray.exe so the
      UninstallString registered in Add/Remove Programs (see
      Register-MeridianUninstallEntry) has something to invoke later --
      clicking "Uninstall" in Windows Settings can happen days or weeks after
      an install that may have run via `irm ... | iex` with no local file at
      all. Prefers copying the actually-running local file (a -File
      invocation); falls back to re-downloading a fresh copy from the
      canonical URL when there is none. Best-effort: returns $null on total
      failure rather than throwing -- the caller skips registering the
      Add/Remove Programs entry in that case (a broken UninstallString would
      be worse than no entry at all).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$DestDir,
        [string]$LocalSourcePath
    )
    $dest = Join-Path $DestDir "install-windows.ps1"
    try {
        if (-not [string]::IsNullOrWhiteSpace($LocalSourcePath) -and (Test-Path -LiteralPath $LocalSourcePath)) {
            Copy-Item -LiteralPath $LocalSourcePath -Destination $dest -Force -ErrorAction Stop
            return $dest
        }
    } catch {
        # Fall through to the network fallback below.
    }
    try {
        Invoke-WebRequest -Uri $MeridianTrayInstallerSelfUrl -OutFile $dest -UseBasicParsing -ErrorAction Stop
        if ((Test-Path -LiteralPath $dest) -and ((Get-Item -LiteralPath $dest).Length -gt 0)) {
            return $dest
        }
    } catch {}
    return $null
}

function Register-MeridianUninstallEntry {
    <#
    .SYNOPSIS
      Register Meridian under the CURRENT USER's Uninstall key so it shows in
      Settings > Apps > Installed apps and the classic Control Panel
      Add-or-Remove-Programs list, without needing an MSI/WiX/Inno Setup
      packaging pipeline. HKCU (not HKLM) -- no admin rights required, same
      as every other write this script makes.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ExePath,
        [Parameter(Mandatory = $true)][string]$InstallDir,
        [string]$Version,
        [string]$LocalSourcePath
    )
    $uninstallerPath = Save-MeridianUninstallerCopy -DestDir $InstallDir -LocalSourcePath $LocalSourcePath
    if (-not $uninstallerPath) {
        Write-Warning "Could not save a runnable uninstaller copy -- skipping Add/Remove Programs registration (a broken entry would be worse than none). You can still remove meridian-tray.exe by hand from $InstallDir."
        return $false
    }
    try {
        if (-not (Test-Path -LiteralPath $MeridianUninstallKeyPath)) {
            New-Item -Path $MeridianUninstallKeyPath -Force | Out-Null
        }
        $uninstallCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallerPath`" -Uninstall"
        $sizeKB = 0
        try { $sizeKB = [int][math]::Round((Get-Item -LiteralPath $ExePath).Length / 1KB) } catch {}
        $displayVersion = if ($Version) { $Version -replace '^v', '' } else { "0.0.0" }
        # Value names/types per Microsoft's documented Uninstall registry key
        # convention (Settings > Apps reads both HKLM and HKCU under this same
        # subpath). DisplayName/DisplayVersion/Publisher/UninstallString/
        # InstallLocation/DisplayIcon/InstallDate are REG_SZ; EstimatedSize/
        # NoModify/NoRepair are REG_DWORD. NoModify+NoRepair=1 because this is
        # a script-based install with no Modify/Repair flow to offer --
        # leaving them unset shows greyed-out buttons that do nothing.
        $props = @{
            DisplayName     = "Meridian"
            DisplayVersion  = $displayVersion
            Publisher       = "Meridian"
            UninstallString = $uninstallCmd
            InstallLocation = $InstallDir
            DisplayIcon     = "$ExePath,0"
            EstimatedSize   = $sizeKB
            NoModify        = 1
            NoRepair        = 1
            InstallDate     = (Get-Date -Format "yyyyMMdd")
        }
        foreach ($name in $props.Keys) {
            $value = $props[$name]
            $valueKind = if ($value -is [int]) { "DWord" } else { "String" }
            New-ItemProperty -Path $MeridianUninstallKeyPath -Name $name -Value $value -PropertyType $valueKind -Force | Out-Null
        }
        Write-Host "Registered Meridian in Add/Remove Programs (Settings > Apps > Installed apps)."
        return $true
    } catch {
        Write-Warning "Could not register the Add/Remove Programs entry: $($_.Exception.Message)"
        return $false
    }
}

function Remove-MeridianUninstallEntry {
    <# Idempotent: safe to call even when the entry was never created or was
       already removed by hand. #>
    if (Test-Path -LiteralPath $MeridianUninstallKeyPath) {
        try {
            Remove-Item -LiteralPath $MeridianUninstallKeyPath -Recurse -Force -ErrorAction Stop
            Write-Host "Removed Add/Remove Programs entry: $MeridianUninstallKeyPath"
        } catch {
            Write-Warning "Could not remove the Add/Remove Programs entry: $($_.Exception.Message)"
        }
    } else {
        Write-Host "Add/Remove Programs entry already absent: $MeridianUninstallKeyPath"
    }
}

# ---- -Uninstall: reverse exactly what the -Tray path installs --------------
# Short-circuits before -Tray / uv / meridian.exe logic below -- -Uninstall is
# a standalone action, not a modifier of a normal install run. Every step
# checks existence first and is independently try/caught, so a partial prior
# manual removal (or a second -Uninstall run) never errors out partway
# through -- each piece is removed if present, reported either way.
#
# Deliberately does NOT touch ~/.local\bin or the user PATH: that directory
# is a SHARED per-user bin directory (uv, serena, and other unrelated tools
# commonly live there too, confirmed on real installs), not something this
# installer owns exclusively -- unlike install.ps1's dedicated
# $env:APPDATA\meridian directory, stripping it from PATH here could silently
# break unrelated tools. Only the meridian-tray.exe file itself is removed.
if ($Uninstall) {
    Write-Host "Uninstalling the Meridian tray/GUI app..." -ForegroundColor Cyan
    $binDir = Join-Path $env:USERPROFILE ".local\bin"
    $exePath = Join-Path $binDir "meridian-tray.exe"
    $uninstallerCopy = Join-Path $binDir "install-windows.ps1"

    if (Test-Path -LiteralPath $exePath) {
        try {
            Remove-Item -LiteralPath $exePath -Force -ErrorAction Stop
            Write-Host "Removed $exePath"
        } catch {
            Write-Warning "Could not remove $exePath -- it may still be running. Close Meridian (tray icon > Quit) and try again. ($($_.Exception.Message))"
        }
    } else {
        Write-Host "meridian-tray.exe not found at $exePath (already removed)."
    }

    Remove-MeridianStartMenuShortcut
    Remove-MeridianAutostart
    Remove-MeridianUninstallEntry

    if (Test-Path -LiteralPath $uninstallerCopy) {
        try { Remove-Item -LiteralPath $uninstallerCopy -Force -ErrorAction Stop } catch {}
    }

    Write-Host ""
    Write-Host "Meridian tray/GUI app uninstalled." -ForegroundColor Green
    Write-Host "Note: $binDir was left on your PATH -- it is a shared user bin directory"
    Write-Host "(other tools may live there too), so it is never removed automatically."
    exit 0
}

if ($Autostart -and -not $Tray) {
    Write-Error "-Autostart applies only to a -Tray install."
    exit 2
}

# ---- f66e8f23: SHA-256 verification of the downloaded binary -----------------
# release.yml publishes a SHA256SUMS file with every release (one "<hex>  <asset
# name>" line per asset). The binary is only kept -- and only ever run -- if its
# hash equals the entry for its asset name. This fails CLOSED: a missing
# SHA256SUMS, a missing entry or a mismatch deletes the download and aborts the
# install. The only escape hatch is an explicit $env:MERIDIAN_INSTALL_ALLOW_UNVERIFIED
# = '1' (loudly warned), meant for a release that predates SHA256SUMS.
# (Same helper as install.ps1 -- both scripts are served stand-alone, so it is
# duplicated rather than shared.)
function Get-MeridianSumsEntry {
    <#
      .SYNOPSIS
      Return the lower-case SHA-256 hex digest listed for $AssetName in the text of
      a SHA256SUMS file ("<hex>  <name>" or "<hex> *<name>" per line), or $null.
    #>
    param(
        [string]$SumsText,
        [string]$AssetName
    )
    foreach ($line in ($SumsText -split '\r?\n')) {
        if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$') {
            if ($Matches[2] -ceq $AssetName) { return $Matches[1].ToLowerInvariant() }
        }
    }
    return $null
}

function Test-MeridianDownloadIntegrity {
    <#
      .SYNOPSIS
      Verify the file at $Path against the SHA256SUMS published at $SumsUrl.
      Returns $true only when the hash matches (or the explicit opt-out is set).
      On any failure the downloaded file is DELETED and $false is returned.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$AssetName,
        [Parameter(Mandatory = $true)][string]$SumsUrl
    )

    if ($env:MERIDIAN_INSTALL_ALLOW_UNVERIFIED -eq '1') {
        Write-Warning "MERIDIAN_INSTALL_ALLOW_UNVERIFIED=1 -- SKIPPING SHA-256 verification of $AssetName."
        Write-Warning "The downloaded binary will be installed and run WITHOUT any integrity check."
        return $true
    }

    $sumsText = $null
    $tmpSums = "$Path.sha256sums"
    try {
        Invoke-WebRequest $SumsUrl -OutFile $tmpSums -UseBasicParsing -ErrorAction Stop
        $sumsText = Get-Content -Raw -LiteralPath $tmpSums -ErrorAction Stop
    } catch {
        Write-Host ("  Could not download SHA256SUMS from {0}: {1}" -f $SumsUrl, $_.Exception.Message) -ForegroundColor Red
    } finally {
        Remove-Item -LiteralPath $tmpSums -Force -ErrorAction SilentlyContinue
    }

    $expected = $null
    if ($sumsText) { $expected = Get-MeridianSumsEntry -SumsText $sumsText -AssetName $AssetName }
    if (-not $expected) {
        Write-Host "  No SHA-256 checksum could be found for $AssetName, so the download cannot be verified." -ForegroundColor Red
        Write-Host "  (Set MERIDIAN_INSTALL_ALLOW_UNVERIFIED=1 to skip verification at your own risk.)" -ForegroundColor Red
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        return $false
    }

    $actual = $null
    try {
        $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    } catch {
        Write-Host ("  Could not hash the download: {0}" -f $_.Exception.Message) -ForegroundColor Red
    }
    if ($actual -ne $expected) {
        Write-Host ("  SHA-256 MISMATCH for {0}: expected {1}, got {2}." -f $AssetName, $expected, $actual) -ForegroundColor Red
        Write-Host "  The download was deleted and nothing was installed." -ForegroundColor Red
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        return $false
    }
    Write-Host "  Checksum verified (sha256 $actual)."
    return $true
}

# ---- cf8a90ec: -Tray path -- download meridian-tray.exe instead -------------
# No uv fast path here: `uv tool install meridian-server` installs the PyPI
# package (the `meridian` CLI), which does not produce this GUI binary. The
# tray build bundles the full server itself (see meridian-tray.spec), so the
# download-and-run pattern below is the whole install -- same hardening
# (retry + non-empty verification) as the meridian.exe path further down, kept
# as a fully separate branch so the default (no -Tray) behavior never changes.
if ($Tray) {
    $binDir = Join-Path $env:USERPROFILE ".local\bin"
    $dest = Join-Path $binDir "meridian-tray.exe"
    $url = "https://github.com/meridianmcp/Meridian/releases/latest/download/meridian-tray.exe"

    New-Item -ItemType Directory -Force -Path $binDir | Out-Null

    $releaseTag = $null
    try {
        $latest = Invoke-RestMethod -Uri "https://api.github.com/repos/meridianmcp/Meridian/releases/latest" `
            -Headers @{ "User-Agent" = "meridian-install" } -TimeoutSec 15
        $releaseTag = $latest.tag_name
    } catch {
        $releaseTag = $null
    }
    if ($releaseTag) {
        Write-Host "Installing meridian-tray.exe $releaseTag (latest release)."
    } else {
        Write-Host "Installing meridian-tray.exe (latest release; could not resolve the exact version tag)."
    }

    Write-Host "Downloading meridian-tray.exe..."
    $maxAttempts = 3
    $downloaded = $false
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        try {
            if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
            Invoke-WebRequest $url -OutFile $dest -UseBasicParsing -ErrorAction Stop
            if ((Test-Path $dest) -and ((Get-Item $dest).Length -gt 0)) { $downloaded = $true; break }
            Write-Warning "Download produced a missing or empty file (attempt $attempt/$maxAttempts)."
        } catch {
            Write-Warning "Download failed (attempt $attempt/$maxAttempts): $($_.Exception.Message)"
        }
        if ($attempt -lt $maxAttempts) { Start-Sleep -Seconds 2 }
    }

    if (-not $downloaded) {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
        Write-Error ("Failed to download meridian-tray.exe from {0} after {1} attempts. " -f $url, $maxAttempts +
            "No binary written. Check your network/proxy and that a release asset named " +
            "'meridian-tray.exe' exists, then re-run this installer.")
        exit 1
    }

    # f66e8f23 -- verify BEFORE the tray binary is reported installed (or run).
    $sumsUrl = "https://github.com/meridianmcp/Meridian/releases/latest/download/SHA256SUMS"
    if (-not (Test-MeridianDownloadIntegrity -Path $dest -AssetName "meridian-tray.exe" -SumsUrl $sumsUrl)) {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
        Write-Error ("Aborting install - could not verify the SHA-256 of 'meridian-tray.exe' against {0}. " -f $sumsUrl +
            "The download was deleted and nothing was installed.")
        exit 1
    }
    $sizeMB = [math]::Round((Get-Item $dest).Length / 1MB, 1)
    Write-Host "Installed meridian-tray.exe ($sizeMB MB) to $dest"

    # ---- Start Menu shortcut + Add/Remove Programs registration -------------
    # Wired into the NORMAL (non-uninstall) install path -- runs on every
    # -Tray install, not as a separate opt-in step. Both are best-effort: a
    # failure here is warned about but never aborts the install (the binary
    # is already in place and usable by direct path at that point).
    $shortcutCreated = New-MeridianStartMenuShortcut -TargetPath $dest
    $registered = Register-MeridianUninstallEntry -ExePath $dest -InstallDir $binDir `
        -Version $releaseTag -LocalSourcePath $ScriptSelfPath
    $autostartConfigured = $false
    if ($Autostart) {
        $autostartConfigured = Enable-MeridianAutostart -TargetPath $dest
    }

    $configureZoteroNow = [bool]$ConfigureZotero
    if (-not $configureZoteroNow -and $Host.Name -eq "ConsoleHost" -and -not [Console]::IsInputRedirected) {
        try {
            $answer = Read-Host "Configure your local Zotero connection now? [y/N]"
            $configureZoteroNow = $answer -match '^(?i:y|yes)$'
        } catch {
            # Install scripts are also used in automation and through irm | iex;
            # declining setup must never turn a successful install into failure.
            $configureZoteroNow = $false
        }
    }
    if ($configureZoteroNow) {
        Write-Host "Opening the local Zotero connection setup..."
        try {
            & $dest --configure-zotero
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Zotero setup did not finish. You can open it later from the Meridian tray menu."
            }
        } catch {
            Write-Warning "Could not open Zotero setup. You can open it later from the Meridian tray menu."
        }
    } else {
        Write-Host "Zotero setup skipped; you can configure it later from the Meridian tray menu."
    }

    Write-Host "Configuring the Meridian MCP bundle for $TargetRepo..."
    try {
        & $dest setup --repo $TargetRepo
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "MCP bundle setup reported a conflict or error. Re-run: meridian-tray setup --repo `"$TargetRepo`""
        }
    } catch {
        Write-Warning "Could not run MCP bundle setup: $($_.Exception.Message)"
    }

    Write-Host ""
    Write-Host "Done. Launch it by double-clicking $dest in File Explorer,"
    if ($shortcutCreated) {
        Write-Host "from the Start Menu (search for `"Meridian`"), or from a terminal:"
    } else {
        Write-Host "or from a terminal:"
    }
    Write-Host "  & `"$dest`""
    Write-Host "This starts the Meridian server in the background and shows a tray icon"
    Write-Host "(Open Dashboard / Status / View Logs / Restart / Quit)."
    if ($autostartConfigured) {
        Write-Host "Meridian tray will also start when this Windows user signs in."
    } else {
        Write-Host "Start-at-sign-in is off; pass -Autostart to enable it on a future install."
    }
    if ($registered) {
        Write-Host ""
        Write-Host "To uninstall: Settings > Apps > Installed apps > Meridian > Uninstall,"
        Write-Host "or run this installer again with -Uninstall."
    }
    exit 0
}

# ---- Primary path: uv tool install ------------------------------------------
# If `uv` is on PATH, install the published PyPI package as a uv tool. This is
# the preferred path -- uv manages an isolated venv + a shim on PATH, and users
# get pip-style upgrades (`uv tool upgrade meridian-server`). Falls back to the
# binary download below if uv isn't installed or the install fails.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $uv) {
    Write-Host "uv detected -- installing meridian-server via uv tool install..."
    & uv tool install meridian-server
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "Installed meridian-server with uv."
        Write-Host "If 'meridian' isn't found, run:  uv tool update-shell  (then restart your terminal)"
        Write-Host ""
        Write-Host "Done. In a NEW terminal, run:  meridian --tunnel --repo ."
        Write-Host "Configuring the Meridian MCP bundle for $TargetRepo..."
        & uv tool run --from meridian-server meridian setup --repo $TargetRepo
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "MCP bundle setup reported a conflict or error. Re-run: meridian setup --repo `"$TargetRepo`""
        }
        exit 0
    }
    Write-Warning "uv tool install failed; falling back to binary download."
}

# ---- Fallback path: download the standalone binary --------------------------
$binDir = Join-Path $env:USERPROFILE ".local\bin"
$dest = Join-Path $binDir "meridian.exe"
# The release attaches the Windows binary as a flat `meridian.exe` asset
# (x86_64), so no arch suffix -- see .github/workflows/release.yml.
$url = "https://github.com/meridianmcp/Meridian/releases/latest/download/meridian.exe"

New-Item -ItemType Directory -Force -Path $binDir | Out-Null

# 50d2664d -- resolve + print the exact release tag being downloaded so users can
# confirm they got the intended release, not a stale cached binary. The
# releases/latest API resolves to the same tag as releases/latest/download.
# Best-effort -- never fatal if the API call fails.
$releaseTag = $null
try {
    $latest = Invoke-RestMethod -Uri "https://api.github.com/repos/meridianmcp/Meridian/releases/latest" `
        -Headers @{ "User-Agent" = "meridian-install" } -TimeoutSec 15
    $releaseTag = $latest.tag_name
} catch {
    $releaseTag = $null
}
if ($releaseTag) {
    Write-Host "Installing meridian.exe $releaseTag (latest release)."
} else {
    Write-Host "Installing meridian.exe (latest release; could not resolve the exact version tag)."
}

Write-Host "Downloading meridian.exe..."
$maxAttempts = 3
$downloaded = $false
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    try {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
        Invoke-WebRequest $url -OutFile $dest -UseBasicParsing -ErrorAction Stop
        if ((Test-Path $dest) -and ((Get-Item $dest).Length -gt 0)) { $downloaded = $true; break }
        Write-Warning "Download produced a missing or empty file (attempt $attempt/$maxAttempts)."
    } catch {
        Write-Warning "Download failed (attempt $attempt/$maxAttempts): $($_.Exception.Message)"
    }
    if ($attempt -lt $maxAttempts) { Start-Sleep -Seconds 2 }
}

if (-not $downloaded) {
    if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
    Write-Error ("Failed to download meridian.exe from {0} after {1} attempts. " -f $url, $maxAttempts +
        "No binary written. Check your network/proxy and that a release asset named " +
        "'meridian.exe' exists, then re-run this installer.")
    exit 1
}

# f66e8f23 -- verify BEFORE the binary is reported installed or put on the PATH.
$sumsUrl = "https://github.com/meridianmcp/Meridian/releases/latest/download/SHA256SUMS"
if (-not (Test-MeridianDownloadIntegrity -Path $dest -AssetName "meridian.exe" -SumsUrl $sumsUrl)) {
    if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
    Write-Error ("Aborting install - could not verify the SHA-256 of 'meridian.exe' against {0}. " -f $sumsUrl +
        "The download was deleted and nothing was installed.")
    exit 1
}
$sizeMB = [math]::Round((Get-Item $dest).Length / 1MB, 1)
Write-Host "Installed meridian.exe ($sizeMB MB) to $dest"

# Add $binDir to the user PATH (persistent, no admin required). Use
# SetEnvironmentVariable, NOT setx: setx silently truncates the PATH to 1024
# characters and can corrupt it. This mirrors install.ps1's safe approach.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
if ($userPath -notlike "*$binDir*") {
    $newPath = if ($userPath) { "$userPath;$binDir" } else { $binDir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added $binDir to your user PATH (restart your terminal to take effect)."
} else {
    Write-Host "$binDir is already on your user PATH."
}

Write-Host ""
Write-Host "Done. In a NEW terminal, run:  meridian --tunnel --repo ."
Write-Host "Configuring the Meridian MCP bundle for $TargetRepo..."
try {
    & $dest setup --repo $TargetRepo
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "MCP bundle setup reported a conflict or error. Re-run: meridian setup --repo `"$TargetRepo`""
    }
} catch {
    Write-Warning "Could not run MCP bundle setup: $($_.Exception.Message)"
}
