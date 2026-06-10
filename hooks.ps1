# hooks.ps1 - Meridian session lifecycle hooks installer (Windows / PowerShell)
#
# Usage:
#   irm https://usemeridian.us/hooks.ps1 | iex
#   .\hooks.ps1
#   .\hooks.ps1 --url http://localhost:7878 --project-id your-project-id
#
# Writes .meridian/config to the current directory and installs GENERIC hooks
# in ~/.claude/settings.json. Hooks read .meridian/config at fire time, so
# they follow the project regardless of which repo directory you're in.
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
        $r = Invoke-WebRequest -Uri "$Url/auth/me" `
            -Headers @{ Authorization = "Bearer $Token" } `
            -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        return $r.Content | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Build-GenericStartCmd {
    $inner = 'try { $cfg = Get-Content (Join-Path $PWD ".meridian/config") | ConvertFrom-StringData; $cwd = (Get-Location).Path.Replace("\","/"); $body = "{""project_id"":""$($cfg.project_id)"",""cwd"":""$cwd""}"; $r = Invoke-WebRequest -Method POST -Uri ($cfg.url + "/hooks/session-start") -Headers @{ Authorization = "Bearer $($cfg.token)" } -ContentType "application/json" -Body $body -UseBasicParsing; $r.Content } catch { "{}" }'
    return "powershell -NoProfile -NonInteractive -Command `"$inner`""
}

function Build-GenericStopCmd {
    $inner = 'try { $cfg = Get-Content (Join-Path $PWD ".meridian/config") | ConvertFrom-StringData; $body = "{""project_id"":""$($cfg.project_id)""}"; Invoke-WebRequest -Method POST -Uri ($cfg.url + "/hooks/stop") -Headers @{ Authorization = "Bearer $($cfg.token)" } -ContentType "application/json" -Body $body -UseBasicParsing | Out-Null } catch { }'
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
if ([string]::IsNullOrWhiteSpace($MeridianUrl)) {
    $MeridianUrl = $DefaultUrl
}
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
    if ($null -eq $Token) {
        $Token = ""
    }
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
    if (-not [string]::IsNullOrWhiteSpace($Token)) {
        try {
            $me2 = Get-MeResponse -Url $MeridianUrl -Token $Token
            if ($me2 -and $me2.projects) {
                $projects = @($me2.projects)
            }
        } catch {}
    }
    if ($projects.Count -gt 0) {
        Write-Host ""
        Write-Host "Your projects:"
        for ($i = 0; $i -lt $projects.Count; $i++) {
            Write-Host "  [$($i + 1)] $($projects[$i].name)  ($($projects[$i].id.Substring(0, [Math]::Min(8, $projects[$i].id.Length)))...)"
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

# ---- Step 4: Generate permanent token (if using short-lived install token) --------
# The install token may be one-time — create a permanent token now.
if (-not $IsLocalhost -and $Token -match "^sk_meridian_") {
    try {
        $body = '{"label":"hooks-installer"}'
        $r = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" `
            -Headers @{ Authorization = "Bearer $Token" } `
            -ContentType "application/json" -Body $body `
            -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
        if ($r.StatusCode -eq 201) {
            $tokenData = $r.Content | ConvertFrom-Json
            $Token = $tokenData.token
            Write-Host "  Permanent token created." -ForegroundColor Green
        }
    } catch {}
}

# ---- Step 5: Write .meridian/config -----------------------------------------------
$ConfigDir = Join-Path (Get-Location).Path ".meridian"
$ConfigFile = Join-Path $ConfigDir "config"

$existsAlready = Test-Path $ConfigFile
if ($existsAlready) {
    $overwrite = Read-Host ".meridian/config already exists. Update? [y/N]"
    if ($overwrite -notmatch "^[Yy]") {
        Write-Host "Skipping config write."
        # Still re-read project info for display
    } else {
        $existsAlready = $false
    }
}

if (-not $existsAlready) {
    if (-not (Test-Path $ConfigDir)) {
        New-Item -ItemType Directory -Path $ConfigDir | Out-Null
    }
    @"
url=$MeridianUrl
token=$Token
project_id=$ProjectId
"@ | Set-Content -Path $ConfigFile -Encoding UTF8
    Write-Host ""
    Write-Host "  Config written to $ConfigFile" -ForegroundColor Green
}

# ---- Add .meridian/ to .gitignore -------------------------------------------------
$GitignorePath = Join-Path (Get-Location).Path ".gitignore"
$MeridianGitignoreEntry = ".meridian/"
if (Test-Path $GitignorePath) {
    $existingContent = Get-Content $GitignorePath -Raw
    if ($existingContent -notmatch [regex]::Escape($MeridianGitignoreEntry)) {
        Add-Content -Path $GitignorePath -Value "`n$MeridianGitignoreEntry" -Encoding UTF8
        Write-Host "  Added .meridian/ to .gitignore" -ForegroundColor Green
    }
} else {
    Set-Content -Path $GitignorePath -Value $MeridianGitignoreEntry -Encoding UTF8
    Write-Host "  Created .gitignore with .meridian/" -ForegroundColor Green
}

# ---- Step 6: Write generic hooks to ~/.claude/settings.json -----------------------
$ClaudeSettingsPath = Join-Path $HOME ".claude\settings.json"
$ClaudeDetected = (Get-Command claude -ErrorAction SilentlyContinue) -ne $null -or (Test-Path $ClaudeSettingsPath)

if ($ClaudeDetected) {
    Write-Host ""
    Write-Host "Claude Code detected — writing hooks to $ClaudeSettingsPath"
    $ClaudeDir = Split-Path $ClaudeSettingsPath
    if (-not (Test-Path $ClaudeDir)) {
        New-Item -ItemType Directory -Path $ClaudeDir | Out-Null
    }

    $startCmd = Build-GenericStartCmd
    $stopCmd  = Build-GenericStopCmd

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

# ---- Step 7: Smoke test -----------------------------------------------------------
Write-Host ""
Write-Host "Testing hook..."
$testOk = $false
try {
    $testBody = "{`"project_id`":`"$ProjectId`",`"cwd`":`"$(Get-Location)`"}"
    $headers = @{ "Content-Type" = "application/json" }
    if (-not [string]::IsNullOrWhiteSpace($Token)) {
        $headers["Authorization"] = "Bearer $Token"
    }
    $r = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/hooks/session-start" `
        -Headers $headers -Body $testBody -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
    if ($r.StatusCode -eq 200) { $testOk = $true }
} catch {}
if ($testOk) {
    Write-Host "  OK hook responded successfully" -ForegroundColor Green
} else {
    Write-Host "  WARNING: hook test returned non-200 (hooks still installed)" -ForegroundColor Yellow
}

# ---- Done -------------------------------------------------------------------------
Write-Host ""
if ($ProjectName) {
    Write-Host "Done. Hooks installed for project '$ProjectName'." -ForegroundColor Green
} else {
    Write-Host "Done. Hooks installed for project $ProjectId." -ForegroundColor Green
}
Write-Host "Start a new Claude Code session to activate."
Write-Host ""
