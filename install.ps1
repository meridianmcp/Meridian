$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x86_64" } else { "x86" }
$binary = "meridian-connect-${arch}-windows.exe"
$dest = "$env:APPDATA\meridian\meridian-connect.exe"
New-Item -Force -ItemType Directory (Split-Path $dest) | Out-Null
Write-Host "Downloading meridian-connect..."
Invoke-WebRequest "https://github.com/meridianmcp/Meridian/releases/latest/download/$binary" -OutFile $dest
Write-Host "Running installer..."
& $dest @args
