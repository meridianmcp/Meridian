# aeba8a80 -- PreToolUse code-intel guard (structural, not text).
# 81b10dec -- extended: proactive slot warmup + visible fallback logging.
#
# Prose guidance in DEFAULT_AGENT_INSTRUCTIONS has been strengthened to v10 and
# has still not held: a live session tonight used raw grep exclusively rather than
# code-intel tools. This hook fires on Grep and Glob tool calls and, ONLY when this
# project has a populated code-intel index available (code_intel_enabled == 1 in the
# project settings), blocks (exit 2) and redirects to the code-intel tools instead.
# If code-intel is not enabled, or the status check fails for ANY reason, FAILS OPEN
# (exit 0) -- never block a session that has nothing to redirect to.
#
# 81b10dec extension: after confirming code_intel_enabled=1, also probe slot
# readiness via /projects/{id}/slot-readiness. If the code/Serena slot is cold
# (idle-killed after 30min), that endpoint triggers a warmup tools/list. We do
# one brief retry (up to 3s) before falling back to fail-open with a VISIBLE
# stderr log -- the fallback is never silent.
#
# Mirrors the structural pattern of hitl_guard.ps1 (PreToolUse, exit 2 to block,
# tolerant JSON parsing) and sprint_guard.ps1 (curl the live Meridian server, fail
# open on any parse/network error).
# NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'
$ProjectId = "5787cc92-ba7d-4788-b17c-28ab7938b839"
$MeridianUrl = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { "http://localhost:7878" }
try { $payload = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $payload) { exit 0 }
try { $tool = ($payload | ConvertFrom-Json).tool_name } catch { exit 0 }
if (-not $tool) { exit 0 }
# Only intercept Grep and Glob tool calls.
if ($tool -ne 'Grep' -and $tool -ne 'Glob') { exit 0 }
# Ask the live Meridian server if code-intel is enabled for this project.
# Fail open on any web/parse error -- never block when we can't confirm an index.
try {
    $resp = Invoke-RestMethod -Uri "$MeridianUrl/projects/$ProjectId/settings" -Method Get -TimeoutSec 5
} catch { exit 0 }
if (-not $resp) { exit 0 }
$ciVal = $resp.code_intel_enabled
if ($ciVal -ne $null -and [int]$ciVal -eq 1) {
    # 81b10dec -- probe slot readiness; warm up an idle-killed Serena daemon.
    # The /slot-readiness endpoint calls _fetch_slot_tools("code") which sends a
    # tools/list to the slot -- waking the lazy-spawn proxy if it was idle-killed.
    # Fail open (exit 0) with a VISIBLE log when the slot is not yet ready after
    # one retry, rather than silently passing through.
    $slotResp = $null
    try {
        $slotResp = Invoke-RestMethod -Uri "$MeridianUrl/projects/$ProjectId/slot-readiness" -Method Get -TimeoutSec 7
    } catch { }
    # 883ce543 -- shared fail-open/block contract with code_intel_guard.sh:
    # slotReady/hasTunnel start UNCONFIRMED ($null) and are only ever set from
    # a successfully-returned response. Blocking below requires BOTH to be
    # positively confirmed $true -- every other outcome (endpoint unreachable
    # or an exception, a response missing ready/has_tunnel, explicit
    # ready=false, explicit has_tunnel=false) falls through to fail-open.
    # $null -eq $true and $null -eq $false both evaluate to False in
    # PowerShell, so a missing/null value never accidentally satisfies either
    # check below. Previously this function only special-cased
    # ready=false/has_tunnel=false explicitly and fell through to BLOCK for
    # anything unhandled ($slotResp -eq $null, missing ready/has_tunnel keys)
    # -- the exact opposite of the documented policy.
    $slotReady = $null
    $hasTunnel = $null
    if ($slotResp -ne $null) {
        $slotReady = $slotResp.ready
        $hasTunnel = $slotResp.has_tunnel
    }
    if ($slotReady -eq $false) {
        # Slot is not ready yet -- brief retry (warmup may be in progress).
        Start-Sleep -Seconds 3
        $slotResp2 = $null
        try {
            $slotResp2 = Invoke-RestMethod -Uri "$MeridianUrl/projects/$ProjectId/slot-readiness" -Method Get -TimeoutSec 7
        } catch { }
        $slotReady = $null
        if ($slotResp2 -ne $null) { $slotReady = $slotResp2.ready }
    }
    if ($slotReady -eq $true -and $hasTunnel -eq $true) {
        # Positively confirmed ready AND tunneled -- block the tool call.
        [Console]::Error.WriteLine("Meridian code-intel guard (aeba8a80): this project has a code-intel index. Use code-intel tools INSTEAD of ${tool}:
  - find_symbol / extractor__find_symbol        -- exact symbol lookup (fastest, most accurate)
  - search_graph / codebase__search_graph       -- structural graph queries (callers, callees, paths)
  - search_code / search_code_semantic          -- fuzzy / conceptual queries across the codebase
  - find_referencing_symbols                    -- find all callers of a function
  - get_code_snippet / get_architecture         -- retrieve file sections or the whole-project architecture
Raw grep/glob is a LAST RESORT for code search (443aa32a) -- the above tools are faster, use far fewer tokens, and don't miss symbol aliases. Fall back to ${tool} ONLY for non-symbol content (log output, data files, config values) or after code-intel tools confirm a file path.")
        exit 2
    }
    # Fail open -- every unconfirmed case lands here with a VISIBLE reason.
    if ($slotResp -eq $null) {
        [Console]::Error.WriteLine("Meridian code-intel guard (81b10dec): the slot-readiness endpoint was unreachable or returned an error. Failing open -- ${tool} is allowed. Check 'meridian --tunnel' status or MERIDIAN_URL.")
    } elseif ($slotReady -ne $true) {
        # Still not ready after retry, or the response was missing/unparseable
        # -- fail open with a VISIBLE warning either way.
        [Console]::Error.WriteLine("Meridian code-intel guard (81b10dec): code-intel slot NOT ready after warmup probe. The Serena/code-intel daemon may still be starting. Failing open -- ${tool} is allowed this time. Retry in a moment or check 'meridian --tunnel' status.")
    } else {
        # No tunnel active (self-hosted or tunnel not connected) -- fail open.
        [Console]::Error.WriteLine("Meridian code-intel guard (81b10dec): code-intel enabled but no tunnel slot is connected. Failing open -- ${tool} is allowed. Connect the meridian tunnel to enable slot-based enforcement.")
    }
    exit 0
}
exit 0
