#!/usr/bin/env bash
# 23f21820 -- PreToolUse package-install verification guard.
# Cross-platform partner of pkg_install_guard.ps1 (covers Linux/macOS executors).
#
# Fires on Bash tool calls. Inspects the command for pip/npm/uvx install patterns.
# Allowlisted packages pass immediately. Others trigger a registry check via the
# local Meridian /pkg-guard/check endpoint.
#
# Gate behaviour:
#   allow   -- package is allowlisted or verified -> exit 0
#   warn    -- suspicious signal or network failure -> exit 1 (advisory; not a hard block)
#
# Fails OPEN (exit 0) on any parse/network/logic error.
# NOT hooks.sh (the token-rotation installer).
set -uo pipefail

MERIDIAN_URL="${MERIDIAN_URL:-http://localhost:7878}"

# Read the JSON payload from stdin.
payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

# Extract tool_name; fail open if absent.
tool="$(printf '%s' "$payload" | grep -oE '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true)"
[ "$tool" != "Bash" ] && exit 0

# Extract the command string (tolerant extraction -- no jq dependency).
cmd="$(printf '%s' "$payload" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true)"
[ -z "$cmd" ] && exit 0

# Quick pre-filter: skip if no install keyword + package manager present.
printf '%s' "$cmd" | grep -qiE '\binstall\b|\badd\b' || exit 0
printf '%s' "$cmd" | grep -qiE '\bpip[23]?\b|\bpython .* pip\b|\buv pip\b|\bnpm\b|\byarn\b|\bpnpm\b|\bbun\b|\buvx\b' || exit 0

# Escape the command for JSON.
cmd_json="$(printf '%s' "$cmd" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || true)"
[ -z "$cmd_json" ] && exit 0

body="{\"command\": $cmd_json}"

# Call the Meridian endpoint.
resp="$(curl -sf --max-time 12 -X POST "$MERIDIAN_URL/pkg-guard/check" \
    -H "Content-Type: application/json" \
    -d "$body" 2>/dev/null || true)"

if [ -z "$resp" ]; then
    echo "Meridian pkg guard (23f21820): registry check unavailable (Meridian server not reachable). Failing open." >&2
    exit 0
fi

action="$(printf '%s' "$resp" | grep -oE '"action"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true)"
message="$(printf '%s' "$resp" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("message",""))' 2>/dev/null || true)"

if [ "$action" = "warn" ]; then
    echo "$message" >&2
    exit 1
fi

exit 0
