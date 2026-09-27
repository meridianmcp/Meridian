# b8fbb4cb -- PreToolUse HITL guard (structural, not text).
#
# Blocks the executor from using Claude Code's NATIVE ask-UI (AskUserQuestion) and
# redirects to Meridian's request_hitl, so every human-in-the-loop question is logged
# in the hitl_requests table (the native ask bypasses it entirely -- confirmed absent 3x).
# Text guidance in agent_instructions failed three times (36edd005, d261ea2e); this is
# the same structural-enforcement pattern as the file-claim guard.
#
# Wired in .claude/settings.json under PreToolUse with matcher "AskUserQuestion", so it
# ONLY ever runs for that one tool -- it can never affect any other tool call. Fails OPEN
# on any parse error. This is NOT hooks.ps1 (the token-rotation installer).
#
# 55d48d69 fix round 1: the block now names the fallback for when request_hitl itself
# is unavailable (Meridian unreachable or unauthenticated): ask in plain text in the
# reply -- the agent is never left without a way to ask. The owner kill switch
# (MERIDIAN_GUARD=off|advisory, guard.off / guard.advisory, MERIDIAN_GUARD_DISABLE=
# hitl_guard) covers this hook too.
#
# fix round 2: round 1 only NAMED the fallback in the deny message -- it still
# blocked AskUserQuestion unconditionally, even when Meridian/request_hitl was
# completely unreachable, which left a session with no way to ask the human at
# all in exactly the situation (server down) where the plain-text fallback is
# needed most. This now probes Meridian first (a fast TCP connect, mirroring
# sprint_guard.ps1's Resolve-LiveUrl, then a real GET /health) and only denies
# the native ask when Meridian actually answers. Unreachable (timeout,
# connection error, non-2xx) => fail OPEN: let the native AskUserQuestion
# through with a stderr note, instead of blocking with nowhere left to ask.
$ErrorActionPreference = 'SilentlyContinue'

function Get-GuardMode([string]$HookName) {
    $raw = ([string]$env:MERIDIAN_GUARD).Trim().ToLowerInvariant()
    if ($raw -eq 'off') { return 'off' }
    $mode = if ($raw -eq '' -or $raw -eq 'enforce') { 'enforce' } else { 'advisory' }
    if ($raw -eq '' -and ([string]$env:MERIDIAN_GUARD_DEFAULT_MODE).Trim().ToLowerInvariant() -eq 'advisory') { $mode = 'advisory' }
    foreach ($tok in ([string]$env:MERIDIAN_GUARD_DISABLE -split '[\s,;]+')) {
        if ($tok.Trim().ToLowerInvariant() -eq $HookName) { return 'off' }
    }
    $gd = $null
    if ($env:LOCALAPPDATA) { $gd = [System.IO.Path]::Combine($env:LOCALAPPDATA, 'meridian', 'guard') }
    elseif ($env:USERPROFILE) { $gd = [System.IO.Path]::Combine($env:USERPROFILE, 'AppData', 'Local', 'meridian', 'guard') }
    elseif ($env:HOME) {
        $st = if ($env:XDG_STATE_HOME) { $env:XDG_STATE_HOME } else { [System.IO.Path]::Combine($env:HOME, '.local', 'state') }
        $gd = [System.IO.Path]::Combine($st, 'meridian', 'guard')
    }
    if ($gd) {
        if ([System.IO.File]::Exists([System.IO.Path]::Combine($gd, 'guard.off'))) { return 'off' }
        if ([System.IO.File]::Exists([System.IO.Path]::Combine($gd, 'guard.advisory'))) { return 'advisory' }
    }
    return $mode
}

# Resolve-LiveUrl: fast TCP-connect probe (300 ms) for a MERIDIAN_URL base, so a down
# server (the default state) fails fast instead of costing multi-second DNS/connect
# stalls. Copied from sprint_guard.ps1 (kept in sync there); see 41f26499 there for why
# "localhost" is special-cased to 127.0.0.1/::1.
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

# fix round 2: is Meridian actually up? A TCP-reachable /health that answers (any
# 2xx) counts; a timeout, a connection error, or a non-2xx status does not. This
# never calls request_hitl itself -- that would create a real HITL request just to
# probe reachability -- /health is a side-effect-free liveness check.
function Test-MeridianReachable([string]$Base) {
    $live = Resolve-LiveUrl $Base
    if (-not $live) { return $false }
    try {
        $null = Invoke-RestMethod -Method GET -Uri "$live/health" -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

try { $payload = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $payload) { exit 0 }
try { $tool = ($payload | ConvertFrom-Json).tool_name } catch { exit 0 }
if ($tool -eq 'AskUserQuestion') {
    $mode = Get-GuardMode 'hitl_guard'
    if ($mode -eq 'off') { exit 0 }
    $msg = "Meridian HITL guard (b8fbb4cb): do NOT use the native AskUserQuestion -- it bypasses Meridian's hitl_requests queue, so the question never appears in the dashboard or handoffs. Call request_hitl(project_id, question) instead: it logs the question and (with auto-answer on) returns the answer inline. If request_hitl is unavailable (Meridian unreachable, unauthenticated or erroring), ask the question in plain text in your reply instead and wait for the answer."
    if ($mode -eq 'advisory') {
        $o = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; additionalContext = ('[advisory, not blocked] ' + $msg) } }
        [Console]::Out.Write(($o | ConvertTo-Json -Compress -Depth 4))
        exit 0
    }
    $Url = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { 'http://localhost:7878' }
    if (-not (Test-MeridianReachable $Url)) {
        [Console]::Error.WriteLine("Meridian HITL guard (b8fbb4cb): could not reach $Url (fail-open) -- allowing the native AskUserQuestion through. Once Meridian is reachable again, prefer request_hitl so the question is logged in the hitl_requests queue.")
        exit 0
    }
    [Console]::Error.WriteLine($msg)
    exit 2  # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
}
exit 0
