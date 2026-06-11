# hooks.ps1 - Meridian session lifecycle hooks installer (Windows / PowerShell)
#
# Usage:
#   irm https://usemeridian.us/hooks.ps1 | iex
#   .\hooks.ps1
#   .\hooks.ps1 --url http://localhost:7878 --project-id your-project-id
#
# Installs Claude Code, Codex, and Cursor integrations. Credentials are embedded
# directly in hook commands — no per-repo config file required.
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
    param([string]$Url, [string]$Token, [string]$ProjectId)
    if ([string]::IsNullOrEmpty($Token)) {
        $inner = 'try { $cwd=(Get-Location).Path.Replace("\","/"); $h=$env:COMPUTERNAME; $body="{""project_id"":""' + $ProjectId + '"",""cwd"":""$cwd"",""hostname"":""$h""}"; (Invoke-WebRequest -Method POST -Uri "' + $Url + '/hooks/session-start" -ContentType "application/json" -Body $body -UseBasicParsing).Content } catch { "{}" }'
    } else {
        $inner = 'try { $cwd=(Get-Location).Path.Replace("\","/"); $h=$env:COMPUTERNAME; $body="{""project_id"":""' + $ProjectId + '"",""cwd"":""$cwd"",""hostname"":""$h""}"; (Invoke-WebRequest -Method POST -Uri "' + $Url + '/hooks/session-start" -Headers @{ Authorization="Bearer ' + $Token + '" } -ContentType "application/json" -Body $body -UseBasicParsing).Content } catch { "{}" }'
    }
    return "powershell -NoProfile -NonInteractive -Command `"$inner`""
}

function Build-StopCmd {
    param([string]$Url, [string]$Token, [string]$ProjectId)
    if ([string]::IsNullOrEmpty($Token)) {
        $inner = 'try { $h=$env:COMPUTERNAME; $body="{""project_id"":""' + $ProjectId + '"",""hostname"":""$h""}"; Invoke-WebRequest -Method POST -Uri "' + $Url + '/hooks/stop" -ContentType "application/json" -Body $body -UseBasicParsing | Out-Null } catch { }'
    } else {
        $inner = 'try { $h=$env:COMPUTERNAME; $body="{""project_id"":""' + $ProjectId + '"",""hostname"":""$h""}"; Invoke-WebRequest -Method POST -Uri "' + $Url + '/hooks/stop" -Headers @{ Authorization="Bearer ' + $Token + '" } -ContentType "application/json" -Body $body -UseBasicParsing | Out-Null } catch { }'
    }
    return "powershell -NoProfile -NonInteractive -Command `"$inner`""
}

Write-Host ""
Write-Host "Meridian hook installer"
Write-Host "-----------------------"
Write-Host ""

# ---- Step 1: URL ------------------------------------------------------------------
$DefaultUrl = "https://usemeridian.us"
$MeridianUrl = Get-ArgValue -Arguments $CliArgs -Name "--url"
if ($null -eq $MeridianUrl) {
    $MeridianUrl = Read-Host "Meridian server URL [$DefaultUrl]"
}
if ([string]::IsNullOrWhiteSpace($MeridianUrl)) { $MeridianUrl = $DefaultUrl }
$MeridianUrl = $MeridianUrl.TrimEnd("/")

if (-not ($MeridianUrl -match "^https?://")) {
    Write-Host "Error: URL must start with https:// or http://" -ForegroundColor Red
    exit 1
}

Write-Host "Checking $MeridianUrl ..."
if (-not (Test-UrlReachable -Url $MeridianUrl)) {
    Write-Host "Error: Cannot reach $MeridianUrl/health — is the server running?" -ForegroundColor Red
    exit 1
}
Write-Host "  OK server is reachable" -ForegroundColor Green

# ---- Step 2: Auth -----------------------------------------------------------------
$IsLocalhost = $MeridianUrl -match "^https?://(localhost|127\.0\.0\.1)(:\d+)?"
$Token = Get-ArgValue -Arguments $CliArgs -Name "--token"

if ($IsLocalhost) {
    Write-Host ""
    Write-Host "Self-hosted / localhost detected — skipping auth."
    if ($null -eq $Token) { $Token = "" }
} else {
    if ($null -eq $Token) {
        Write-Host ""
        Write-Host "Opening browser to authenticate..."
        Start-Process "$MeridianUrl/auth/install"
        Write-Host ""
        $Token = Read-Host "Paste the token shown in your browser"
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
        Write-Host "Error: Token validation failed — is the token correct?" -ForegroundColor Red
        exit 1
    }
    Write-Host "  Authenticated as: $($me.email)" -ForegroundColor Green
}

# ---- Step 3: Project selection ----------------------------------------------------
$ProjectId = Get-ArgValue -Arguments $CliArgs -Name "--project-id"
$ProjectName = ""

if ($null -eq $ProjectId) {
    $projects = @()
    try {
        $me2 = Get-MeResponse -Url $MeridianUrl -Token $Token
        if ($me2 -and $me2.projects) { $projects = @($me2.projects) }
    } catch {}
    if ($projects.Count -gt 0) {
        Write-Host ""
        Write-Host "Your projects:"
        for ($i = 0; $i -lt $projects.Count; $i++) {
            $shortId = $projects[$i].id.Substring(0, [Math]::Min(8, $projects[$i].id.Length))
            Write-Host "  [$($i + 1)] $($projects[$i].name)  ($shortId...)"
        }
        Write-Host ""
        $choice = Read-Host "Select project number [1-$($projects.Count)]"
        $idx = [int]$choice - 1
        if ($idx -lt 0 -or $idx -ge $projects.Count) {
            Write-Host "Error: invalid selection." -ForegroundColor Red
            exit 1
        }
        $ProjectId = $projects[$idx].id
        $ProjectName = $projects[$idx].name
    } else {
        $ProjectId = Read-Host "Project ID"
    }
}
if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    Write-Host "Error: project_id is required." -ForegroundColor Red
    exit 1
}

# ---- Step 4: Generate permanent token --------------------------------------------
if (-not $IsLocalhost -and -not [string]::IsNullOrWhiteSpace($Token)) {
    try {
        $r = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" `
            -Headers @{ Authorization = "Bearer $Token" } `
            -ContentType "application/json" -Body '{"label":"hooks-installer"}' `
            -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
        if ($r.StatusCode -eq 201) {
            $td = $r.Content | ConvertFrom-Json
            if ($td.token) {
                $Token = $td.token
                Write-Host "  Permanent token created." -ForegroundColor Green
            }
        }
    } catch {}
}

# ---- Step 5: Build hook commands -------------------------------------------------
$startCmd = Build-StartCmd -Url $MeridianUrl -Token $Token -ProjectId $ProjectId
$stopCmd  = Build-StopCmd  -Url $MeridianUrl -Token $Token -ProjectId $ProjectId

# ---- Step 6: Write hooks to ~/.claude/settings.json ------------------------------
$ClaudeSettingsPath = Join-Path $HOME ".claude\settings.json"
$ClaudeDetected = (Get-Command claude -ErrorAction SilentlyContinue) -ne $null -or (Test-Path $ClaudeSettingsPath)

if ($ClaudeDetected) {
    Write-Host ""
    Write-Host "Claude Code detected — writing hooks to $ClaudeSettingsPath"
    $ClaudeDir = Split-Path $ClaudeSettingsPath
    if (-not (Test-Path $ClaudeDir)) { New-Item -ItemType Directory -Path $ClaudeDir | Out-Null }

    if (Test-Path $ClaudeSettingsPath) {
        $settings = Get-Content $ClaudeSettingsPath -Raw | ConvertFrom-Json
    } else {
        $settings = [PSCustomObject]@{}
    }
    if (-not $settings.PSObject.Properties["hooks"]) {
        $settings | Add-Member -NotePropertyName "hooks" -NotePropertyValue ([PSCustomObject]@{})
    }
    $settings.hooks | Add-Member -NotePropertyName "SessionStart" -NotePropertyValue @(
        [PSCustomObject]@{ matcher = ""; hooks = @([PSCustomObject]@{ type = "command"; command = $startCmd }) }
    ) -Force
    $settings.hooks | Add-Member -NotePropertyName "Stop" -NotePropertyValue @(
        [PSCustomObject]@{ matcher = ""; hooks = @([PSCustomObject]@{ type = "command"; command = $stopCmd }) }
    ) -Force
    $settings | ConvertTo-Json -Depth 10 | Set-Content $ClaudeSettingsPath -Encoding UTF8
    Write-Host "  OK SessionStart + Stop hooks written" -ForegroundColor Green
}

# ---- Step 7: Codex detection + config.toml ---------------------------------------
$CodexDetected = (Get-Command codex -ErrorAction SilentlyContinue) -ne $null -or (Test-Path (Join-Path $HOME ".codex"))
if ($CodexDetected) {
    Write-Host ""
    Write-Host "Codex detected — writing MCP config to ~/.codex/config.toml"
    $CodexDir = Join-Path $HOME ".codex"
    $CodexConfigPath = Join-Path $CodexDir "config.toml"
    if (-not (Test-Path $CodexDir)) { New-Item -ItemType Directory -Path $CodexDir | Out-Null }

    $authLine = if ([string]::IsNullOrEmpty($Token)) { "" } else { "`napi_key = `"$Token`"" }
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
        # Strip old meridian + hooks blocks then append new ones
        $existing = $existing -replace '(?s)\[mcp_servers\.meridian\].*?(?=\n\[|\z)', ''
        $existing = $existing -replace '(?s)\[hooks\].*?(?=\n\[|\z)', ''
        $combined = $existing.TrimEnd() + $newBlock
    } else {
        $combined = $newBlock.TrimStart()
    }
    $combined | Set-Content -Path $CodexConfigPath -Encoding UTF8
    Write-Host "  OK MCP config written to $CodexConfigPath" -ForegroundColor Green
}

# ---- Step 8: Cursor detection + .cursor/mcp.json ---------------------------------
$CursorDetected = (Get-Command cursor -ErrorAction SilentlyContinue) -ne $null -or (Test-Path (Join-Path $HOME ".cursor"))
if ($CursorDetected) {
    Write-Host ""
    Write-Host "Cursor detected — writing .cursor/mcp.json in current directory"
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
    Write-Host "  Note: Cursor MCP tools available. Automatic session tracking requires Claude Code or Codex." -ForegroundColor Yellow
}

# ---- Step 9: Smoke test ----------------------------------------------------------
Write-Host ""
Write-Host "Testing hook..."
$testOk = $false
try {
    $testCwd = (Get-Location).Path.Replace("\", "/")
    $testHostname = $env:COMPUTERNAME
    $testBody = "{`"project_id`":`"$ProjectId`",`"cwd`":`"$testCwd`",`"hostname`":`"$testHostname`"}"
    $hdrs = @{ "Content-Type" = "application/json" }
    if (-not [string]::IsNullOrWhiteSpace($Token)) { $hdrs["Authorization"] = "Bearer $Token" }
    $r = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/hooks/session-start" `
        -Headers $hdrs -Body $testBody -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
    if ($r.StatusCode -eq 200) { $testOk = $true }
} catch {}
if ($testOk) {
    Write-Host "  OK hook responded successfully" -ForegroundColor Green
} else {
    Write-Host "  WARNING: hook test returned non-200 (hooks still installed)" -ForegroundColor Yellow
}

# ---- Done ------------------------------------------------------------------------
Write-Host ""
$displayName = if ($ProjectName) { "'$ProjectName'" } else { $ProjectId }
Write-Host "Done. Hooks installed for project $displayName." -ForegroundColor Green
Write-Host "Restart Claude Code to activate."
Write-Host ""
