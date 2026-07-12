#!/usr/bin/env bash
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
# Wire-in (already present in .claude/settings.json SessionStart):
#   { "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/post_compact_refresh.sh" }
set -uo pipefail

EMPTY='{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'

payload="$(cat 2>/dev/null || true)"

# Extract the "source" field without a JSON parser dependency.
source="$(printf '%s' "$payload" \
  | grep -oE '"source"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | head -1 \
  | sed -E 's/.*"source"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' || true)"

if [ "$source" != "compact" ]; then
  printf '%s' "$EMPTY"
  exit 0
fi

reminder="[Meridian] Context was just compacted. Before continuing, RE-ORIENT:\n1. Call refresh_context(project_name=...) for a compact snapshot of the sprint, progress, active session id, recent handoffs, and key decisions.\n2. Call refresh_tool_manifest (re-issue tools/list) so the tool list is current after the compaction - a mid-session deploy may have added or renamed tools, and the pre-compaction manifest can be stale.\n3. Re-read the sprint goal / next pending item so you resume the SAME work instead of re-deriving it. Do not treat the summarized transcript as the full plan."

# Emit the SessionStart envelope. printf renders \n as a real newline; the JSON
# body needs those as escaped \n, so build it with the literal escape sequence.
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}' "$reminder"
exit 0
