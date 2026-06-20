$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x86_64" } else { "x86" }
$binary = "meridian-connect-${arch}-windows.exe"
$meridianDir = "$env:APPDATA\meridian"
$dest = "$meridianDir\meridian-connect.exe"
New-Item -Force -ItemType Directory $meridianDir | Out-Null
Write-Host "Downloading meridian-connect..."
$url = "https://github.com/meridianmcp/Meridian/releases/latest/download/$binary"
$maxAttempts = 3
$downloaded = $false
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    try {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
        Invoke-WebRequest $url -OutFile $dest -UseBasicParsing -ErrorAction Stop
        if ((Test-Path $dest) -and ((Get-Item $dest).Length -gt 0)) {
            $downloaded = $true
            break
        }
        Write-Warning "Download produced a missing or empty file (attempt $attempt/$maxAttempts)."
    } catch {
        Write-Warning "Download failed (attempt $attempt/$maxAttempts): $($_.Exception.Message)"
    }
    if ($attempt -lt $maxAttempts) { Start-Sleep -Seconds 2 }
}

if (-not $downloaded) {
    if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
    Write-Error ("Failed to download meridian-connect from {0} after {1} attempts. " -f $url, $maxAttempts +
        "Aborting install - no binary written. Check your network/proxy and that a release asset " +
        "named '$binary' exists, then re-run this installer.")
    exit 1
}
$sizeKB = [math]::Round((Get-Item $dest).Length / 1KB, 1)
Write-Host "Downloaded meridian-connect ($sizeKB KB)."

# Add $meridianDir to the user PATH (persistent, no admin required)
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
if ($userPath -notlike "*$meridianDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$meridianDir", "User")
    Write-Host "Added $meridianDir to user PATH (restart terminal to take effect)"
}

Write-Host "Running installer..."
& $dest @args
