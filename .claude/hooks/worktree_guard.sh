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
# Mirrors the structural pattern of hitl_guard.sh (PreToolUse, exit 2 to block,
# tolerant JSON extraction, fail open on any parse error).
# NOT hooks.sh (the token-rotation installer).
#
# 71f597b7 (decision 9ce6420e) -- git-common-dir lockfile extension.
#
# The worktree-boundary check above only stops a session from editing a file
# OUTSIDE its own claimed worktree. It does nothing about two LIVE sessions that
# each stay within their own worktree (or the main tree) but happen to be editing
# the SAME repo-relative file at the same time -- e.g. two sessions in sibling
# worktrees of one clone both touching meridian/server.py, or a main-tree session
# and a worktree session both touching it. claim_file/claim_symbol (meridian/db/
# locks.py) track that in the DB but have zero enforcement power over a local
# disk write, and there is no reliable bridge between Claude Code's own hook-
# supplied session_id and Meridian's independently-minted DB session_id (see
# decision 9ce6420e, option 2). So this section adds a purely LOCAL, best-effort
# lock keyed by repo-relative path, stored as a small JSON file under this
# clone's shared `git rev-parse --git-common-dir` -- every worktree of one clone
# (main tree included) resolves to the SAME common dir, so the lock is visible
# to every session sharing this checkout, with zero network calls and zero
# session-identity bridging (Claude Code's own per-CLI session_id is used purely
# as a local lock-owner token, never sent anywhere).
#
# This is advisory-hard (a live foreign lock exit-2 blocks, matching this hook's
# own worktree-boundary fail-mode), not a distributed/atomic guarantee: the
# noclobber-based check-then-write below has a small race window on a genuinely
# simultaneous first touch, which is judged acceptable for a local, single-
# machine, no-network guard whose job is to catch the common "another live
# session already owns this file" case, not to replace a real consensus
# protocol.
#
# Staleness: a lock older than 2 hours (by file mtime) is treated as abandoned
# and silently reclaimed. 2 hours mirrors meridian/db/locks.py's own
# _FILE_LOCK_TTL_HOURS -- the same threshold Meridian's server-side file claims
# already use to decide a claim is dead, chosen here for consistency rather than
# reinvented. This hook has no network access to ask "is that session still
# alive" against anything external, so an mtime heuristic is the most it can do
# on its own -- documented here rather than left implicit.
set -uo pipefail

payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

# Extract tool_name tolerantly; fail open if absent.
tool="$(printf '%s' "$payload" | grep -oE '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
[ -z "$tool" ] && exit 0

# Only intercept file-edit tools.
case "$tool" in
    Edit|Write|MultiEdit|NotebookEdit) ;;
    *) exit 0 ;;
esac

# CLAUDE_PROJECT_DIR is set by Claude Code to the session's project root.
# Fail open if the env var is absent (unknown execution context).
project_dir="${CLAUDE_PROJECT_DIR:-}"
[ -z "$project_dir" ] && exit 0

# Normalize to forward slashes for consistent matching.
norm_project="$(printf '%s' "$project_dir" | tr '\\' '/')"

# Is this session inside a worktree, or the main tree?
is_worktree=0
case "$norm_project" in
    */.claude/worktrees/*) is_worktree=1 ;;
esac

# Extract the edited path from tool_input; fail open if absent.
# 6f07aa89: NotebookEdit's real schema field is notebook_path, not file_path --
# Claude Code's own NotebookEdit tool_input never contains a file_path key at
# all (verified against the tool's own parameter schema: notebook_path,
# cell_id, cell_type, edit_mode, new_source -- no file_path). Falling back to
# notebook_path when file_path is absent means NotebookEdit calls are still
# boundary-checked instead of silently no-op'ing (fail-open) on every call.
file_path="$(printf '%s' "$payload" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
if [ -z "$file_path" ]; then
    file_path="$(printf '%s' "$payload" | grep -oE '"notebook_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
fi
[ -z "$file_path" ] && exit 0

# Normalize file_path separators.
# The JSON-extracted value may contain literal \\ (JSON-escaped backslash, two chars:
# backslash + backslash) representing a single Windows path separator.  We use tr to
# convert all backslashes (whether doubled from JSON or already single) to forward
# slashes, then collapse duplicate slashes (//).  Since tr maps each input char to an
# output char, a double \\ becomes // which we then deduplicate.
norm_file="$(printf '%s' "$file_path" | tr '\\' '/' | sed 's|//|/|g')"

# Check if the file_path starts with the claimed project dir.
# Use a trailing-slash on the prefix so partial-name matches don't pass
# (e.g. worktree 'wf_abc' must not match a sibling 'wf_abcdef').
worktree_prefix="${norm_project%/}/"
inside_project_dir=0
rel_path=""
case "$norm_file/" in
    "$worktree_prefix"*)
        inside_project_dir=1
        rel_path="${norm_file#"$worktree_prefix"}"
        ;;
esac

if [ "$is_worktree" = "1" ] && [ "$inside_project_dir" != "1" ]; then
    # The file is outside this session's worktree. Block it.
    # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
    echo "Meridian worktree guard (a3984d96): $tool target '$file_path' is OUTSIDE this session's worktree ('$project_dir'). Edit only files under your own worktree. If you need to affect the main tree or a different worktree, coordinate via request_hitl or complete this session first." >&2
    exit 2
fi

if [ "$inside_project_dir" != "1" ]; then
    # Main-tree session editing something outside its own project dir (rare --
    # e.g. an absolute path elsewhere on disk). Nothing meaningful to lock;
    # the original hook's behavior here was an unconditional allow.
    exit 0
fi

[ -z "$rel_path" ] && exit 0

# ---------------------------------------------------------------------------
# 71f597b7 -- git-common-dir lockfile check.
# ---------------------------------------------------------------------------
session_id="$(printf '%s' "$payload" | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*"session_id"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')"
[ -z "$session_id" ] && exit 0  # no attributable local owner -- fail open on locking only

# `-C` takes project_dir as a literal path argument, which git itself never
# reinterprets for the platform it happens to be built for -- unlike an
# inherited process cwd, which the launcher (WSL's bash.exe stub, MSYS2's
# git-bash, etc.) DOES translate on process start. A native Windows git.exe
# (the common case: PowerShell running the .ps1 sibling, or MSYS2 git-bash)
# accepts a drive-letter path directly; a WSL/Linux git binary (reachable when
# this .sh mirror is exercised through Windows' own WSL bash.exe launcher,
# confirmed live in this repo's own dev environment) does not understand
# "C:/..." at all and needs "/mnt/c/...". Try the path as-is first, and only
# if that fails, retry through a best-effort Windows-to-WSL translation --
# this keeps the common (non-WSL) case a single git call while still working
# under WSL. Any other exotic layout falls through to the existing
# fail-open-on-locking-only behavior below.
git_common_dir_raw="$(git -C "$project_dir" rev-parse --git-common-dir 2>/dev/null)"
project_dir_for_git="$project_dir"
if [ -z "$git_common_dir_raw" ]; then
    case "$norm_project" in
        [A-Za-z]:/*)
            drive_letter="$(printf '%s' "${norm_project%%:*}" | tr '[:upper:]' '[:lower:]')"
            wsl_project_dir="/mnt/$drive_letter${norm_project#*:}"
            git_common_dir_raw="$(git -C "$wsl_project_dir" rev-parse --git-common-dir 2>/dev/null)"
            [ -n "$git_common_dir_raw" ] && project_dir_for_git="$wsl_project_dir"
            ;;
    esac
fi
[ -z "$git_common_dir_raw" ] && exit 0

case "$git_common_dir_raw" in
    /*|[A-Za-z]:[\\/]*) git_common_dir_candidate="$git_common_dir_raw" ;;  # already absolute
    *) git_common_dir_candidate="$project_dir_for_git/$git_common_dir_raw" ;;
esac

# Resolve to a canonical absolute path (git may emit relative ../ segments).
git_common_dir="$(cd "$git_common_dir_candidate" 2>/dev/null && pwd)"
[ -z "$git_common_dir" ] && exit 0

lock_root="$git_common_dir/meridian-locks"
mkdir -p "$lock_root" 2>/dev/null || exit 0

lock_file="$lock_root/$rel_path.lock"
lock_dir="$(dirname "$lock_file")"
mkdir -p "$lock_dir" 2>/dev/null || exit 0

now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
lock_json="{\"session_id\":\"$session_id\",\"path\":\"$rel_path\",\"tool\":\"$tool\",\"locked_at\":\"$now_iso\"}"

acquired=0
if ( set -o noclobber; printf '%s' "$lock_json" > "$lock_file" ) 2>/dev/null; then
    acquired=1
fi

if [ "$acquired" = "1" ]; then
    exit 0
fi

# Lock file already existed -- inspect its owner.
existing_owner="$(cat "$lock_file" 2>/dev/null | grep -oE '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*"session_id"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')"

if [ -n "$existing_owner" ] && [ "$existing_owner" = "$session_id" ]; then
    # Same session re-editing (or continuing to edit) its own file -- refresh
    # the lock's timestamp (extends its staleness TTL) and allow.
    printf '%s' "$lock_json" > "$lock_file" 2>/dev/null
    exit 0
fi

# Different (or unattributable) owner -- check staleness via file mtime before
# treating this as a real, live collision. 7200s = 2 hours, mirrors
# meridian/db/locks.py's _FILE_LOCK_TTL_HOURS (see header comment above).
stale_threshold_secs=7200
now_epoch="$(date -u +%s 2>/dev/null || echo 0)"
lock_mtime_epoch="$(stat -c %Y "$lock_file" 2>/dev/null || stat -f %m "$lock_file" 2>/dev/null || echo 0)"

is_stale=0
if [ "$lock_mtime_epoch" = "0" ] || [ "$now_epoch" = "0" ]; then
    is_stale=1  # can't stat it / can't get current time -- treat as unusable rather than trap forever
else
    age=$(( now_epoch - lock_mtime_epoch ))
    if [ "$age" -ge "$stale_threshold_secs" ]; then
        is_stale=1
    fi
fi

if [ "$is_stale" = "1" ]; then
    printf '%s' "$lock_json" > "$lock_file" 2>/dev/null
    exit 0
fi

# A live lock is held by a DIFFERENT session. Block it, matching this hook's
# own worktree-boundary fail-mode (hard exit-2 block naming the other owner).
owner_display="${existing_owner:-(unknown session)}"
echo "Meridian worktree lock guard (71f597b7): $tool target '$file_path' (repo-relative '$rel_path') is locked by another live session ($owner_display) sharing this checkout's git-common-dir. Coordinate via request_hitl, wait for that session to finish, or -- only if you are certain it is dead -- remove the stale lock at '$lock_file' (locks self-expire after 2 hours of inactivity)." >&2
exit 2
