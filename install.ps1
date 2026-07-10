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
#   5fb084fe -- COMPONENT SELECTION. The installer no longer forces the full
#              stack on every run. -Component picks what to install:
#                  binary  -- only download + run the meridian-connect tunnel binary
#                  hooks   -- only install the Meridian session hooks (keyless
#                             RFC 8628 device auth, inline in this script)
#                  both    -- binary + hooks (the default; the old behavior)
#                  custom  -- prompt for each component interactively
#   a1ba9aa8 -- INSTALLER CONSOLIDATION. install.ps1 is now the SINGLE
#              client-connector entry point. The hooks-install logic lives inline
#              in the -Component hooks path (see below), and scripts/hooks_install.ps1
#              is a thin backward-compat shim that fetches this script and runs it
#              with -Component hooks, so the old
#                  irm https://usemeridian.us/hooks_install.ps1 | iex
#              path keeps working. Behavior-preserving for the default (both) case.
#              A summary line up front states exactly what will be installed.
#              When piped through `iex` (no -File), pass it after the script, e.g.
#                  & ([scriptblock]::Create((irm https://usemeridian.us/install.ps1))) -Component hooks
#              or run the saved file directly: powershell -File install.ps1 -Component binary
param(
    [ValidateSet('binary', 'hooks', 'both', 'custom')]
    [string]$Component = 'both',

    # Passthrough args forwarded to meridian-connect (kept out of the named param
    # so `-Component X <tunnel args...>` still works).
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs = @()
)
$ErrorActionPreference = "Stop"

$repo = "meridianmcp/Meridian"
$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x86_64" } else { "x86" }
$binary = "meridian-connect-${arch}-windows.exe"
$meridianDir = "$env:APPDATA\meridian"
$dest = "$meridianDir\meridian-connect.exe"

# ---- 5fb084fe: component selection ------------------------------------------
# Resolve the passthrough tunnel args first (the target URL is parsed from them,
# and it drives which server the 'hooks' component's installer is fetched from).
# Combine the ValueFromRemainingArguments capture with any bare $args so both the
# `-File install.ps1 -Component X ...` and the `iex`/scriptblock invocation paths
# forward the same tunnel arguments.
$passthroughArgs = @()
if ($RemainingArgs) { $passthroughArgs += $RemainingArgs }
if ($args)          { $passthroughArgs += $args }

# Parse the tunnel target URL out of the passthrough args (defaults to hosted).
$targetUrl = 'https://usemeridian.us'
for ($i = 0; $i -lt $passthroughArgs.Count; $i++) {
    if ("$($passthroughArgs[$i])" -eq '--url' -and ($i + 1) -lt $passthroughArgs.Count) {
        $targetUrl = "$($passthroughArgs[$i + 1])"
    }
}

# Decide, per component, what actually gets installed.
$installBinary = $false
$installHooks  = $false
switch ($Component) {
    'binary' { $installBinary = $true }
    'hooks'  { $installHooks  = $true }
    'both'   { $installBinary = $true; $installHooks = $true }
    'custom' {
        # Interactive per-component choice. Non-interactive hosts (piped `iex`
        # with no console) fall back to installing both so `custom` never
        # dead-ends silently.
        $interactive = $false
        try { $interactive = -not [System.Console]::IsInputRedirected } catch { $interactive = $false }
        if ($interactive) {
            $bAns = Read-Host "Install the meridian-connect tunnel binary? [Y/n]"
            $installBinary = ($bAns -notmatch '^(n|no)$')
            $hAns = Read-Host "Install the Meridian session hooks? [Y/n]"
            $installHooks = ($hAns -notmatch '^(n|no)$')
        } else {
            Write-Host "No interactive console for -Component custom; installing both components."
            $installBinary = $true
            $installHooks  = $true
        }
    }
}

# Nothing selected (e.g. custom with both declined) -- there is no work to do.
if (-not $installBinary -and -not $installHooks) {
    Write-Host "Nothing selected to install. Re-run with -Component binary|hooks|both."
    exit 0
}

# Confirmation / summary line: state exactly what will be installed up front.
$componentList = @()
if ($installBinary) { $componentList += 'meridian-connect tunnel binary' }
if ($installHooks)  { $componentList += 'Meridian session hooks' }
Write-Host ""
Write-Host ("Meridian install plan (-Component {0}): {1}." -f $Component, ($componentList -join ' + ')) -ForegroundColor Cyan
Write-Host ("Target server: {0}" -f $targetUrl)
Write-Host ""

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

# =============================================================================
# COMPONENT: binary -- download + run the meridian-connect tunnel binary.
# Skipped entirely for -Component hooks (5fb084fe).
# =============================================================================
if ($installBinary) {

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
$binaryArgs = @($passthroughArgs)
# $targetUrl was already resolved from the passthrough args near the top.
$hasToken = $false
for ($i = 0; $i -lt $binaryArgs.Count; $i++) {
    $a = "$($binaryArgs[$i])"
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

} # end if ($installBinary)

# =============================================================================
# COMPONENT: hooks -- install the Meridian session hooks (5fb084fe, a1ba9aa8).
#
# a1ba9aa8 -- installer consolidation. The hooks-install logic now lives INLINE
# here, in the single install.ps1 entry point, rather than being fetched from a
# separate hooks_install.ps1 script. scripts/hooks_install.ps1 is now a thin
# backward-compat shim that fetches this script and runs it with
# -Component hooks, so the old `irm .../hooks_install.ps1 | iex` path still
# works. Inlining the logic here is what BREAKS the fetch loop: install.ps1
# must NOT fetch hooks_install.ps1 (which now re-fetches install.ps1).
#
# This block reproduces exactly what the standalone hooks_install.ps1 used to
# do: the RFC 8628 keyless device flow (reusing the SAME Get-MeridianDeviceToken
# / Get-MeridianCachedToken helpers already defined above), honouring an
# existing MERIDIAN_TOKEN or a still-valid cached token before prompting the
# browser. It acquires the sk_meridian_ API key the hooks install needs; the
# token-rotating hooks themselves (hooks.ps1 / hooks.sh) are intentionally NOT
# touched here.
# =============================================================================
if ($installHooks) {
    Write-Host ""
    Write-Host "Installing Meridian session hooks (keyless device auth)..." -ForegroundColor Cyan

    $hooksToken = $null
    if (-not [string]::IsNullOrWhiteSpace($env:MERIDIAN_TOKEN) `
            -and $env:MERIDIAN_TOKEN -match '^sk_meridian_') {
        Write-Host "Using existing MERIDIAN_TOKEN from the environment." -ForegroundColor Green
        $hooksToken = $env:MERIDIAN_TOKEN
    } else {
        # Honour a still-valid token already cached on this machine before any
        # browser auth (mirrors the binary component's cee295bd behaviour).
        $cachedHooksToken = Get-MeridianCachedToken -MeridianUrl $targetUrl
        if (-not [string]::IsNullOrWhiteSpace($cachedHooksToken)) {
            Write-Host "Using an existing valid Meridian token from ~/.meridian/config.json (no auth needed)." -ForegroundColor Green
            $hooksToken = $cachedHooksToken
        } else {
            $hooksToken = Get-MeridianDeviceToken -MeridianUrl $targetUrl
        }
    }

    if ([string]::IsNullOrWhiteSpace($hooksToken)) {
        Write-Warning "Authentication failed -- no token obtained. Skipping hooks install."
    } else {
        # $hooksToken now holds a valid sk_meridian_ API key. The remainder of the
        # hooks install (writing ~/.claude/hooks/*.ps1, wiring settings.json) is
        # performed by the hooks installer proper (hooks.ps1) using this token,
        # exactly as it would with a pasted key -- intentionally not duplicated here.
        Write-Host ""
        Write-Host "Meridian authentication complete. Token acquired for hooks install." -ForegroundColor Green
    }
}
