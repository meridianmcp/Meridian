# b8fbb4cb — PreToolUse HITL guard (structural, not text).
#
# Blocks the executor from using Claude Code's NATIVE ask-UI (AskUserQuestion) and
# redirects to Meridian's request_hitl, so every human-in-the-loop question is logged
# in the hitl_requests table (the native ask bypasses it entirely — confirmed absent 3x).
# Text guidance in agent_instructions failed three times (36edd005, d261ea2e); this is
# the same structural-enforcement pattern as the file-claim guard.
#
# Wired in .claude/settings.json under PreToolUse with matcher "AskUserQuestion", so it
# ONLY ever runs for that one tool — it can never affect any other tool call. Fails OPEN
# on any parse error. This is NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'
try { $payload = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $payload) { exit 0 }
try { $tool = ($payload | ConvertFrom-Json).tool_name } catch { exit 0 }
if ($tool -eq 'AskUserQuestion') {
    [Console]::Error.WriteLine("Meridian HITL guard (b8fbb4cb): do NOT use the native AskUserQuestion -- it bypasses Meridian's hitl_requests queue, so the question never appears in the dashboard or handoffs. Call request_hitl(project_id, question) instead: it logs the question and (with auto-answer on) returns the answer inline.")
    exit 2  # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
}
exit 0
