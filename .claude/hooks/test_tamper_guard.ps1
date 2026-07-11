# 43539c70 - PostToolUse test-tamper guard (structural). Windows partner of
# test_tamper_guard.sh. ASCII-only on purpose: PS 5.1 reads BOM-less UTF-8 as
# cp1252, so any non-ASCII byte would corrupt and break the parser.
#
# Named failure mode the other hooks DO NOT catch: an agent makes a FAILING test
# pass by editing the test (loosening an assertion / deleting a case) instead of
# fixing the code under test. Distinct from and complementary to the Stop hook
# (sprint_guard.*) and the PreToolUse protected-files / HITL guards (hitl_guard.*).
# None of those look at WHAT changed in a test file.
#
# Contract (Claude Code PostToolUse): JSON on stdin with {tool_name, tool_input}.
# tool_input.file_path is the edited path. We FLAG (warn to stderr) whenever a
# test file is edited, reminding the human/agent to confirm the change fixes the
# CODE, not the assertion. Default posture is NON-BLOCKING: exit 0 always, UNLESS
# MERIDIAN_TEST_TAMPER_BLOCK=1 is set, in which case a flagged edit exits 2 (block).
#
# Exemption: if the current in-progress sprint item explicitly calls for
# test/coverage work, stay SILENT (legitimate feature work adds tests). That
# signal is fetched best-effort from Meridian; any failure just falls back to
# flagging. Fails OPEN on every parse / network error.
#
# This is NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'

$ProjectId = '5787cc92-ba7d-4788-b17c-28ab7938b839'
$Url = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { 'http://localhost:7878' }

try { $raw = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $raw) { exit 0 }
try { $payload = $raw | ConvertFrom-Json } catch { exit 0 }
if ($null -eq $payload) { exit 0 }

# Only act on file-writing tools.
$tool = [string]$payload.tool_name
if ($tool -notin @('Edit', 'Write', 'MultiEdit', 'NotebookEdit')) { exit 0 }

# tool_input.file_path holds the edited path; fail open if absent.
$path = $null
if ($payload.tool_input) { $path = [string]$payload.tool_input.file_path }
if (-not $path) { exit 0 }

# Normalise separators so the match works for posix or windows paths.
$norm = $path -replace '\\', '/'
$base = $norm -replace '.*/', ''

$isTest = $false
if ($base -match '^test_.*\.py$' -or
    $base -match '_test\.py$' -or
    $base -match '\.(test|spec)\.(ts|tsx|js|jsx)$') {
    $isTest = $true
}
if ($norm -match '(^|/)(tests|__tests__)/') {
    $isTest = $true
}
if (-not $isTest) { exit 0 }

# Exemption: in-progress sprint item explicitly calls for test/coverage work.
# Best-effort; any failure leaves it un-exempt (we still flag - safe).
$exempt = $false
try {
    $r = Invoke-RestMethod -Method GET -TimeoutSec 3 `
        -Uri "$Url/projects/$ProjectId/sprint/test_coverage_expected"
    if ($r -and $r.test_coverage_expected -eq $true) { $exempt = $true }
} catch { $exempt = $false }
if ($exempt) { exit 0 }

$msg = "Meridian test-tamper guard (43539c70): '$base' is a TEST file. If this " +
    "edit makes a failing test pass by changing the assertion/expectation rather " +
    "than fixing the code under test, that is the test-tampering anti-pattern - " +
    "confirm the change fixes the CODE, not the test. (Legitimate new/updated " +
    "coverage for a sprint item that calls for it is fine; set " +
    "MERIDIAN_TEST_TAMPER_BLOCK=1 to make this a hard block.)"

[Console]::Error.WriteLine($msg)

# Default: non-blocking flag (exit 0). Opt-in hard block via env var.
if ($env:MERIDIAN_TEST_TAMPER_BLOCK -eq '1') { exit 2 }
exit 0
