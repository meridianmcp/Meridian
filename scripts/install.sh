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

# db03774e -- this fallback needs pixi, and this repo's pixi workspace declares
# only win-64, linux-64, osx-64 and osx-arm64 (pixi.toml `platforms`). On Linux
# arm64 `pixi install` therefore dies with an opaque "unsupported platform" error
# only AFTER the git clone. Say so up front, before any network access, and point
# at the published package, which does not go through pixi. (Intel macOS is
# osx-64 and IS supported here, so it is deliberately not blocked.)
case "$(uname -s)-$(uname -m)" in
  Linux-aarch64|Linux-arm64)
    {
      echo "error: the source-install fallback is not available on Linux arm64 (aarch64):"
      echo "the pixi workspace does not declare linux-aarch64, so 'pixi install' would fail."
      echo ""
      echo "Install the published package instead:"
      echo "  uv tool install meridian-server      (get uv: https://docs.astral.sh/uv/getting-started/installation/)"
      echo "  pipx install meridian-server         (or: pip install meridian-server)"
      echo "  meridian --tunnel --repo ."
    } >&2
    exit 1
    ;;
esac

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
