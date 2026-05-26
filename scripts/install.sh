#!/usr/bin/env bash
set -e

REPO="https://github.com/meridianmcp/Meridian.git"
INSTALL_DIR="$HOME/.meridian"

echo "Installing Meridian..."

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
