# hooks_install.ps1 - Meridian hooks installer, keyless auth (e9f18530)
#
# Authenticates via the OAuth 2.0 Device Authorization Grant (RFC 8628) instead
# of a pasted static API key -- the same pattern gh / az / docker CLI use:
#   1. POST /oauth/device            -> device_code + short user_code + URL
#   2. print the user_code + URL; the user approves in an already-logged-in tab
#   3. poll POST /oauth/token at `interval` until the token is issued
#
# The minted token is a normal Meridian API key (sk_meridian_...) and is used
# for the rest of the hooks install exactly as a pasted key would be.
#
# Run:  irm https://usemeridian.us/hooks_install.ps1 | iex
#
# NOTE: this is the *installer's auth step* only. It does NOT touch the
# token-rotating hooks (hooks.ps1 / hooks.sh).

$ErrorActionPreference = "Stop"

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

# --- Determine Meridian URL ---------------------------------------------------
if ([string]::IsNullOrWhiteSpace($MeridianUrl)) {
    $MeridianUrl = 'https://usemeridian.us'
}
$MeridianUrl = $MeridianUrl.TrimEnd('/')

# --- Fallback: reuse an existing token if one is already present --------------
# If the caller already exported a token (e.g. $env:MERIDIAN_TOKEN), skip the
# device flow entirely -- no need to re-authorize an already-provisioned box.
$Token = ''
if (-not [string]::IsNullOrWhiteSpace($env:MERIDIAN_TOKEN) `
        -and $env:MERIDIAN_TOKEN -match '^sk_meridian_') {
    Write-Host "Using existing MERIDIAN_TOKEN from the environment." -ForegroundColor Green
    $Token = $env:MERIDIAN_TOKEN
} else {
    $Token = Get-MeridianDeviceToken -MeridianUrl $MeridianUrl
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Error "Authentication failed -- no token obtained. Aborting hooks install."
    exit 1
}

# $Token now holds a valid sk_meridian_ API key. The remainder of the hooks
# install (writing ~/.claude/hooks/*.ps1, wiring settings.json, etc.) proceeds
# with this token exactly as it would with a pasted key. That step lives in the
# hooks installer proper (hooks.ps1) and is intentionally not duplicated here --
# this script's sole responsibility is the keyless auth handshake.
Write-Host ""
Write-Host "Meridian authentication complete. Token acquired for hooks install." -ForegroundColor Green
