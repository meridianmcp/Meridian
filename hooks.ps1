& {
# hooks.ps1 - Meridian Connect (Windows / PowerShell)
# Run: irm https://usemeridian.us/hooks.ps1 | iex

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

Write-Host "Checking $MeridianUrl ..."
if (-not (Test-ServerHealth -Url $MeridianUrl)) {
    Write-Host "  Error: Cannot reach $MeridianUrl/health -- is the server running?" -ForegroundColor Red
    return
}
Write-Host "  OK server is reachable" -ForegroundColor Green

# ---- Step 2: Authenticate --------------------------------------------------------
$Token = ''
$HooksDir    = Join-Path $HOME '.claude\hooks'
$startPsPath = Join-Path $HooksDir 'meridian-start.ps1'

if (Test-Path $startPsPath) {
    try {
        $pscontent = [System.IO.File]::ReadAllText($startPsPath)
        if ($pscontent -match '(?:Bearer |MERIDIAN_TOKEN: )(sk_meridian_[A-Za-z0-9_\-]+)') {
            $candidate = $Matches[1]
            $check = Get-MeResponse -Url $MeridianUrl -Token $candidate
            if ($null -ne $check) {
                $Token = $candidate
                Write-Host "  Found existing API key -- authenticated as: $($check.email)" -ForegroundColor Green
            }
        }
    } catch {}
}

if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Host "Opening browser to authenticate..."
    try { Start-Process "$MeridianUrl/auth/install-token" } catch {}
    $pastedToken = (Read-Host "Paste the token shown in your browser").Trim()
    Write-Host "Validating token..."
    $check = Get-MeResponse -Url $MeridianUrl -Token $pastedToken
    if ($null -eq $check) {
        Write-Host "  Error: token is invalid or expired." -ForegroundColor Red
        return
    }
    $Token = $pastedToken
    Write-Host "  Authenticated as: $($check.email)" -ForegroundColor Green
}

# ---- Step 3: Detect tools --------------------------------------------------------
$ClaudeSettingsPath = Join-Path $HOME '.claude\settings.json'
$CodexDir           = Join-Path $HOME '.codex'
$CodexConfigPath    = Join-Path $CodexDir 'config.toml'
$ClaudeDetected     = Test-Path $ClaudeSettingsPath
$CodexDetected      = Test-Path $CodexDir

# ---- Step 4: Write hook scripts --------------------------------------------------
$null = New-Item -ItemType Directory -Force $HooksDir
$enc  = New-Object System.Text.UTF8Encoding $false
$sp   = Join-Path $HooksDir 'meridian-start.ps1'
$tp   = Join-Path $HooksDir 'meridian-stop.ps1'

$startContent = @'
# MERIDIAN_TOKEN: __TOKEN__
# Meridian session-start hook -- self-healing
$fallback = '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'
function Get-Tok {
    try { $c=[System.IO.File]::ReadAllText("__SP__"); if($c -match '(?:Bearer |MERIDIAN_TOKEN: )(sk_meridian_[A-Za-z0-9_\-]+)'){return $Matches[1]} } catch {}
    try { $c=[System.IO.File]::ReadAllText("$env:USERPROFILE\.codex\config.toml"); if($c -match 'Bearer (sk_meridian_[A-Za-z0-9_\-]+)'){return $Matches[1]} } catch {}
    return $null
}
function Refresh-Tok {
    foreach ($ep in @("$env:USERPROFILE\Documents\Meridian\repository\.env")) {
        if(Test-Path $ep){
            $line = Get-Content $ep | Where-Object {$_ -match '^MERIDIAN_API_SECRET_KEY=(.+)'}
            if($line){
                $k=$line -replace 'MERIDIAN_API_SECRET_KEY=',''
                try{
                    $r=Invoke-RestMethod -Uri '__URL__/auth/tokens' -Method POST -Headers @{Authorization="Bearer $k";"Content-Type"='application/json'} -Body '{"label":"hooks-installer"}' -TimeoutSec 5
                    if($r.token){
                        $old=Get-Tok
                        foreach($f in @('__SP__','__TP__',"$env:USERPROFILE\.codex\config.toml")){
                            if(Test-Path $f){
                                $fc=[System.IO.File]::ReadAllText($f)
                                if($old){$fc=$fc -replace [regex]::Escape($old),$r.token}else{$fc=$fc -replace 'sk_meridian_[A-Za-z0-9_\-]+',$r.token}
                                [System.IO.File]::WriteAllText($f,$fc)
                            }
                        }
                        return $r.token
                    }
                }catch{}
            }
        }
    }
    return $null
}
function Invoke-Hook($tok){
    $cwd=(Get-Location).Path -replace '\\','/'
    $h=$env:COMPUTERNAME
    $b='{"cwd":"'+$cwd+'","hostname":"'+$h+'"}'
    try{
        $r=(Invoke-WebRequest -Method POST -Uri '__URL__/hooks/session-start' -Headers @{Authorization="Bearer $tok"} -ContentType 'application/json' -Body $b -UseBasicParsing -TimeoutSec 5).Content
        if($r -and $r.Contains('hookSpecificOutput')){return $r}
    }catch{}
    return $null
}
$tok=Get-Tok
$result=$null
if($tok){
    try{ $null=Invoke-RestMethod -Uri '__URL__/auth/me' -Headers @{Authorization="Bearer $tok"} -TimeoutSec 3; $result=Invoke-Hook $tok }
    catch{ $nt=Refresh-Tok; if($nt){$result=Invoke-Hook $nt} }
}else{ $nt=Refresh-Tok; if($nt){$result=Invoke-Hook $nt} }
if($result){$result}else{$fallback}
'@

$stopContent = @'
# MERIDIAN_TOKEN: __TOKEN__
$h=$env:COMPUTERNAME
$b='{"hostname":"'+$h+'"}'
try{Invoke-WebRequest -Method POST -Uri '__URL__/hooks/stop' -Headers @{Authorization='Bearer __TOKEN__'} -ContentType 'application/json' -Body $b -UseBasicParsing -TimeoutSec 5|Out-Null}catch{}
'@

$hd = $HooksDir.Replace('\','\\')
$startContent = $startContent.Replace('__URL__',$MeridianUrl).Replace('__TOKEN__',$Token).Replace('__SP__',$sp.Replace('\','\\')).Replace('__TP__',$tp.Replace('\','\\'))
$stopContent  = $stopContent.Replace('__URL__',$MeridianUrl).Replace('__TOKEN__',$Token)

[System.IO.File]::WriteAllText($sp, $startContent, $enc)
[System.IO.File]::WriteAllText($tp, $stopContent,  $enc)

$startCmd = "& `"$sp`""
$stopCmd  = "& `"$tp`""

# ---- Step 5: Handle existing hooks -----------------------------------------------
$ExistingHooks = $false
$SkipInstall   = $false
if ($ClaudeDetected -and (Test-Path $ClaudeSettingsPath)) {
    try {
        $ex = Get-Content $ClaudeSettingsPath -Raw | ConvertFrom-Json
        if ($ex.hooks -and ($ex.hooks.SessionStart -or $ex.hooks.Stop)) { $ExistingHooks = $true }
    } catch {}
}

if ($ExistingHooks) {
    Write-Host ""
    Write-Host "Existing Meridian hooks detected." -ForegroundColor Yellow
    $tv = $null -ne (Get-MeResponse -Url $MeridianUrl -Token $Token)
    if ($tv) {
        Write-Host "  Token is valid -- hooks are working." -ForegroundColor Green
        $ch = Read-Host "  (S)kip -- leave as-is / (U)pdate format / (R)egenerate key [S/u/r]"
        if ($ch -match '^[Uu]') {
            Write-Host "  Updating hooks..."
        } elseif ($ch -match '^[Rr]') {
            Write-Host "  Regenerating API key..."
            try {
                $r2 = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" -Headers @{Authorization="Bearer $Token"} -ContentType 'application/json' -Body '{"label":"hooks-installer"}' -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
                if ($r2.StatusCode -eq 201) {
                    $td2 = $r2.Content | ConvertFrom-Json
                    if ($td2.token) {
                        $Token = $td2.token
                        Write-Host "  New key generated." -ForegroundColor Green
                        $sc2 = $startContent -replace 'sk_meridian_[A-Za-z0-9_\-]+', $Token
                        $tc2 = $stopContent  -replace 'sk_meridian_[A-Za-z0-9_\-]+', $Token
                        [System.IO.File]::WriteAllText($sp, $sc2, $enc)
                        [System.IO.File]::WriteAllText($tp, $tc2, $enc)
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
        Write-Host "  Token invalid -- regenerating..." -ForegroundColor Yellow
        try {
            $r2 = Invoke-WebRequest -Method POST -Uri "$MeridianUrl/auth/tokens" -Headers @{Authorization="Bearer $Token"} -ContentType 'application/json' -Body '{"label":"hooks-installer"}' -UseBasicParsing -TimeoutSec 10 -ErrorAction SilentlyContinue
            if ($r2.StatusCode -eq 201) {
                $td2 = $r2.Content | ConvertFrom-Json
                if ($td2.token) {
                    $Token = $td2.token
                    $sc2 = $startContent -replace 'sk_meridian_[A-Za-z0-9_\-]+', $Token
                    $tc2 = $stopContent  -replace 'sk_meridian_[A-Za-z0-9_\-]+', $Token
                    [System.IO.File]::WriteAllText($sp, $sc2, $enc)
                    [System.IO.File]::WriteAllText($tp, $tc2, $enc)
                    $startCmd = "& `"$sp`""
                    $stopCmd  = "& `"$tp`""
                }
            }
        } catch {}
    }
}

if (-not $SkipInstall) {

# ---- Step 6: Write Claude Code settings ------------------------------------------
if ($ClaudeDetected) {
    Write-Host ""
    Write-Host "Claude Code detected -- writing hooks to $ClaudeSettingsPath"
    $ClaudeDir = Split-Path $ClaudeSettingsPath
    if (-not (Test-Path $ClaudeDir)) { New-Item -ItemType Directory -Path $ClaudeDir | Out-Null }
    if (Test-Path $ClaudeSettingsPath) {
        try { $settings = Get-Content $ClaudeSettingsPath -Raw | ConvertFrom-Json } catch { $settings = [PSCustomObject]@{} }
    } else { $settings = [PSCustomObject]@{} }
    if (-not $settings.PSObject.Properties['hooks']) {
        $settings | Add-Member -NotePropertyName 'hooks' -NotePropertyValue ([PSCustomObject]@{})
    }
    $settings.hooks | Add-Member -NotePropertyName 'SessionStart' -NotePropertyValue @(
        [PSCustomObject]@{ matcher=''; hooks=@([PSCustomObject]@{ type='command'; command=$startCmd; shell='powershell' }) }
    ) -Force
    $settings.hooks | Add-Member -NotePropertyName 'Stop' -NotePropertyValue @(
        [PSCustomObject]@{ matcher=''; hooks=@([PSCustomObject]@{ type='command'; command=$stopCmd; shell='powershell' }) }
    ) -Force
    $settings | ConvertTo-Json -Depth 10 | Set-Content $ClaudeSettingsPath -Encoding UTF8
    Write-Host "  OK hooks written" -ForegroundColor Green
}

# ---- Step 7: Write Codex config --------------------------------------------------
if ($CodexDetected) {
    Write-Host ""
    Write-Host "Codex detected -- writing MCP config"
    $null = New-Item -ItemType Directory -Force $CodexDir
    $mb = "`n# Meridian - added by hooks.ps1`n[mcp_servers.meridian]`ntype = `"http`"`nurl = `"$MeridianUrl/mcp`"`n`n[mcp_servers.meridian.http_headers]`nAuthorization = `"Bearer $Token`""
    if (Test-Path $CodexConfigPath) {
        $ec = [System.IO.File]::ReadAllText($CodexConfigPath)
        # Remove ALL existing meridian blocks (handles duplicates)
        $lines2 = $ec -split "`n"
        $kept = @(); $inBlock = $false
        foreach ($ln in $lines2) {
            if ($ln -match '^\[mcp_servers\.meridian' -or $ln -match '^# Meridian - added by hooks') { $inBlock = $true; continue }
            if ($inBlock -and $ln -match '^\[' -and $ln -notmatch 'mcp_servers\.meridian') { $inBlock = $false }
            if (-not $inBlock) { $kept += $ln }
        }
        $ec2 = ($kept -join "`n").TrimEnd() + $mb
    } else { $ec2 = $mb.TrimStart() }
    [System.IO.File]::WriteAllText($CodexConfigPath, $ec2, $enc)
    Write-Host "  OK Codex config written" -ForegroundColor Green
}

# ---- Step 8: Test ----------------------------------------------------------------
Write-Host ""
Write-Host "Testing hook..."
$tr = powershell -NoProfile -NonInteractive -Command "& '$sp'"
if ($tr -and $tr.Contains('hookSpecificOutput')) {
    Write-Host "  OK hook test passed" -ForegroundColor Green
} else {
    Write-Host "  Warning: unexpected output: $tr" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. Hooks installed for $MeridianUrl." -ForegroundColor Green
Write-Host "  claude --rc --permission-mode bypassPermissions"

} # end -not SkipInstall
}