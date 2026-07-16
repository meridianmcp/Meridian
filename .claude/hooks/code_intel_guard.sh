#!/usr/bin/env bash
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
# Mirrors the structural pattern of hitl_guard.sh (PreToolUse, exit 2 to block,
# tolerant JSON extraction) and sprint_guard.sh (curl the live Meridian server, fail
# open on any parse/network error).
# NOT hooks.sh (the token-rotation installer).
set -uo pipefail
PROJECT_ID="5787cc92-ba7d-4788-b17c-28ab7938b839"
MERIDIAN_URL="${MERIDIAN_URL:-http://localhost:7878}"

# 14575683 -- optional jq fast path for JSON extraction, Linux/macOS only.
# Additive: Windows/Git-Bash keeps the regex chain below byte-for-byte
# unchanged (uname there is never Linux/Darwin). Even on Linux/macOS, if jq
# is absent or a jq extraction comes back empty, we fall through to the same
# tolerant regex this hook always used -- jq is never a hard dependency.
_jq_fastpath=0
if command -v jq >/dev/null 2>&1; then
    case "$(uname -s 2>/dev/null)" in
        Linux|Darwin) _jq_fastpath=1 ;;
    esac
fi

payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0
# Extract "tool_name": "..." tolerantly; fail open if it can't be parsed.
tool=""
if [ "$_jq_fastpath" -eq 1 ]; then
    tool="$(printf '%s' "$payload" | jq -r '.tool_name // empty' 2>/dev/null || true)"
fi
if [ -z "$tool" ]; then
    tool="$(printf '%s' "$payload" | grep -oE '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
fi
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
    # 81b10dec -- probe slot readiness; warm up an idle-killed Serena daemon.
    # The /slot-readiness endpoint calls _fetch_slot_tools("code") which sends a
    # tools/list to the slot -- waking the lazy-spawn proxy if it was idle-killed.
    # Fail open (exit 0) with a VISIBLE log when the slot is not yet ready after
    # one retry, rather than silently passing through.
    slot_resp="$(curl -sf --max-time 6 "$MERIDIAN_URL/projects/$PROJECT_ID/slot-readiness" 2>/dev/null || true)"
    if [ -n "$slot_resp" ]; then
        # Extract "ready": true/false tolerantly (jq fast path, else regex).
        slot_ready=""
        has_tunnel=""
        if [ "$_jq_fastpath" -eq 1 ]; then
            slot_ready="$(printf '%s' "$slot_resp" | jq -r '.ready | tostring' 2>/dev/null || true)"
            has_tunnel="$(printf '%s' "$slot_resp" | jq -r '.has_tunnel | tostring' 2>/dev/null || true)"
            case "$slot_ready" in true|false) ;; *) slot_ready="" ;; esac
            case "$has_tunnel" in true|false) ;; *) has_tunnel="" ;; esac
        fi
        if [ -z "$slot_ready" ]; then
            slot_ready="$(printf '%s' "$slot_resp" | grep -oE '"ready"[[:space:]]*:[[:space:]]*(true|false)' | grep -oE '(true|false)$' || true)"
        fi
        if [ -z "$has_tunnel" ]; then
            has_tunnel="$(printf '%s' "$slot_resp" | grep -oE '"has_tunnel"[[:space:]]*:[[:space:]]*(true|false)' | grep -oE '(true|false)$' || true)"
        fi
        if [ "$slot_ready" = "false" ]; then
            # Slot is not ready yet -- brief retry (warmup may be in progress).
            sleep 3
            slot_resp2="$(curl -sf --max-time 6 "$MERIDIAN_URL/projects/$PROJECT_ID/slot-readiness" 2>/dev/null || true)"
            if [ -n "$slot_resp2" ]; then
                slot_ready=""
                if [ "$_jq_fastpath" -eq 1 ]; then
                    slot_ready="$(printf '%s' "$slot_resp2" | jq -r '.ready | tostring' 2>/dev/null || true)"
                    case "$slot_ready" in true|false) ;; *) slot_ready="" ;; esac
                fi
                if [ -z "$slot_ready" ]; then
                    slot_ready="$(printf '%s' "$slot_resp2" | grep -oE '"ready"[[:space:]]*:[[:space:]]*(true|false)' | grep -oE '(true|false)$' || true)"
                fi
            fi
            if [ "$slot_ready" != "true" ]; then
                # Still not ready after retry -- fail open with a VISIBLE warning.
                echo "Meridian code-intel guard (81b10dec): code-intel slot NOT ready after warmup probe. The Serena/code-intel daemon may still be starting. Failing open -- $tool is allowed this time. Retry in a moment or check 'meridian --tunnel' status." >&2
                exit 0
            fi
        elif [ "$has_tunnel" = "false" ]; then
            # No tunnel active (self-hosted or tunnel not connected) -- fail open.
            # The enabled flag alone isn't enough; a live slot is needed to block.
            echo "Meridian code-intel guard (81b10dec): code-intel enabled but no tunnel slot is connected. Failing open -- $tool is allowed. Connect the meridian tunnel to enable slot-based enforcement." >&2
            exit 0
        fi
    fi
    # Slot is ready (or probe was skipped due to no-tunnel) -- block the tool call.
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
