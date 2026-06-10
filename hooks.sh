#!/usr/bin/env bash
# hooks.sh - Meridian session lifecycle hooks installer
#
# Usage:
#   curl -fsSL https://usemeridian.us/hooks.sh | bash
#   bash hooks.sh
#   bash hooks.sh --url http://localhost:7878 --project-id your-project-id
#
# Writes .meridian/config to the current directory and installs GENERIC hooks
# in ~/.claude/settings.json. Hooks read .meridian/config at fire time, so
# they follow the project regardless of which repo directory you're in.
#
# Requirements: curl, jq (for Claude Code JSON editing)
set -euo pipefail

# ---- Defaults ----------------------------------------------------------------
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
  PROJECTS="[]"
  if [[ -n "$TOKEN" ]]; then
    PROJECTS=$(curl -sf --max-time 10 \
      -H "Authorization: Bearer $TOKEN" \
      "$MERIDIAN_URL/auth/me" 2>/dev/null | jq -r '.projects // []' 2>/dev/null || echo "[]")
  fi
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

# ---- Step 4: Generate permanent token (if using short-lived install token) ---
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

# ---- Step 5: Write .meridian/config ------------------------------------------
CONFIG_DIR="$(pwd)/.meridian"
CONFIG_FILE="$CONFIG_DIR/config"

if [[ -f "$CONFIG_FILE" ]]; then
  read -rp ".meridian/config already exists. Update? [y/N]: " OVERWRITE
  if [[ ! "$OVERWRITE" =~ ^[Yy] ]]; then
    echo "Skipping config write."
  else
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG_FILE" <<EOF
url=$MERIDIAN_URL
token=$TOKEN
project_id=$PROJECT_ID
EOF
    echo "  Config written to $CONFIG_FILE"
  fi
else
  mkdir -p "$CONFIG_DIR"
  cat > "$CONFIG_FILE" <<EOF
url=$MERIDIAN_URL
token=$TOKEN
project_id=$PROJECT_ID
EOF
  echo "  Config written to $CONFIG_FILE"
fi

# ---- Add .meridian/ to .gitignore --------------------------------------------
GITIGNORE="$(pwd)/.gitignore"
if [[ -f "$GITIGNORE" ]]; then
  if ! grep -qF ".meridian/" "$GITIGNORE"; then
    printf '\n.meridian/\n' >> "$GITIGNORE"
    echo "  Added .meridian/ to .gitignore"
  fi
else
  echo ".meridian/" > "$GITIGNORE"
  echo "  Created .gitignore with .meridian/"
fi

# ---- Step 6: Write generic hooks to ~/.claude/settings.json ------------------
SETTINGS_PATH="${HOME}/.claude/settings.json"
CLAUDE_DETECTED=0
if command -v claude &>/dev/null || [[ -f "$SETTINGS_PATH" ]]; then
  CLAUDE_DETECTED=1
fi

START_CMD='if [ -f "$(pwd)/.meridian/config" ]; then . "$(pwd)/.meridian/config"; curl -s -X POST -H "Authorization: Bearer $token" -H "Content-Type: application/json" -d "{\"project_id\":\"$project_id\",\"cwd\":\"$PWD\"}" "$url/hooks/session-start" | jq -r '"'"'.hookSpecificOutput.additionalContext // empty'"'"' 2>/dev/null; fi'
STOP_CMD='if [ -f "$(pwd)/.meridian/config" ]; then . "$(pwd)/.meridian/config"; curl -s -X POST -H "Authorization: Bearer $token" -H "Content-Type: application/json" -d "{\"project_id\":\"$project_id\"}" "$url/hooks/stop" > /dev/null; fi'

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

# ---- Step 7: Smoke test ------------------------------------------------------
echo ""
echo "Testing hook..."
TEST_ARGS=(-s -X POST -H "Content-Type: application/json")
if [[ -n "$TOKEN" ]]; then
  TEST_ARGS+=(-H "Authorization: Bearer $TOKEN")
fi
TEST_BODY="{\"project_id\":\"$PROJECT_ID\",\"cwd\":\"$PWD\"}"
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
echo "Start a new Claude Code session to activate."
echo ""
