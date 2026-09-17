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
# check-then-write below has a small race window on a genuinely simultaneous
# first touch, which is judged acceptable for a local, single-machine, no-
# network guard whose job is to catch the common "another live session already
# owns this file" case, not to replace a real consensus protocol.
#
# Staleness: a lock older than 2 hours (by file mtime) is treated as abandoned
# and silently reclaimed. 2 hours mirrors meridian/db/locks.py's own
# _FILE_LOCK_TTL_HOURS -- the same threshold Meridian's server-side file claims
# already use to decide a claim is dead, chosen here for consistency rather than
# reinvented. This hook has no network access to ask "is that session still
# alive" against anything external, so an mtime heuristic is the most it can do
# on its own -- documented here rather than left implicit.
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

# Is this session inside a worktree, or the main tree?
$isWorktreeSession = $normProject -match '/\.claude/worktrees/'

# Extract file_path from tool_input; fail open if absent.
$filePath = $null
if ($obj.tool_input) { $filePath = [string]$obj.tool_input.file_path }
if (-not $filePath) { exit 0 }

# Normalize file_path separators.
$normFile = $filePath -replace '\\', '/'

# Check if the file is under the claimed project dir (add trailing slash to
# prevent partial-name prefix matches, e.g. worktree 'wf_abc' vs 'wf_abcdef').
$prefix = $normProject.TrimEnd('/') + '/'
$insideProjectDir = $normFile.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)

if ($isWorktreeSession -and -not $insideProjectDir) {
    # The file is outside this session's worktree. Block it.
    # exit 2 blocks the tool call; stderr is fed back to Claude as the reason.
    [Console]::Error.WriteLine("Meridian worktree guard (a3984d96): ${tool} target '$filePath' is OUTSIDE this session's worktree ('$projectDir'). Edit only files under your own worktree. If you need to affect the main tree or a different worktree, coordinate via request_hitl or complete this session first.")
    exit 2
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
# 71f597b7 -- git-common-dir lockfile check.
# ---------------------------------------------------------------------------
$sessionId = [string]$obj.session_id
if (-not $sessionId) { exit 0 }  # no attributable local owner -- fail open on locking only

$gitCommonDirRaw = $null
try { $gitCommonDirRaw = (& git -C $projectDir rev-parse --git-common-dir 2>$null) } catch { $gitCommonDirRaw = $null }
if (($LASTEXITCODE -and $LASTEXITCODE -ne 0) -or -not $gitCommonDirRaw) { exit 0 }
$gitCommonDirRaw = [string]$gitCommonDirRaw

if ([System.IO.Path]::IsPathRooted($gitCommonDirRaw)) {
    $gitCommonDirCandidate = $gitCommonDirRaw
} else {
    $gitCommonDirCandidate = Join-Path $projectDir $gitCommonDirRaw
}
$gitCommonDir = $null
try { $gitCommonDir = (Resolve-Path -LiteralPath $gitCommonDirCandidate -ErrorAction Stop).ProviderPath } catch { exit 0 }
if (-not $gitCommonDir) { exit 0 }

$lockRoot = Join-Path $gitCommonDir 'meridian-locks'
try { New-Item -ItemType Directory -Force -Path $lockRoot -ErrorAction Stop | Out-Null } catch { exit 0 }

$lockRelPath = ($relPath -replace '/', [System.IO.Path]::DirectorySeparatorChar) + '.lock'
$lockFile = Join-Path $lockRoot $lockRelPath
$lockFileDir = Split-Path -Parent $lockFile
try { New-Item -ItemType Directory -Force -Path $lockFileDir -ErrorAction Stop | Out-Null } catch { exit 0 }

$staleThresholdHours = 2  # mirrors meridian/db/locks.py _FILE_LOCK_TTL_HOURS
$nowUtc = [DateTime]::UtcNow
$lockJson = "{`"session_id`":`"$sessionId`",`"path`":`"$relPath`",`"tool`":`"$tool`",`"locked_at`":`"$($nowUtc.ToString('o'))`"}"

$acquired = $false
try {
    $fs = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($lockJson)
        $fs.Write($bytes, 0, $bytes.Length)
    } finally { $fs.Close() }
    $acquired = $true
} catch [System.IO.IOException] {
    $acquired = $false
} catch {
    exit 0  # unexpected I/O problem with lock bookkeeping -- never trap real work over it
}

if ($acquired) { exit 0 }

# Lock file already existed -- inspect its owner.
$existingOwner = $null
try {
    $existingRaw = Get-Content -LiteralPath $lockFile -Raw -ErrorAction Stop
    $existingObj = $existingRaw | ConvertFrom-Json -ErrorAction Stop
    $existingOwner = [string]$existingObj.session_id
} catch { $existingOwner = $null }

if ($existingOwner -and $existingOwner -eq $sessionId) {
    # Same session re-editing (or continuing to edit) its own file -- refresh
    # the lock's timestamp (extends its staleness TTL) and allow.
    # WriteAllBytes (not WriteAllText -- [System.Text.Encoding]::UTF8's
    # preamble adds a BOM that WriteAllText honors and GetBytes doesn't,
    # which would silently corrupt the JSON on every refresh while leaving
    # the initial CreateNew-path write above BOM-free) to match the encoding
    # used when the lock was first created.
    try { [System.IO.File]::WriteAllBytes($lockFile, [System.Text.Encoding]::UTF8.GetBytes($lockJson)) } catch { }
    exit 0
}

# Different (or unattributable) owner -- check staleness via file mtime before
# treating this as a real, live collision.
$isStale = $false
try {
    $mtime = (Get-Item -LiteralPath $lockFile -ErrorAction Stop).LastWriteTimeUtc
    if (([DateTime]::UtcNow - $mtime).TotalHours -ge $staleThresholdHours) { $isStale = $true }
} catch { $isStale = $true }  # can't stat it -- treat as unusable rather than trap forever

if ($isStale) {
    try { [System.IO.File]::WriteAllBytes($lockFile, [System.Text.Encoding]::UTF8.GetBytes($lockJson)) } catch { exit 0 }
    exit 0
}

# A live lock is held by a DIFFERENT session. Block it, matching this hook's
# own worktree-boundary fail-mode (hard exit-2 block naming the other owner).
$ownerDisplay = if ($existingOwner) { $existingOwner } else { '(unknown session)' }
[Console]::Error.WriteLine("Meridian worktree lock guard (71f597b7): ${tool} target '$filePath' (repo-relative '$relPath') is locked by another live session ($ownerDisplay) sharing this checkout's git-common-dir. Coordinate via request_hitl, wait for that session to finish, or -- only if you are certain it is dead -- remove the stale lock at '$lockFile' (locks self-expire after $staleThresholdHours hours of inactivity).")
exit 2
