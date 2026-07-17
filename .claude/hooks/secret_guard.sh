#!/usr/bin/env bash
# 14491654 -- PreToolUse secret-file guard (fail-closed on sensitive paths).
# Cross-platform partner of secret_guard.ps1; the .sh version covers Linux/macOS
# executors and is what the regression test (test_secret_redaction.py) exercises.
#
# Incident: Claude displayed raw .env contents (Stripe live key,
# MERIDIAN_ENCRYPTION_KEY, admin password, DB connection strings) via Read/Bash/Grep
# calls -- fully unredacted, no existing guard caught it.
#
# This hook fires on Read, Bash, Grep, and Glob tool calls and BLOCKS (exit 2,
# fail-closed) when the target file path matches a known-sensitive filename
# pattern (.env, *.pem, *.key, id_rsa*, secrets.*, etc.).
#
# Implementation rationale: Claude Code's PostToolUse hooks receive only the REQUEST
# (tool_name + tool_input), not the response content, so they cannot intercept and
# rewrite tool OUTPUT before it reaches model context. PreToolUse CAN block the call
# entirely (exit 2) before any file content is read -- correct fail-closed posture
# for this threat class.
#
# Bash commands: we inspect the command string for common env-dump patterns
# (cat .env, printenv, env) and flag those. Coverage is best-effort; Read/Grep/Glob
# are authoritative.
#
# Tolerant JSON extraction (no jq dependency) via grep + sed, same pattern as
# hitl_guard.sh and worktree_guard.sh. Fails OPEN on any parse error.
# NOT hooks.sh (the token-rotation installer).
set -uo pipefail

# Read the full JSON payload from stdin.
payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

# Extract tool_name tolerantly; fail open if absent.
tool="$(printf '%s' "$payload" | grep -oE '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
[ -z "$tool" ] && exit 0

# Only intercept file-reading tools and Bash.
case "$tool" in
    Read|Bash|Grep|Glob) ;;
    *) exit 0 ;;
esac

# Sensitive basename patterns for fnmatch-style matching (case-insensitive via tr).
# Must stay in sync with meridian/secret_redaction.py _SENSITIVE_BASENAME_PATTERNS
# and secret_guard.ps1 $SensitivePatterns.
is_sensitive_path() {
    local path="$1"
    [ -z "$path" ] && return 1
    # Extract basename (last path component after final / or \).
    local base
    base="$(printf '%s' "$path" | tr '\\' '/' | sed 's|.*[/]||')"
    [ -z "$base" ] && return 1
    # Lowercase for case-insensitive matching.
    local lower
    lower="$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')"
    case "$lower" in
        # dotenv files
        .env|.env.*|*.env) return 0 ;;
        # Key/cert files
        *.key|*.pem|*.p12|*.pfx|*.jks|*.keystore) return 0 ;;
        *.crt|*.cer|*.der) return 0 ;;
        # SSH keys
        id_rsa|id_rsa.*|id_dsa|id_dsa.*) return 0 ;;
        id_ecdsa|id_ecdsa.*|id_ed25519|id_ed25519.*) return 0 ;;
        # Secret/credential patterns
        *secret*|*secrets*|*credential*|*credentials*) return 0 ;;
        *password*|*passwd*) return 0 ;;
        # Vault/terraform
        *.vault|vault.yaml|vault.yml) return 0 ;;
        *.tfvars|terraform.tfstate|terraform.tfstate.backup) return 0 ;;
        # Auth files
        .netrc|netrc|*.htpasswd) return 0 ;;
        # Key/token patterns
        *apikey*|*api_key*|*auth_key*|*access_key*|*private_key*) return 0 ;;
        *_token|*_token.*) return 0 ;;
        token|token.*) return 0 ;;
    esac
    return 1
}

is_sensitive_bash_cmd() {
    local cmd="$1"
    [ -z "$cmd" ] && return 1
    # Patterns that indicate environment variable / credential file dumping.
    case "$cmd" in
        *printenv*) return 0 ;;
        *cat\ *\.env*|*cat\ *\.env) return 0 ;;
        *cat*\.key*|*cat*id_rsa*|*cat*\.pem*) return 0 ;;
    esac
    # 'env' as a standalone command or with flags (but not 'environ' in code, etc.)
    if printf '%s' "$cmd" | grep -qE '^\s*env(\s|$)'; then
        return 0
    fi
    return 1
}

# --- Read ---
if [ "$tool" = "Read" ]; then
    file_path="$(printf '%s' "$payload" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
    if [ -n "$file_path" ] && is_sensitive_path "$file_path"; then
        echo "Meridian secret guard (14491654): BLOCKED Read of sensitive file '$file_path'. Reading .env, key, pem, and other credential files exposes secrets in model context. If you genuinely need this value, use a secrets manager or ask the human operator to provide only the specific value needed (not the whole file)." >&2
        exit 2
    fi
fi

# --- Grep / Glob ---
if [ "$tool" = "Grep" ] || [ "$tool" = "Glob" ]; then
    # Check the 'path' field (used by both Grep and Glob).
    file_path="$(printf '%s' "$payload" | grep -oE '"path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
    if [ -n "$file_path" ] && is_sensitive_path "$file_path"; then
        echo "Meridian secret guard (14491654): BLOCKED $tool targeting sensitive path '$file_path'. Searching inside credential files exposes secrets in model context." >&2
        exit 2
    fi
fi

# --- Bash ---
if [ "$tool" = "Bash" ]; then
    cmd="$(printf '%s' "$payload" | grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
    if [ -n "$cmd" ] && is_sensitive_bash_cmd "$cmd"; then
        # Truncate for display
        display_cmd="$(printf '%s' "$cmd" | head -c 80)"
        echo "Meridian secret guard (14491654): BLOCKED Bash command that appears to dump environment variables or read credential files: '${display_cmd}...'. Use only the specific env var you need (e.g. echo \$SOME_SAFE_VAR) rather than printing all environment variables or cat-ing credential files." >&2
        exit 2
    fi
fi

exit 0
