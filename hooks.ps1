# hooks.ps1 - Meridian session lifecycle hooks installer (Windows / PowerShell)
#
# Usage:
#   .\hooks.ps1
#   .\hooks.ps1 --url http://localhost:7878 --project-id your-project-id
#   .\hooks.ps1 --url https://usemeridian.us --project-id your-project-id --token sk_meridian_...
# Detects Claude Code -> writes SessionStart + Stop HTTP hooks to ~/.claude/settings.json
# Detects Codex      -> writes MCP block + hooks to ~/.codex/config.toml
#
# Requirements: PowerShell 5.1+

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$ErrorActionPreference = "Stop"

function Get-ArgValue {
    param(
        [string[]]$Arguments,
        [string]$Name
    )

    for ($i = 0; $i -lt $Arguments.Length; $i++) {
        if ($Arguments[$i] -eq $Name) {
            if ($i + 1 -ge $Arguments.Length) {
                Write-Error "Missing value for $Name"
                exit 1
            }
            return $Arguments[$i + 1]
        }
    }
    return $null
}

function Get-HookHeaderClause {
    param([string]$Token)

    if ([string]::IsNullOrWhiteSpace($Token)) {
        return ""
    }
    return " -Headers @{ Authorization = 'Bearer $Token' }"
}

function New-SessionStartHookCommand {
    param(
        [string]$BaseUrl,
        [string]$ProjectId,
        [string]$Token
    )

    $headerClause = Get-HookHeaderClause -Token $Token
    $scriptBody = "try { `$cwd = (Get-Location).Path.Replace('\', '/'); `$body = '{""project_id"":""$ProjectId"",""cwd"":""' + `$cwd + '""}'; `$r = Invoke-WebRequest -Method POST -Uri '$BaseUrl/hooks/session-start'$headerClause -ContentType 'application/json' -Body `$body -UseBasicParsing; `$r.Content } catch { '{}' }"
    return "powershell -NoProfile -NonInteractive -Command `"$scriptBody`""
}

function New-StopHookCommand {
    param(
        [string]$BaseUrl,
        [string]$ProjectId,
        [string]$Token
    )

    $bodyJson = '{"project_id":"' + $ProjectId + '"}'
    $headerClause = Get-HookHeaderClause -Token $Token
    $scriptBody = "try { Invoke-WebRequest -Method POST -Uri '$BaseUrl/hooks/stop'$headerClause -ContentType 'application/json' -Body '$bodyJson' -UseBasicParsing | Out-Null } catch { }"
    return "powershell -NoProfile -NonInteractive -Command `"$scriptBody`""
}

Write-Host ""
Write-Host "Meridian hook installer"
Write-Host "-----------------------"
Write-Host ""

$DefaultUrl = "http://localhost:7878"
$MeridianUrl = Get-ArgValue -Arguments $CliArgs -Name "--url"
if ($null -eq $MeridianUrl) {
    $MeridianUrl = Read-Host "Meridian server URL [$DefaultUrl]"
}
if ([string]::IsNullOrWhiteSpace($MeridianUrl)) {
    $MeridianUrl = $DefaultUrl
}
$MeridianUrl = $MeridianUrl.TrimEnd("/")

$ProjectId = Get-ArgValue -Arguments $CliArgs -Name "--project-id"
if ($null -eq $ProjectId) {
    $ProjectId = Read-Host "Project ID"
}
if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    Write-Error "project_id is required."
    exit 1
}

$Token = Get-ArgValue -Arguments $CliArgs -Name "--token"

Write-Host ""
Write-Host "Server URL : $MeridianUrl"
Write-Host "Project ID : $ProjectId"
if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Host "API Token  : not set"
} else {
    Write-Host "API Token  : set"
}
Write-Host ""

# ---- Claude Code detection --------------------------------------------------
$ClaudeSettingsPath = Join-Path $HOME ".claude\settings.json"
$ClaudeDetected = (Get-Command claude -ErrorAction SilentlyContinue) -ne $null -or (Test-Path $ClaudeSettingsPath)

# ---- Codex detection --------------------------------------------------------
$CodexConfigPath = Join-Path $HOME ".codex\config.toml"
$CodexDetected = (Get-Command codex -ErrorAction SilentlyContinue) -ne $null -or (Test-Path $CodexConfigPath)

# ---- Claude Code hooks ------------------------------------------------------
if ($ClaudeDetected) {
    Write-Host "Claude Code detected - writing hooks to $ClaudeSettingsPath"
    $ClaudeDir = Split-Path $ClaudeSettingsPath
    if (-not (Test-Path $ClaudeDir)) {
        New-Item -ItemType Directory -Path $ClaudeDir | Out-Null
    }

    $startCmd = New-SessionStartHookCommand -BaseUrl $MeridianUrl -ProjectId $ProjectId -Token $Token
    $stopCmd = New-StopHookCommand -BaseUrl $MeridianUrl -ProjectId $ProjectId -Token $Token

    if (Test-Path $ClaudeSettingsPath) {
        $settings = Get-Content $ClaudeSettingsPath -Raw | ConvertFrom-Json
    } else {
        $settings = [PSCustomObject]@{}
    }

    if (-not $settings.PSObject.Properties["hooks"]) {
        $settings | Add-Member -NotePropertyName "hooks" -NotePropertyValue ([PSCustomObject]@{})
    }
    $settings.hooks | Add-Member -NotePropertyName "SessionStart" -NotePropertyValue @(
        [PSCustomObject]@{ type = "command"; command = $startCmd }
    ) -Force
    $settings.hooks | Add-Member -NotePropertyName "Stop" -NotePropertyValue @(
        [PSCustomObject]@{ type = "command"; command = $stopCmd }
    ) -Force

    $settings | ConvertTo-Json -Depth 10 | Set-Content $ClaudeSettingsPath -Encoding UTF8
    Write-Host "  OK SessionStart + Stop hooks written to $ClaudeSettingsPath"
}

# ---- Codex hooks ------------------------------------------------------------
if ($CodexDetected) {
    Write-Host "Codex detected - writing config to $CodexConfigPath"
    $CodexDir = Split-Path $CodexConfigPath
    if (-not (Test-Path $CodexDir)) {
        New-Item -ItemType Directory -Path $CodexDir | Out-Null
    }

    $startCmd = New-SessionStartHookCommand -BaseUrl $MeridianUrl -ProjectId $ProjectId -Token $Token
    $stopCmd = New-StopHookCommand -BaseUrl $MeridianUrl -ProjectId $ProjectId -Token $Token

    $toml = @"

# Meridian - added by hooks.ps1
[mcp_servers.meridian]
type = "http"
url = "$MeridianUrl/mcp"

[hooks]
session_start = $(ConvertTo-Json $startCmd -Compress)
stop = $(ConvertTo-Json $stopCmd -Compress)
"@
    Add-Content -Path $CodexConfigPath -Value $toml -Encoding UTF8
    Write-Host "  OK Meridian MCP + hooks written to $CodexConfigPath"
}

if (-not $ClaudeDetected -and -not $CodexDetected) {
    Write-Host "Neither Claude Code nor Codex detected."
    Write-Host "Add hooks manually using these commands:"
    Write-Host ""
    Write-Host "  SessionStart command:"
    Write-Host "    $(New-SessionStartHookCommand -BaseUrl $MeridianUrl -ProjectId $ProjectId -Token $Token)"
    Write-Host ""
    Write-Host "  Stop command:"
    Write-Host "    $(New-StopHookCommand -BaseUrl $MeridianUrl -ProjectId $ProjectId -Token $Token)"
}

Write-Host ""
Write-Host "Done. Start a new Claude Code / Codex session to test."
