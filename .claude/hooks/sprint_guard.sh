#!/usr/bin/env bash
# c0d2356d -- Claude Code Stop hook (auto-written by generate_handoff). Blocks an
# EXECUTOR session from stopping while this project has pending sprint items. Fails OPEN.
# This is NOT hooks.sh (the token-rotation installer).
# b4ce3274 -- bounded retry ceiling: the server stops reporting pending>0 for a
# session after MERIDIAN_STOP_OVERRIDE_CEILING forced continuations, so this
# guard then lets the stop through (exit 0) instead of blocking forever.
# e2e1b682 -- verification_pending_count is ADVISORY ONLY: it surfaces items
# flagged require_verification that are still missing an independent
# fresh-session PASS, but never changes the exit code (only
# complete_sprint_item's structural gate blocks the completion itself).
# 55d48d69 fix round 1 (mirrors sprint_guard.ps1): only a session that claimed a
# sprint item (a claim_sprint_item tool call in its own transcript) is held back;
# curl gets a 1 s connect timeout; a03c0eeb's worktree sweep trigger was REMOVED
# (the server's sweep force-removes terminal worktrees, dirty ones included, and
# already runs periodically there); the owner kill switch (MERIDIAN_GUARD=off|advisory,
# guard.off / guard.advisory, MERIDIAN_GUARD_DISABLE=sprint_guard) never blocks.
set -uo pipefail
PROJECT_ID="5787cc92-ba7d-4788-b17c-28ab7938b839"
MERIDIAN_URL="${MERIDIAN_URL:-http://localhost:7878}"
payload="$(cat 2>/dev/null || true)"
if printf '%s' "$payload" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

mode=enforce
_raw="$(printf '%s' "${MERIDIAN_GUARD:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
[ -n "$_raw" ] && [ "$_raw" != "enforce" ] && mode=advisory
[ -z "$_raw" ] && [ "$(printf '%s' "${MERIDIAN_GUARD_DEFAULT_MODE:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" = "advisory" ] && mode=advisory
[ "$_raw" = "off" ] && mode=off
for _tok in $(printf '%s' "${MERIDIAN_GUARD_DISABLE:-}" | tr ',;' '  ' | tr '[:upper:]' '[:lower:]'); do
  [ "$_tok" = "sprint_guard" ] && mode=off
done
if [ -n "${LOCALAPPDATA:-}" ]; then _gd="$LOCALAPPDATA/meridian/guard"
elif [ -n "${USERPROFILE:-}" ]; then _gd="$USERPROFILE/AppData/Local/meridian/guard"
else _gd="${XDG_STATE_HOME:-${HOME:-/nonexistent}/.local/state}/meridian/guard"
fi
if [ "$mode" != "off" ]; then
  if [ -f "$_gd/guard.off" ]; then mode=off; elif [ -f "$_gd/guard.advisory" ]; then mode=advisory; fi
fi
[ "$mode" = "off" ] && exit 0

# Only an executor session -- one that claimed a sprint item -- is held back.
tp="$(printf '%s' "$payload" | grep -oE '"transcript_path"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"' | head -1 | sed -E 's/^"transcript_path"[[:space:]]*:[[:space:]]*"//; s/"$//')"
tp="${tp//\\\\/\\}"
[ -n "$tp" ] && [ -f "$tp" ] || exit 0
grep -qE '"name"[[:space:]]*:[[:space:]]*"mcp__[^"]*__claim_sprint_item"' "$tp" 2>/dev/null || exit 0

# b4ce3274 -- forward the session id (if the hook payload carries one) so the
# override budget is counted per session, not per project.
sid="$(printf '%s' "$payload" | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*"session_id"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' || true)"
url="$MERIDIAN_URL/projects/$PROJECT_ID/sprint/pending_count"
[ -n "$sid" ] && url="$url?session_id=$sid"
# Hosted instances require a bearer token. Read it from the hook environment,
# validate it, and pass it to curl through stdin config so it never appears in
# the process arguments or this hook's output.
_auth_token="${MERIDIAN_TOKEN:-${BEARER_TOKEN:-}}"
_auth_config=""
if [[ "$_auth_token" =~ ^[A-Za-z0-9._~+/-]+=*$ ]]; then
  _auth_quote='"'
  _auth_config="header = ${_auth_quote}Authorization: Bearer ${_auth_token}${_auth_quote}"
fi
# 41f26499 -- a Meridian-unreachable window (or a malformed/empty response)
# used to fail open SILENTLY here, which could abandon this session's file
# claims with no visible signal (they then only clear via the file-claim 2h
# TTL). Fail-open behavior is UNCHANGED (still exit 0) but now surfaces a
# clear stderr warning so the human/agent notices instead of silently
# continuing.
if [ -n "$_auth_config" ]; then
  resp="$(printf '%s\n' "$_auth_config" | curl -sf --connect-timeout 1 --max-time 5 --config - "$url" 2>/dev/null || true)"
else
  resp="$(curl -sf --connect-timeout 1 --max-time 5 "$url" 2>/dev/null || true)"
fi
if [ -z "$resp" ]; then
  echo "Meridian (41f26499): could not reach $MERIDIAN_URL to check pending sprint items - allowing stop (fail-open). WARNING: any file claims held by this session will NOT be released and will only clear via the 2h claim TTL; release them manually (release_file) once Meridian is reachable again." >&2
  exit 0
fi
pending="$(printf '%s' "$resp" | grep -oE '"pending_count"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$' || true)"
if [ -z "$pending" ]; then
  echo "Meridian (41f26499): got an empty or malformed response from $MERIDIAN_URL - allowing stop (fail-open). WARNING: any file claims held by this session will NOT be released and will only clear via the 2h claim TTL; release them manually (release_file) once Meridian is reachable again." >&2
  exit 0
fi
if [ "$pending" -gt 0 ] 2>/dev/null; then
  if [ "$mode" = "advisory" ]; then
    echo "[advisory, not blocked] Meridian: $pending sprint item(s) still pending - complete or skip them (complete_sprint_item) before stopping." >&2
    exit 0
  fi
  echo "Meridian: $pending sprint item(s) still pending - complete or skip them (complete_sprint_item) before stopping." >&2
  exit 2
fi
# pending==0: either genuinely done, or the stop-override ceiling was reached --
# surface the ceiling case so the human/agent knows to generate a delta handoff.
if printf '%s' "$resp" | grep -Eq '"stopped_at_ceiling"[[:space:]]*:[[:space:]]*true'; then
  echo "Meridian: stop-override ceiling reached - allowing stop despite pending items; generate a delta handoff." >&2
fi
verpending="$(printf '%s' "$resp" | grep -oE '"verification_pending_count"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$' || true)"
if [ -n "$verpending" ] && [ "$verpending" -gt 0 ] 2>/dev/null; then
  echo "Meridian: $verpending item(s) require an independent fresh-session PASS/FAIL verification before their completion can stick (require_verification=true, no independent PASS on file yet)." >&2
fi
exit 0
