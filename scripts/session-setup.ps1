# Per-session Meridian setup for Claude Code and Codex: set up the git repo that the chat opened in.
#
# Run from a SessionStart hook. It does nothing outside a git repo, and it always exits 0, so a failed setup never
# blocks a session. It enters this Meridian checkout first, so `python -m meridian` imports this checkout's package.

$ErrorActionPreference = 'SilentlyContinue'

$repo = $env:CLAUDE_PROJECT_DIR
if (-not $repo) { $repo = (Get-Location).Path }

$checkout = Split-Path -Parent $PSScriptRoot
$pixi = (Get-Command pixi -ErrorAction SilentlyContinue).Source

if ($pixi -and
    (Test-Path -LiteralPath (Join-Path $repo '.git')) -and
    (Test-Path -LiteralPath (Join-Path $checkout 'pixi.toml'))) {
    Push-Location -LiteralPath $checkout
    try {
        & $pixi run python -m meridian setup --repo $repo --host claude-code --host codex --host cursor *> $null
    } finally {
        Pop-Location
    }
}

exit 0
