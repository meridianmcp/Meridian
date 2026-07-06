# install-windows.ps1 — install the Meridian standalone binary (meridian.exe) on
# Windows. The Windows counterpart to install.sh: downloads the latest release
# binary into ~/.local/bin and puts that directory on the user PATH so `meridian`
# works in a new terminal.
#
#   irm https://usemeridian.us/install-windows.ps1 | iex
#
$ErrorActionPreference = "Stop"

# ---- Primary path: uv tool install ------------------------------------------
# If `uv` is on PATH, install the published PyPI package as a uv tool. This is
# the preferred path — uv manages an isolated venv + a shim on PATH, and users
# get pip-style upgrades (`uv tool upgrade meridian-server`). Falls back to the
# binary download below if uv isn't installed or the install fails.
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $uv) {
    Write-Host "uv detected — installing meridian-server via uv tool install..."
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
# (x86_64), so no arch suffix — see .github/workflows/release.yml.
$url = "https://github.com/meridianmcp/Meridian/releases/latest/download/meridian.exe"

New-Item -ItemType Directory -Force -Path $binDir | Out-Null

# 50d2664d — resolve + print the exact release tag being downloaded so users can
# confirm they got the intended release, not a stale cached binary. The
# releases/latest API resolves to the same tag as releases/latest/download.
# Best-effort — never fatal if the API call fails.
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
