#!/usr/bin/env bash
# b8fbb4cb — PreToolUse HITL guard (structural, not text). Cross-platform fallback for
# hitl_guard.ps1 (the .ps1 runs on the maintainer's Windows box; this .sh covers
# Linux/macOS executors + is what the regression test exercises).
#
# Blocks the executor from using Claude Code's NATIVE ask-UI (AskUserQuestion) and
# redirects to Meridian's request_hitl, so every human-in-the-loop question is logged in
# the hitl_requests table (the native ask bypasses it — confirmed absent 3x). Text
# guidance failed 3 times (36edd005, d261ea2e); this is structural enforcement, the same
# pattern as the file-claim guard. Wired under PreToolUse with matcher "AskUserQuestion",
# so it ONLY runs for that one tool and can never affect another. Fails OPEN.
# NOT hooks.sh (the token-rotation installer).
set -uo pipefail

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
if [ "$tool" = "AskUserQuestion" ]; then
  # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
  echo "Meridian HITL guard (b8fbb4cb): do NOT use the native AskUserQuestion — it bypasses Meridian's hitl_requests queue, so the question never appears in the dashboard or handoffs. Call request_hitl(project_id, question) instead: it logs the question and (with auto-answer on) returns the answer inline." >&2
  exit 2
fi
exit 0
