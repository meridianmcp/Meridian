# c0d2356d — Claude Code Stop hook: block a session from stopping while this
# project still has pending sprint items. Structural prevention of RLHF-style
# early-stopping. generate_handoff() auto-writes this file with PROJECT_ID + URL
# baked in; this committed copy is the cross-platform fallback.
#
# This is NOT hooks.ps1 (the token-rotation installer) — never confuse the two.
$ErrorActionPreference = 'SilentlyContinue'

$ProjectId = if ($env:MERIDIAN_PROJECT_ID) { $env:MERIDIAN_PROJECT_ID } else { '5787cc92-ba7d-4788-b17c-28ab7938b839' }
$Url = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { 'http://localhost:7878' }

# The Stop hook receives a JSON payload on stdin.
$raw = [Console]::In.ReadToEnd()
try { $payload = $raw | ConvertFrom-Json } catch { $payload = $null }

# Loop guard: allow the stop if we're already re-firing from a previous block.
if ($payload -and $payload.stop_hook_active -eq $true) { exit 0 }

# Fail OPEN on any network/parse error so a down server never traps the user.
try {
    $r = Invoke-RestMethod -Method GET -Uri "$Url/projects/$ProjectId/sprint/pending_count" -TimeoutSec 5
} catch { exit 0 }
if ($null -eq $r -or $null -eq $r.pending_count) { exit 0 }

$pending = [int]$r.pending_count
if ($pending -gt 0) {
    # exit 2 blocks the stop; stderr is fed back to Claude as the reason.
    [Console]::Error.WriteLine("Meridian: $pending sprint item(s) still pending - complete or skip them (complete_sprint_item) before stopping.")
    exit 2
}
exit 0
