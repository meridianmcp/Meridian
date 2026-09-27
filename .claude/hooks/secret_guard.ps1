# 14491654 -- PreToolUse secret-file guard (fail-closed on sensitive paths).
#
# Incident: Claude displayed raw .env contents (Stripe live key,
# MERIDIAN_ENCRYPTION_KEY, admin password, DB connection strings) in tool output
# via Read/Bash/Grep calls -- fully unredacted, no existing guard caught it.
#
# This hook fires on Read, Bash, PowerShell, Grep, and Glob tool calls and BLOCKS (exit 2,
# fail-closed) when the target file path matches a known-sensitive filename
# pattern (.env, *.pem, *.key, id_rsa*, secrets.*, meridian.toml, etc.).
#
# Implementation rationale: Claude Code's PostToolUse hooks receive
# {tool_name, tool_input} on stdin -- the REQUEST, not the response.  There
# is no hook mechanism to intercept and rewrite tool OUTPUT before it reaches
# model context.  PreToolUse IS able to block the call entirely (exit 2) before
# any file content is read, which is the correct fail-closed posture for this
# threat class.
#
# 55d48d69 fix round 1 (the launcher fix made this hook's exit 2 real for the first
# time, which exposed its false positives):
# - Source, script, test and template files are never credential files, whatever
#   their name says: secret_redaction.py, test_secret_redaction.py, refresh_token.py,
#   secret_guard.ps1, .env.example, secrets.env.example are all readable
#   ($SafeExtensions / $TemplateSuffixes). meridian.toml (live credentials per
#   AGENTS.md) is now sensitive; a Grep of it for project_id only is allowed.
# - Grep's real glob field is 'glob' (the old code checked 'include').
# - Shell checks are statement-anchored: only a real dump (bare printenv / env /
#   set / export / declare -p) or a reader verb (cat, head, grep, ...) naming a
#   credential FILE blocks. 'set -euo pipefail', 'export X=1 && ...', 'env X=1 cmd',
#   os.environ, import.meta.env and jq '.key' no longer block.
# - The owner kill switch (MERIDIAN_GUARD=off|advisory, guard.off / guard.advisory
#   in the guard dir, MERIDIAN_GUARD_DISABLE=secret_guard) covers this hook too;
#   advisory mode explains via additionalContext instead of blocking.
#
# Fail-open (exit 0) on any parse error -- never trap the executor on ambiguity
# EXCEPT for the specific sensitive-path match (which is fail-closed).
#
# Pure ASCII: PS 5.1 reads BOM-less UTF-8 as cp1252; non-ASCII bytes corrupt
# and break the parser. Keep this file ASCII-only.
#
# NOT hooks.ps1 (the token-rotation installer).
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

function Stop-Call([string]$Message) {
    if ($script:GuardMode -eq 'advisory') {
        $o = @{ hookSpecificOutput = @{ hookEventName = 'PreToolUse'; additionalContext = ('[advisory, not blocked] ' + $Message) } }
        [Console]::Out.Write(($o | ConvertTo-Json -Compress -Depth 4))
        exit 0
    }
    [Console]::Error.WriteLine($Message)
    exit 2
}

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
    '*_token', '*_token.*', 'token', 'token.*',
    'meridian.toml'
)
# A file with one of these final extensions is source/script/test code (or a
# notebook), never a credential store, whatever its name contains.
# Keep in sync with meridian/secret_redaction.py _SAFE_SOURCE_EXTENSIONS.
$SafeExtensions = @(
    'py', 'pyi', 'pyx', 'ipynb', 'ps1', 'psm1', 'psd1', 'sh', 'bash', 'zsh', 'fish', 'bat', 'cmd',
    'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'mts', 'cts', 'go', 'rs', 'java', 'kt', 'kts', 'scala',
    'rb', 'php', 'cs', 'fs', 'c', 'h', 'cc', 'cpp', 'cxx', 'hpp', 'swift', 'm', 'lua', 'r', 'jl',
    'dart', 'ex', 'exs', 'erl', 'hs', 'ml', 'clj', 'groovy', 'pl', 'pm', 'sql', 'vue', 'svelte', 'awk'
)
# ... and a template of a credential file carries no secret.
$TemplateSuffixes = @('example', 'sample', 'template', 'tmpl', 'dist')

function Test-SensitivePath {
    param([string]$Path)
    if (-not $Path) { return $false }
    # Extract basename, normalise separators first.
    $norm = $Path -replace '\\', '/'
    $base = $norm -replace '.*/', ''
    $baseLower = $base.ToLower()
    $dot = $baseLower.LastIndexOf('.')
    if ($dot -gt 0) {
        $ext = $baseLower.Substring($dot + 1)
        if ($SafeExtensions -contains $ext -or $TemplateSuffixes -contains $ext) { return $false }
    }
    foreach ($pat in $SensitivePatterns) {
        if ($baseLower -like $pat) { return $true }
    }
    return $false
}

# A credential FILE token inside a shell command: .env / .env.<x> (not a template),
# *.pem|key|p12|pfx|jks, id_rsa..., .netrc, .htpasswd, secrets.<yaml|json|toml|env>,
# meridian.toml, terraform.tfvars. Bounded on both sides by a non-name character.
$CredTok = '(?<![\w.-])(\.env(\.(?!(example|sample|template|tmpl|dist)\b)[\w-]+)?|[\w.-]+\.(pem|key|p12|pfx|jks|keystore|tfvars)|id_(rsa|dsa|ecdsa|ed25519)|\.?netrc|\.htpasswd|[\w.-]*secrets?\.(ya?ml|json|toml|env)|meridian\.toml|[\w.-]*credentials?\.(json|ya?ml|toml))(?![\w.-])'
# Reader verbs that print a file's content (the start of a statement or pipeline stage).
$BashReaders = '(cat|tac|head|tail|less|more|bat|nl|strings|xxd|od|hexdump|base64|grep|egrep|fgrep|rg|ag|awk|gawk|sed|cut|sort|uniq|type|jq|yq)'

# Bash command patterns that really dump the environment or read a credential file.
$BashDumpPatterns = @(
    # bare dumps: printenv / env / set / export / export -p / declare -p|-x with nothing else
    '(^|[;&|(]|&&|\|\|)\s*(printenv|env|set|export(\s+-p)?|declare\s+-[px]|typeset\s+-x|compgen\s+-e)\s*($|[;&|)])',
    # a reader verb naming a credential file anywhere in its own statement
    ('(^|[;&|(]|&&|\|\|)\s*(sudo\s+)?' + $BashReaders + '\s[^;&|]*' + $CredTok),
    # input redirection from a credential file
    ('<\s*[''"]?[^;&|''"\s]*' + $CredTok),
    # interpreters opening a credential file directly
    ('open\(\s*[''"][^''"]*' + $CredTok + '[''"]')
)

# PowerShell tool command patterns (55d48d69 widened the matcher to PowerShell).
# The Bash list above is NOT reused for PowerShell: -match is case-insensitive,
# so 'set' would block every Set-Location / Set-Content. Each pattern is
# anchored to the start of a statement (start, ; | & ( or an assignment).
$PsDumpPatterns = @(
    '(^|[;|&(=])\s*printenv\s*($|[;|&)])',
    ('(^|[;|&(=])\s*(cat|gc|type|Get-Content|sls|Select-String|more)\s[^;|&]*' + $CredTok),
    '(^|[;|&(=])\s*(gci|ls|dir|Get-ChildItem|gi|Get-Item)\s+(-Path\s+)?env:\\?\s*($|[;|&)])',
    '\[(System\.)?Environment\]::GetEnvironmentVariables\(\s*\)',
    ('\[(System\.)?IO\.File\]::Read\w*\(\s*[''"][^''"]*' + $CredTok)
)

function Test-Any([string]$Command, $Patterns) {
    if (-not $Command) { return $false }
    foreach ($pat in $Patterns) {
        if ($Command -match $pat) { return $true }
    }
    return $false
}

# 14491654 write-path follow-up -- the write-side bypass: Read/Grep of a credential file is blocked by
# Test-SensitivePath above, but nothing stopped Write/Edit/MultiEdit from putting a
# fresh secret VALUE into one (meridian.toml was not protected at all on this side).
# Only the VALUE is checked here -- ordinary content (e.g. project_id = "...") in a
# sensitive file must still be writable, matching the "read meridian.toml [project]
# key only" convention documented elsewhere. Mirrors the SECRET_PATTERNS /
# dotenv-credential shapes in meridian/secret_redaction.py, kept independent
# (no python dependency) so the hook stays a pure shell/PowerShell script.
$SecretTokenPatterns = @(
    'AKIA[0-9A-Z]{16}',
    'sk_live_[A-Za-z0-9]{24,}',
    'sk_meridian_[A-Za-z0-9_-]{16,}',
    'gh[pousr]_[A-Za-z0-9]{36,}',
    'github_pat_[A-Za-z0-9_]{40,}',
    'xox[baprs]-[A-Za-z0-9][A-Za-z0-9_-]{5,}',
    'sk-[A-Za-z0-9-]{20,}',
    '-----BEGIN( [A-Z ]+)?PRIVATE KEY-----'
)
# dotenv-style KEY=value assignment naming a credential (case-insensitive, mirrors
# secret_redaction.py's dotenv-credential pattern). Requires a real value (>=6 chars).
$SecretAssignmentPattern = '(?m)^[ \t]*[A-Za-z0-9_]*(SECRET|KEY|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Za-z0-9_]*[ \t]*=[ \t]*[^\s"'']{6,}'

function Test-SecretValue([string]$Text) {
    if (-not $Text) { return $false }
    foreach ($pat in $SecretTokenPatterns) {
        if ($Text -cmatch $pat) { return $true }
    }
    if ($Text -match $SecretAssignmentPattern) { return $true }
    return $false
}

# Read stdin.
try { $raw = [Console]::In.ReadToEnd() } catch { exit 0 }
if (-not $raw) { exit 0 }

try { $payload = $raw | ConvertFrom-Json } catch { exit 0 }
if ($null -eq $payload) { exit 0 }

$tool = [string]$payload.tool_name
if (-not $tool) { exit 0 }

# Only intercept file-reading tools, the shell tools (Bash, PowerShell), and the
# file-writing tools (Write, Edit, MultiEdit -- guards against a secret VALUE
# being written into a credential file such as meridian.toml or .env).
if ($tool -notin @('Read', 'Bash', 'PowerShell', 'Grep', 'Glob', 'Write', 'Edit', 'MultiEdit')) { exit 0 }

$script:GuardMode = Get-GuardMode 'secret_guard'
if ($script:GuardMode -eq 'off') { exit 0 }

# Not '$input': that is PowerShell's automatic pipeline-input variable, and a
# script that mentions it at all has its redirected stdin drained into it
# under 'powershell -File', so [Console]::In above would read nothing.
$toolInput = $payload.tool_input

# --- Read ---
if ($tool -eq 'Read') {
    $filePath = if ($toolInput) { [string]$toolInput.file_path } else { '' }
    if (Test-SensitivePath $filePath) {
        Stop-Call ("Meridian secret guard (14491654): BLOCKED Read of sensitive file '$filePath'. " +
            "Reading .env, key, pem, meridian.toml and other credential files exposes secrets in model context. " +
            "If you genuinely need this value, use a secrets manager or ask the human operator " +
            "to provide only the specific value needed (not the whole file).")
    }
}

# --- Grep / Glob ---
if ($tool -eq 'Grep' -or $tool -eq 'Glob') {
    $checkPaths = @()
    if ($toolInput) {
        if ($toolInput.path) { $checkPaths += [string]$toolInput.path }
        if ($toolInput.glob) { $checkPaths += [string]$toolInput.glob }
        if ($toolInput.include) { $checkPaths += [string]$toolInput.include }
    }
    $pat = if ($toolInput) { [string]$toolInput.pattern } else { '' }
    foreach ($p in $checkPaths) {
        $pn = ($p -replace '\\', '/') -replace '.*/', ''
        # 'read that key only': a Grep of meridian.toml for project_id shows just that line
        if ($pn.ToLower() -eq 'meridian.toml' -and $tool -eq 'Grep' -and $pat.Trim() -match '^\^?\s*project_id[\s\\*=]*$') { continue }
        if (Test-SensitivePath $p) {
            Stop-Call ("Meridian secret guard (14491654): BLOCKED ${tool} targeting sensitive path '$p'. " +
                "Searching inside credential files exposes secrets in model context.")
        }
    }
}

# --- Bash ---
if ($tool -eq 'Bash') {
    $cmd = if ($toolInput) { [string]$toolInput.command } else { '' }
    if (Test-Any $cmd $BashDumpPatterns) {
        Stop-Call ("Meridian secret guard (14491654): BLOCKED Bash command that appears to dump " +
            "environment variables or read credential files: '$($cmd.Substring(0, [Math]::Min(80, $cmd.Length)))...'. " +
            "Use only the specific env var you need (e.g. echo `$SOME_SAFE_VAR) rather than " +
            "printing all environment variables or cat-ing credential files.")
    }
}

# --- PowerShell ---
if ($tool -eq 'PowerShell') {
    $cmd = if ($toolInput) { [string]$toolInput.command } else { '' }
    if (Test-Any $cmd $PsDumpPatterns) {
        Stop-Call ("Meridian secret guard (14491654): BLOCKED PowerShell command that appears to dump " +
            "environment variables or read credential files: '$($cmd.Substring(0, [Math]::Min(80, $cmd.Length)))...'. " +
            "Read only the specific env var you need (e.g. `$env:SOME_SAFE_VAR) rather than " +
            "listing the env: drive or reading credential files.")
    }
}

# --- Write / Edit / MultiEdit (14491654 write-path follow-up) ---
if ($tool -eq 'Write' -or $tool -eq 'Edit' -or $tool -eq 'MultiEdit') {
    $filePath = if ($toolInput) { [string]$toolInput.file_path } else { '' }
    if (Test-SensitivePath $filePath) {
        $content = ''
        if ($tool -eq 'Write') {
            $content = [string]$toolInput.content
        } elseif ($tool -eq 'Edit') {
            $content = [string]$toolInput.new_string
        } elseif ($tool -eq 'MultiEdit') {
            if ($toolInput.edits) {
                $parts = @()
                foreach ($e in $toolInput.edits) { $parts += [string]$e.new_string }
                $content = $parts -join "`n"
            }
        }
        if (Test-SecretValue $content) {
            Stop-Call ("Meridian secret guard (14491654): BLOCKED $tool of sensitive file '$filePath' " +
                "-- the new content looks like it contains a real credential VALUE. " +
                "Writing secrets into .env, meridian.toml or other credential files exposes them to " +
                "source control and model context. Ask the human operator to set the value directly.")
        }
    }
}

exit 0
