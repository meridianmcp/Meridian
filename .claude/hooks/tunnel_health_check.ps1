# Best-effort launcher for the shared tunnel health check implementation.
$ErrorActionPreference = 'SilentlyContinue'
$empty = '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Output $empty
    exit 0
}

try {
    $helper = Join-Path $PSScriptRoot 'tunnel_health_check.py'
    $output = @(& $python.Source $helper 2>$null)
    if ($LASTEXITCODE -eq 0 -and $output.Count -gt 0) {
        $output | ForEach-Object { Write-Output $_ }
    } else {
        Write-Output $empty
    }
} catch {
    Write-Output $empty
}
exit 0
