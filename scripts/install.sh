#!/usr/bin/env bash
set -e

REPO="https://github.com/meridianmcp/Meridian.git"
INSTALL_DIR="$HOME/.meridian"

echo "Installing Meridian..."

# ---- Primary path: uv tool install ------------------------------------------
# If `uv` is on PATH, install the published PyPI package as a uv tool. This is
# the preferred path — uv manages an isolated venv + a shim on PATH, and users
# upgrade with `uv tool upgrade meridian-server`. Falls back to the source
# clone + pixi install below if uv isn't installed or the install fails.
if command -v uv >/dev/null 2>&1; then
  echo "uv detected — installing meridian-server via uv tool install..."
  if uv tool install meridian-server; then
    echo ""
    echo "Installed meridian-server with uv."
    echo "If 'meridian' isn't found, run: uv tool update-shell  (then restart your shell)"
    echo "Run: meridian --tunnel --repo ."
    exit 0
  fi
  echo "uv tool install failed; falling back to source install." >&2
fi

# ---- Fallback path: clone repo + pixi ---------------------------------------

# Check dependencies
command -v git >/dev/null 2>&1 || { echo "git required"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 required"; exit 1; }

# Install pixi if not present
if ! command -v pixi >/dev/null 2>&1; then
  echo "Installing pixi..."
  curl -fsSL https://pixi.sh/install.sh | bash
  export PATH="$HOME/.pixi/bin:$PATH"
fi

# Clone or update
if [ -d "$INSTALL_DIR" ]; then
  echo "Updating existing install..."
  cd "$INSTALL_DIR" && git pull
else
  git clone "$REPO" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

# Install dependencies
pixi install

# Create launcher
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/meridian" << 'LAUNCHER'
#!/usr/bin/env bash
cd "$HOME/.meridian" && pixi run start "$@"
LAUNCHER
chmod +x "$HOME/.local/bin/meridian"

echo ""
echo "Meridian installed. Run: meridian"
echo "Dashboard: http://localhost:7878/dashboard"
