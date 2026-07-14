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

# If this session is NOT inside a worktree, fail open -- the main-tree session owns
# the main tree, no restriction needed.
case "$norm_project" in
    */.claude/worktrees/*) ;;   # inside a worktree -- enforce
    *) exit 0 ;;                # main tree or unknown -- fail open
esac

# Extract file_path from tool_input; fail open if absent.
file_path="$(printf '%s' "$payload" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
[ -z "$file_path" ] && exit 0

# Normalize file_path separators.
# The JSON-extracted value may contain literal \\ (JSON-escaped backslash, two chars:
# backslash + backslash) representing a single Windows path separator.  We use tr to
# convert all backslashes (whether doubled from JSON or already single) to forward
# slashes, then collapse duplicate slashes (//).  Since tr maps each input char to an
# output char, a double \\ becomes // which we then deduplicate.
norm_file="$(printf '%s' "$file_path" | tr '\\' '/' | sed 's|//|/|g')"

# Check if the file_path starts with the claimed worktree path.
# Use a trailing-slash on the prefix so partial-name matches don't pass.
worktree_prefix="${norm_project%/}/"
case "$norm_file/" in
    "$worktree_prefix"*) exit 0 ;;  # inside the claimed worktree -- allow
esac

# The file is outside this session's worktree. Block it.
# exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
echo "Meridian worktree guard (a3984d96): $tool target '$file_path' is OUTSIDE this session's worktree ('$project_dir'). Edit only files under your own worktree. If you need to affect the main tree or a different worktree, coordinate via request_hitl or complete this session first." >&2
exit 2
