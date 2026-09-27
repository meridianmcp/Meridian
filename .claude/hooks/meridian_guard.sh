#!/usr/bin/env bash
# meridian_guard.sh -- Meridian guard, Claude Code PreToolUse hook (sprint item 55d48d69).
#
# POSIX-host twin of meridian_guard.ps1. The decision engine is meridian_guard.awk
# (a mirror of meridian/guard_core.py, the SPEC); tests/fixtures/guard_cases.json pins
# the Python core, the ps1 shim and this shim to the same decisions.
#
#   G0  kill switch   MERIDIAN_GUARD=off|advisory|enforce, MERIDIAN_GUARD_DISABLE,
#                     $LOCALAPPDATA/meridian/guard/guard.off | guard.advisory (owner-created);
#                     installer inputs MERIDIAN_GUARD_DEFAULT_MODE=advisory (lowest
#                     precedence) and MERIDIAN_GUARD_SCOPE=user (G0, G6-G8 only)
#   G1  Grep in a fresh own codebase-memory index          G6  auto-memory write (tool)
#   G2  stale/canonical/ancestor index, code Glob (inject)  G7  auto-memory write (shell)
#   G3  first-stage recursive shell search                  G8  Serena memory write
#   G4  Desktop Commander content search                    G9  guard dir / kill switch writes
#   G5  duplicate codebase-memory project                   G10 settings weakening (ask)
#   G11 paper/repo-shaped web research after a Meridian research receipt
# PostToolUse G12-G14 run through meridian_guard_post.sh (this file with --post).
#
# Contract: the only blocking channel is stdout JSON (permissionDecision), printed
# once, last; the exit code is ALWAYS 0 (never 2). Any error, parse failure, missing
# awk, missing/corrupt snapshot or unreadable state file => allow with no output.
# Hot path: no network, no Python, no SQLite. This is NOT hooks.sh (the
# token-rotation installer).
#
# --batch MANIFEST [FACTS] is a test entry point (tests/test_guard_hooks.py): the awk
# engine evaluates every case directory listed in MANIFEST in one process.

# Started by a non-bash sh (e.g. "sh meridian_guard.sh" on a dash system)? Re-run
# under bash before any bash-only syntax is read: dash's "Bad substitution" on
# BASH_SOURCE below would exit 2 -- the one exit code Claude Code treats as a block.
if [ -z "${BASH_VERSION:-}" ]; then
    command -v bash >/dev/null 2>&1 || exit 0
    exec bash "$0" "$@"
    exit 0
fi

set +e
trap 'exit 0' ERR

_mg_self=${BASH_SOURCE[0]:-$0}
case $_mg_self in
    */* | *\\*) _mg_dir=${_mg_self%[/\\]*} ;;
    *) _mg_dir=. ;;
esac
_mg_awk=$_mg_dir/meridian_guard.awk
[ -f "$_mg_awk" ] || exit 0

_mg_mode=pre
if [ "${1:-}" = "--post" ]; then _mg_mode=post; shift; fi

if [ "${1:-}" = "--batch" ]; then
    MG_BATCH=${2:-} MG_FACTS=${3:-} LC_ALL=C awk -f "$_mg_awk" </dev/null >/dev/null 2>&1 || true
    exit 0
fi

# awk reads the payload from stdin and prints a small protocol:
#   S<TAB>path<TAB>json   write the per-session state file (atomically)
#   A<TAB>path<TAB>json   append one audit line (rule, decision, tool, root; never command text)
#   O<TAB>json            the hook output, printed last
_mg_res=$(LC_ALL=C awk -v MODE="$_mg_mode" -f "$_mg_awk" 2>/dev/null) || _mg_res=''
[ -n "$_mg_res" ] || exit 0

_mg_tab=$'\t'
_mg_out=''

_mg_write_state() {
    local path=$1 json=$2 dir tmp
    dir=${path%/*}
    [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || return 0
    tmp="$dir/.state-$$.tmp"
    { printf '%s' "$json" >"$tmp"; } 2>/dev/null || return 0
    mv -f "$tmp" "$path" 2>/dev/null || rm -f "$tmp" 2>/dev/null
    return 0
}

_mg_append_audit() {
    local path=$1 json=$2 dir
    dir=${path%/*}
    [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || return 0
    { printf '%s\n' "$json" >>"$path"; } 2>/dev/null
    return 0
}

while IFS= read -r _mg_line; do
    case $_mg_line in
        "S${_mg_tab}"*)
            _mg_rest=${_mg_line#S"$_mg_tab"}
            _mg_write_state "${_mg_rest%%"$_mg_tab"*}" "${_mg_rest#*"$_mg_tab"}" || true
            ;;
        "A${_mg_tab}"*)
            _mg_rest=${_mg_line#A"$_mg_tab"}
            _mg_append_audit "${_mg_rest%%"$_mg_tab"*}" "${_mg_rest#*"$_mg_tab"}" || true
            ;;
        "O${_mg_tab}"*)
            _mg_out=${_mg_line#O"$_mg_tab"}
            ;;
    esac
done <<EOF
$_mg_res
EOF

if [ -n "$_mg_out" ]; then printf '%s' "$_mg_out"; fi
exit 0
