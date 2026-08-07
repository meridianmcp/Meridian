# 23f21820 -- PreToolUse package-install verification guard.
#
# Fires on Bash tool calls and inspects the command string for pip/npm/uvx
# install patterns. Packages already in the known-good allowlist (seeded from
# pyproject.toml + common dev tooling) pass immediately. For anything else, this
# hook calls the local Meridian /pkg-guard/check endpoint which performs a live
# registry lookup (PyPI JSON API or npm registry).
#
# Gate behaviour:
#   allow   -- package is allowlisted or verified with no warnings -> exit 0
#   warn    -- suspicious signals (very new, not found, network error) -> exit 1
#              Claude Code surfaces exit-1 stderr to the model but allows the
#              tool call to proceed; the model should then call request_hitl.
#   NOTE: we use exit 1 (warn/advisory) not exit 2 (hard block) because the
#   fail-open philosophy means a network hiccup must never permanently wedge a
#   session. The advisory text in stderr is strong enough to route through HITL.
#
# Fails OPEN (exit 0) on ANY parse/network/logic error so a structural defect
# never blocks legitimate work.
#
# Pure ASCII: PS 5.1 reads BOM-less UTF-8 as cp1252.
# NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'
$MeridianUrl = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { 'http://localhost:7878' }

# Read stdin.
try { $raw = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $raw) { exit 0 }

# Parse JSON payload.
try { $payload = $raw | ConvertFrom-Json } catch { exit 0 }
if ($null -eq $payload) { exit 0 }

# Only intercept Bash tool calls.
$tool = [string]$payload.tool_name
if ($tool -ne 'Bash') { exit 0 }

# Extract the command string.
$cmd = ''
try { $cmd = [string]$payload.tool_input.command } catch { exit 0 }
if (-not $cmd) { exit 0 }

# Quick pre-filter: skip if no install keyword present (cheap regex before HTTP call).
if ($cmd -notmatch '(?i)\binstall\b|\badd\b') { exit 0 }
if ($cmd -notmatch '(?i)\bpip[23]?\b|\bpython\s+-m\s+pip\b|\buv\s+pip\b|\bnpm\b|\byarn\b|\bpnpm\b|\bbun\b|\buvx\b') { exit 0 }

# Call the local Meridian endpoint.
$body = @{ command = $cmd } | ConvertTo-Json -Compress
try {
    $resp = Invoke-RestMethod `
        -Method POST `
        -Uri "$MeridianUrl/pkg-guard/check" `
        -ContentType 'application/json' `
        -Body $body `
        -TimeoutSec 12
} catch {
    # Network failure -- fail open with a silent advisory (not a block).
    [Console]::Error.WriteLine("Meridian pkg guard (23f21820): registry check unavailable (Meridian server not reachable). Failing open -- proceed manually with caution.")
    exit 0
}

if ($null -eq $resp) { exit 0 }

$action = [string]$resp.action
$message = [string]$resp.message

if ($action -eq 'warn') {
    [Console]::Error.WriteLine($message)
    # exit 1 = advisory warning (not a hard block); Claude Code shows stderr to the model.
    exit 1
}

exit 0
