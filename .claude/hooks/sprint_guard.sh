#!/usr/bin/env bash
# c0d2356d — Claude Code Stop hook: block a session from stopping while this
# project still has pending sprint items. Structural prevention of RLHF-style
# early-stopping. generate_handoff() auto-writes this file with PROJECT_ID + URL
# baked in; this committed copy is the cross-platform fallback.
#
# This is NOT hooks.sh (the token-rotation installer) — never confuse the two.
set -uo pipefail

PROJECT_ID="${MERIDIAN_PROJECT_ID:-5787cc92-ba7d-4788-b17c-28ab7938b839}"
MERIDIAN_URL="${MERIDIAN_URL:-http://localhost:7878}"

# The Stop hook receives a JSON payload on stdin.
payload="$(cat 2>/dev/null || true)"

# Loop guard: if we're already continuing because of a previous Stop block
# (stop_hook_active=true), allow the stop so we can never spin forever.
if printf '%s' "$payload" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

# Fail OPEN on any network/parse error so a down server never traps the user.
resp="$(curl -sf --max-time 5 "$MERIDIAN_URL/projects/$PROJECT_ID/sprint/pending_count" 2>/dev/null || true)"
[ -z "$resp" ] && exit 0
pending="$(printf '%s' "$resp" | grep -oE '"pending_count"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$' || true)"
[ -z "$pending" ] && exit 0

if [ "$pending" -gt 0 ] 2>/dev/null; then
  # exit 2 blocks the stop; stderr is fed back to Claude as the reason.
  echo "Meridian: $pending sprint item(s) still pending — complete or skip them (complete_sprint_item) before stopping." >&2
  exit 2
fi
exit 0
