# c0d2356d -- Claude Code Stop hook (auto-written by generate_handoff). Blocks an
# EXECUTOR session from stopping while this project has pending sprint items. Fails OPEN.
# This is NOT hooks.ps1 (the token-rotation installer).
# b4ce3274 -- bounded retry ceiling: after MERIDIAN_STOP_OVERRIDE_CEILING forced
# continuations the server reports pending 0 + stopped_at_ceiling, so this guard
# lets the stop through instead of blocking forever.
# e2e1b682 -- verification_pending_count is ADVISORY ONLY: it surfaces items
# flagged require_verification that are still missing an independent
# fresh-session PASS, but never changes the exit code (only
# complete_sprint_item's structural gate blocks the completion itself).
# 55d48d69 fix round 1 (the launcher fix made this hook's exit 2 real):
# - Only a session that claimed a sprint item (a claim_sprint_item tool call in its
#   own transcript) is held back. The server's pending count is project-wide, so
#   planner, Q&A and review sessions stop freely -- and without any network call.
# - The server is probed with a 300 ms TCP connect first: a down server (the
#   default state) no longer costs ~5 s at every turn end.
# - a03c0eeb's worktree sweep trigger was REMOVED from this hook: the server's
#   sweep force-removes worktrees whose item or session is terminal, dirty ones
#   holding uncommitted work included, and it already runs periodically there.
# - The owner kill switch (MERIDIAN_GUARD=off|advisory, guard.off / guard.advisory,
#   MERIDIAN_GUARD_DISABLE=sprint_guard) covers this hook: off and advisory never block.
# Pure ASCII: PS 5.1 reads BOM-less UTF-8 as cp1252.
$ErrorActionPreference = 'SilentlyContinue'
$ProjectId = '5787cc92-ba7d-4788-b17c-28ab7938b839'
$Url = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { 'http://localhost:7878' }
$raw = [Console]::In.ReadToEnd()
try { $payload = $raw | ConvertFrom-Json } catch { $payload = $null }
if ($payload -and $payload.stop_hook_active -eq $true) { exit 0 }

function Get-GuardMode([string]$HookName) {
    $gm = ([string]$env:MERIDIAN_GUARD).Trim().ToLowerInvariant()
    if ($gm -eq 'off') { return 'off' }
    $mode = if ($gm -eq '' -or $gm -eq 'enforce') { 'enforce' } else { 'advisory' }
    if ($gm -eq '' -and ([string]$env:MERIDIAN_GUARD_DEFAULT_MODE).Trim().ToLowerInvariant() -eq 'advisory') { $mode = 'advisory' }
    foreach ($tok in ([string]$env:MERIDIAN_GUARD_DISABLE -split '[\s,;]+')) {
        if ($tok.Trim().ToLowerInvariant() -eq $HookName) { return 'off' }
    }
    $gd = $null
    if ($env:LOCALAPPDATA) { $gd = [System.IO.Path]::Combine($env:LOCALAPPDATA, 'meridian', 'guard') }
    elseif ($env:USERPROFILE) { $gd = [System.IO.Path]::Combine($env:USERPROFILE, 'AppData', 'Local', 'meridian', 'guard') }
    if ($gd) {
        if ([System.IO.File]::Exists([System.IO.Path]::Combine($gd, 'guard.off'))) { return 'off' }
        if ([System.IO.File]::Exists([System.IO.Path]::Combine($gd, 'guard.advisory'))) { return 'advisory' }
    }
    return $mode
}

# Resolve MERIDIAN_URL to a base URL whose TCP port answers within the timeout, or $null.
function Resolve-LiveUrl([string]$Base, [int]$TimeoutMs = 300) {
    try { $u = [System.Uri]$Base } catch { return $null }
    $hostName = $u.Host
    $cands = if ($hostName -eq 'localhost') { @('127.0.0.1', '::1') } else { @($hostName) }
    foreach ($h in $cands) {
        $fam = if ($h -eq '::1' -or $h.StartsWith('[')) { [System.Net.Sockets.AddressFamily]::InterNetworkV6 } else { [System.Net.Sockets.AddressFamily]::InterNetwork }
        $c = $null
        try {
            $c = [System.Net.Sockets.TcpClient]::new($fam)
            $ar = $c.BeginConnect($h.Trim('[', ']'), $u.Port, $null, $null)
            if ($ar.AsyncWaitHandle.WaitOne($TimeoutMs) -and $c.Connected) {
                $c.EndConnect($ar)
                if ($hostName -ne 'localhost') { return $Base.TrimEnd('/') }
                $lit = if ($h -eq '::1') { '[::1]' } else { $h }
                return ($u.Scheme + '://' + $lit + ':' + $u.Port + $u.AbsolutePath.TrimEnd('/'))
            }
        } catch { } finally { if ($c) { $c.Close() } }
    }
    return $null
}

$mode = Get-GuardMode 'sprint_guard'
if ($mode -eq 'off') { exit 0 }

# Only an executor session -- one that claimed a sprint item -- is held back.
$claimed = $false
$tp = if ($payload) { [string]$payload.transcript_path } else { '' }
if ($tp -and [System.IO.File]::Exists($tp)) {
    try { $claimed = [bool](Select-String -LiteralPath $tp -Pattern '"name"\s*:\s*"mcp__[^"]*__claim_sprint_item"' -Quiet) } catch { $claimed = $false }
}
if (-not $claimed) { exit 0 }

# 41f26499 -- a Meridian-unreachable window (or a malformed/empty response)
# used to fail open SILENTLY here, which could abandon this session's file
# claims with no visible signal (they then only clear via the file-claim 2h
# TTL). Fail-open behavior is UNCHANGED (still exit 0) but surfaces a clear
# stderr warning so the human/agent notices instead of silently continuing.
$live = Resolve-LiveUrl $Url
if (-not $live) {
    [Console]::Error.WriteLine("Meridian (41f26499): could not reach $Url to check pending sprint items - allowing stop (fail-open). WARNING: any file claims held by this session will NOT be released and will only clear via the 2h claim TTL; release them manually (release_file) once Meridian is reachable again.")
    exit 0
}
# b4ce3274 -- forward the session id (when present) so the override budget is
# counted per session, not per project.
# 41f26499 -- MUST use ${reqUrl} (braced) here, not bare $reqUrl: PowerShell
# treats "?" as a legal bare-variable-name character, so "$reqUrl?session_id="
# parsed as the (nonexistent, empty) variable $reqUrl?session_id followed by
# literal "=", silently dropping the whole base URL and producing an invalid URI.
$reqUrl = "$live/projects/$ProjectId/sprint/pending_count"
if ($payload -and $payload.session_id) {
    $reqUrl = "${reqUrl}?session_id=$([uri]::EscapeDataString([string]$payload.session_id))"
}
$authHeaders = @{}
$authToken = if ($env:MERIDIAN_TOKEN) { [string]$env:MERIDIAN_TOKEN } else { [string]$env:BEARER_TOKEN }
if ($authToken -match '^[A-Za-z0-9._~+/-]+=*$') {
    $authHeaders["Authorization"] = "Bearer $authToken"
}
try {
    $r = Invoke-RestMethod -Method GET -Uri $reqUrl -Headers $authHeaders -TimeoutSec 5
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
    $msg = "Meridian: $pending sprint item(s) still pending - complete or skip them (complete_sprint_item) before stopping."
    if ($mode -eq 'advisory') { [Console]::Error.WriteLine('[advisory, not blocked] ' + $msg); exit 0 }
    [Console]::Error.WriteLine($msg)
    exit 2
}
if ($r.stopped_at_ceiling -eq $true) {
    [Console]::Error.WriteLine("Meridian: stop-override ceiling reached - allowing stop despite pending items; generate a delta handoff.")
}
if ($null -ne $r.verification_pending_count -and [int]$r.verification_pending_count -gt 0) {
    [Console]::Error.WriteLine("Meridian: $([int]$r.verification_pending_count) item(s) require an independent fresh-session PASS/FAIL verification before their completion can stick (require_verification=true, no independent PASS on file yet).")
}
exit 0
