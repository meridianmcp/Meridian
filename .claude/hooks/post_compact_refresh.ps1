# 3617361d - Claude Code SessionStart hook: post-compaction re-orientation.
#
# The server-side planner nudge (bf51b12e) is turn-count based and cannot see a
# /compact (a client-side harness event with no server visibility). This hook
# runs on SessionStart and, ONLY when source == "compact", injects a reminder to
# re-orient: call refresh_context / refresh_tool_manifest and re-read the sprint
# goal. For every other source (startup/resume/clear) it is a no-op so it does
# not disturb the existing meridian-start hook.
#
# Canonical logic lives in meridian/post_compact_refresh.py; this is a
# dependency-free mirror so the hook runs anywhere. Fails OPEN: any error still
# emits a valid, empty SessionStart envelope and exits 0.
#
# ASCII-only (PS 5.1 reads BOM-less UTF-8 as cp1252 -- no em-dashes/smart-quotes).
$ErrorActionPreference = 'SilentlyContinue'

$empty = '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'

$raw = [Console]::In.ReadToEnd()
$source = $null
try {
    $payload = $raw | ConvertFrom-Json
    if ($payload) { $source = [string]$payload.source }
} catch { $source = $null }

if ($source -ne 'compact') {
    Write-Output $empty
    exit 0
}

$reminder = @(
    '[Meridian] Context was just compacted. Before continuing, RE-ORIENT:',
    '1. Call refresh_context(project_name=...) for a compact snapshot of the sprint, progress, active session id, recent handoffs, and key decisions.',
    '2. Call refresh_tool_manifest (re-issue tools/list) so the tool list is current after the compaction - a mid-session deploy may have added or renamed tools, and the pre-compaction manifest can be stale.',
    '3. Re-read the sprint goal / next pending item so you resume the SAME work instead of re-deriving it. Do not treat the summarized transcript as the full plan.'
) -join "`n"

$out = [ordered]@{
    hookSpecificOutput = [ordered]@{
        hookEventName    = 'SessionStart'
        additionalContext = $reminder
    }
}
try {
    Write-Output ($out | ConvertTo-Json -Compress -Depth 5)
} catch {
    Write-Output $empty
}
exit 0
