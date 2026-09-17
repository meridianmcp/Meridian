#!/usr/bin/env bash
# c0d2356d — Claude Code Stop hook (auto-written by generate_handoff). Blocks a
# session from stopping while this project has pending sprint items. Fails OPEN.
# This is NOT hooks.sh (the token-rotation installer).
# b4ce3274 — bounded retry ceiling: the server stops reporting pending>0 for a
# session after MERIDIAN_STOP_OVERRIDE_CEILING forced continuations, so this
# guard then lets the stop through (exit 0) instead of blocking forever.
# e2e1b682 — verification_pending_count is ADVISORY ONLY: it surfaces items
# flagged require_verification that are still missing an independent
# fresh-session PASS, but never changes the exit code (only
# complete_sprint_item's structural gate blocks the completion itself).
set -uo pipefail
PROJECT_ID="5787cc92-ba7d-4788-b17c-28ab7938b839"
MERIDIAN_URL="${MERIDIAN_URL:-http://localhost:7878}"
payload="$(cat 2>/dev/null || true)"
if printf '%s' "$payload" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi
# b4ce3274 — forward the session id (if the hook payload carries one) so the
# override budget is counted per session, not per project.
sid="$(printf '%s' "$payload" | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*"session_id"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' || true)"
url="$MERIDIAN_URL/projects/$PROJECT_ID/sprint/pending_count"
[ -n "$sid" ] && url="$url?session_id=$sid"
# 41f26499 — a Meridian-unreachable window (or a malformed/empty response)
# used to fail open SILENTLY here, which could abandon this session's file
# claims with no visible signal (they then only clear via the file-claim 2h
# TTL). Fail-open behavior is UNCHANGED (still exit 0) but now surfaces a
# clear stderr warning so the human/agent notices instead of silently
# continuing.
resp="$(curl -sf --max-time 5 "$url" 2>/dev/null || true)"
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
  echo "Meridian: $pending sprint item(s) still pending — complete or skip them (complete_sprint_item) before stopping." >&2
  exit 2
fi
# pending==0: either genuinely done, or the stop-override ceiling was reached —
# surface the ceiling case so the human/agent knows to generate a delta handoff.
if printf '%s' "$resp" | grep -Eq '"stopped_at_ceiling"[[:space:]]*:[[:space:]]*true'; then
  echo "Meridian: stop-override ceiling reached — allowing stop despite pending items; generate a delta handoff." >&2
fi
verpending="$(printf '%s' "$resp" | grep -oE '"verification_pending_count"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$' || true)"
if [ -n "$verpending" ] && [ "$verpending" -gt 0 ] 2>/dev/null; then
  echo "Meridian: $verpending item(s) require an independent fresh-session PASS/FAIL verification before their completion can stick (require_verification=true, no independent PASS on file yet)." >&2
fi
# a03c0eeb - the moment the guard is about to let a stop through is the
# natural post-integration point (this session has no pending sprint items
# left): fire a best-effort real disk cleanup pass for this project's git
# worktrees so items that already merged/finished don't leave orphaned
# worktree dirs behind. Self-hosted only server-side (see worktree_cleanup.py
# module docstring); fire-and-forget here, never blocks or fails the stop.
curl -sf --max-time 5 -X POST "$MERIDIAN_URL/projects/$PROJECT_ID/worktrees/sweep" >/dev/null 2>&1 || true
exit 0
