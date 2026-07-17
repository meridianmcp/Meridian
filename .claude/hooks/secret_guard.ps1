# 14491654 -- PreToolUse secret-file guard (fail-closed on sensitive paths).
#
# Incident: Claude displayed raw .env contents (Stripe live key,
# MERIDIAN_ENCRYPTION_KEY, admin password, DB connection strings) in tool output
# via Read/Bash/Grep calls -- fully unredacted, no existing guard caught it.
#
# This hook fires on Read, Bash, Grep, and Glob tool calls and BLOCKS (exit 2,
# fail-closed) when the target file path matches a known-sensitive filename
# pattern (.env, *.pem, *.key, id_rsa*, secrets.*, etc.).
#
# Implementation rationale: Claude Code's PostToolUse hooks receive
# {tool_name, tool_input} on stdin -- the REQUEST, not the response.  There
# is no hook mechanism to intercept and rewrite tool OUTPUT before it reaches
# model context.  PreToolUse IS able to block the call entirely (exit 2) before
# any file content is read, which is the correct fail-closed posture for this
# threat class.
#
# Bash commands: we inspect the command string for common env-dump patterns
# (cat .env, printenv, env, set) and flag those.  We cannot catch every possible
# Bash invocation, so Bash coverage is best-effort; Read/Grep/Glob are authoritative.
#
# Fail-open (exit 0) on any parse/network error -- never trap the executor on
# ambiguity EXCEPT for the specific sensitive-path match (which is fail-closed).
#
# Pure ASCII: PS 5.1 reads BOM-less UTF-8 as cp1252; non-ASCII bytes corrupt
# and break the parser. Keep this file ASCII-only.
#
# NOT hooks.ps1 (the token-rotation installer).
$ErrorActionPreference = 'SilentlyContinue'

# Sensitive basename patterns (fnmatch-style, case-insensitive).
# Keep in sync with meridian/secret_redaction.py _SENSITIVE_BASENAME_PATTERNS.
$SensitivePatterns = @(
    '.env', '.env.*', '*.env',
    '*.key', '*.pem', '*.p12', '*.pfx', '*.jks', '*.keystore',
    '*.crt', '*.cer', '*.der',
    'id_rsa', 'id_rsa.*', 'id_dsa', 'id_dsa.*',
    'id_ecdsa', 'id_ecdsa.*', 'id_ed25519', 'id_ed25519.*',
    '*secret*', '*secrets*', '*credential*', '*credentials*',
    '*password*', '*passwd*',
    '*.vault', 'vault.yaml', 'vault.yml',
    '*.tfvars', 'terraform.tfstate', 'terraform.tfstate.backup',
    '.netrc', 'netrc', '*.htpasswd',
    '*apikey*', '*api_key*', '*auth_key*', '*access_key*', '*private_key*',
    '*_token', '*_token.*', 'token', 'token.*'
)

# Bash command patterns that are likely env dumps.
$BashDumpPatterns = @(
    '^\s*cat\s+.*\.env',
    '^\s*printenv\b',
    '^\s*env\b',
    '^\s*set\b',
    '^\s*export\b',
    'cat\s+.*\.env',
    '\bprintenv\b',
    '\bcat\b.*\bpasswd\b',
    '\bcat\b.*\.key\b',
    '\bcat\b.*\.pem\b',
    '\bcat\b.*id_rsa\b'
)

function Test-SensitivePath {
    param([string]$Path)
    if (-not $Path) { return $false }
    # Extract basename, normalise separators first.
    $norm = $Path -replace '\\', '/'
    $base = $norm -replace '.*/', ''
    $baseLower = $base.ToLower()
    foreach ($pat in $SensitivePatterns) {
        if ($baseLower -like $pat) { return $true }
    }
    return $false
}

function Test-SensitiveBashCommand {
    param([string]$Command)
    if (-not $Command) { return $false }
    foreach ($pat in $BashDumpPatterns) {
        if ($Command -match $pat) { return $true }
    }
    return $false
}

# Read stdin.
try { $raw = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $raw) { exit 0 }

try { $payload = $raw | ConvertFrom-Json } catch { exit 0 }
if ($null -eq $payload) { exit 0 }

$tool = [string]$payload.tool_name
if (-not $tool) { exit 0 }

# Only intercept file-reading tools and Bash.
if ($tool -notin @('Read', 'Bash', 'Grep', 'Glob')) { exit 0 }

$input = $payload.tool_input

# --- Read ---
if ($tool -eq 'Read') {
    $filePath = if ($input) { [string]$input.file_path } else { '' }
    if (Test-SensitivePath $filePath) {
        [Console]::Error.WriteLine(
            "Meridian secret guard (14491654): BLOCKED Read of sensitive file '$filePath'. " +
            "Reading .env, key, pem, and other credential files exposes secrets in model context. " +
            "If you genuinely need this value, use a secrets manager or ask the human operator " +
            "to provide only the specific value needed (not the whole file)."
        )
        exit 2
    }
}

# --- Grep / Glob ---
if ($tool -eq 'Grep' -or $tool -eq 'Glob') {
    # Check both 'path' and 'pattern' fields -- Grep uses 'path', Glob uses 'path'.
    $checkPaths = @()
    if ($input) {
        if ($input.path) { $checkPaths += [string]$input.path }
        if ($input.include) { $checkPaths += [string]$input.include }
    }
    foreach ($p in $checkPaths) {
        if (Test-SensitivePath $p) {
            [Console]::Error.WriteLine(
                "Meridian secret guard (14491654): BLOCKED ${tool} targeting sensitive path '$p'. " +
                "Searching inside credential files exposes secrets in model context."
            )
            exit 2
        }
    }
}

# --- Bash ---
if ($tool -eq 'Bash') {
    $cmd = if ($input) { [string]$input.command } else { '' }
    if (Test-SensitiveBashCommand $cmd) {
        [Console]::Error.WriteLine(
            "Meridian secret guard (14491654): BLOCKED Bash command that appears to dump " +
            "environment variables or read credential files: '$($cmd.Substring(0, [Math]::Min(80, $cmd.Length)))...'. " +
            "Use only the specific env var you need (e.g. echo `$SOME_SAFE_VAR) rather than " +
            "printing all environment variables or cat-ing credential files."
        )
        exit 2
    }
}

exit 0
