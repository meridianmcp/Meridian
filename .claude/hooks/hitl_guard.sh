#!/usr/bin/env bash
# b8fbb4cb -- PreToolUse HITL guard (structural, not text). Cross-platform fallback for
# hitl_guard.ps1 (the .ps1 runs on the maintainer's Windows box; this .sh covers
# Linux/macOS executors + is what the regression test exercises).
#
# Blocks the executor from using Claude Code's NATIVE ask-UI (AskUserQuestion) and
# redirects to Meridian's request_hitl, so every human-in-the-loop question is logged in
# the hitl_requests table (the native ask bypasses it -- confirmed absent 3x). Text
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
  # 55d48d69 fix round 1: the owner kill switch covers this hook (same inputs as the
  # Meridian guard's G0), and the block names the plain-text fallback for when
  # request_hitl itself is unavailable.
  #
  # fix round 2: round 1 only NAMED the fallback -- it still blocked AskUserQuestion
  # unconditionally, even with Meridian completely unreachable, which left a session
  # with no way to ask the human at all in exactly the situation where the fallback
  # is needed most. Now probes Meridian's /health (mirroring sprint_guard.sh's fast
  # curl connect-timeout) and only denies when it actually answers 2xx; a timeout,
  # connection error, or non-2xx fails OPEN (allows the native ask through).
  _raw="$(printf '%s' "${MERIDIAN_GUARD:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  _mode=enforce
  [ -n "$_raw" ] && [ "$_raw" != "enforce" ] && _mode=advisory
  [ -z "$_raw" ] && [ "$(printf '%s' "${MERIDIAN_GUARD_DEFAULT_MODE:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" = "advisory" ] && _mode=advisory
  [ "$_raw" = "off" ] && _mode=off
  for _tok in $(printf '%s' "${MERIDIAN_GUARD_DISABLE:-}" | tr ',;' '  ' | tr '[:upper:]' '[:lower:]'); do
    [ "$_tok" = "hitl_guard" ] && _mode=off
  done
  _gd=''
  if [ -n "${LOCALAPPDATA:-}" ]; then _gd="$LOCALAPPDATA/meridian/guard"
  elif [ -n "${USERPROFILE:-}" ]; then _gd="$USERPROFILE/AppData/Local/meridian/guard"
  elif [ -n "${HOME:-}" ]; then _gd="${XDG_STATE_HOME:-$HOME/.local/state}/meridian/guard"
  fi
  if [ "$_mode" != "off" ] && [ -n "$_gd" ]; then
    if [ -f "$_gd/guard.off" ]; then _mode=off; elif [ -f "$_gd/guard.advisory" ]; then _mode=advisory; fi
  fi
  [ "$_mode" = "off" ] && exit 0
  _msg="Meridian HITL guard (b8fbb4cb): do NOT use the native AskUserQuestion -- it bypasses Meridian's hitl_requests queue, so the question never appears in the dashboard or handoffs. Call request_hitl(project_id, question) instead: it logs the question and (with auto-answer on) returns the answer inline. If request_hitl is unavailable (Meridian unreachable, unauthenticated or erroring), ask the question in plain text in your reply instead and wait for the answer."
  if [ "$_mode" = "advisory" ]; then
    _esc=${_msg//\\/\\\\}; _esc=${_esc//\"/\\\"}
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"[advisory, not blocked] %s"}}' "$_esc"
    exit 0
  fi
  _url="${MERIDIAN_URL:-http://localhost:7878}"
  # side-effect-free liveness probe -- never calls request_hitl itself just to check
  # reachability. -f makes curl fail (nonzero) on a non-2xx response too.
  if ! curl -sf --connect-timeout 1 --max-time 3 -o /dev/null "$_url/health" 2>/dev/null; then
    echo "Meridian HITL guard (b8fbb4cb): could not reach $_url (fail-open) -- allowing the native AskUserQuestion through. Once Meridian is reachable again, prefer request_hitl so the question is logged in the hitl_requests queue." >&2
    exit 0
  fi
  # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
  echo "$_msg" >&2
  exit 2
fi
exit 0
