#!/usr/bin/env bash
# hooks.sh — Meridian session lifecycle hooks installer
#
# Usage: bash hooks.sh
# Detects Claude Code → writes SessionStart + Stop HTTP hooks to ~/.claude/settings.json
# Detects Codex      → writes MCP block + hooks to ~/.codex/config.toml
#
# Requirements: curl, jq (for Claude Code JSON editing)
set -euo pipefail

# ---- Defaults ----------------------------------------------------------------
DEFAULT_URL="http://localhost:7878"

# ---- Prompts -----------------------------------------------------------------
echo ""
echo "Meridian hook installer"
echo "-----------------------"
echo ""
read -rp "Meridian server URL [${DEFAULT_URL}]: " MERIDIAN_URL
MERIDIAN_URL="${MERIDIAN_URL:-$DEFAULT_URL}"
MERIDIAN_URL="${MERIDIAN_URL%/}"

read -rp "Project ID: " PROJECT_ID
if [[ -z "$PROJECT_ID" ]]; then
  echo "Error: project_id is required." >&2
  exit 1
fi

echo ""
echo "Server URL : $MERIDIAN_URL"
echo "Project ID : $PROJECT_ID"
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

# ---- Claude Code hooks -------------------------------------------------------
if [[ $CLAUDE_CODE_DETECTED -eq 1 ]]; then
  echo "Claude Code detected — writing hooks to ${SETTINGS_PATH}"
  mkdir -p "$(dirname "$SETTINGS_PATH")"

  # Start with existing settings or empty object
  if [[ -f "$SETTINGS_PATH" ]]; then
    SETTINGS=$(cat "$SETTINGS_PATH")
  else
    SETTINGS="{}"
  fi

  START_CMD="curl -s -X POST ${MERIDIAN_URL}/hooks/session-start -H 'Content-Type: application/json' -d '{\"project_id\":\"${PROJECT_ID}\"}' | jq -r '.hookSpecificOutput.additionalContext // empty'"
  STOP_CMD="curl -s -X POST ${MERIDIAN_URL}/hooks/stop -H 'Content-Type: application/json' -d '{\"project_id\":\"${PROJECT_ID}\"}'"

  # Use jq to merge hooks into settings without clobbering existing keys
  UPDATED=$(echo "$SETTINGS" | jq \
    --arg start "$START_CMD" \
    --arg stop "$STOP_CMD" \
    '.hooks.SessionStart = [{"type": "command", "command": $start}] |
     .hooks.Stop = [{"type": "command", "command": $stop}]')

  echo "$UPDATED" > "$SETTINGS_PATH"
  echo "  ✓ SessionStart + Stop hooks written to ${SETTINGS_PATH}"
fi

# ---- Codex hooks -------------------------------------------------------------
if [[ $CODEX_DETECTED -eq 1 ]]; then
  echo "Codex detected — writing config to ${CODEX_CONFIG}"
  mkdir -p "$(dirname "$CODEX_CONFIG")"

  # Write/append Meridian MCP block and hook config
  cat >> "$CODEX_CONFIG" <<TOML

# Meridian — added by hooks.sh
[mcp_servers.meridian]
type = "http"
url = "${MERIDIAN_URL}/mcp"

[hooks]
session_start = "curl -s -X POST ${MERIDIAN_URL}/hooks/session-start -H 'Content-Type: application/json' -d '{\"project_id\":\"${PROJECT_ID}\"}' | jq -r '.hookSpecificOutput.additionalContext // empty'"
stop = "curl -s -X POST ${MERIDIAN_URL}/hooks/stop -H 'Content-Type: application/json' -d '{\"project_id\":\"${PROJECT_ID}\"}'"
TOML
  echo "  ✓ Meridian MCP + hooks written to ${CODEX_CONFIG}"
fi

if [[ $CLAUDE_CODE_DETECTED -eq 0 && $CODEX_DETECTED -eq 0 ]]; then
  echo "Neither Claude Code nor Codex detected."
  echo "Add hooks manually using these commands:"
  echo ""
  echo "  Start: curl -s -X POST ${MERIDIAN_URL}/hooks/session-start \\"
  echo "         -H 'Content-Type: application/json' \\"
  echo "         -d '{\"project_id\":\"${PROJECT_ID}\"}'"
  echo ""
  echo "  Stop:  curl -s -X POST ${MERIDIAN_URL}/hooks/stop \\"
  echo "         -H 'Content-Type: application/json' \\"
  echo "         -d '{\"project_id\":\"${PROJECT_ID}\"}'"
fi

echo ""
echo "Done. Start a new Claude Code / Codex session to test."
