#!/usr/bin/env bash
# 14491654 -- PreToolUse secret-file guard (fail-closed on sensitive paths).
# Cross-platform partner of secret_guard.ps1; the .sh version covers Linux/macOS
# executors and is what the regression test (test_secret_redaction.py) exercises.
#
# Incident: Claude displayed raw .env contents (Stripe live key,
# MERIDIAN_ENCRYPTION_KEY, admin password, DB connection strings) via Read/Bash/Grep
# calls -- fully unredacted, no existing guard caught it.
#
# This hook fires on Read, Bash, PowerShell, Grep, and Glob tool calls and BLOCKS (exit 2,
# fail-closed) when the target file path matches a known-sensitive filename
# pattern (.env, *.pem, *.key, id_rsa*, secrets.*, meridian.toml, etc.).
#
# Implementation rationale: Claude Code's PostToolUse hooks receive only the REQUEST
# (tool_name + tool_input), not the response content, so they cannot intercept and
# rewrite tool OUTPUT before it reaches model context. PreToolUse CAN block the call
# entirely (exit 2) before any file content is read -- correct fail-closed posture
# for this threat class.
#
# 55d48d69 fix round 1 (mirrors secret_guard.ps1): source/script/test files and
# credential-file templates (*.example, *.sample, ...) are never sensitive, whatever
# their name says; meridian.toml is; Grep's 'glob' field is checked; shell checks
# block only a bare environment dump (printenv / env / set / export / declare -p with
# nothing else) or a reader verb (cat, head, grep, ...) naming a credential FILE.
# The owner kill switch (MERIDIAN_GUARD=off|advisory, guard.off / guard.advisory,
# MERIDIAN_GUARD_DISABLE=secret_guard) covers this hook too.
#
# Tolerant JSON extraction (no jq dependency) via grep + sed, same pattern as
# hitl_guard.sh and worktree_guard.sh. Fails OPEN on any parse error.
# NOT hooks.sh (the token-rotation installer).
set -uo pipefail

# Read the full JSON payload from stdin.
payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

# JSON string field (first occurrence), escaped quotes/backslashes included.
# JSON string field (first occurrence) with \" and \\ un-escaped -- bash builtins only
# (no fork per field: Git Bash forks cost seconds on a loaded Windows host).
json_field() {
    local re="\"$1\"[[:space:]]*:[[:space:]]*\"(([^\"\\\\]|\\\\.)*)\"" v
    if [[ $payload =~ $re ]]; then
        v="${BASH_REMATCH[1]}"
        v="${v//\\\"/\"}"; v="${v//\\\\/\\}"
        printf '%s' "$v"
    fi
}

# Extract tool_name tolerantly; fail open if absent.
tool="$(json_field tool_name)"
[ -z "$tool" ] && exit 0

# All occurrences of a JSON string field across the whole payload (used for
# MultiEdit's "edits":[{"new_string": "..."}, ...] array -- json_field above only
# returns the first occurrence). Same tolerant, no-jq-dependency, bash-builtins-only
# style as json_field.
json_field_all() {
    local re="\"$1\"[[:space:]]*:[[:space:]]*\"(([^\"\\\\]|\\\\.)*)\"" rest="$payload" v out=""
    while [[ $rest =~ $re ]]; do
        v="${BASH_REMATCH[1]}"
        v="${v//\\\"/\"}"; v="${v//\\\\/\\}"
        out="${out}${v}"$'\n'
        rest="${rest#*"${BASH_REMATCH[0]}"}"
    done
    printf '%s' "$out"
}

# Only intercept file-reading tools, the shell tools (Bash, PowerShell), and the
# file-writing tools (Write, Edit, MultiEdit -- guards against a secret VALUE
# being written into a credential file such as meridian.toml or .env).
case "$tool" in
    Read|Bash|PowerShell|Grep|Glob|Write|Edit|MultiEdit) ;;
    *) exit 0 ;;
esac

# --- owner kill switch (same inputs as the Meridian guard's G0) ------------------
# Sets GUARD_MODE (off|advisory|enforce) -- bash builtins only, no subshell.
guard_mode() {
    local raw="${MERIDIAN_GUARD:-}" dm="${MERIDIAN_GUARD_DEFAULT_MODE:-}" dis tok gd
    raw="${raw//[[:space:]]/}"; dm="${dm//[[:space:]]/}"
    shopt -s nocasematch
    GUARD_MODE=enforce
    if [[ $raw == off ]]; then GUARD_MODE=off; shopt -u nocasematch; return; fi
    if [ -n "$raw" ] && [[ $raw != enforce ]]; then GUARD_MODE=advisory; fi
    if [ -z "$raw" ] && [[ $dm == advisory ]]; then GUARD_MODE=advisory; fi
    dis="${MERIDIAN_GUARD_DISABLE:-}"; dis="${dis//[,;]/ }"
    for tok in $dis; do
        if [[ $tok == "$1" ]]; then GUARD_MODE=off; shopt -u nocasematch; return; fi
    done
    shopt -u nocasematch
    gd=''
    if [ -n "${LOCALAPPDATA:-}" ]; then gd="$LOCALAPPDATA/meridian/guard"
    elif [ -n "${USERPROFILE:-}" ]; then gd="$USERPROFILE/AppData/Local/meridian/guard"
    elif [ -n "${HOME:-}" ]; then gd="${XDG_STATE_HOME:-$HOME/.local/state}/meridian/guard"
    fi
    if [ -n "$gd" ]; then
        if [ -f "$gd/guard.off" ]; then GUARD_MODE=off; elif [ -f "$gd/guard.advisory" ]; then GUARD_MODE=advisory; fi
    fi
}
guard_mode secret_guard
[ "$GUARD_MODE" = "off" ] && exit 0

json_escape() {
    local s=$1
    s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
    printf '%s' "$s"
}

stop_call() {
    if [ "$GUARD_MODE" = "advisory" ]; then
        printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}' "$(json_escape "[advisory, not blocked] $1")"
        exit 0
    fi
    echo "$1" >&2
    exit 2
}

# every case/[[ ]] match below is case-insensitive (no fork to lowercase)
shopt -s nocasematch
basename_of() { local p="${1//\\//}"; printf '%s' "${p##*/}"; }

# Sensitive basename patterns for fnmatch-style matching (case-insensitive via tr).
# Must stay in sync with meridian/secret_redaction.py _SENSITIVE_BASENAME_PATTERNS
# (+ _SAFE_SOURCE_EXTENSIONS / _TEMPLATE_SUFFIXES) and secret_guard.ps1.
is_sensitive_path() {
    local path="$1"
    [ -z "$path" ] && return 1
    # Extract basename (last path component after final / or \).
    local p="${path//\\//}"
    local lower_base="${p##*/}"
    [ -z "$lower_base" ] && return 1
    # Source / script / test code and credential-file templates are never sensitive.
    case "$lower_base" in
        ?*.py|?*.pyi|?*.pyx|?*.ipynb|?*.ps1|?*.psm1|?*.psd1|?*.sh|?*.bash|?*.zsh|?*.fish|?*.bat|?*.cmd) return 1 ;;
        ?*.js|?*.mjs|?*.cjs|?*.ts|?*.tsx|?*.jsx|?*.mts|?*.cts|?*.go|?*.rs|?*.java|?*.kt|?*.kts|?*.scala) return 1 ;;
        ?*.rb|?*.php|?*.cs|?*.fs|?*.c|?*.h|?*.cc|?*.cpp|?*.cxx|?*.hpp|?*.swift|?*.m|?*.lua|?*.r|?*.jl) return 1 ;;
        ?*.dart|?*.ex|?*.exs|?*.erl|?*.hs|?*.ml|?*.clj|?*.groovy|?*.pl|?*.pm|?*.sql|?*.vue|?*.svelte|?*.awk) return 1 ;;
        ?*.example|?*.sample|?*.template|?*.tmpl|?*.dist) return 1 ;;
    esac
    case "$lower_base" in
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
        # live Meridian credentials (AGENTS.md hard rules)
        meridian.toml) return 0 ;;
    esac
    return 1
}

# A credential FILE word inside a shell command (mirrors $CredTok in secret_guard.ps1).
is_cred_word() {
    local w b
    w="$1"
    # strip quotes, parens and a leading redirect
    w="${w#<}"; w="${w//\"/}"; w="${w//\'/}"; w="${w//(/}"; w="${w//)/}"
    b="${w//\\//}"; b="${b##*/}"
    case "$b" in
        .env.example|.env.sample|.env.template|.env.tmpl|.env.dist|.env.example.*|.env.sample.*) return 1 ;;
        .env|.env.*) return 0 ;;
        ?*.pem|?*.key|?*.p12|?*.pfx|?*.jks|?*.keystore|?*.tfvars) return 0 ;;
        id_rsa|id_dsa|id_ecdsa|id_ed25519|.netrc|netrc|.htpasswd) return 0 ;;
        *secret.yaml|*secret.yml|*secret.json|*secret.toml|*secret.env) return 0 ;;
        *secrets.yaml|*secrets.yml|*secrets.json|*secrets.toml|*secrets.env) return 0 ;;
        *credential.json|*credential.yaml|*credential.yml|*credential.toml) return 0 ;;
        *credentials.json|*credentials.yaml|*credentials.yml|*credentials.toml) return 0 ;;
        meridian.toml) return 0 ;;
    esac
    return 1
}

# Split a command into statements/pipeline stages on ; & | (not quote-aware: best effort).
statements() { local s="${1//[;&|]/$'\n'}"; printf '%s\n' "$s"; }

is_sensitive_bash_cmd() {
    local cmd="$1" st verb w first opened
    [ -z "$cmd" ] && return 1
    while IFS= read -r st; do
        # shellcheck disable=SC2086
        set -f; set -- $st; set +f
        [ $# -eq 0 ] && continue
        verb="$1"
        [[ $verb == sudo ]] && { shift; [ $# -eq 0 ] && continue; verb="$1"; }
        # bare dumps: printenv / env / set / export [-p] / declare -p|-x with nothing else
        case "$verb" in
            printenv|env|set) [ $# -eq 1 ] && return 0 ;;
            export) { [ $# -eq 1 ] || { [ $# -eq 2 ] && [ "$2" = "-p" ]; }; } && return 0 ;;
            declare|typeset) [ $# -eq 2 ] && case "$2" in -p|-x) return 0 ;; esac ;;
            compgen) [ $# -eq 2 ] && [ "$2" = "-e" ] && return 0 ;;
        esac
        first=1
        for w in "$@"; do
            if [ "$first" = 1 ]; then first=0; continue; fi
            case "$verb" in
                cat|tac|head|tail|less|more|bat|nl|strings|xxd|od|hexdump|base64|grep|egrep|fgrep|rg|ag|awk|gawk|sed|cut|sort|uniq|type|jq|yq)
                    is_cred_word "$w" && return 0 ;;
            esac
            # input redirection from a credential file
            case "$w" in
                "<"?*) is_cred_word "$w" && return 0 ;;
            esac
        done
        # "< file" with a separate word
        case " $st " in
            *" < "*) w="${st##*< }"; w="${w%% *}"; is_cred_word "$w" && return 0 ;;
        esac
    done <<EOF_STATEMENTS
$(statements "$cmd")
EOF_STATEMENTS
    # interpreters opening a credential file directly: open('...')
    local rest="$cmd" re="open\\([[:space:]]*['\"]([^'\"]*)['\"]"
    while [[ $rest =~ $re ]]; do
        is_cred_word "${BASH_REMATCH[1]}" && return 0
        rest="${rest#*"${BASH_REMATCH[0]}"}"
    done
    return 1
}

# 14491654 write-path follow-up -- the write-side bypass: Read/Grep of a credential file is blocked by
# is_sensitive_path above, but nothing stopped Write/Edit/MultiEdit from putting a
# fresh secret VALUE into one (meridian.toml was not protected at all on this side).
# Only the VALUE is checked here -- ordinary content (e.g. project_id = "...") in a
# sensitive file must still be writable, matching the "read meridian.toml [project]
# key only" convention documented elsewhere. Mirrors the SECRET_PATTERNS /
# dotenv-credential shapes in meridian/secret_redaction.py and secret_guard.ps1's
# Test-SecretValue, kept independent (no python dependency) so the hook stays a
# pure shell script.
is_secret_value() {
    local t="$1"
    [ -z "$t" ] && return 1
    # Known credential-shaped VALUE prefixes -- case-sensitive, like secret_redaction.py
    # (a lowercase "akia..." is not a real AWS key). nocasematch is on globally in this
    # script, so turn it off just for this check.
    shopt -u nocasematch
    local re_tok='AKIA[0-9A-Z]{16}|sk_live_[A-Za-z0-9]{24,}|sk_meridian_[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|xox[baprs]-[A-Za-z0-9][A-Za-z0-9_-]{5,}|sk-[A-Za-z0-9-]{20,}|-----BEGIN( [A-Z ]+)?PRIVATE KEY-----'
    if [[ $t =~ $re_tok ]]; then shopt -s nocasematch; return 0; fi
    shopt -s nocasematch
    # dotenv-style KEY=value assignment naming a credential (case-insensitive,
    # mirrors secret_redaction.py's dotenv-credential pattern). Requires a real
    # value (>=6 chars).
    local re_env='^[[:space:]]*[A-Za-z0-9_]*(SECRET|KEY|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Za-z0-9_]*[[:space:]]*=[[:space:]]*.{6,}'
    [[ $t =~ $re_env ]] && return 0
    return 1
}

# PowerShell tool commands. The bash checks above are not reused: 'set' and
# 'export' style patterns would match every Set-Location / Set-Content. Mirrors
# secret_guard.ps1 $PsDumpPatterns (statement-anchored, case-insensitive).
is_sensitive_ps_cmd() {
    local cmd="$1" st verb w first
    [ -z "$cmd" ] && return 1
    while IFS= read -r st; do
        # 55d48d69 (confirm pass, secret_guard.sh-only gap): a leading simple
        # variable assignment ('$x = Get-Content ...', '$x=Get-Content ...',
        # '$x= Get-Content ...') hid the reader verb from the naive
        # first-token check below -- '$x' became "verb" and 'Get-Content'
        # was never inspected, even though secret_guard.ps1's own regex
        # already treats '=' as a valid statement-start anchor (line 146ish,
        # '(^|[;|&(=])'). Strip that prefix from the STATEMENT TEXT (not the
        # token list) before tokenizing, so the reader verb after '=' is
        # 'first' the same way it would be after ';'/'|'/'&'/'('. Only a
        # plain '=' is handled (not '+=' etc.): the same narrower scope as
        # this hook's other statement-anchored checks, not a full PowerShell
        # parser (see the file header's documented residual-risk class).
        if [[ $st =~ ^[[:space:]]*\$[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=[[:space:]]*(.+)$ ]]; then
            st="${BASH_REMATCH[1]}"
        fi
        set -f; set -- $st; set +f
        [ $# -eq 0 ] && continue
        verb="$1"
        [[ $verb == printenv ]] && [ $# -eq 1 ] && return 0
        case "$verb" in
            gci|ls|dir|get-childitem|gi|get-item)
                case "$*" in
                    "$verb env:"|"$verb env:\\"|"$verb -path env:"|"$verb -path env:\\") return 0 ;;
                esac ;;
            cat|gc|type|get-content|sls|select-string|more)
                first=1
                for w in "$@"; do
                    if [ "$first" = 1 ]; then first=0; continue; fi
                    is_cred_word "$w" && return 0
                done ;;
        esac
    done <<EOF_PS
$(statements "$cmd")
EOF_PS
    local re1='\[(System\.)?Environment\]::GetEnvironmentVariables\([[:space:]]*\)'
    [[ $cmd =~ $re1 ]] && return 0
    local re2="\[(System\.)?IO\.File\]::Read[A-Za-z]*\([[:space:]]*['\"]([^'\"]*)['\"]"
    [[ $cmd =~ $re2 ]] && is_cred_word "${BASH_REMATCH[2]}" && return 0
    return 1
}

# --- Read ---
if [ "$tool" = "Read" ]; then
    file_path="$(json_field file_path)"
    if [ -n "$file_path" ] && is_sensitive_path "$file_path"; then
        stop_call "Meridian secret guard (14491654): BLOCKED Read of sensitive file '$file_path'. Reading .env, key, pem, meridian.toml and other credential files exposes secrets in model context. If you genuinely need this value, use a secrets manager or ask the human operator to provide only the specific value needed (not the whole file)."
    fi
fi

# --- Grep / Glob ---
if [ "$tool" = "Grep" ] || [ "$tool" = "Glob" ]; then
    pattern="$(json_field pattern)"
    for key in path glob include; do
        p="$(json_field "$key")"
        [ -z "$p" ] && continue
        pb="${p//\\//}"; pb="${pb##*/}"
        # 'read that key only': a Grep of meridian.toml for project_id shows just that line
        pre='^\^?[[:space:]]*project_id[[:space:]\\*=]*$'
        if [[ $pb == meridian.toml ]] && [ "$tool" = "Grep" ] && [[ $pattern =~ $pre ]]; then
            continue
        fi
        if is_sensitive_path "$p"; then
            stop_call "Meridian secret guard (14491654): BLOCKED $tool targeting sensitive path '$p'. Searching inside credential files exposes secrets in model context."
        fi
    done
fi

# --- Bash ---
if [ "$tool" = "Bash" ]; then
    cmd="$(json_field command)"
    if [ -n "$cmd" ] && is_sensitive_bash_cmd "$cmd"; then
        display_cmd="${cmd:0:80}"
        stop_call "Meridian secret guard (14491654): BLOCKED Bash command that appears to dump environment variables or read credential files: '${display_cmd}...'. Use only the specific env var you need (e.g. echo \$SOME_SAFE_VAR) rather than printing all environment variables or cat-ing credential files."
    fi
fi

# --- PowerShell ---
if [ "$tool" = "PowerShell" ]; then
    cmd="$(json_field command)"
    if [ -n "$cmd" ] && is_sensitive_ps_cmd "$cmd"; then
        display_cmd="${cmd:0:80}"
        stop_call "Meridian secret guard (14491654): BLOCKED PowerShell command that appears to dump environment variables or read credential files: '${display_cmd}...'. Read only the specific env var you need (e.g. \$env:SOME_SAFE_VAR) rather than listing the env: drive or reading credential files."
    fi
fi

# --- Write / Edit / MultiEdit (14491654 write-path follow-up) ---
if [ "$tool" = "Write" ] || [ "$tool" = "Edit" ] || [ "$tool" = "MultiEdit" ]; then
    file_path="$(json_field file_path)"
    if [ -n "$file_path" ] && is_sensitive_path "$file_path"; then
        blocked=1
        case "$tool" in
            Write)
                content="$(json_field content)"
                is_secret_value "$content" && blocked=0
                ;;
            Edit)
                content="$(json_field new_string)"
                is_secret_value "$content" && blocked=0
                ;;
            MultiEdit)
                # Test each edit's new_string on its own -- is_secret_value's
                # dotenv-style pattern is ^-anchored per LINE, and concatenating
                # every edit into one blob would only anchor at its very start.
                while IFS= read -r one_new_string; do
                    [ -z "$one_new_string" ] && continue
                    if is_secret_value "$one_new_string"; then
                        blocked=0
                        break
                    fi
                done <<EOF_EDITS
$(json_field_all new_string)
EOF_EDITS
                ;;
        esac
        if [ "$blocked" = 0 ]; then
            stop_call "Meridian secret guard (14491654): BLOCKED $tool of sensitive file '$file_path' -- the new content looks like it contains a real credential VALUE. Writing secrets into .env, meridian.toml or other credential files exposes them to source control and model context. Ask the human operator to set the value directly."
        fi
    fi
fi

exit 0
