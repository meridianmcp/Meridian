# hooks.ps1 - Meridian Connect (Windows / PowerShell)
#
# Usage:
#   irm https://usemeridian.us/hooks.ps1 | iex
#   .\hooks.ps1
#   .\hooks.ps1 --url http://localhost:7878 --token sk_meridian_xxx
#
# Installs Claude Code, Codex, and Cursor integrations.
# One install per machine. Hooks are global - project_id comes from your goal.
#
# Requirements: PowerShell 5.1+

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = "Stop"

function Get-ArgValue {
    param([string[]]$Arguments, [string]$Name)
    for ($i = 0; $i -lt $Arguments.Length; $i++) {
        if ($Arguments[$i] -eq $Name) {
            if ($i + 1 -ge $Arguments.Length) { Write-Error "Missing value for $Name"; exit 1 }
            return $Arguments[$i + 1]
        }
    }
    return $null
}

function Test-UrlReachable {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-MeResponse {
    param([string]$Url, [string]$Token)
    try {
        $headers = @{}
        if (-not [string]::IsNullOrEmpty($Token)) { $headers["Authorization"] = "Bearer $Token" }
        $r = Invoke-WebRequest -Uri "$Url/auth/me" -Headers $headers -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        return $r.Content | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Build-StartCmd {
    param([string]$ScriptPath)
    return "& `"`$ScriptPath`""
}

function Write-HookScripts {
    param([string]$Url, [string]$Token, [string]$HooksDir)
    $null = New-Item -ItemType Directory -Force $HooksDir
    $sp = Join-Path $HooksDir "meridian-start.ps1"
    $tp = Join-Path $HooksDir "meridian-stop.ps1"
    $enc = New-Object System.Text.UTF8Encoding $false

    # If scripts exist, update token in-place
    if ($Token) {
        foreach ($file in @($sp, $tp)) {
            if (Test-Path $file) {
                $c = [System.IO.File]::ReadAllText($file)
                $c = $c -replace "sk_meridian_[A-Za-z0-9_\-]+", $Token
                [System.IO.File]::WriteAllText($file, $c, $enc)
            }
        }
    }

    if (-not (Test-Path $sp)) {
        $isLocal = $Url -match "(localhost|127\.0\.0\.1)"
        if ($isLocal) {
            $startContent = @'
# Meridian session-start hook (localhost)
$fallback = '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'
$cwd = (Get-Location).Path -replace "\\","/"
$h = $env:COMPUTERNAME
$b = '{"cwd":"' + $cwd + '","hostname":"' + $h + '"}'
$alive = $false
try { $alive = (Invoke-WebRequest -Uri "__URL__/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop).StatusCode -eq 200 } catch {}
if (-not $alive) {
    $pixi = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
    if (Test-Path "$pixi\pixi.toml") { Start-Process pixi -ArgumentList "run","start" -WorkingDirectory $pixi -WindowStyle Hidden; Start-Sleep 3 }
}
$result = $null
try {
    $r = (Invoke-WebRequest -Method POST -Uri "__URL__/hooks/session-start" -ContentType 'application/json' -Body $b -UseBasicParsing -TimeoutSec 5).Content
    if ($r -and $r.Contains("hookSpecificOutput")) { $result = $r }
} catch {}
if ($result) { $result } else { $fallback }
'@
            $stopContent = @'
# Meridian session-stop hook (localhost)
$cwd = (Get-Location).Path -replace "\\","/"
$h = $env:COMPUTERNAME
$b = '{"cwd":"' + $cwd + '","hostname":"' + $h + '"}'
try { Invoke-WebRequest -Method POST -Uri "__URL__/hooks/stop" -ContentType 'application/json' -Body $b -UseBasicParsing | Out-Null } catch {}
'@
        } else {
            $startContent = @'
# Meridian session-start hook -- self-healing token
$fallback = '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'

function Get-MeridianToken {
    $self = $MyInvocation.ScriptName
    if (-not $self) { $self = "$env:USERPROFILE\.claude\hooks\meridian-start.ps1" }
    try {
        $c = [System.IO.File]::ReadAllText($self)
        if ($c -match 'Authorization="Bearer (sk_meridian_[A-Za-z0-9_\-]+)"') { return $Matches[1] }
    } catch {}
    try {
        $cc = [System.IO.File]::ReadAllText("$env:USERPROFILE\.codex\config.toml")
        if ($cc -match 'Bearer (sk_meridian_[A-Za-z0-9_\-]+)') { return $Matches[1] }
    } catch {}
    return $null
}

function Refresh-Token {
    $envPaths = @(
        "$env:USERPROFILE\Documents\Meridian\repository\.env",
        "$env:USERPROFILE\Meridian\.env"
    )
    foreach ($ep in $envPaths) {
        if (Test-Path $ep) {
            $line = Get-Content $ep | Where-Object { $_ -match "^MERIDIAN_API_SECRET_KEY=(.+)" }
            if ($line) {
                $installKey = $line -replace "MERIDIAN_API_SECRET_KEY=",""
                try {
                    $r = Invoke-RestMethod -Uri "__URL__/auth/tokens" `
                        -Method POST `
                        -Headers @{Authorization="Bearer $installKey"; "Content-Type"="application/json"} `
                        -Body '{"label":"hooks-installer"}' -TimeoutSec 5
                    if ($r.token) {
                        $oldTok = Get-MeridianToken
                        foreach ($f in @(
                            "$env:USERPROFILE\.claude\hooks\meridian-start.ps1",
                            "$env:USERPROFILE\.claude\hooks\meridian-stop.ps1",
                            "$env:USERPROFILE\.codex\config.toml"
                        )) {
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
        $r = (Invoke-WebRequest -Method POST -Uri "__URL__/hooks/session-start" `
            -Headers @{Authorization="Bearer __TOKEN__"} `
            -ContentType 'application/json' -Body $b -UseBasicParsing -TimeoutSec 5).Content
        if ($r -and $r.Contains("hookSpecificOutput")) { return $r }
    } catch {}
    return $null
}

# Main flow
$tok = Get-MeridianToken
$result = $null
if ($tok) {
    try {
        $null = Invoke-RestMethod -Uri "__URL__/auth/me" `
            -Headers @{Authorization="Bearer $tok"} -TimeoutSec 3
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
# Meridian session-stop hook
$cwd = (Get-Location).Path -replace "\\","/"
$h = $env:COMPUTERNAME
$b = '{"cwd":"' + $cwd + '","hostname":"' + $h + '"}'
try {
    Invoke-WebRequest -Method POST -Uri "__URL__/hooks/stop" -Headers @{Authorization="Bearer __TOKEN__"} -ContentType 'application/json' -Body $b -UseBasicParsing | Out-Null
} catch {}
'@
        }
        $startContent = $startContent.Replace("__URL__", $Url)
        $stopContent  = $stopContent.Replace("__URL__", $Url)
        if ($Token) {
            $startContent = $startContent.Replace("__TOKEN__", $Token)
            $stopContent  = $stopContent.Replace("__TOKEN__", $Token)
        }
        [System.IO.File]::WriteAllText($sp, $startContent, $enc)
        [System.IO.File]::WriteAllText($tp, $stopContent,  $enc)
    }
    return @{ Start = $sp; Stop = $tp }
}

function Build-StopCmd {
    param([string]$ScriptPath)
    return "& `"`$ScriptPath`""
}

# ---- Step 1: URL ------------------------------------------------------------------
$DefaultUrl = "https://usemeridian.us"
$MeridianUrl = Get-ArgValue -Arguments $CliArgs -Name "--url"
if ($null -eq $MeridianUrl) {
    Write-Host "Where is Meridian running?"
    Write-Host "  [1] usemeridian.us -- hosted (recommended, press Enter)"
    Write-Host "  [2] localhost:7878 -- self-hosted"
    Write-Host "  [3] Other URL"
    $choice = Read-Host "Choice [1]"
    if ($choice -eq "2") {
        $MeridianUrl = "http://localhost:7878"
    } elseif ($choice -eq "3") {
        $MeridianUrl = Read-Host "Enter URL (e.g. https://my-meridian.example.com)"
    } else {
        $MeridianUrl = $DefaultUrl
    }
}
$MeridianUrl = $MeridianUrl.TrimEnd("/")

if (-not ($MeridianUrl -match "^https?://")) {
    Write-Host "Error: URL must start with https:// or http://" -ForegroundColor Red
    exit 1
}

Write-Host "Checking $MeridianUrl ..."
if (-not (Test-UrlReachable -Url $MeridianUrl)) {
    Write-Host "Error: Cannot reach $MeridianUrl/health -- is the server running?" -ForegroundColor Red
    exit 1
}
Write-Host "  OK server is reachable" -ForegroundColor Green

# ---- Step 2: Auth -----------------------------------------------------------------
$IsLocalhost = $MeridianUrl -match "^https?://(localhost|127\.0\.0\.1)(:\d+)?"
$Token = Get-ArgValue -Arguments $CliArgs -Name "--token"

if ($IsLocalhost) {
    Write-Host ""
    Write-Host "Self-hosted / localhost detected -- skipping auth."
    if ($null -eq $Token) { $Token = "" }
} else {
    # Check for existing valid token in already-installed hooks first
    $existingToken = $null
    $settingsPath = Join-Path $HOME ".claude\settings.json"
    if (Test-Path $settingsPath) {
        try {
            $s = Get-Content $settingsPath -Raw | ConvertFrom-Json
            $cmd = $s.hooks.SessionStart[0].hooks[0].command
            if ($cmd -match 'Bearer (sk_meridian_[A-Za-z0-9_\-]+)') {
                $candidate = $Matches[1]
                $check = Get-MeResponse -Url $MeridianUrl -Token $candidate
                if ($null -ne $check) {
                    $existingToken = $candidate
                    $Token = $candidate
                    Write-Host "  Found existing API key -- authenticated as: $($check.email)" -ForegroundColor Green
                }
            }
        } catch {}
    }
    # Also check ps1 script directly (hooks use & "script.ps1" format not inline Bearer)
    if ([string]::IsNullOrWhiteSpace($existingToken)) {
        $psPath = Join-Path $HOME ".claude\hooks\meridian-start.ps1"
        if (Test-Path $psPath) {
            try {
                $pscontent = Get-Content $psPath -Raw
                if ($pscontent -match 'Bearer (sk_meridian_[A-Za-z0-9_\-]+)') {
                    $candidate = $Matches[1]
                    $check = Get-MeResponse -Url $MeridianUrl -Token $candidate
                    if ($null -ne $check) {
                        $existingToken = $candidate
                        $Token = $candidate
                        Write-Host "  Found existing API key in hooks script -- authenticated as: $($check.email)" -ForegroundColor Green
                    }
                }
            } catch {}
        }
    }

    if ([string]::IsNullOrWhiteSpace($existingToken)) {
        if ($null -eq $Token) {
            Write-Host ""
            Write-Host "Opening browser to authenticate..."
            Start-Process "$MeridianUrl/auth/install"
            Write-Host ""
            $secToken = Read-Host "Paste the token shown in your browser" -AsSecureString
            $Token = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secToken)
            )
        }
        $Token = $Token.Trim()
        if ([string]::IsNullOrWhiteSpace($Token)) {
            Write-Host "Error: token is required for hosted Meridian." -ForegroundColor Red
            exit 1
        }
        Write-Host ""
        Write-Host "Validating token..."
        $me = Get-MeResponse -Url $MeridianUrl -Token $Token
        if ($null -eq $me) {
            Write-Host "Error: Token validation failed -- is the token correct?" -ForegroundColor Red
            exit 1
        }
        Write-Host "  Authenticated as: $($me.email)" -ForegroundColor Green

        # Exchange install token for permanent sk_meridian_ key
        if (-not $Token.StartsWith("sk_meridian_")) {
            try {
                $r = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" `
                    -Headers @{ Authorization = "Bearer $Token" } `
                    -ContentType "application/json" -Body '{"label":"hooks-installer"}' `
                    -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
                if ($r.StatusCode -eq 201) {
                    $td = $r.Content | ConvertFrom-Json
                    if ($td.token) {
                        $Token = $td.token
                        Write-Host "  Permanent API key saved." -ForegroundColor Green
                    }
                }
            } catch {}
        }
    }
}

# No project selection -- hooks are global, project_id comes from the goal at session time.

# ---- Step 4: Build hook commands --------------------------------------------------
$HooksDir = Join-Path $HOME ".claude\hooks"
$scripts = Write-HookScripts -Url $MeridianUrl -Token $Token -HooksDir $HooksDir
$startCmd = Build-StartCmd -ScriptPath $scripts.Start
$stopCmd  = Build-StopCmd  -ScriptPath $scripts.Stop

# ---- Step 5: Check for existing hooks --------------------------------------------
$ClaudeSettingsPath = Join-Path $HOME ".claude\settings.json"
$ClaudeDetected = (Get-Command claude -ErrorAction SilentlyContinue) -ne $null -or (Test-Path $ClaudeSettingsPath)
$ExistingHooks = $false

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
    if (-not [string]::IsNullOrWhiteSpace($existingToken)) {
        Write-Host "  Token is valid -- hooks are working." -ForegroundColor Green
        $choice = Read-Host "  (S)kip -- leave as-is / (U)pdate format / (R)egenerate key [S/u/r]"
        if ($choice -match "^[Uu]") {
            Write-Host "  Updating hooks..."
        } elseif ($choice -match "^[Rr]") {
            Write-Host "  Regenerating API key..."
            try {
                $r2 = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" `
                    -Headers @{ Authorization = "Bearer $Token" } `
                    -ContentType "application/json" -Body 
'
{"label":"hooks-installer"}
'
 `
                    -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
                if ($r2.StatusCode -eq 201) { $td2 = $r2.Content | ConvertFrom-Json; if ($td2.token) { $Token = $td2.token; Write-Host "  New key generated." -ForegroundColor Green } }
            } catch {}
        } else {
            Write-Host "  Skipped." -ForegroundColor Yellow
            exit 0
        }
    } else {
        Write-Host "  Token invalid or expired -- updating automatically..." -ForegroundColor Yellow
        # No prompt needed -- just regenerate silently
        try {
            $r2 = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" `
                -Headers @{ Authorization = "Bearer $Token" } `
                -ContentType "application/json" -Body 
'
{"label":"hooks-installer"}
'
 `
                -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
            if ($r2.StatusCode -eq 201) { $td2 = $r2.Content | ConvertFrom-Json; if ($td2.token) { $Token = $td2.token; Write-Host "  New key generated." -ForegroundColor Green } }
        } catch {}
    }
}
}

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

# ---- Step 7: Codex ---------------------------------------------------------------
$CodexDetected = (Get-Command codex -ErrorAction SilentlyContinue) -ne $null -or (Test-Path (Join-Path $HOME ".codex"))
if ($CodexDetected) {
    Write-Host ""
    Write-Host "Codex detected -- writing MCP config to ~/.codex/config.toml"
    $CodexDir = Join-Path $HOME ".codex"
    $CodexConfigPath = Join-Path $CodexDir "config.toml"
    if (-not (Test-Path $CodexDir)) { New-Item -ItemType Directory -Path $CodexDir | Out-Null }

    $authLine = if ([string]::IsNullOrEmpty($Token)) { "" } else { "`n`n[mcp_servers.meridian.http_headers]`nAuthorization = `"Bearer $Token`"" }
    $escapedStart = $startCmd.Replace('\', '\\').Replace('"', '\"')
    $escapedStop  = $stopCmd.Replace('\', '\\').Replace('"', '\"')
    $newBlock = @"

[mcp_servers.meridian]
type = "http"
url = "$MeridianUrl/mcp"$authLine

[hooks]
session_start = "$escapedStart"
stop = "$escapedStop"
"@

    if (Test-Path $CodexConfigPath) {
        $existing = Get-Content $CodexConfigPath -Raw
        $existing = $existing -replace '(?s)\[mcp_servers\.meridian\].*?(?=\n\[|\z)', ''
        $existing = $existing -replace '(?s)\[hooks\].*?(?=\n\[|\z)', ''
        $combined = $existing.TrimEnd() + $newBlock
    } else {
        $combined = $newBlock.TrimStart()
    }
    $combined | Set-Content -Path $CodexConfigPath -Encoding UTF8
    Write-Host "  OK MCP config written to $CodexConfigPath" -ForegroundColor Green
}

# ---- Step 8: Cursor --------------------------------------------------------------
$CursorDetected = (Get-Command cursor -ErrorAction SilentlyContinue) -ne $null -or (Test-Path (Join-Path $HOME ".cursor"))
if ($CursorDetected) {
    Write-Host ""
    Write-Host "Cursor detected -- writing .cursor/mcp.json in current directory"
    $CursorDir = Join-Path (Get-Location).Path ".cursor"
    $CursorConfigPath = Join-Path $CursorDir "mcp.json"
    if (-not (Test-Path $CursorDir)) { New-Item -ItemType Directory -Path $CursorDir | Out-Null }
    if ([string]::IsNullOrEmpty($Token)) {
        $cursorCfg = @{ mcpServers = @{ meridian = @{ url = "$MeridianUrl/mcp" } } }
    } else {
        $cursorCfg = @{ mcpServers = @{ meridian = @{ url = "$MeridianUrl/mcp"; headers = @{ Authorization = "Bearer $Token" } } } }
    }
    $cursorCfg | ConvertTo-Json -Depth 5 | Set-Content -Path $CursorConfigPath -Encoding UTF8
    Write-Host "  OK .cursor/mcp.json written" -ForegroundColor Green
    Write-Host "  Note: Cursor MCP tools available. Auto session tracking requires Claude Code or Codex." -ForegroundColor Yellow
}

# ---- Step 9: Smoke test ----------------------------------------------------------
Write-Host ""
Write-Host "Testing hook..."
$testOk = $false
try {
    $testCwd = (Get-Location).Path.Replace("\", "/")
    $testHostname = $env:COMPUTERNAME
    $testBody = "{`"cwd`":`"$testCwd`",`"hostname`":`"$testHostname`"}"
    $hdrs = @{ "Content-Type" = "application/json" }
    if (-not [string]::IsNullOrWhiteSpace($Token)) { $hdrs["Authorization"] = "Bearer $Token" }
    $r = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/hooks/session-start" `
        -Headers $hdrs -Body $testBody -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
    if ($r.StatusCode -eq 200) { $testOk = $true }
} catch {}
if ($testOk) {
    Write-Host "  OK hook test passed" -ForegroundColor Green
} else {
    Write-Host "  WARNING: hook test returned non-200 (hooks still installed)" -ForegroundColor Yellow
}

# ---- Done ------------------------------------------------------------------------
Write-Host ""
Write-Host "Done. Hooks installed for $MeridianUrl." -ForegroundColor Green
Write-Host ""
Write-Host "To start with hooks + remote control enabled:"
Write-Host "  claude --rc --permission-mode bypassPermissions" -ForegroundColor Cyan
Write-Host "('claude rc' server mode does NOT fire hooks)"
Write-Host ""
