# hooks.ps1 — Meridian session lifecycle hooks installer (Windows / PowerShell)
#
# Usage: .\hooks.ps1
# Detects Claude Code → writes SessionStart + Stop HTTP hooks to ~/.claude/settings.json
# Detects Codex      → writes MCP block + hooks to ~/.codex/config.toml
#
# Requirements: PowerShell 5.1+

param()
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "Meridian hook installer"
Write-Host "-----------------------"
Write-Host ""

$DefaultUrl = "http://localhost:7878"
$MeridianUrl = Read-Host "Meridian server URL [$DefaultUrl]"
if ([string]::IsNullOrWhiteSpace($MeridianUrl)) { $MeridianUrl = $DefaultUrl }
$MeridianUrl = $MeridianUrl.TrimEnd("/")

$ProjectId = Read-Host "Project ID"
if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    Write-Error "project_id is required."
    exit 1
}

Write-Host ""
Write-Host "Server URL : $MeridianUrl"
Write-Host "Project ID : $ProjectId"
Write-Host ""

# ---- Claude Code detection --------------------------------------------------
$ClaudeSettingsPath = Join-Path $HOME ".claude\settings.json"
$ClaudeDetected = (Get-Command claude -ErrorAction SilentlyContinue) -ne $null -or (Test-Path $ClaudeSettingsPath)

# ---- Codex detection --------------------------------------------------------
$CodexConfigPath = Join-Path $HOME ".codex\config.toml"
$CodexDetected = (Get-Command codex -ErrorAction SilentlyContinue) -ne $null -or (Test-Path $CodexConfigPath)

# ---- Claude Code hooks -------------------------------------------------------
if ($ClaudeDetected) {
    Write-Host "Claude Code detected — writing hooks to $ClaudeSettingsPath"
    $ClaudeDir = Split-Path $ClaudeSettingsPath
    if (-not (Test-Path $ClaudeDir)) { New-Item -ItemType Directory -Path $ClaudeDir | Out-Null }

    $startCmd = "curl -s -X POST $MeridianUrl/hooks/session-start -H `"Content-Type: application/json`" -d `"{`\`"project_id`\`":`\`"$ProjectId`\`"}`""
    $stopCmd  = "curl -s -X POST $MeridianUrl/hooks/stop -H `"Content-Type: application/json`" -d `"{`\`"project_id`\`":`\`"$ProjectId`\`"}`""

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

# ---- Codex hooks -------------------------------------------------------------
if ($CodexDetected) {
    Write-Host "Codex detected — writing config to $CodexConfigPath"
    $CodexDir = Split-Path $CodexConfigPath
    if (-not (Test-Path $CodexDir)) { New-Item -ItemType Directory -Path $CodexDir | Out-Null }

    $toml = @"

# Meridian — added by hooks.ps1
[mcp_servers.meridian]
type = "http"
url = "$MeridianUrl/mcp"

[hooks]
session_start = "curl -s -X POST $MeridianUrl/hooks/session-start -H 'Content-Type: application/json' -d '{\"project_id\":\"$ProjectId\"}'"
stop = "curl -s -X POST $MeridianUrl/hooks/stop -H 'Content-Type: application/json' -d '{\"project_id\":\"$ProjectId\"}'"
"@
    Add-Content -Path $CodexConfigPath -Value $toml -Encoding UTF8
    Write-Host "  OK Meridian MCP + hooks written to $CodexConfigPath"
}

if (-not $ClaudeDetected -and -not $CodexDetected) {
    Write-Host "Neither Claude Code nor Codex detected."
    Write-Host "Add hooks manually using these commands:"
    Write-Host ""
    Write-Host "  Start: curl -s -X POST $MeridianUrl/hooks/session-start ``"
    Write-Host "         -H 'Content-Type: application/json' ``"
    Write-Host "         -d '{\"project_id\":\"$ProjectId\"}'"
    Write-Host ""
    Write-Host "  Stop:  curl -s -X POST $MeridianUrl/hooks/stop ``"
    Write-Host "         -H 'Content-Type: application/json' ``"
    Write-Host "         -d '{\"project_id\":\"$ProjectId\"}'"
}

Write-Host ""
Write-Host "Done. Start a new Claude Code / Codex session to test."
