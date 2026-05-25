# Meridian — one-shot installer for Windows.
#
# Installs pixi (if missing), resolves the Meridian env, and prints how to start.
# Re-runnable: skips already-installed steps.

$ErrorActionPreference = 'Stop'

function Say($msg) { Write-Host "→ $msg" -ForegroundColor Cyan }
function Ok($msg)  { Write-Host "✓ $msg" -ForegroundColor Green }
function Die($msg) { Write-Host "✗ $msg" -ForegroundColor Red; exit 1 }

# 1. pixi
if (-not (Get-Command pixi -ErrorAction SilentlyContinue)) {
    Say "pixi not found — installing from pixi.sh ..."
    Invoke-WebRequest -UseBasicParsing https://pixi.sh/install.ps1 | Invoke-Expression
    # pixi.sh installs to %USERPROFILE%\.pixi\bin; expose for this shell.
    $env:Path = "$env:USERPROFILE\.pixi\bin;$env:Path"
    if (-not (Get-Command pixi -ErrorAction SilentlyContinue)) {
        Die "pixi install completed but pixi is still not on PATH. Restart your shell and retry, or add %USERPROFILE%\.pixi\bin to PATH manually."
    }
    Ok "pixi installed"
} else {
    $v = (pixi --version 2>$null)
    Ok "pixi already installed ($v)"
}

# 2. Repo sanity
if (-not (Test-Path "pixi.toml")) {
    Die "Run this from the Meridian repo root (no pixi.toml here)."
}

# 3. Resolve env
Say "Resolving Meridian environment (this can take ~30s on first run)..."
pixi install
if ($LASTEXITCODE -ne 0) { Die "pixi install failed" }
Ok "Environment ready"

# 4. Next steps
Write-Host ""
Write-Host "Meridian is ready." -ForegroundColor Green
Write-Host ""
Write-Host "  Start the local server:  " -NoNewline; Write-Host "pixi run start" -ForegroundColor Cyan
Write-Host "  Dashboard:               " -NoNewline; Write-Host "http://localhost:7878" -ForegroundColor Cyan
Write-Host "  Demo (read-only):        " -NoNewline; Write-Host "http://localhost:7878/demo" -ForegroundColor Cyan
Write-Host ""
Write-Host "MCP wiring for Claude Code / Cursor / Windsurf:"
Write-Host "  Run " -NoNewline; Write-Host "pixi run mcp-install" -ForegroundColor Cyan -NoNewline
Write-Host " or paste the snippet from https://docs.usemeridian.us/quickstart"
Write-Host ""
