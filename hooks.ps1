& {
# hooks.ps1 - Meridian Connect (Windows / PowerShell)
# Run: irm https://usemeridian.us/hooks.ps1 | iex

param([string]$NonInteractive)

function Get-Arg {
    param([string]$Name, [string]$Default = '')
    $i = $args.IndexOf("--$Name")
    if ($i -ge 0 -and $i + 1 -lt $args.Count) { return $args[$i + 1] }
    return $Default
}

function Test-ServerHealth {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch { return $false }
}

function Get-MeResponse {
    param([string]$Url, [string]$Token)
    try {
        $r = Invoke-RestMethod -Uri "$Url/auth/me" -Headers @{ Authorization = "Bearer $Token" } -TimeoutSec 5 -ErrorAction Stop
        return $r
    } catch { return $null }
}

# ---- Step 1: Determine Meridian URL ----------------------------------------------
Write-Host ""
Write-Host "Where is Meridian running?"
Write-Host "  [1] usemeridian.us -- hosted (recommended, press Enter)"
Write-Host "  [2] localhost:7878 -- self-hosted"
Write-Host "  [3] Other URL"
$urlChoice = Read-Host "Choice [1]"
switch ($urlChoice) {
    '2'   { $MeridianUrl = 'http://localhost:7878' }
    '3'   { $MeridianUrl = (Read-Host 'Enter URL (no trailing slash)').TrimEnd('/') }
    default { $MeridianUrl = 'https://usemeridian.us' }
}
$isLocal = $MeridianUrl -match 'localhost'

Write-Host "Checking $MeridianUrl ..."
if (-not (Test-ServerHealth -Url $MeridianUrl)) {
    Write-Host "  Error: Cannot reach $MeridianUrl/health -- is the server running?" -ForegroundColor Red
    return
}
Write-Host "  OK server is reachable" -ForegroundColor Green

# ---- Step 2: Authenticate --------------------------------------------------------
$Token = ''
$AuthUser = $null

# Try to find existing token from ps1 script comment
$HooksDir     = Join-Path $HOME '.claude\hooks'
$startPsPath  = Join-Path $HooksDir 'meridian-start.ps1'
if (Test-Path $startPsPath) {
    try {
        $pscontent = [System.IO.File]::ReadAllText($startPsPath)
        if ($pscontent -match '(?:Bearer |MERIDIAN_TOKEN: )(sk_meridian_[A-Za-z0-9_\-]+)') {
            $candidate = $Matches[1]
            $check = Get-MeResponse -Url $MeridianUrl -Token $candidate
            if ($null -ne $check) {
                $Token = $candidate
                $AuthUser = $check
                Write-Host "  Found existing API key in hooks script -- authenticated as: $($check.email)" -ForegroundColor Green
            }
        }
    } catch {}
}

# Fall back to browser auth if no valid token found
if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Host "Opening browser to authenticate..."
    $authUrl = "$MeridianUrl/auth/install-token"
    try { Start-Process $authUrl } catch {}
    $pastedToken = Read-Host "Paste the token shown in your browser"
    $pastedToken = $pastedToken.Trim()
    Write-Host "Validating token..."
    $check = Get-MeResponse -Url $MeridianUrl -Token $pastedToken
    if ($null -eq $check) {
        Write-Host "  Error: token is invalid or expired. Re-run and try again." -ForegroundColor Red
        return
    }
    $Token = $pastedToken
    $AuthUser = $check
    Write-Host "  Authenticated as: $($check.email)" -ForegroundColor Green
}

# ---- Step 3: Detect Claude Code / Codex ------------------------------------------
$ClaudeSettingsPath = Join-Path $HOME '.claude\settings.json'
$CodexDir           = Join-Path $HOME '.codex'
$CodexConfigPath    = Join-Path $CodexDir 'config.toml'
$ClaudeDetected     = Test-Path $ClaudeSettingsPath
$CodexDetected      = Test-Path $CodexDir

# ---- Step 4: Write hook scripts ---------------------------------------------------
$null = New-Item -ItemType Directory -Force $HooksDir
$enc  = New-Object System.Text.UTF8Encoding $false

$startContent = @'
# MERIDIAN_TOKEN: __TOKEN__
# Meridian session-start hook -- self-healing token with no-.env fallback
$fallback = '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'

function Get-MeridianToken {
    $self = "__HOOKDIR__\meridian-start.ps1"
    try {
        $c = [System.IO.File]::ReadAllText($self)
        if ($c -match '(?:Bearer |MERIDIAN_TOKEN: )(sk_meridian_[A-Za-z0-9_\-]+)') { return $Matches[1] }
    } catch {}
    try {
        $cc = [System.IO.File]::ReadAllText("$env:USERPROFILE\.codex\config.toml")
        if ($cc -match 'Bearer (sk_meridian_[A-Za-z0-9_\-]+)') { return $Matches[1] }
    } catch {}
    return $null
}

function Refresh-Token {
    $envPaths = @("$env:USERPROFILE\Documents\Meridian\repository\.env")
    foreach ($ep in $envPaths) {
        if (Test-Path $ep) {
            $line = Get-Content $ep | Where-Object { $_ -match "^MERIDIAN_API_SECRET_KEY=(.+)" }
            if ($line) {
                $installKey = $line -replace "MERIDIAN_API_SECRET_KEY=",""
                try {
                    $r = Invoke-RestMethod -Uri "__URL__/auth/tokens" -Method POST -Headers @{Authorization="Bearer $installKey";"Content-Type"="application/json"} -Body '{"label":"hooks-installer"}' -TimeoutSec 5
                    if ($r.token) {
                        $oldTok = Get-MeridianToken
                        foreach ($f in @("__HOOKDIR__\meridian-start.ps1","__HOOKDIR__\meridian-stop.ps1","$env:USERPROFILE\.codex\config.toml")) {
                            if (Test-Path $f) {
                                $fc = [System.IO.File]::ReadAllText($f)
                                if ($oldTok) { $fc = $fc -replace [regex]::Escape($oldTok), $r.token }
                                else { $fc = $fc -replace "sk_meridian_[A-Za-z0-9_\-]+", $r.token }
                                [System.IO.File]::WriteAllText($f, $fc)
                            }
                        }
                        return $r.token
                    }
                } catch {}
            }
        }
    }
    return $null
}

function Invoke-Hook($token) {
    $cwd = (Get-Location).Path -replace "\\","/"
    $h = $env:COMPUTERNAME
    $b = '{"cwd":"' + $cwd + '","hostname":"' + $h + '"}'
    try {
        $r = (Invoke-WebRequest -Method POST -Uri "__URL__/hooks/session-start" -Headers @{Authorization="Bearer $token"} -ContentType 'application/json' -Body $b -UseBasicParsing -TimeoutSec 5).Content
        if ($r -and $r.Contains("hookSpecificOutput")) { return $r }
    } catch {}
    return $null
}

$tok = Get-MeridianToken
$result = $null
if ($tok) {
    try {
        $null = Invoke-RestMethod -Uri "__URL__/auth/me" -Headers @{Authorization="Bearer $tok"} -TimeoutSec 3
        $result = Invoke-Hook $tok
    } catch {
        $newTok = Refresh-Token
        if ($newTok) { $result = Invoke-Hook $newTok }
    }
} else {
    $newTok = Refresh-Token
    if ($newTok) { $result = Invoke-Hook $newTok }
}
if ($result) { $result } else { $fallback }
'@

$stopContent = @'
# MERIDIAN_TOKEN: __TOKEN__
$h = $env:COMPUTERNAME
$b = '{"hostname":"' + $h + '"}'
try { Invoke-WebRequest -Method POST -Uri "__URL__/hooks/stop" -Headers @{Authorization="Bearer __TOKEN__"} -ContentType 'application/json' -Body $b -UseBasicParsing -TimeoutSec 5 | Out-Null } catch {}
'@

$sp = Join-Path $HooksDir 'meridian-start.ps1'
$tp = Join-Path $HooksDir 'meridian-stop.ps1'

$startContent = $startContent.Replace('__URL__', $MeridianUrl).Replace('__TOKEN__', $Token).Replace('__HOOKDIR__', $HooksDir.Replace('\', '\\'))
$stopContent  = $stopContent.Replace('__URL__', $MeridianUrl).Replace('__TOKEN__', $Token)

[System.IO.File]::WriteAllText($sp, $startContent, $enc)
[System.IO.File]::WriteAllText($tp, $stopContent,  $enc)

$startCmd = "& `"$sp`""
$stopCmd  = "& `"$tp`""

# ---- Step 5: Handle existing hooks -----------------------------------------------
$ExistingHooks = $false
$SkipInstall = $false
if ($ClaudeDetected -and (Test-Path $ClaudeSettingsPath)) {
    try {
        $existing = Get-Content $ClaudeSettingsPath -Raw | ConvertFrom-Json
        if ($existing.hooks -and ($existing.hooks.SessionStart -or $existing.hooks.Stop)) {
            $ExistingHooks = $true
        }
    } catch {}
}

if ($ExistingHooks) {
    Write-Host ""
    Write-Host "Existing Meridian hooks detected." -ForegroundColor Yellow
    $tokenValid = $null -ne (Get-MeResponse -Url $MeridianUrl -Token $Token)
    if ($tokenValid) {
        Write-Host "  Token is valid -- hooks are working." -ForegroundColor Green
        $choice = Read-Host "  (S)kip -- leave as-is / (U)pdate format / (R)egenerate key [S/u/r]"
        if ($choice -match '^[Uu]') {
            Write-Host "  Updating hooks..."
        } elseif ($choice -match '^[Rr]') {
            Write-Host "  Regenerating API key..."
            try {
                $r2 = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" -Headers @{ Authorization = "Bearer $Token" } -ContentType "application/json" -Body '{"label":"hooks-installer"}' -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
                if ($r2.StatusCode -eq 201) {
                    $td2 = $r2.Content | ConvertFrom-Json
                    if ($td2.token) {
                        $Token = $td2.token
                        Write-Host "  New key generated." -ForegroundColor Green
                        $startContent = $startContent -replace 'sk_meridian_[A-Za-z0-9_\-]+', $Token
                        $stopContent  = $stopContent  -replace 'sk_meridian_[A-Za-z0-9_\-]+', $Token
                        [System.IO.File]::WriteAllText($sp, $startContent, $enc)
                        [System.IO.File]::WriteAllText($tp, $stopContent,  $enc)
                        $startCmd = "& `"$sp`""
                        $stopCmd  = "& `"$tp`""
                    }
                }
            } catch {}
        } else {
            Write-Host "  Skipped -- hooks unchanged." -ForegroundColor Yellow
            $SkipInstall = $true
        }
    } else {
        Write-Host "  Token invalid or expired -- updating automatically..." -ForegroundColor Yellow
        try {
            $r2 = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" -Headers @{ Authorization = "Bearer $Token" } -ContentType "application/json" -Body '{"label":"hooks-installer"}' -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
            if ($r2.StatusCode -eq 201) {
                $td2 = $r2.Content | ConvertFrom-Json
                if ($td2.token) {
                    $Token = $td2.token
                    Write-Host "  New key generated." -ForegroundColor Green
                    $startContent = $startContent -replace 'sk_meridian_[A-Za-z0-9_\-]+', $Token
                    $stopContent  = $stopContent  -replace 'sk_meridian_[A-Za-z0-9_\-]+', $Token
                    [System.IO.File]::WriteAllText($sp, $startContent, $enc)
                    [System.IO.File]::WriteAllText($tp, $stopContent,  $enc)
                    $startCmd = "& `"$sp`""
                    $stopCmd  = "& `"$tp`""
                }
            }
        } catch {}
    }
}

if (-not $SkipInstall) {

# ---- Step 6: Write hooks to ~/.claude/settings.json ------------------------------
if ($ClaudeDetected) {
    Write-Host ""
    Write-Host "Claude Code detected -- writing hooks to $ClaudeSettingsPath"
    $ClaudeDir = Split-Path $ClaudeSettingsPath
    if (-not (Test-Path $ClaudeDir)) { New-Item -ItemType Directory -Path $ClaudeDir | Out-Null }
    if (Test-Path $ClaudeSettingsPath) {
        try { $settings = Get-Content $ClaudeSettingsPath -Raw | ConvertFrom-Json } catch { $settings = [PSCustomObject]@{} }
    } else {
        $settings = [PSCustomObject]@{}
    }
    if (-not $settings.PSObject.Properties["hooks"]) {
        $settings | Add-Member -NotePropertyName "hooks" -NotePropertyValue ([PSCustomObject]@{})
    }
    $settings.hooks | Add-Member -NotePropertyName "SessionStart" -NotePropertyValue @(
        [PSCustomObject]@{ matcher = ""; hooks = @([PSCustomObject]@{ type = "command"; command = $startCmd; shell = "powershell" }) }
    ) -Force
    $settings.hooks | Add-Member -NotePropertyName "Stop" -NotePropertyValue @(
        [PSCustomObject]@{ matcher = ""; hooks = @([PSCustomObject]@{ type = "command"; command = $stopCmd; shell = "powershell" }) }
    ) -Force
    $settings | ConvertTo-Json -Depth 10 | Set-Content $ClaudeSettingsPath -Encoding UTF8
    Write-Host "  OK SessionStart + Stop hooks written" -ForegroundColor Green
}

# ---- Step 7: Write Codex config --------------------------------------------------
if ($CodexDetected) {
    Write-Host ""
    Write-Host "Codex detected -- writing MCP config to ~/.codex/config.toml"
    $null = New-Item -ItemType Directory -Force $CodexDir
    $meridianBlock = @"

# Meridian - added by hooks.ps1
[mcp_servers.meridian]
type = "http"
url = "$MeridianUrl/mcp"

[mcp_servers.meridian.http_headers]
Authorization = "Bearer $Token"
"@
    if (Test-Path $CodexConfigPath) {
        $existing = Get-Content $CodexConfigPath -Raw
        $existing = $existing -replace '(?s)\n# Meridian - added by hooks\.ps1.*?(?=\n#|\n\[(?!mcp_servers\.meridian)|$)', ''
        $existing = $existing -replace '(?s)\n\[mcp_servers\.meridian\].*?(?=\n\[(?!mcp_servers\.meridian)|$)', ''
        $combined = $existing.TrimEnd() + $meridianBlock
    } else {
        $combined = $meridianBlock.TrimStart()
    }
    $combined | Set-Content -Path $CodexConfigPath -Encoding UTF8
    Write-Host "  OK MCP config written to $CodexConfigPath" -ForegroundColor Green
}

# ---- Step 8: Test hook -----------------------------------------------------------
Write-Host ""
Write-Host "Testing hook..."
$testResult = powershell -NoProfile -NonInteractive -Command "& '$sp'"
if ($testResult -and $testResult.Contains('hookSpecificOutput')) {
    Write-Host "  OK hook test passed" -ForegroundColor Green
} else {
    Write-Host "  Warning: hook test returned unexpected output: $testResult" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. Hooks installed for $MeridianUrl." -ForegroundColor Green
Write-Host "To start with hooks + remote control enabled:"
Write-Host "  claude --rc --permission-mode bypassPermissions"
Write-Host "('claude rc' server mode does NOT fire hooks)"

} # end if (-not $SkipInstall)
}