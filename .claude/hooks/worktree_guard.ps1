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
# 55d48d69 fix round 1: a worktree session may still write its own scratch files:
# the OS temp dirs (TEMP/TMP/TMPDIR, /tmp -- the session scratchpad lives there) and
# Claude Code's plan files (~/.claude/plans). Only other checkouts (the main tree, a
# sibling worktree, any other path) stay blocked. The owner kill switch
# (MERIDIAN_GUARD=off|advisory, guard.off / guard.advisory, MERIDIAN_GUARD_DISABLE=
# worktree_guard) covers this hook too.
#
# Mirrors the structural pattern of hitl_guard.ps1 (PreToolUse, exit 2 to block,
# tolerant JSON parsing, fail open on any parse error).
# NOT hooks.ps1 (the token-rotation installer).
#
# 71f597b7 (decision 9ce6420e) -- same-file lock, revised in 55d48d69 fix round 1.
#
# The lock records which session last edited a repo-relative file, as a small JSON
# file under THIS CHECKOUT's own git dir (`git rev-parse --git-dir`: the main tree's
# .git, or .git/worktrees/<name> for a linked worktree). It is WARN-ONLY: when
# another session edited the same file in the same working tree within the last 15
# minutes, the edit is allowed and the model gets an additionalContext warning to
# coordinate (two sessions must never share one working tree -- AGENTS.md).
#
# Why not the old design (git-common-dir key, 2 h TTL, exit 2): the common dir is
# shared by every worktree of the clone, so parallel worktree agents editing their
# OWN copies of one file blocked each other; nothing released a lock when a session
# ended, so a new session -- or the same human after /clear -- was locked out of a
# file for 2 h after the last edit; and the only escape was /hooks or
# disableAllHooks. A hook cannot tell a live session from a finished one (no
# network, no reliable session-to-process mapping; a process lookup costs ~1.5 s in
# Windows PowerShell), so it cannot hard-block safely: it warns instead, keyed per
# working tree, with a 15 minute window refreshed by every edit.
$ErrorActionPreference = 'SilentlyContinue'

# --- owner kill switch (same inputs as the Meridian guard's G0) ------------------
function Get-GuardMode([string]$HookName) {
    $raw = ([string]$env:MERIDIAN_GUARD).Trim().ToLowerInvariant()
    if ($raw -eq 'off') { return 'off' }
    $mode = if ($raw -eq '' -or $raw -eq 'enforce') { 'enforce' } else { 'advisory' }
    if ($raw -eq '' -and ([string]$env:MERIDIAN_GUARD_DEFAULT_MODE).Trim().ToLowerInvariant() -eq 'advisory') { $mode = 'advisory' }
    foreach ($tok in ([string]$env:MERIDIAN_GUARD_DISABLE -split '[\s,;]+')) {
        if ($tok.Trim().ToLowerInvariant() -eq $HookName) { return 'off' }
    }
    $gd = $null
    if ($env:LOCALAPPDATA) { $gd = [System.IO.Path]::Combine($env:LOCALAPPDATA, 'meridian', 'guard') }
    elseif ($env:USERPROFILE) { $gd = [System.IO.Path]::Combine($env:USERPROFILE, 'AppData', 'Local', 'meridian', 'guard') }
    elseif ($env:HOME) {
        $st = if ($env:XDG_STATE_HOME) { $env:XDG_STATE_HOME } else { [System.IO.Path]::Combine($env:HOME, '.local', 'state') }
        $gd = [System.IO.Path]::Combine($st, 'meridian', 'guard')
    }
    if ($gd) {
        if ([System.IO.File]::Exists([System.IO.Path]::Combine($gd, 'guard.off'))) { return 'off' }
        if ([System.IO.File]::Exists([System.IO.Path]::Combine($gd, 'guard.advisory'))) { return 'advisory' }
    }
    return $mode
}

function Write-Context([string]$Message) {
    $o = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; additionalContext = $Message } }
    [Console]::Out.Write(($o | ConvertTo-Json -Compress -Depth 4))
}

function Stop-Call([string]$Message) {
    if ($script:GuardMode -eq 'advisory') { Write-Context ('[advisory, not blocked] ' + $Message); exit 0 }
    [Console]::Error.WriteLine($Message)
    exit 2
}

function Norm([string]$p) { return (($p -replace '\\', '/') -replace '/+', '/').TrimEnd('/') }

function Test-Under([string]$File, [string]$Dir) {
    if (-not $Dir) { return $false }
    $d = (Norm $Dir) + '/'
    return ((Norm $File) + '/').StartsWith($d, [System.StringComparison]::OrdinalIgnoreCase)
}

try { $payload = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $payload) { exit 0 }
try { $obj = $payload | ConvertFrom-Json } catch { exit 0 }
if (-not $obj) { exit 0 }

$tool = [string]$obj.tool_name
if ($tool -notin @('Edit', 'Write', 'MultiEdit', 'NotebookEdit')) { exit 0 }

$script:GuardMode = Get-GuardMode 'worktree_guard'
if ($script:GuardMode -eq 'off') { exit 0 }

# CLAUDE_PROJECT_DIR is set by Claude Code to the session's project root.
# Fail open if the env var is absent (unknown execution context).
$projectDir = $env:CLAUDE_PROJECT_DIR
if (-not $projectDir) { exit 0 }

# Normalize to forward slashes for consistent matching.
$normProject = $projectDir -replace '\\', '/'

# Is this session inside a worktree, or the main tree?
$isWorktreeSession = $normProject -match '/\.claude/worktrees/'

# Extract the edited path from tool_input; fail open if absent.
# 6f07aa89: NotebookEdit's real schema field is notebook_path, not file_path --
# Claude Code's own NotebookEdit tool_input never contains a file_path key at
# all (verified against the tool's own parameter schema: notebook_path,
# cell_id, cell_type, edit_mode, new_source -- no file_path). Falling back to
# notebook_path when file_path is absent means NotebookEdit calls are still
# boundary-checked instead of silently no-op'ing (fail-open) on every call.
$filePath = $null
if ($obj.tool_input) {
    $filePath = [string]$obj.tool_input.file_path
    if (-not $filePath) { $filePath = [string]$obj.tool_input.notebook_path }
}
if (-not $filePath) { exit 0 }

# Normalize file_path separators.
$normFile = $filePath -replace '\\', '/'

# Check if the file is under the claimed project dir (add trailing slash to
# prevent partial-name prefix matches, e.g. worktree 'wf_abc' vs 'wf_abcdef').
$prefix = $normProject.TrimEnd('/') + '/'
$insideProjectDir = $normFile.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)

if ($isWorktreeSession -and -not $insideProjectDir) {
    # Scratch space a worktree session legitimately writes outside its checkout.
    $scratch = $false
    foreach ($d in @($env:TEMP, $env:TMP, $env:TMPDIR)) { if ($d -and (Test-Under $normFile $d)) { $scratch = $true } }
    if ((Norm $normFile).StartsWith('/tmp/', [System.StringComparison]::Ordinal)) { $scratch = $true }
    foreach ($h in @($env:USERPROFILE, $env:HOME)) {
        if ($h -and (Test-Under $normFile ((Norm $h) + '/.claude/plans'))) { $scratch = $true }
    }
    if ($env:CLAUDE_CONFIG_DIR -and (Test-Under $normFile ((Norm $env:CLAUDE_CONFIG_DIR) + '/plans'))) { $scratch = $true }
    if ($scratch) { exit 0 }
    # The file is outside this session's worktree. Block it.
    # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
    Stop-Call "Meridian worktree guard (a3984d96): ${tool} target '$filePath' is OUTSIDE this session's worktree ('$projectDir'). Edit only files under your own worktree (the temp dir / session scratchpad and ~/.claude/plans are fine). If you need to affect the main tree or a different worktree, coordinate via request_hitl or complete this session first."
}

if (-not $insideProjectDir) {
    # Main-tree session editing something outside its own project dir (rare --
    # e.g. an absolute path elsewhere on disk). Nothing meaningful to lock;
    # the original hook's behavior here was an unconditional allow.
    exit 0
}

# File is inside this session's own project dir (worktree or main tree) --
# the worktree-boundary check above is satisfied either way. Compute its
# repo-relative path for the lockfile check below.
$relPath = $normFile.Substring($prefix.Length)
if (-not $relPath) { exit 0 }

# ---------------------------------------------------------------------------
# 71f597b7 -- per-checkout same-file lock (warn-only, see header).
# ---------------------------------------------------------------------------
$sessionId = [string]$obj.session_id
if (-not $sessionId) { exit 0 }  # no attributable local owner -- nothing to record

$gitDirRaw = $null
try { $gitDirRaw = (& git -C $projectDir rev-parse --git-dir 2>$null) } catch { $gitDirRaw = $null }
if (($LASTEXITCODE -and $LASTEXITCODE -ne 0) -or -not $gitDirRaw) { exit 0 }
$gitDirRaw = [string]$gitDirRaw

if ([System.IO.Path]::IsPathRooted($gitDirRaw)) {
    $gitDirCandidate = $gitDirRaw
} else {
    $gitDirCandidate = [System.IO.Path]::Combine($projectDir, $gitDirRaw)
}
$gitDir = $null
try { $gitDir = [System.IO.Path]::GetFullPath($gitDirCandidate) } catch { exit 0 }
if (-not $gitDir -or -not [System.IO.Directory]::Exists($gitDir)) { exit 0 }

$lockRoot = [System.IO.Path]::Combine($gitDir, 'meridian-locks')
$lockFile = [System.IO.Path]::Combine($lockRoot, ($relPath -replace '/', [System.IO.Path]::DirectorySeparatorChar) + '.lock')
try { [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($lockFile)) } catch { exit 0 }

$windowMinutes = 15
$nowUtc = [DateTime]::UtcNow
$lockJson = "{`"session_id`":`"$sessionId`",`"path`":`"$relPath`",`"tool`":`"$tool`",`"locked_at`":`"$($nowUtc.ToString('o'))`"}"

$existingOwner = $null
$ageMinutes = $null
if ([System.IO.File]::Exists($lockFile)) {
    try {
        $existingObj = [System.IO.File]::ReadAllText($lockFile) | ConvertFrom-Json
        $existingOwner = [string]$existingObj.session_id
    } catch { $existingOwner = $null }
    try { $ageMinutes = ($nowUtc - [System.IO.File]::GetLastWriteTimeUtc($lockFile)).TotalMinutes } catch { $ageMinutes = $null }
}

# Record this session as the file's latest editor. WriteAllBytes (not WriteAllText --
# [System.Text.Encoding]::UTF8's preamble would add a BOM) keeps the JSON BOM-free.
try { [System.IO.File]::WriteAllBytes($lockFile, [System.Text.Encoding]::UTF8.GetBytes($lockJson)) } catch { }

if ($existingOwner -and $existingOwner -ne $sessionId -and $null -ne $ageMinutes -and $ageMinutes -lt $windowMinutes) {
    $mins = [Math]::Max(0, [int][Math]::Floor($ageMinutes))
    Write-Context ("Meridian worktree lock guard (71f597b7), warning only -- the edit is allowed: '$relPath' in this " +
        "working tree ('$projectDir') was edited $mins minute(s) ago by another session ($existingOwner). " +
        "Two sessions must not edit one working tree at the same time (AGENTS.md: one worktree per session). " +
        "If that session is still running, stop and coordinate (claim_file / request_hitl) before editing further; " +
        "if it has ended (or it was you before /clear), carry on.")
}
exit 0
