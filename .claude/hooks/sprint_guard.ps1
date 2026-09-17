# c0d2356d — Claude Code Stop hook (auto-written by generate_handoff). Blocks a
# session from stopping while this project has pending sprint items. Fails OPEN.
# This is NOT hooks.ps1 (the token-rotation installer).
# b4ce3274 — bounded retry ceiling: after MERIDIAN_STOP_OVERRIDE_CEILING forced
# continuations the server reports pending 0 + stopped_at_ceiling, so this guard
# lets the stop through instead of blocking forever.
# e2e1b682 — verification_pending_count is ADVISORY ONLY: it surfaces items
# flagged require_verification that are still missing an independent
# fresh-session PASS, but never changes the exit code (only
# complete_sprint_item's structural gate blocks the completion itself).
$ErrorActionPreference = 'SilentlyContinue'
$ProjectId = '5787cc92-ba7d-4788-b17c-28ab7938b839'
$Url = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { 'http://localhost:7878' }
$raw = [Console]::In.ReadToEnd()
try { $payload = $raw | ConvertFrom-Json } catch { $payload = $null }
if ($payload -and $payload.stop_hook_active -eq $true) { exit 0 }
# b4ce3274 — forward the session id (when present) so the override budget is
# counted per session, not per project.
# 41f26499 — MUST use ${reqUrl} (braced) here, not bare $reqUrl: PowerShell
# treats "?" as a legal bare-variable-name character, so "$reqUrl?session_id="
# parsed as the (nonexistent, empty) variable $reqUrl?session_id followed by
# literal "=", silently dropping the whole base URL and producing an invalid
# URI. That sent Invoke-RestMethod down the catch branch below on every stop
# with a session_id present -- i.e. always -- making this whole endpoint a
# silent no-op on Windows. Caught while adding the warning below, which would
# otherwise have falsely reported "server unreachable" on every normal stop.
$reqUrl = "$Url/projects/$ProjectId/sprint/pending_count"
if ($payload -and $payload.session_id) {
    $reqUrl = "${reqUrl}?session_id=$([uri]::EscapeDataString([string]$payload.session_id))"
}
# 41f26499 — a Meridian-unreachable window (or a malformed/empty response)
# used to fail open SILENTLY here, which could abandon this session's file
# claims with no visible signal (they then only clear via the file-claim 2h
# TTL). Fail-open behavior is UNCHANGED (still exit 0) but now surfaces a
# clear stderr warning so the human/agent notices instead of silently
# continuing.
try {
    $r = Invoke-RestMethod -Method GET -Uri $reqUrl -TimeoutSec 5
} catch {
    [Console]::Error.WriteLine("Meridian (41f26499): could not reach $Url to check pending sprint items - allowing stop (fail-open). WARNING: any file claims held by this session will NOT be released and will only clear via the 2h claim TTL; release them manually (release_file) once Meridian is reachable again.")
    exit 0
}
if ($null -eq $r -or $null -eq $r.pending_count) {
    [Console]::Error.WriteLine("Meridian (41f26499): got an empty or malformed response from $Url - allowing stop (fail-open). WARNING: any file claims held by this session will NOT be released and will only clear via the 2h claim TTL; release them manually (release_file) once Meridian is reachable again.")
    exit 0
}
$pending = [int]$r.pending_count
if ($pending -gt 0) {
    [Console]::Error.WriteLine("Meridian: $pending sprint item(s) still pending - complete or skip them (complete_sprint_item) before stopping.")
    exit 2
}
if ($r.stopped_at_ceiling -eq $true) {
    [Console]::Error.WriteLine("Meridian: stop-override ceiling reached - allowing stop despite pending items; generate a delta handoff.")
}
if ($null -ne $r.verification_pending_count -and [int]$r.verification_pending_count -gt 0) {
    [Console]::Error.WriteLine("Meridian: $([int]$r.verification_pending_count) item(s) require an independent fresh-session PASS/FAIL verification before their completion can stick (require_verification=true, no independent PASS on file yet).")
}
# a03c0eeb - the moment the guard is about to let a stop through is the
# natural post-integration point (this session has no pending sprint items
# left): fire a best-effort real disk cleanup pass for this project's git
# worktrees so items that already merged/finished don't leave orphaned
# worktree dirs behind. Self-hosted only server-side (see worktree_cleanup.py
# module docstring); fire-and-forget here, never blocks or fails the stop.
try {
    Invoke-RestMethod -Method POST -Uri "$Url/projects/$ProjectId/worktrees/sweep" -TimeoutSec 5 | Out-Null
} catch { }
exit 0
