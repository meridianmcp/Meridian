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
# Mirrors the structural pattern of hitl_guard.ps1 (PreToolUse, exit 2 to block,
# tolerant JSON parsing, fail open on any parse error).
# NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'

try { $payload = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $payload) { exit 0 }
try { $obj = $payload | ConvertFrom-Json } catch { exit 0 }
if (-not $obj) { exit 0 }

$tool = [string]$obj.tool_name
if ($tool -notin @('Edit', 'Write', 'MultiEdit', 'NotebookEdit')) { exit 0 }

# CLAUDE_PROJECT_DIR is set by Claude Code to the session's project root.
# Fail open if the env var is absent (unknown execution context).
$projectDir = $env:CLAUDE_PROJECT_DIR
if (-not $projectDir) { exit 0 }

# Normalize to forward slashes for consistent matching.
$normProject = $projectDir -replace '\\', '/'

# If this session is NOT inside a worktree, fail open -- the main-tree session owns
# the main tree, no restriction needed.
if ($normProject -notmatch '/\.claude/worktrees/') { exit 0 }

# Extract file_path from tool_input; fail open if absent.
$filePath = $null
if ($obj.tool_input) { $filePath = [string]$obj.tool_input.file_path }
if (-not $filePath) { exit 0 }

# Normalize file_path separators.
$normFile = $filePath -replace '\\', '/'

# Check if the file is under the claimed worktree (add trailing slash to prevent
# partial-name prefix matches).
$prefix = $normProject.TrimEnd('/') + '/'
if ($normFile.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { exit 0 }

# The file is outside this session's worktree. Block it.
# exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
[Console]::Error.WriteLine("Meridian worktree guard (a3984d96): ${tool} target '$filePath' is OUTSIDE this session's worktree ('$projectDir'). Edit only files under your own worktree. If you need to affect the main tree or a different worktree, coordinate via request_hitl or complete this session first.")
exit 2
