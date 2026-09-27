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
#   B<TAB>path<TAB>json   the audit line to use instead of A when the state cannot be saved
#   F<TAB>json            the output to use instead of O when the state cannot be saved
#                         (guard_core.fail_open_result: an escapable deny becomes an inject)
# A call that changes the state is re-decided under a per-session lock (mkdir) from the
# state as it is then, so parallel hooks cannot lose each other's counter updates.
_mg_in=$(cat 2>/dev/null) || _mg_in=''
_mg_run() {
    printf '%s' "$_mg_in" | LC_ALL=C awk -v MODE="$_mg_mode" -f "$_mg_awk" 2>/dev/null
}
_mg_res=$(_mg_run) || _mg_res=''
[ -n "$_mg_res" ] || exit 0

_mg_tab=$'\t'

_mg_write_state() {
    local path=$1 json=$2 dir tmp
    dir=${path%/*}
    [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || return 1
    # a directory (or anything but a regular file) at the state path is unwritable state
    if [ -e "$path" ] && [ ! -f "$path" ]; then return 1; fi
    tmp="$dir/.state-$$.tmp"
    { printf '%s' "$json" >"$tmp"; } 2>/dev/null || return 1
    mv -f "$tmp" "$path" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 1; }
    return 0
}

_mg_append_audit() {
    local path=$1 json=$2 dir
    dir=${path%/*}
    [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || return 0
    { printf '%s\n' "$json" >>"$path"; } 2>/dev/null
    return 0
}

# guard_core._lock_state: a mkdir lock next to the state file with the owner pid
# inside; a lock whose owner is gone is taken over. Gives up (=> fail open) after
# about 1.5 s.
_mg_lock=''
_mg_take_lock() {
    local path=$1 dir i pid
    dir=${path%/*}
    [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || return 1
    _mg_lock="${path%.json}.lockd"
    i=0
    while [ "$i" -lt 75 ]; do
        if mkdir "$_mg_lock" 2>/dev/null; then
            printf '%s' "$$" >"$_mg_lock/pid" 2>/dev/null
            return 0
        fi
        pid=$(cat "$_mg_lock/pid" 2>/dev/null) || pid=''
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            rm -rf "$_mg_lock" 2>/dev/null
            i=$((i + 1))
            continue
        fi
        sleep 0.02 2>/dev/null || sleep 1
        i=$((i + 1))
    done
    _mg_lock=''
    return 1
}
_mg_drop_lock() {
    if [ -n "$_mg_lock" ]; then rm -rf "$_mg_lock" 2>/dev/null; fi
    _mg_lock=''
}

# $1 = protocol text; sets the state line, the outputs and the two audit lists
_mg_parse() {
    _mg_s_path=''; _mg_s_json=''; _mg_out=''; _mg_fout=''; _mg_have_f=0
    _mg_a=(); _mg_b=()
    while IFS= read -r _mg_line; do
        case $_mg_line in
            "S${_mg_tab}"*)
                _mg_rest=${_mg_line#S"$_mg_tab"}
                _mg_s_path=${_mg_rest%%"$_mg_tab"*}; _mg_s_json=${_mg_rest#*"$_mg_tab"} ;;
            "A${_mg_tab}"*) _mg_a+=("${_mg_line#A"$_mg_tab"}") ;;
            "B${_mg_tab}"*) _mg_b+=("${_mg_line#B"$_mg_tab"}") ;;
            "O${_mg_tab}"*) _mg_out=${_mg_line#O"$_mg_tab"} ;;
            "F${_mg_tab}"*) _mg_fout=${_mg_line#F"$_mg_tab"}; _mg_have_f=1 ;;
        esac
    done <<MG_EOF
$1
MG_EOF
}

_mg_parse "$_mg_res"
_mg_failopen=0
if [ -n "$_mg_s_path" ]; then
    if _mg_take_lock "$_mg_s_path"; then
        # re-decide under the lock from the state as it is now
        _mg_res2=$(_mg_run) || _mg_res2=''
        if [ -n "$_mg_res2" ]; then
            _mg_parse "$_mg_res2"
            if [ -n "$_mg_s_path" ]; then
                _mg_write_state "$_mg_s_path" "$_mg_s_json" || _mg_failopen=1
            fi
        else
            _mg_failopen=1
        fi
        _mg_drop_lock
    else
        _mg_failopen=1
    fi
fi
if [ "$_mg_failopen" = 1 ]; then
    for _mg_x in "${_mg_b[@]}"; do _mg_append_audit "${_mg_x%%"$_mg_tab"*}" "${_mg_x#*"$_mg_tab"}" || true; done
    if [ "$_mg_have_f" = 1 ]; then _mg_out=$_mg_fout; fi
else
    for _mg_x in "${_mg_a[@]}"; do _mg_append_audit "${_mg_x%%"$_mg_tab"*}" "${_mg_x#*"$_mg_tab"}" || true; done
fi

if [ -n "$_mg_out" ]; then printf '%s' "$_mg_out"; fi
exit 0
