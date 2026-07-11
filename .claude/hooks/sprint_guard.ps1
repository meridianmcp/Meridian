# c0d2356d — Claude Code Stop hook (auto-written by generate_handoff). Blocks a
# session from stopping while this project has pending sprint items. Fails OPEN.
# This is NOT hooks.ps1 (the token-rotation installer).
# b4ce3274 — bounded retry ceiling: after MERIDIAN_STOP_OVERRIDE_CEILING forced
# continuations the server reports pending 0 + stopped_at_ceiling, so this guard
# lets the stop through instead of blocking forever.
$ErrorActionPreference = 'SilentlyContinue'
$ProjectId = '5787cc92-ba7d-4788-b17c-28ab7938b839'
$Url = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { 'http://localhost:7878' }
$raw = [Console]::In.ReadToEnd()
try { $payload = $raw | ConvertFrom-Json } catch { $payload = $null }
if ($payload -and $payload.stop_hook_active -eq $true) { exit 0 }
# b4ce3274 — forward the session id (when present) so the override budget is
# counted per session, not per project.
$reqUrl = "$Url/projects/$ProjectId/sprint/pending_count"
if ($payload -and $payload.session_id) {
    $reqUrl = "$reqUrl?session_id=$([uri]::EscapeDataString([string]$payload.session_id))"
}
try {
    $r = Invoke-RestMethod -Method GET -Uri $reqUrl -TimeoutSec 5
} catch { exit 0 }
if ($null -eq $r -or $null -eq $r.pending_count) { exit 0 }
$pending = [int]$r.pending_count
if ($pending -gt 0) {
    [Console]::Error.WriteLine("Meridian: $pending sprint item(s) still pending - complete or skip them (complete_sprint_item) before stopping.")
    exit 2
}
if ($r.stopped_at_ceiling -eq $true) {
    [Console]::Error.WriteLine("Meridian: stop-override ceiling reached - allowing stop despite pending items; generate a delta handoff.")
}
exit 0
