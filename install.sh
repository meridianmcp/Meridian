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
# 50d2664d — resolve + print the exact release tag being downloaded so users can
# confirm they got the intended release, not a stale cached binary. Best-effort:
# no jq dependency, and a failed/absent lookup never aborts the install.
TAG="$(curl -fsSL "https://api.github.com/repos/meridianmcp/Meridian/releases/latest" 2>/dev/null \
  | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)"
if [ -n "$TAG" ]; then
  echo "Installing meridian-connect ${TAG} (latest release)."
else
  echo "Installing meridian-connect (latest release; could not resolve the exact version tag)."
fi
echo "Downloading meridian-connect for ${ARCH}-${PLATFORM}..."
curl -fsSL "https://github.com/meridianmcp/Meridian/releases/latest/download/${BINARY}" -o "$DEST"
chmod +x "$DEST"
echo "Running installer..."
"$DEST" "$@"
