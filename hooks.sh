#!/usr/bin/env bash
# hooks.sh - Meridian session lifecycle hooks installer
#
# Usage:
#   curl -fsSL https://usemeridian.us/hooks.sh | bash
#   bash hooks.sh
#   bash hooks.sh --url http://localhost:7878 --project-id your-project-id
#
# Installs Claude Code, Codex, and Cursor integrations. Credentials are embedded
# directly in hook commands — no per-repo config file required.
#
# Requirements: curl, jq
set -euo pipefail

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
echo "Meridian hook installer"
echo "-----------------------"
echo ""

# ---- Step 1: URL -------------------------------------------------------------
if [[ -z "$MERIDIAN_URL" ]]; then
  read -rp "Meridian server URL [$DEFAULT_URL]: " MERIDIAN_URL
fi
MERIDIAN_URL="${MERIDIAN_URL:-$DEFAULT_URL}"
MERIDIAN_URL="${MERIDIAN_URL%/}"

if [[ ! "$MERIDIAN_URL" =~ ^https?:// ]]; then
  echo "Error: URL must start with https:// or http://" >&2
  exit 1
fi

echo "Checking $MERIDIAN_URL ..."
if ! curl -sf --max-time 5 "$MERIDIAN_URL/health" > /dev/null; then
  echo "Error: Cannot reach $MERIDIAN_URL/health — is the server running?" >&2
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
  echo "Self-hosted / localhost detected — skipping auth."
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
    read -rp "Paste the token shown in your browser: " TOKEN
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
    echo "Error: Token validation failed — is the token correct?" >&2
    exit 1
  fi
  EMAIL=$(echo "$ME" | jq -r '.email // empty' 2>/dev/null || echo "")
  echo "  Authenticated as: $EMAIL"
fi

# ---- Step 3: Project selection -----------------------------------------------
PROJECT_NAME=""
if [[ -z "$PROJECT_ID" ]]; then
  ME_DATA="{}"
  if [[ -n "$TOKEN" ]]; then
    ME_DATA=$(curl -sf --max-time 10 -H "Authorization: Bearer $TOKEN" \
      "$MERIDIAN_URL/auth/me" 2>/dev/null || echo "{}")
  elif [[ $IS_LOCAL -eq 1 ]]; then
    ME_DATA=$(curl -sf --max-time 10 "$MERIDIAN_URL/auth/me" 2>/dev/null || echo "{}")
  fi
  PROJECTS=$(echo "$ME_DATA" | jq '.projects // []' 2>/dev/null || echo "[]")
  COUNT=$(echo "$PROJECTS" | jq 'length' 2>/dev/null || echo "0")
  if [[ "$COUNT" -gt 0 ]]; then
    echo ""
    echo "Your projects:"
    echo "$PROJECTS" | jq -r 'to_entries[] | "  [\(.key + 1)] \(.value.name)  (\(.value.id | .[0:8])...)"' 2>/dev/null
    echo ""
    read -rp "Select project number [1-$COUNT]: " CHOICE
    if [[ "$CHOICE" -lt 1 ]] || [[ "$CHOICE" -gt "$COUNT" ]]; then
      echo "Error: invalid selection." >&2
      exit 1
    fi
    IDX=$((CHOICE - 1))
    PROJECT_ID=$(echo "$PROJECTS" | jq -r ".[$IDX].id")
    PROJECT_NAME=$(echo "$PROJECTS" | jq -r ".[$IDX].name")
  else
    read -rp "Project ID: " PROJECT_ID
  fi
fi
if [[ -z "$PROJECT_ID" ]]; then
  echo "Error: project_id is required." >&2
  exit 1
fi

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
  START_CMD="curl -s -X POST -H 'Authorization: Bearer ${TOKEN}' -H 'Content-Type: application/json' -d \"{\\\"project_id\\\":\\\"${PROJECT_ID}\\\",\\\"cwd\\\":\\\"\$PWD\\\",\\\"hostname\\\":\\\"\$(hostname)\\\"}\" '${MERIDIAN_URL}/hooks/session-start' | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null"
  STOP_CMD="curl -s -X POST -H 'Authorization: Bearer ${TOKEN}' -H 'Content-Type: application/json' -d \"{\\\"project_id\\\":\\\"${PROJECT_ID}\\\",\\\"hostname\\\":\\\"\$(hostname)\\\"}\" '${MERIDIAN_URL}/hooks/stop' >/dev/null 2>&1"
else
  START_CMD="curl -s -X POST -H 'Content-Type: application/json' -d \"{\\\"project_id\\\":\\\"${PROJECT_ID}\\\",\\\"cwd\\\":\\\"\$PWD\\\",\\\"hostname\\\":\\\"\$(hostname)\\\"}\" '${MERIDIAN_URL}/hooks/session-start' | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null"
  STOP_CMD="curl -s -X POST -H 'Content-Type: application/json' -d \"{\\\"project_id\\\":\\\"${PROJECT_ID}\\\",\\\"hostname\\\":\\\"\$(hostname)\\\"}\" '${MERIDIAN_URL}/hooks/stop' >/dev/null 2>&1"
fi

# ---- Step 6: Write hooks to ~/.claude/settings.json -------------------------
SETTINGS_PATH="${HOME}/.claude/settings.json"
CLAUDE_DETECTED=0
if command -v claude &>/dev/null || [[ -f "$SETTINGS_PATH" ]]; then
  CLAUDE_DETECTED=1
fi

if [[ $CLAUDE_DETECTED -eq 1 ]]; then
  echo ""
  echo "Claude Code detected — writing hooks to $SETTINGS_PATH"
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
  echo "Codex detected — writing MCP config to ~/.codex/config.toml"
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
  echo "Cursor detected — writing .cursor/mcp.json in current directory"
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
TEST_BODY="{\"project_id\":\"$PROJECT_ID\",\"cwd\":\"$PWD\",\"hostname\":\"$(hostname)\"}"
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
