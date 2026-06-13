# deploy.ps1 — build, smoke test, update SHA secret, deploy to Fly
# Usage: .\scripts\deploy.ps1 [-app meridian-hosted] [-preview]
param(
    [string]$app = "meridian-hosted",
    [switch]$preview
)
if ($preview) { $app = "meridian-preview" }

Set-Location $PSScriptRoot\..

Write-Host "=== 1. Building bundle ===" -ForegroundColor Cyan
node build.mjs
if ($LASTEXITCODE -ne 0) { Write-Host "Build FAILED" -ForegroundColor Red; exit 1 }

Write-Host "=== 2. Bundle smoke test ===" -ForegroundColor Cyan
node scripts/smoke_bundle.mjs
if ($LASTEXITCODE -ne 0) { Write-Host "Smoke test FAILED -- aborting deploy" -ForegroundColor Red; exit 1 }

Write-Host "=== 3. Updating MERIDIAN_GIT_SHA secret ===" -ForegroundColor Cyan
$sha = git rev-parse --short=12 HEAD
Write-Host "SHA: $sha"
flyctl secrets set MERIDIAN_GIT_SHA=$sha --app $app --stage 2>&1 | Out-Null

Write-Host "=== 4. Deploying to $app ===" -ForegroundColor Cyan
flyctl deploy --app $app --strategy immediate
if ($LASTEXITCODE -ne 0) { Write-Host "Deploy FAILED" -ForegroundColor Red; exit 1 }

Write-Host "=== Done ===" -ForegroundColor Green
