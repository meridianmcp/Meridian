# install.ps1 -- download + run the Meridian Connect tunnel binary (meridian-connect.exe).
#
#   irm https://usemeridian.us/install.ps1 | iex
#
# Beyond a bare download this installer now:
#   50d2664d -- resolves and prints the exact release version being installed
#              (the GitHub "latest" tag) up front, so a user can confirm they got
#              the intended release and not a stale cached binary.
#   73b65117 -- acquires an sk_meridian_ API token via the RFC 8628 device
#              authorization grant (reusing the SAME /oauth/device + /oauth/token
#              infra as hooks_install.ps1) and passes it to the binary with
#              --token. That lets `irm ... | iex` complete end-to-end without a
#              TTY to paste a token into -- the old flow dead-ended on hosted
#              because meridian-connect's paste prompt needs an interactive stdin.
$ErrorActionPreference = "Stop"

$repo = "meridianmcp/Meridian"
$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x86_64" } else { "x86" }
$binary = "meridian-connect-${arch}-windows.exe"
$meridianDir = "$env:APPDATA\meridian"
$dest = "$meridianDir\meridian-connect.exe"
New-Item -Force -ItemType Directory $meridianDir | Out-Null

# ---- RFC 8628 device flow (reused from hooks_install.ps1, item e9f18530) -----
function Get-MeridianDeviceToken {
    <#
    .SYNOPSIS
      Run the RFC 8628 device flow against $MeridianUrl and return an
      sk_meridian_ API token, or $null on failure/timeout.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$MeridianUrl
    )

    $base = $MeridianUrl.TrimEnd('/')

    # --- Step 1: request a device code ---------------------------------------
    try {
        $dc = Invoke-RestMethod -Method POST -Uri "$base/oauth/device" `
            -ContentType 'application/json' -TimeoutSec 15
    } catch {
        Write-Host "  Error: could not start device authorization: $($_.Exception.Message)" -ForegroundColor Red
        return $null
    }

    if (-not $dc.device_code -or -not $dc.user_code) {
        Write-Host "  Error: device endpoint returned an unexpected response." -ForegroundColor Red
        return $null
    }

    $deviceCode   = $dc.device_code
    $userCode     = $dc.user_code
    $verifyUri    = if ($dc.verification_uri) { $dc.verification_uri } else { "$base/activate" }
    $verifyFull   = $dc.verification_uri_complete
    # RFC 8628 defaults if the server omits them.
    $interval     = if ($dc.interval)   { [int]$dc.interval }   else { 5 }
    $expiresIn    = if ($dc.expires_in) { [int]$dc.expires_in } else { 300 }

    # --- Step 2: tell the user how to approve --------------------------------
    Write-Host ""
    Write-Host "To authorize this machine, open:" -ForegroundColor Cyan
    Write-Host "    $verifyUri"
    Write-Host "and enter the code:" -ForegroundColor Cyan
    Write-Host "    $userCode" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Waiting for approval in your browser..."

    # Best-effort: open the prefilled verification URL so an already-logged-in
    # tab can approve with one click. Never fatal if it can't launch a browser.
    if ($verifyFull) {
        try { Start-Process $verifyFull | Out-Null } catch {}
    }

    # --- Step 3: poll for the token ------------------------------------------
    $deadline = (Get-Date).AddSeconds($expiresIn)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $interval

        $body = @{
            grant_type  = 'urn:ietf:params:oauth:grant-type:device_code'
            device_code = $deviceCode
        } | ConvertTo-Json -Compress

        try {
            $resp = Invoke-RestMethod -Method POST -Uri "$base/oauth/token" `
                -ContentType 'application/json' -Body $body -TimeoutSec 15
        } catch {
            # RFC 8628 signals authorization_pending / slow_down / access_denied
            # / expired_token via the JSON body. Invoke-RestMethod throws on the
            # 4xx (access_denied / expired_token / invalid_request) -- parse it.
            $err = $null
            try {
                $stream = $_.Exception.Response.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $err = ($reader.ReadToEnd() | ConvertFrom-Json).error
            } catch {}
            switch ($err) {
                'access_denied'  { Write-Host "  Authorization was denied." -ForegroundColor Red; return $null }
                'expired_token'  { Write-Host "  The code expired before it was approved." -ForegroundColor Red; return $null }
                'slow_down'      { $interval += 5; continue }
                default          {
                    Write-Host "  Error polling for token: $($_.Exception.Message)" -ForegroundColor Red
                    return $null
                }
            }
            continue
        }

        # A 200 with an error field means still pending (or slow_down).
        if ($resp.error) {
            if ($resp.error -eq 'slow_down') { $interval += 5 }
            elseif ($resp.error -eq 'access_denied') { Write-Host "  Authorization was denied." -ForegroundColor Red; return $null }
            elseif ($resp.error -eq 'expired_token') { Write-Host "  The code expired before it was approved." -ForegroundColor Red; return $null }
            # authorization_pending -> keep polling.
            continue
        }

        if ($resp.access_token) {
            Write-Host "  Authorized." -ForegroundColor Green
            return $resp.access_token
        }
    }

    Write-Host "  Timed out waiting for approval." -ForegroundColor Red
    return $null
}

# ---- Resolve + print the release version (50d2664d) --------------------------
# releases/latest/download/... follows GitHub's "latest non-prerelease" release;
# the releases/latest API resolves to the SAME tag, so this is exactly what we are
# about to download. Best-effort -- never fatal if the API call fails.
$releaseTag = $null
try {
    $latest = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
        -Headers @{ "User-Agent" = "meridian-install" } -TimeoutSec 15
    $releaseTag = $latest.tag_name
} catch {
    $releaseTag = $null
}
if ($releaseTag) {
    Write-Host "Installing Meridian Connect $releaseTag (latest release)."
} else {
    Write-Host "Installing Meridian Connect (latest release; could not resolve the exact version tag)."
}

Write-Host "Downloading meridian-connect..."
$url = "https://github.com/$repo/releases/latest/download/$binary"
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
if ($releaseTag) {
    Write-Host "Downloaded meridian-connect $releaseTag ($sizeKB KB)."
} else {
    Write-Host "Downloaded meridian-connect ($sizeKB KB)."
}

# Add $meridianDir to the user PATH (persistent, no admin required)
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }
if ($userPath -notlike "*$meridianDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$meridianDir", "User")
    Write-Host "Added $meridianDir to user PATH (restart terminal to take effect)"
}

# ---- cee295bd: reuse an existing valid local token before ANY auth flow ------
function Get-MeridianCachedToken {
    <#
      .SYNOPSIS
      Return a still-valid sk_meridian_ token already cached on this machine for
      $MeridianUrl, or $null. Mirrors the client's own ~/.meridian/config.json
      cache (_read_cached_token in tunnel_client.py): base_url must match and the
      30-day expiry must not have passed. This lets the installer SKIP the browser
      device flow when the machine is already authorized, instead of forcing a
      fresh auth round-trip on every run.
    #>
    param([Parameter(Mandatory)][string]$MeridianUrl)
    $cfg = Join-Path (Join-Path $HOME '.meridian') 'config.json'
    if (-not (Test-Path -LiteralPath $cfg)) { return $null }
    try {
        $data = Get-Content -Raw -LiteralPath $cfg -ErrorAction Stop | ConvertFrom-Json
    } catch { return $null }
    $entry = $data.tunnel_token
    if (-not $entry) { return $null }
    if ("$($entry.base_url)" -ne "$MeridianUrl") { return $null }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    try { $exp = [int64]$entry.expires_at } catch { return $null }
    if ($exp -le $now) { return $null }
    $tok = "$($entry.token)"
    if ($tok -notmatch '^sk_meridian_') { return $null }
    return $tok
}

# ---- Keyless auth: acquire a token via the device flow (73b65117) ------------
# Copy the passthrough args, then decide whether we need to mint a token. We skip
# the device flow when: the caller already passed --token, a MERIDIAN_TOKEN is in
# the environment, or the target is a local/self-hosted server (no auth needed).
$binaryArgs = @($args)
$targetUrl = 'https://usemeridian.us'
$hasToken = $false
for ($i = 0; $i -lt $binaryArgs.Count; $i++) {
    $a = "$($binaryArgs[$i])"
    if ($a -eq '--url'   -and ($i + 1) -lt $binaryArgs.Count) { $targetUrl = "$($binaryArgs[$i + 1])" }
    if ($a -eq '--token' -and ($i + 1) -lt $binaryArgs.Count) { $hasToken = $true }
}
if (-not $hasToken `
        -and -not [string]::IsNullOrWhiteSpace($env:MERIDIAN_TOKEN) `
        -and $env:MERIDIAN_TOKEN -match '^sk_meridian_') {
    Write-Host "Using existing MERIDIAN_TOKEN from the environment."
    $binaryArgs += @('--token', $env:MERIDIAN_TOKEN)
    $hasToken = $true
}

$isLocal = $targetUrl -match '^https?://(localhost|127\.0\.0\.1)(:\d+)?(/|$)'

# cee295bd -- before any browser auth, honour a still-valid token already cached on
# this machine. Previously the installer forced the device flow on every run when
# no --token / MERIDIAN_TOKEN was present, even if a valid token was already on disk.
if (-not $hasToken -and -not $isLocal) {
    $cachedToken = Get-MeridianCachedToken -MeridianUrl $targetUrl
    if (-not [string]::IsNullOrWhiteSpace($cachedToken)) {
        Write-Host "Using an existing valid Meridian token from ~/.meridian/config.json (no auth needed)."
        $binaryArgs += @('--token', $cachedToken)
        $hasToken = $true
    }
}

if (-not $hasToken -and -not $isLocal) {
    Write-Host ""
    Write-Host "Authenticating with Meridian (no token to paste -- approve in your browser)..."
    $deviceToken = Get-MeridianDeviceToken -MeridianUrl $targetUrl
    if (-not [string]::IsNullOrWhiteSpace($deviceToken)) {
        $binaryArgs += @('--token', $deviceToken)
    } else {
        Write-Warning "Device authorization did not complete; the installer will fall back to its own token prompt."
    }
}

Write-Host "Running installer..."
& $dest @binaryArgs
