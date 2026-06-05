#!/usr/bin/env bash
# test_hooks.sh — hooks.sh WSL/bash end-to-end test (457440c3)
#
# Run this from the repo root on a machine with WSL (or natively on Linux/Mac).
# It exercises hooks.sh with piped inputs and verifies the output JSON.
#
# Usage:
#   bash scripts/test_hooks.sh [meridian_url] [project_id]
#
# Defaults to localhost:7878 and the meridian-build project.

set -euo pipefail

MERIDIAN_URL="${1:-http://localhost:7878}"
PROJECT_ID="${2:-${MERIDIAN_PROJECT_ID:-}}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS="\033[32mPASS\033[0m"
FAIL="\033[31mFAIL\033[0m"
FAILURES=0

if [ -z "$PROJECT_ID" ]; then
  echo "Set MERIDIAN_PROJECT_ID or pass a project ID as the second argument." >&2
  exit 1
fi

echo ""
echo "hooks.sh WSL/bash end-to-end test"
echo "==================================="
echo "Meridian URL : $MERIDIAN_URL"
echo "Project ID   : $PROJECT_ID"
echo ""

check() {
  local label="$1" ok="$2" detail="${3:-}"
  if [ "$ok" = "1" ]; then
    echo -e "  [$PASS] $label${detail:+ — $detail}"
  else
    echo -e "  [$FAIL] $label${detail:+ — $detail}"
    FAILURES=$((FAILURES + 1))
  fi
}

# Step 1: hooks.sh exists and is executable
[ -f "$REPO_ROOT/hooks.sh" ]
check "hooks.sh exists" 1

# Step 2: Run hooks.sh with piped inputs (non-interactive)
SETTINGS_FILE="$(mktemp /tmp/meridian-hooks-test-XXXXXX.json)"
ORIG_HOME="$HOME"
export HOME="$(mktemp -d /tmp/meridian-home-test-XXXXXX)"
mkdir -p "$HOME/.claude"

printf '%s\n%s\n' "$MERIDIAN_URL" "$PROJECT_ID" | bash "$REPO_ROOT/hooks.sh" > /tmp/hooks-sh-output.txt 2>&1
EXIT_CODE=$?
export HOME="$ORIG_HOME"

check "hooks.sh exits 0" "$([ $EXIT_CODE -eq 0 ] && echo 1 || echo 0)" "exit=$EXIT_CODE"

# Step 3: Verify ~/.claude/settings.json was written and is valid JSON
SETTINGS_PATH="$(mktemp -d /tmp/meridian-home-test-XXXXXX)"
mkdir -p "$SETTINGS_PATH/.claude"
printf '%s\n%s\n' "$MERIDIAN_URL" "$PROJECT_ID" | HOME="$SETTINGS_PATH" bash "$REPO_ROOT/hooks.sh" > /dev/null 2>&1

if [ -f "$SETTINGS_PATH/.claude/settings.json" ]; then
  check "settings.json was created" 1
  # Parse JSON with python3 if available, else jq
  if command -v python3 &>/dev/null; then
    VALID=$(python3 -c "import json,sys; json.load(open('$SETTINGS_PATH/.claude/settings.json')); print('1')" 2>/dev/null || echo "0")
    check "settings.json is valid JSON" "$VALID"
    HAS_START=$(python3 -c "import json; d=json.load(open('$SETTINGS_PATH/.claude/settings.json')); print('1' if d.get('hooks',{}).get('SessionStart') else '0')" 2>/dev/null || echo "0")
    check "settings.json has hooks.SessionStart" "$HAS_START"
    HAS_STOP=$(python3 -c "import json; d=json.load(open('$SETTINGS_PATH/.claude/settings.json')); print('1' if d.get('hooks',{}).get('Stop') else '0')" 2>/dev/null || echo "0")
    check "settings.json has hooks.Stop" "$HAS_STOP"
    CMD_TYPE=$(python3 -c "import json; d=json.load(open('$SETTINGS_PATH/.claude/settings.json')); print(d.get('hooks',{}).get('SessionStart',[{}])[0].get('type',''))" 2>/dev/null || echo "")
    check "SessionStart type=command" "$([ "$CMD_TYPE" = "command" ] && echo 1 || echo 0)" "type=$CMD_TYPE"
  elif command -v jq &>/dev/null; then
    check "settings.json is valid JSON" "$(jq . "$SETTINGS_PATH/.claude/settings.json" &>/dev/null && echo 1 || echo 0)"
    check "settings.json has SessionStart" "$(jq -e '.hooks.SessionStart' "$SETTINGS_PATH/.claude/settings.json" &>/dev/null && echo 1 || echo 0)"
  else
    echo "  [SKIP] JSON validation (python3/jq not available)"
  fi
else
  check "settings.json was created" 0 "file not found"
fi

rm -rf "$SETTINGS_PATH"

# Step 4: Verify hook endpoints (only if server is running)
if curl -sf "$MERIDIAN_URL/health" &>/dev/null; then
  echo ""
  echo "  Server at $MERIDIAN_URL is reachable — testing hook endpoints..."

  START_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$MERIDIAN_URL/hooks/session-start" \
    -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJECT_ID\",\"session_name\":\"hooks-test\"}")
  check "POST /hooks/session-start → 200" "$([ "$START_CODE" = "200" ] && echo 1 || echo 0)" "code=$START_CODE"

  STOP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$MERIDIAN_URL/hooks/stop" \
    -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$PROJECT_ID\"}")
  check "POST /hooks/stop → 200" "$([ "$STOP_CODE" = "200" ] && echo 1 || echo 0)" "code=$STOP_CODE"
else
  echo "  [SKIP] Hook endpoint tests — server not running at $MERIDIAN_URL"
  echo "         Start with: pixi run start"
  echo "         Then re-run this script"
fi

echo ""
if [ $FAILURES -eq 0 ]; then
  echo "All checks passed."
else
  echo "$FAILURES check(s) FAILED."
fi
echo ""
exit $FAILURES
