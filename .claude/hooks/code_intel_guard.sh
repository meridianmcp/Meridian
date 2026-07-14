#!/usr/bin/env bash
# aeba8a80 -- PreToolUse code-intel guard (structural, not text).
#
# Prose guidance in DEFAULT_AGENT_INSTRUCTIONS has been strengthened to v10 and
# has still not held: a live session tonight used raw grep exclusively rather than
# code-intel tools. This hook fires on Grep and Glob tool calls and, ONLY when this
# project has a populated code-intel index available (code_intel_enabled == 1 in the
# project settings), blocks (exit 2) and redirects to the code-intel tools instead.
# If code-intel is not enabled, or the status check fails for ANY reason, FAILS OPEN
# (exit 0) -- never block a session that has nothing to redirect to.
#
# Mirrors the structural pattern of hitl_guard.sh (PreToolUse, exit 2 to block,
# tolerant JSON extraction) and sprint_guard.sh (curl the live Meridian server, fail
# open on any parse/network error).
# NOT hooks.sh (the token-rotation installer).
set -uo pipefail
PROJECT_ID="5787cc92-ba7d-4788-b17c-28ab7938b839"
MERIDIAN_URL="${MERIDIAN_URL:-http://localhost:7878}"
payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0
# Extract "tool_name": "..." tolerantly; fail open if it can't be parsed.
tool="$(printf '%s' "$payload" | grep -oE '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
[ -z "$tool" ] && exit 0
# Only intercept Grep and Glob tool calls.
case "$tool" in
    Grep|Glob) ;;
    *) exit 0 ;;
esac
# Ask the live Meridian server if code-intel is enabled for this project.
# Fail open on any curl/parse error -- never block when we can't confirm an index.
resp="$(curl -sf --max-time 5 "$MERIDIAN_URL/projects/$PROJECT_ID/settings" 2>/dev/null || true)"
[ -z "$resp" ] && exit 0
# Extract "code_intel_enabled": N tolerantly; fail open if missing.
ci_val="$(printf '%s' "$resp" | grep -oE '"code_intel_enabled"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$' || true)"
[ -z "$ci_val" ] && exit 0
if [ "$ci_val" -eq 1 ] 2>/dev/null; then
    # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
    echo "Meridian code-intel guard (aeba8a80): this project has a code-intel index. Use code-intel tools INSTEAD of $tool:
  - find_symbol / extractor__find_symbol        -- exact symbol lookup (fastest, most accurate)
  - search_graph / codebase__search_graph       -- structural graph queries (callers, callees, paths)
  - search_code / search_code_semantic          -- fuzzy / conceptual queries across the codebase
  - find_referencing_symbols                    -- find all callers of a function
  - get_code_snippet / get_architecture         -- retrieve file sections or the whole-project architecture
Raw grep/glob is a LAST RESORT for code search (443aa32a) -- the above tools are faster, use far fewer tokens, and don't miss symbol aliases. Fall back to $tool ONLY for non-symbol content (log output, data files, config values) or after code-intel tools confirm a file path." >&2
    exit 2
fi
exit 0
