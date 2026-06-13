#!/usr/bin/env bash
# Strip Windows CRLF if present
sed -i 's/\r//' "$0" 2>/dev/null || true
# hooks.sh - Meridian session lifecycle hooks installer
#
# Usage:
#   curl -fsSL https://usemeridian.us/hooks.sh | bash
#   bash hooks.sh
#   bash hooks.sh --url http://localhost:7878 --project-id your-project-id
#
# Installs Claude Code, Codex, and Cursor integrations. Credentials are embedded
# directly in hook commands â€” no per-repo config file required.
#
# Requirements: curl, jq
set -euo pipefail

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required but not found. Install it:"
  echo "  macOS:         brew install jq"
  echo "  Debian/Ubuntu: sudo apt install -y jq"
  echo "  Fedora/RHEL:   sudo dnf install -y jq"
  echo "  Arch:          sudo pacman -S jq"
  echo "  Alpine:        apk add jq"
  echo "  HPC/no sudo:   download from https://jqlang.github.io/jq/download/"
  exit 1
fi

DEFAULT_URL="https://usemeridian.us"
MERIDIAN_URL=""
PROJECT_ID=""
TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)        MERIDIAN_URL="${2:-}"; shift 2 ;;
    --project-id) PROJECT_ID="${2:-}";  shift 2 ;;
    --token)      TOKEN="${2:-}";       shift 2 ;;
    -h|--help)
      echo "Usage: bash hooks.sh [--url URL] [--project-id ID] [--token TOKEN]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo ""
echo "Meridian Connect"
echo "-----------------------"
echo ""

# ---- Step 1: URL -------------------------------------------------------------
if [[ -z "$MERIDIAN_URL" ]]; then
  if [ -t 0 ]; then
    read -rp "Meridian server URL [$DEFAULT_URL]: " input_url
    MERIDIAN_URL="${input_url:-$DEFAULT_URL}"
  else
    MERIDIAN_URL="$DEFAULT_URL"
  fi
fi
MERIDIAN_URL="${MERIDIAN_URL%/}"

if [[ ! "$MERIDIAN_URL" =~ ^https?:// ]]; then
  echo "Error: URL must start with https:// or http://" >&2
  exit 1
fi

echo "Checking $MERIDIAN_URL ..."
if ! curl -sf --max-time 5 "$MERIDIAN_URL/health" > /dev/null; then
  echo "Error: Cannot reach $MERIDIAN_URL/health â€” is the server running?" >&2
  exit 1
fi
echo "  OK server is reachable"

# ---- Step 2: Auth ------------------------------------------------------------
IS_LOCAL=0
if [[ "$MERIDIAN_URL" =~ ^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?(/|$) ]]; then
  IS_LOCAL=1
fi

if [[ $IS_LOCAL -eq 1 ]]; then
  echo ""
  echo "Self-hosted / localhost detected â€” skipping auth."
else
  if [[ -z "$TOKEN" ]]; then
    echo ""
    echo "Opening browser to authenticate..."
    if command -v xdg-open &>/dev/null; then
      xdg-open "$MERIDIAN_URL/auth/install" 2>/dev/null || true
    elif command -v open &>/dev/null; then
      open "$MERIDIAN_URL/auth/install" 2>/dev/null || true
    else
      echo "  Visit: $MERIDIAN_URL/auth/install"
    fi
    echo ""
    read -rsp "Paste the token shown in your browser: " TOKEN; echo ""
  fi
  TOKEN="${TOKEN// /}"
  if [[ -z "$TOKEN" ]]; then
    echo "Error: token is required for hosted Meridian." >&2
    exit 1
  fi
  echo ""
  echo "Validating token..."
  ME=$(curl -sf --max-time 10 \
    -H "Authorization: Bearer $TOKEN" \
    "$MERIDIAN_URL/auth/me" 2>/dev/null || echo "null")
  if [[ "$ME" == "null" ]] || [[ -z "$ME" ]]; then
    echo "Error: Token validation failed â€” is the token correct?" >&2
    exit 1
  fi
  EMAIL=$(echo "$ME" | jq -r '.email // empty' 2>/dev/null || echo "")
  echo "  Authenticated as: $EMAIL"
fi

# No project selection â€” hooks are global, project_id comes from the goal at session time.

# ---- Step 4: Generate permanent token ----------------------------------------
if [[ $IS_LOCAL -eq 0 ]] && [[ -n "$TOKEN" ]]; then
  PERM=$(curl -sf --max-time 10 -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"label":"hooks-installer"}' \
    "$MERIDIAN_URL/auth/tokens" 2>/dev/null || echo "")
  NEW_TOKEN=$(echo "$PERM" | jq -r '.token // empty' 2>/dev/null || echo "")
  if [[ -n "$NEW_TOKEN" ]]; then
    TOKEN="$NEW_TOKEN"
    echo "  Permanent token created."
  fi
fi

# ---- Step 5: Build hook commands (cwd + hostname read at fire time) ----------
if [[ -n "$TOKEN" ]]; then
  START_CMD="curl -s -X POST -H 'Authorization: Bearer ${TOKEN}' -H 'Content-Type: application/json' -d \"{\\\"cwd\\\":\\\"\$PWD\\\",\\\"hostname\\\":\\\"\$(hostname)\\\"}\" '${MERIDIAN_URL}/hooks/session-start' | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null"
  STOP_CMD="curl -s -X POST -H 'Authorization: Bearer ${TOKEN}' -H 'Content-Type: application/json' -d \"{\\\"hostname\\\":\\\"\$(hostname)\\\"}\" '${MERIDIAN_URL}/hooks/stop' >/dev/null 2>&1"
else
  START_CMD="curl -s -X POST -H 'Content-Type: application/json' -d \"{\\\"cwd\\\":\\\"\$PWD\\\",\\\"hostname\\\":\\\"\$(hostname)\\\"}\" '${MERIDIAN_URL}/hooks/session-start' | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null"
  STOP_CMD="curl -s -X POST -H 'Content-Type: application/json' -d \"{\\\"hostname\\\":\\\"\$(hostname)\\\"}\" '${MERIDIAN_URL}/hooks/stop' >/dev/null 2>&1"
fi

# Auto-start: prepend health-check + pixi start for localhost installs
if [[ $IS_LOCAL -eq 1 ]]; then
  START_CMD="curl -sf --max-time 3 '${MERIDIAN_URL}/health' >/dev/null 2>&1 || { [ -f \"\$HOME/pixi.toml\" ] && (cd \"\$HOME\" && nohup pixi run start >/dev/null 2>&1 &) && sleep 3; }; ${START_CMD}"
fi

# ---- Step 6: Write hooks to ~/.claude/settings.json -------------------------
SETTINGS_PATH="${HOME}/.claude/settings.json"
CLAUDE_DETECTED=0
if command -v claude &>/dev/null || [[ -f "$SETTINGS_PATH" ]]; then
  CLAUDE_DETECTED=1
fi

if [[ $CLAUDE_DETECTED -eq 1 ]]; then
  echo ""
  echo "Claude Code detected â€” writing hooks to $SETTINGS_PATH"
  mkdir -p "$(dirname "$SETTINGS_PATH")"

  EXISTING="{}"
  if [[ -f "$SETTINGS_PATH" ]]; then
    EXISTING=$(cat "$SETTINGS_PATH")
  fi

  UPDATED=$(echo "$EXISTING" | jq \
    --arg start "$START_CMD" \
    --arg stop "$STOP_CMD" \
    '.hooks.SessionStart = [{"matcher": "", "hooks": [{"type": "command", "command": $start}]}] |
     .hooks.Stop = [{"matcher": "", "hooks": [{"type": "command", "command": $stop}]}]')

  echo "$UPDATED" > "$SETTINGS_PATH"
  echo "  OK SessionStart + Stop hooks written"
fi

# ---- Step 7: Codex detection + config.toml -----------------------------------
CODEX_DETECTED=0
if command -v codex &>/dev/null || [[ -d "${HOME}/.codex" ]]; then
  CODEX_DETECTED=1
fi

if [[ $CODEX_DETECTED -eq 1 ]]; then
  echo ""
  echo "Codex detected â€” writing MCP config to ~/.codex/config.toml"
  mkdir -p "${HOME}/.codex"
  CODEX_CONFIG="${HOME}/.codex/config.toml"

  AUTH_LINE=""
  if [[ -n "$TOKEN" ]]; then
    AUTH_LINE=$'\napi_key = "'"$TOKEN"'"'
  fi

  # jq -Rs encodes the command as a JSON string, then strip outer quotes
  START_TOML=$(printf '%s' "$START_CMD" | jq -Rs '.[0:-1]')  # remove trailing newline via -s then strip
  STOP_TOML=$(printf '%s' "$STOP_CMD"  | jq -Rs '.[0:-1]')

  NEW_BLOCK="
[mcp_servers.meridian]
type = \"http\"
url = \"${MERIDIAN_URL}/mcp\"${AUTH_LINE}

[hooks]
session_start = ${START_TOML}
stop = ${STOP_TOML}"

  if [[ -f "$CODEX_CONFIG" ]]; then
    # Strip old meridian/hooks blocks and append new ones
    perl -0777 -i -pe 's/\[mcp_servers\.meridian\].*?(?=\n\[|\z)//gs; s/\[hooks\].*?(?=\n\[|\z)//gs' "$CODEX_CONFIG" 2>/dev/null || true
    printf '%s\n' "$NEW_BLOCK" >> "$CODEX_CONFIG"
  else
    printf '%s\n' "${NEW_BLOCK#$'\n'}" > "$CODEX_CONFIG"
  fi
  echo "  OK MCP config written to $CODEX_CONFIG"
fi

# ---- Step 8: Cursor detection + .cursor/mcp.json ----------------------------
CURSOR_DETECTED=0
if command -v cursor &>/dev/null || [[ -d "${HOME}/.cursor" ]]; then
  CURSOR_DETECTED=1
fi

if [[ $CURSOR_DETECTED -eq 1 ]]; then
  echo ""
  echo "Cursor detected â€” writing .cursor/mcp.json in current directory"
  mkdir -p ".cursor"
  if [[ -n "$TOKEN" ]]; then
    CURSOR_JSON=$(jq -n --arg url "${MERIDIAN_URL}/mcp" --arg tok "$TOKEN" \
      '{"mcpServers":{"meridian":{"url":$url,"headers":{"Authorization":("Bearer "+$tok)}}}}')
  else
    CURSOR_JSON=$(jq -n --arg url "${MERIDIAN_URL}/mcp" \
      '{"mcpServers":{"meridian":{"url":$url}}}')
  fi
  echo "$CURSOR_JSON" > ".cursor/mcp.json"
  echo "  OK .cursor/mcp.json written"
  echo "  Note: Cursor MCP tools available. Automatic session tracking requires Claude Code or Codex."
fi

# ---- Step 9: Smoke test -------------------------------------------------------
echo ""
echo "Testing hook..."
TEST_ARGS=(-s -X POST -H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  TEST_ARGS+=(-H "Authorization: Bearer $TOKEN")
fi
TEST_BODY="{\"cwd\":\"$PWD\",\"hostname\":\"$(hostname)\"}"
HTTP_STATUS=$(curl -o /dev/null -w "%{http_code}" --max-time 10 \
  "${TEST_ARGS[@]}" -d "$TEST_BODY" "$MERIDIAN_URL/hooks/session-start" 2>/dev/null || echo "0")
if [[ "$HTTP_STATUS" == "200" ]]; then
  echo "  OK hook responded successfully"
else
  echo "  WARNING: hook test returned $HTTP_STATUS (hooks still installed)"
fi

# ---- Done --------------------------------------------------------------------
echo ""
if [[ -n "$PROJECT_NAME" ]]; then
  echo "Done. Hooks installed for project '$PROJECT_NAME'."
else
  echo "Done. Hooks installed for project $PROJECT_ID."
fi
echo "Restart Claude Code to activate."
echo ""

