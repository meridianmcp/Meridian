#!/bin/sh
# Best-effort launcher for the shared tunnel health check implementation.
empty='{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)
if [ -z "$script_dir" ]; then
    printf '%s\n' "$empty"
    exit 0
fi

if command -v python3 >/dev/null 2>&1; then
    output=$(python3 "$script_dir/tunnel_health_check.py" 2>/dev/null)
elif command -v python >/dev/null 2>&1; then
    output=$(python "$script_dir/tunnel_health_check.py" 2>/dev/null)
else
    printf '%s\n' "$empty"
    exit 0
fi

if [ -n "$output" ]; then
    printf '%s\n' "$output"
else
    printf '%s\n' "$empty"
fi
exit 0
