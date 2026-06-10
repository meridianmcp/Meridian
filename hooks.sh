#!/usr/bin/env bash
# hooks.sh - Meridian session lifecycle hooks installer
#
# Usage:
#   bash hooks.sh
#   bash hooks.sh --url http://localhost:7878 --project-id your-project-id
#   bash hooks.sh --url https://usemeridian.us --project-id your-project-id --token sk_meridian_...
# Detects Claude Code -> writes SessionStart + Stop HTTP hooks to ~/.claude/settings.json
# Detects Codex      -> writes MCP block + hooks to ~/.codex/config.toml
#
# Requirements: curl, jq (for Claude Code JSON editing)
set -euo pipefail

# ---- Defaults ----------------------------------------------------------------
DEFAULT_URL="http://localhost:7878"
MERIDIAN_URL=""
PROJECT_ID=""
TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      MERIDIAN_URL="${2:-}"
      shift 2
      ;;
    --project-id)
      PROJECT_ID="${2:-}"
      shift 2
      ;;
    --token)
      TOKEN="${2:-}"
      shift 2
      ;;
    -h|--help)
      echo "Usage: bash hooks.sh [--url URL] [--project-id ID] [--token TOKEN]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

build_headers() {
  local headers="-H 'Content-Type: application/json'"
  if [[ -n "$TOKEN" ]]; then
    headers="-H 'Authorization: Bearer ${TOKEN}' ${headers}"
  fi
  printf '%s' "$headers"
}

toml_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

# ---- Prompts -----------------------------------------------------------------
echo ""
echo "Meridian hook installer"
echo "-----------------------"
echo ""

if [[ -z "$MERIDIAN_URL" ]]; then
  read -rp "Meridian server URL [${DEFAULT_URL}]: " MERIDIAN_URL
fi
MERIDIAN_URL="${MERIDIAN_URL:-$DEFAULT_URL}"
MERIDIAN_URL="${MERIDIAN_URL%/}"

if [[ -z "$PROJECT_ID" ]]; then
  read -rp "Project ID: " PROJECT_ID
fi
if [[ -z "$PROJECT_ID" ]]; then
  echo "Error: project_id is required." >&2
  exit 1
fi

echo ""
echo "Server URL : $MERIDIAN_URL"
echo "Project ID : $PROJECT_ID"
if [[ -n "$TOKEN" ]]; then
  echo "API Token  : set"
else
  echo "API Token  : not set"
fi
echo ""

# ---- Claude Code detection --------------------------------------------------
SETTINGS_PATH="${HOME}/.claude/settings.json"
CLAUDE_CODE_DETECTED=0
if command -v claude &>/dev/null || [[ -f "${HOME}/.claude/settings.json" ]]; then
  CLAUDE_CODE_DETECTED=1
fi

# ---- Codex detection --------------------------------------------------------
CODEX_CONFIG="${HOME}/.codex/config.toml"
CODEX_DETECTED=0
if command -v codex &>/dev/null || [[ -f "$CODEX_CONFIG" ]]; then
  CODEX_DETECTED=1
fi

HEADERS="$(build_headers)"

# ---- Claude Code hooks ------------------------------------------------------
if [[ $CLAUDE_CODE_DETECTED -eq 1 ]]; then
  echo "Claude Code detected - writing hooks to ${SETTINGS_PATH}"
  mkdir -p "$(dirname "$SETTINGS_PATH")"

  # Start with existing settings or empty object
  if [[ -f "$SETTINGS_PATH" ]]; then
    SETTINGS=$(cat "$SETTINGS_PATH")
  else
    SETTINGS="{}"
  fi

  START_CMD="curl -s -X POST ${HEADERS} -d '{\"project_id\":\"${PROJECT_ID}\",\"cwd\":\"'\"\$PWD\"'\"}' ${MERIDIAN_URL}/hooks/session-start | jq -r '.hookSpecificOutput.additionalContext // empty'"
  STOP_CMD="curl -s -X POST ${HEADERS} -d '{\"project_id\":\"${PROJECT_ID}\"}' ${MERIDIAN_URL}/hooks/stop"

  # New format required by Claude Code v2.1.169+: matcher + hooks array wrapper
  UPDATED=$(echo "$SETTINGS" | jq \
    --arg start "$START_CMD" \
    --arg stop "$STOP_CMD" \
    '.hooks.SessionStart = [{"matcher": "", "hooks": [{"type": "command", "command": $start}]}] |
     .hooks.Stop = [{"matcher": "", "hooks": [{"type": "command", "command": $stop}]}]')

  echo "$UPDATED" > "$SETTINGS_PATH"
  echo "  OK SessionStart + Stop hooks written to ${SETTINGS_PATH}"
fi

# ---- Codex hooks ------------------------------------------------------------
if [[ $CODEX_DETECTED -eq 1 ]]; then
  echo "Codex detected - writing config to ${CODEX_CONFIG}"
  mkdir -p "$(dirname "$CODEX_CONFIG")"

  START_CMD="curl -s -X POST ${HEADERS} -d '{\"project_id\":\"${PROJECT_ID}\",\"cwd\":\"'\"\$PWD\"'\"}' ${MERIDIAN_URL}/hooks/session-start | jq -r '.hookSpecificOutput.additionalContext // empty'"
  STOP_CMD="curl -s -X POST ${HEADERS} -d '{\"project_id\":\"${PROJECT_ID}\"}' ${MERIDIAN_URL}/hooks/stop"
  START_CMD_TOML="$(toml_quote "$START_CMD")"
  STOP_CMD_TOML="$(toml_quote "$STOP_CMD")"

  # Write/append Meridian MCP block and hook config
  cat >> "$CODEX_CONFIG" <<TOML

# Meridian - added by hooks.sh
[mcp_servers.meridian]
type = "http"
url = "${MERIDIAN_URL}/mcp"

[hooks]
session_start = ${START_CMD_TOML}
stop = ${STOP_CMD_TOML}
TOML
  echo "  OK Meridian MCP + hooks written to ${CODEX_CONFIG}"
fi

if [[ $CLAUDE_CODE_DETECTED -eq 0 && $CODEX_DETECTED -eq 0 ]]; then
  echo "Neither Claude Code nor Codex detected."
  echo "Add hooks manually using these commands:"
  echo ""
  echo "  SessionStart command:"
  echo "    curl -s -X POST ${HEADERS} -d '{\"project_id\":\"${PROJECT_ID}\"}' ${MERIDIAN_URL}/hooks/session-start | jq -r '.hookSpecificOutput.additionalContext // empty'"
  echo ""
  echo "  Stop command:"
  echo "    curl -s -X POST ${HEADERS} -d '{\"project_id\":\"${PROJECT_ID}\"}' ${MERIDIAN_URL}/hooks/stop"
fi

echo ""
echo "Done. Start a new Claude Code / Codex session to test."
