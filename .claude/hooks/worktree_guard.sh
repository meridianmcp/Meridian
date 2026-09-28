#!/usr/bin/env bash
# a3984d96 -- PreToolUse worktree guard (structural, not text).
#
# Near-miss incident: an executor accidentally edited tests/conftest.py in the main
# repo tree instead of its own worktree, caught only by luck, not by any enforcement
# mechanism. This hook fires on Edit/Write/MultiEdit/NotebookEdit and blocks the call
# (exit 2) whenever the target file_path is NOT under the session's claimed worktree.
#
# Detection: CLAUDE_PROJECT_DIR is set by Claude Code to the project root for the
# current session. When a session runs inside a worktree, CLAUDE_PROJECT_DIR points
# to the worktree directory (under .claude/worktrees/<name>/). If CLAUDE_PROJECT_DIR
# does NOT contain '.claude/worktrees/' the session is in the main tree -- fail open
# (no restriction: the main-tree session owns the main tree).
#
# 55d48d69 fix round 1 (mirrors worktree_guard.ps1): a worktree session may still
# write its own scratch files -- the OS temp dirs (TEMP/TMP/TMPDIR, /tmp: the session
# scratchpad) and Claude Code's plan files (~/.claude/plans). The owner kill switch
# (MERIDIAN_GUARD=off|advisory, guard.off / guard.advisory, MERIDIAN_GUARD_DISABLE=
# worktree_guard) covers this hook too.
#
# Mirrors the structural pattern of hitl_guard.sh (PreToolUse, exit 2 to block,
# tolerant JSON extraction, fail open on any parse error).
# NOT hooks.sh (the token-rotation installer).
#
# 71f597b7 (decision 9ce6420e) -- same-file lock, revised in 55d48d69 fix round 1:
# keyed on THIS CHECKOUT's own git dir (`git rev-parse --git-dir`), WARN-ONLY (an
# additionalContext warning, never exit 2) when another session edited the same
# repo-relative file in the same working tree within the last 15 minutes. The old
# git-common-dir key made parallel worktree agents block each other on their OWN
# copies, and the 2 h hard block outlived finished sessions and /clear. See
# worktree_guard.ps1 for the full rationale.
set -uo pipefail

payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

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

# Only intercept file-edit tools.
case "$tool" in
    Edit|Write|MultiEdit|NotebookEdit) ;;
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
guard_mode worktree_guard
[ "$GUARD_MODE" = "off" ] && exit 0

json_escape() {
    local s=$1
    s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
    printf '%s' "$s"
}
write_context() {
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}' "$(json_escape "$1")"
}
stop_call() {
    if [ "$GUARD_MODE" = "advisory" ]; then write_context "[advisory, not blocked] $1"; exit 0; fi
    echo "$1" >&2
    exit 2
}
# is $1 (a file) under directory $2? case-insensitive (Windows paths), no forks
is_under() {
    local f="${1//\\//}" d="${2//\\//}" r=1
    [ -z "$d" ] && return 1
    while [[ $f == *//* ]]; do f="${f//\/\//\/}"; done
    while [[ $d == *//* ]]; do d="${d//\/\//\/}"; done
    f="${f%/}/"; d="${d%/}/"
    shopt -s nocasematch
    [[ $f == "$d"* ]] && r=0
    shopt -u nocasematch
    return $r
}

# CLAUDE_PROJECT_DIR is set by Claude Code to the session's project root.
# Fail open if the env var is absent (unknown execution context).
project_dir="${CLAUDE_PROJECT_DIR:-}"
[ -z "$project_dir" ] && exit 0

# Normalize to forward slashes for consistent matching.
norm_project="${project_dir//\\//}"

# Is this session inside a worktree, or the main tree?
is_worktree=0
case "$norm_project" in
    */.claude/worktrees/*) is_worktree=1 ;;
esac

# Extract the edited path from tool_input; fail open if absent.
# 6f07aa89: NotebookEdit's real schema field is notebook_path, not file_path --
# falling back to notebook_path keeps NotebookEdit calls boundary-checked.
file_path="$(json_field file_path)"
if [ -z "$file_path" ]; then
    file_path="$(json_field notebook_path)"
fi
[ -z "$file_path" ] && exit 0

# Normalize file_path separators (json_field already un-escaped \\ to \).
norm_file="${file_path//\\//}"
while [[ $norm_file == *//* ]]; do norm_file="${norm_file//\/\//\/}"; done

# Check if the file_path starts with the claimed project dir.
# Use a trailing-slash on the prefix so partial-name matches don't pass
# (e.g. worktree 'wf_abc' must not match a sibling 'wf_abcdef').
worktree_prefix="${norm_project%/}/"
inside_project_dir=0
rel_path=""
shopt -s nocasematch
if [[ "$norm_file/" == "$worktree_prefix"* ]]; then
    inside_project_dir=1
    rel_path="${norm_file:${#worktree_prefix}}"
fi
shopt -u nocasematch

if [ "$is_worktree" = "1" ] && [ "$inside_project_dir" != "1" ]; then
    # Scratch space a worktree session legitimately writes outside its checkout.
    # 55d48d69 fix round 1 regression (found while verifying this same fix round):
    # this used to exempt ANY path under the bare OS temp dir, not just Claude
    # Code's own '<temp>/claude/...' tree (scratchpad, logs, other per-session
    # state) -- so any unrelated file that merely happened to live under $TEMP
    # (e.g. a pytest tmp_path fixture, an extracted archive, another tool's
    # scratch file) silently bypassed the worktree boundary block entirely.
    # Scoped to '<temp>/claude' to match the session scratchpad's real shape
    # (see the harness environment block: "<TEMP>\claude\<project>\<session>\scratchpad").
    for d in "${TEMP:+$TEMP/claude}" "${TMP:+$TMP/claude}" "${TMPDIR:+$TMPDIR/claude}" "/tmp/claude" "${HOME:+$HOME/.claude/plans}" "${USERPROFILE:+$USERPROFILE/.claude/plans}" "${CLAUDE_CONFIG_DIR:+$CLAUDE_CONFIG_DIR/plans}"; do
        [ -n "$d" ] && is_under "$norm_file" "$d" && exit 0
    done
    # The file is outside this session's worktree. Block it.
    # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
    stop_call "Meridian worktree guard (a3984d96): $tool target '$file_path' is OUTSIDE this session's worktree ('$project_dir'). Edit only files under your own worktree (the temp dir / session scratchpad and ~/.claude/plans are fine). If you need to affect the main tree or a different worktree, coordinate via request_hitl or complete this session first."
fi

if [ "$inside_project_dir" != "1" ]; then
    # Main-tree session editing something outside its own project dir (rare --
    # e.g. an absolute path elsewhere on disk). Nothing meaningful to lock.
    exit 0
fi

[ -z "$rel_path" ] && exit 0

# ---------------------------------------------------------------------------
# 71f597b7 -- per-checkout same-file lock (warn-only, see header).
# ---------------------------------------------------------------------------
session_id="$(json_field session_id)"
[ -z "$session_id" ] && exit 0  # no attributable local owner -- nothing to record

# `-C` takes project_dir as a literal path argument. A native Windows git (Git for
# Windows / MSYS2) accepts a drive-letter path; a WSL/Linux git needs /mnt/c/...,
# so retry through a best-effort translation when the first call fails.
git_dir_raw="$(git -C "$project_dir" rev-parse --git-dir 2>/dev/null)"
project_dir_for_git="$project_dir"
if [ -z "$git_dir_raw" ]; then
    case "$norm_project" in
        [A-Za-z]:/*)
            drive_letter="$(printf '%s' "${norm_project%%:*}" | tr '[:upper:]' '[:lower:]')"
            wsl_project_dir="/mnt/$drive_letter${norm_project#*:}"
            git_dir_raw="$(git -C "$wsl_project_dir" rev-parse --git-dir 2>/dev/null)"
            [ -n "$git_dir_raw" ] && project_dir_for_git="$wsl_project_dir"
            ;;
    esac
fi
[ -z "$git_dir_raw" ] && exit 0

case "$git_dir_raw" in
    /*|[A-Za-z]:[\\/]*) git_dir_candidate="$git_dir_raw" ;;  # already absolute
    *) git_dir_candidate="$project_dir_for_git/$git_dir_raw" ;;
esac

# Resolve to a canonical absolute path (git may emit relative segments).
git_dir="$(cd "$git_dir_candidate" 2>/dev/null && pwd)"
[ -z "$git_dir" ] && exit 0

lock_file="$git_dir/meridian-locks/$rel_path.lock"
mkdir -p "$(dirname "$lock_file")" 2>/dev/null || exit 0

window_secs=900
now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
lock_json="{\"session_id\":\"$session_id\",\"path\":\"$rel_path\",\"tool\":\"$tool\",\"locked_at\":\"$now_iso\"}"

existing_owner=''
age=''
if [ -f "$lock_file" ]; then
    existing_owner="$(grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$lock_file" 2>/dev/null | head -1 | sed -E 's/.*"session_id"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')"
    now_epoch="$(date -u +%s 2>/dev/null || echo 0)"
    lock_mtime_epoch="$(stat -c %Y "$lock_file" 2>/dev/null || stat -f %m "$lock_file" 2>/dev/null || echo 0)"
    if [ "$now_epoch" != "0" ] && [ "$lock_mtime_epoch" != "0" ]; then
        age=$(( now_epoch - lock_mtime_epoch ))
    fi
fi

# Record this session as the file's latest editor.
printf '%s' "$lock_json" > "$lock_file" 2>/dev/null

if [ -n "$existing_owner" ] && [ "$existing_owner" != "$session_id" ] && [ -n "$age" ] && [ "$age" -lt "$window_secs" ]; then
    mins=$(( age / 60 ))
    [ "$mins" -lt 0 ] && mins=0
    write_context "Meridian worktree lock guard (71f597b7), warning only -- the edit is allowed: '$rel_path' in this working tree ('$project_dir') was edited $mins minute(s) ago by another session ($existing_owner). Two sessions must not edit one working tree at the same time (AGENTS.md: one worktree per session). If that session is still running, stop and coordinate (claim_file / request_hitl) before editing further; if it has ended (or it was you before /clear), carry on."
fi
exit 0
