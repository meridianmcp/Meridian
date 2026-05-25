#!/usr/bin/env bash
# Meridian — one-shot installer for Linux + macOS.
#
# Installs pixi (if missing), resolves the Meridian env, and prints how to start.
# Re-runnable: skips already-installed steps.
set -euo pipefail

err() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
ok()  { printf '\033[32m✓\033[0m %s\n' "$*"; }
say() { printf '\033[36m→\033[0m %s\n' "$*"; }

# 1. pixi
if ! command -v pixi >/dev/null 2>&1; then
  say "pixi not found — installing from pixi.sh ..."
  curl -fsSL https://pixi.sh/install.sh | bash
  # pixi.sh installs to ~/.pixi/bin; expose for this shell.
  export PATH="$HOME/.pixi/bin:$PATH"
  if ! command -v pixi >/dev/null 2>&1; then
    err "pixi install completed but pixi is still not on PATH. Add ~/.pixi/bin to your shell rc and retry."
  fi
  ok "pixi installed"
else
  ok "pixi already installed ($(pixi --version 2>/dev/null || echo unknown))"
fi

# 2. Repo sanity
if [ ! -f "pixi.toml" ]; then
  err "Run this from the Meridian repo root (no pixi.toml here)."
fi

# 3. Resolve env
say "Resolving Meridian environment (this can take ~30s on first run)..."
pixi install
ok "Environment ready"

# 4. Tell them what to do.
cat <<EOF

\033[32mMeridian is ready.\033[0m

  Start the local server:  \033[36mpixi run start\033[0m
  Dashboard:               \033[36mhttp://localhost:7878\033[0m
  Demo (read-only):        \033[36mhttp://localhost:7878/demo\033[0m

MCP wiring for Claude Code / Cursor / Windsurf:
  Run \033[36mpixi run mcp-install\033[0m or paste the snippet from
  https://docs.usemeridian.us/quickstart.

EOF
