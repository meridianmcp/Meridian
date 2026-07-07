# hooks_install.ps1 -- backward-compat shim (a1ba9aa8)
#
# The Meridian installer has been consolidated into a SINGLE client-connector
# entry point: install.ps1. The hooks-only install is now just a component of it
# -- install.ps1 -Component hooks -- which runs the same OAuth 2.0 Device
# Authorization Grant (RFC 8628) keyless auth this script used to run standalone.
#
# This file is kept ONLY so the historical path still works for anyone curl-ing
# it directly:
#
#   irm https://usemeridian.us/hooks_install.ps1 | iex
#
# It fetches install.ps1 from the same server and runs it with -Component hooks.
# It does NOT re-implement the hooks logic -- that lives inline in install.ps1's
# -Component hooks path. (install.ps1 must never fetch THIS script back, or the
# two would fetch each other forever; the hooks logic being inline in install.ps1
# is what keeps that loop from ever forming.)
#
# NOTE: like before, this is the *installer's auth step* only. It does NOT touch
# the token-rotating hooks (hooks.ps1 / hooks.sh).

$ErrorActionPreference = "Stop"

# --- Determine Meridian URL ---------------------------------------------------
# Preserve the old contract: an ambient $MeridianUrl (or the default hosted URL)
# selects which server the installer -- and its keyless device flow -- targets.
if ([string]::IsNullOrWhiteSpace($MeridianUrl)) {
    $MeridianUrl = 'https://usemeridian.us'
}
$MeridianUrl = $MeridianUrl.TrimEnd('/')

$installUrl = "$MeridianUrl/install.ps1"
Write-Host "Meridian hooks install (via consolidated installer) from $installUrl ..." -ForegroundColor Cyan

try {
    $installScript = Invoke-RestMethod -Uri $installUrl -TimeoutSec 30 -ErrorAction Stop
} catch {
    Write-Error ("Could not fetch the Meridian installer from {0}: {1}" -f $installUrl, $_.Exception.Message)
    exit 1
}

# Run install.ps1 with the hooks component only. Build it as a scriptblock so the
# named -Component parameter binds correctly (a bare `iex $installScript` cannot
# accept parameters). --url forwards the target server to the inline hooks flow.
$installBlock = [scriptblock]::Create($installScript)
& $installBlock -Component hooks --url $MeridianUrl
