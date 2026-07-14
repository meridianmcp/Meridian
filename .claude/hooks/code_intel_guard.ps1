# aeba8a80 -- PreToolUse code-intel guard (structural, not text).
#
# Prose guidance in DEFAULT_AGENT_INSTRUCTIONS has been strengthened to v10 and
# has still not held: a live session tonight used raw grep exclusively rather than
# code-intel tools. This hook fires on Grep and Glob tool calls and, ONLY when this
# project has a populated code-intel index available (code_intel_enabled == 1 in the
# project settings), blocks (exit 2) and redirects to the code-intel tools instead.
# If code-intel is not enabled, or the status check fails for ANY reason, FAILS OPEN
# (exit 0) -- never block a session that has nothing to redirect to.
#
# Mirrors the structural pattern of hitl_guard.ps1 (PreToolUse, exit 2 to block,
# tolerant JSON parsing) and sprint_guard.ps1 (curl the live Meridian server, fail
# open on any parse/network error).
# NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'
$ProjectId = "5787cc92-ba7d-4788-b17c-28ab7938b839"
$MeridianUrl = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { "http://localhost:7878" }
try { $payload = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $payload) { exit 0 }
try { $tool = ($payload | ConvertFrom-Json).tool_name } catch { exit 0 }
if (-not $tool) { exit 0 }
# Only intercept Grep and Glob tool calls.
if ($tool -ne 'Grep' -and $tool -ne 'Glob') { exit 0 }
# Ask the live Meridian server if code-intel is enabled for this project.
# Fail open on any web/parse error -- never block when we can't confirm an index.
try {
    $resp = Invoke-RestMethod -Uri "$MeridianUrl/projects/$ProjectId/settings" -Method Get -TimeoutSec 5
} catch { exit 0 }
if (-not $resp) { exit 0 }
$ciVal = $resp.code_intel_enabled
if ($ciVal -ne $null -and [int]$ciVal -eq 1) {
    [Console]::Error.WriteLine("Meridian code-intel guard (aeba8a80): this project has a code-intel index. Use code-intel tools INSTEAD of ${tool}:
  - find_symbol / extractor__find_symbol        -- exact symbol lookup (fastest, most accurate)
  - search_graph / codebase__search_graph       -- structural graph queries (callers, callees, paths)
  - search_code / search_code_semantic          -- fuzzy / conceptual queries across the codebase
  - find_referencing_symbols                    -- find all callers of a function
  - get_code_snippet / get_architecture         -- retrieve file sections or the whole-project architecture
Raw grep/glob is a LAST RESORT for code search (443aa32a) -- the above tools are faster, use far fewer tokens, and don't miss symbol aliases. Fall back to ${tool} ONLY for non-symbol content (log output, data files, config values) or after code-intel tools confirm a file path.")
    exit 2
}
exit 0
