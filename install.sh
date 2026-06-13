#!/usr/bin/env sh
set -e
PLATFORM="$(uname -s)"
ARCH="$(uname -m)"
case "$PLATFORM" in
  Darwin) PLATFORM="apple-darwin" ;;
  Linux)  PLATFORM="unknown-linux" ;;
esac
case "$ARCH" in
  arm64|aarch64) ARCH="aarch64" ;;
  x86_64)        ARCH="x86_64" ;;
esac
BINARY="meridian-connect-${ARCH}-${PLATFORM}"
DEST="${MERIDIAN_BIN_DIR:-$HOME/.local/bin}/meridian-connect"
mkdir -p "$(dirname "$DEST")"
echo "Downloading meridian-connect for ${ARCH}-${PLATFORM}..."
curl -fsSL "https://github.com/meridianmcp/Meridian/releases/latest/download/${BINARY}" -o "$DEST"
chmod +x "$DEST"
echo "Running installer..."
"$DEST" "$@"
