$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x86_64" } else { "x86" }
$binary = "meridian-connect-${arch}-windows.exe"
$meridianDir = "$env:APPDATA\meridian"
$dest = "$meridianDir\meridian-connect.exe"
New-Item -Force -ItemType Directory $meridianDir | Out-Null
Write-Host "Downloading meridian-connect..."
Invoke-WebRequest "https://github.com/meridianmcp/Meridian/releases/latest/download/$binary" -OutFile $dest

# Add $meridianDir to the user PATH (persistent, no admin required)
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
if ($userPath -notlike "*$meridianDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$meridianDir", "User")
    Write-Host "Added $meridianDir to user PATH (restart terminal to take effect)"
}

Write-Host "Running installer..."
& $dest @args
