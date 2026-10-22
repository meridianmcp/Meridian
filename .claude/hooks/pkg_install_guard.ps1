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
# 55d48d69 fix round 1: a Meridian server that is down (the default state) used to
# cost 4-5 s per matching command (Invoke-RestMethod tries ::1 then 127.0.0.1 with
# long connect timeouts, and the pre-filter also matches 'git add ... && npm run
# build'). The server is now probed with a 300 ms TCP connect first; no listener =>
# fail open at once. The owner kill switch (MERIDIAN_GUARD=off, guard.off,
# MERIDIAN_GUARD_DISABLE=pkg_install_guard) covers this hook too.
#
# Pure ASCII: PS 5.1 reads BOM-less UTF-8 as cp1252.
# NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'
$MeridianUrl = if ($env:MERIDIAN_URL) { $env:MERIDIAN_URL } else { 'http://localhost:7878' }

function Test-GuardOff([string]$HookName) {
    if (([string]$env:MERIDIAN_GUARD).Trim().ToLowerInvariant() -eq 'off') { return $true }
    foreach ($tok in ([string]$env:MERIDIAN_GUARD_DISABLE -split '[\s,;]+')) {
        if ($tok.Trim().ToLowerInvariant() -eq $HookName) { return $true }
    }
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } elseif ($env:USERPROFILE) { [System.IO.Path]::Combine($env:USERPROFILE, 'AppData', 'Local') } else { $null }
    return ($base -and [System.IO.File]::Exists([System.IO.Path]::Combine($base, 'meridian', 'guard', 'guard.off')))
}

# Resolve MERIDIAN_URL to a base URL whose TCP port answers within the timeout, or
# $null. 'localhost' is tried as 127.0.0.1 then ::1 (the literal address is kept).
function Resolve-LiveUrl([string]$Url, [int]$TimeoutMs = 300) {
    try { $u = [System.Uri]$Url } catch { return $null }
    $hostName = $u.Host
    $cands = if ($hostName -eq 'localhost') { @('127.0.0.1', '::1') } else { @($hostName) }
    foreach ($h in $cands) {
        $fam = if ($h -eq '::1' -or $h.StartsWith('[')) { [System.Net.Sockets.AddressFamily]::InterNetworkV6 } else { [System.Net.Sockets.AddressFamily]::InterNetwork }
        $c = $null
        try {
            $c = [System.Net.Sockets.TcpClient]::new($fam)
            $ar = $c.BeginConnect($h.Trim('[', ']'), $u.Port, $null, $null)
            if ($ar.AsyncWaitHandle.WaitOne($TimeoutMs) -and $c.Connected) {
                $c.EndConnect($ar)
                if ($hostName -ne 'localhost') { return $Url.TrimEnd('/') }
                $lit = if ($h -eq '::1') { '[::1]' } else { $h }
                return ($u.Scheme + '://' + $lit + ':' + $u.Port + $u.AbsolutePath.TrimEnd('/'))
            }
        } catch { } finally { if ($c) { $c.Close() } }
    }
    return $null
}

# Read stdin.
try { $raw = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $raw) { exit 0 }

# Parse JSON payload.
try { $payload = $raw | ConvertFrom-Json } catch { exit 0 }
if ($null -eq $payload) { exit 0 }

# Only intercept shell tool calls (55d48d69: Bash and PowerShell run the same
# install commands).
$tool = [string]$payload.tool_name
if ($tool -ne 'Bash' -and $tool -ne 'PowerShell') { exit 0 }

# Extract the command string.
$cmd = ''
try { $cmd = [string]$payload.tool_input.command } catch { exit 0 }
if (-not $cmd) { exit 0 }

if (Test-GuardOff 'pkg_install_guard') { exit 0 }

# Quick pre-filter: skip if no install keyword present (cheap regex before HTTP call).
if ($cmd -notmatch '(?i)\binstall\b|\badd\b') { exit 0 }
if ($cmd -notmatch '(?i)\bpip[23]?\b|\bpython\s+-m\s+pip\b|\buv\s+pip\b|\bnpm\b|\byarn\b|\bpnpm\b|\bbun\b|\buvx\b') { exit 0 }

# Call the local Meridian endpoint -- only when something is listening.
$live = Resolve-LiveUrl $MeridianUrl
if (-not $live) {
    [Console]::Error.WriteLine("Meridian pkg guard (23f21820): registry check unavailable (Meridian server not reachable). Failing open -- proceed manually with caution.")
    exit 0
}
$MeridianUrl = $live
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
