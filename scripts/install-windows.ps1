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
#
param(
    [switch]$Tray
)
$ErrorActionPreference = "Stop"

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

    Write-Host ""
    Write-Host "Done. Launch it by double-clicking $dest in File Explorer, or from a"
    Write-Host "terminal:"
    Write-Host "  & `"$dest`""
    Write-Host "This starts the Meridian server in the background and shows a tray icon"
    Write-Host "(Open Dashboard / Status / View Logs / Restart / Quit)."
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
