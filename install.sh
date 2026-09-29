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
RELEASE_URL="https://github.com/meridianmcp/Meridian/releases/latest/download"
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

# f66e8f23 -- the download lands in a temp file next to DEST and only replaces
# DEST after its SHA-256 has been checked against the release's SHA256SUMS
# (published by release.yml). Anything unverifiable is deleted and aborts the
# install: an unchecked binary must never be marked executable or run.
TMP="$(mktemp "${DEST}.XXXXXX")"
SUMS="${TMP}.sums"
trap 'rm -f "$TMP" "$SUMS"' EXIT INT TERM

fail() {
  echo "error: $*" >&2
  exit 1
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{ print tolower($1) }'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{ print tolower($1) }'
  else
    return 1
  fi
}

echo "Downloading meridian-connect for ${ARCH}-${PLATFORM}..."
curl -fsSL "${RELEASE_URL}/${BINARY}" -o "$TMP" \
  || fail "could not download ${RELEASE_URL}/${BINARY}"

if [ "${MERIDIAN_INSTALL_ALLOW_UNVERIFIED:-}" = "1" ]; then
  {
    echo "WARNING: MERIDIAN_INSTALL_ALLOW_UNVERIFIED=1 -- SKIPPING SHA-256 verification."
    echo "WARNING: the downloaded binary is being installed and run WITHOUT any integrity check."
  } >&2
else
  curl -fsSL "${RELEASE_URL}/SHA256SUMS" -o "$SUMS" \
    || fail "could not download ${RELEASE_URL}/SHA256SUMS, so the download cannot be verified. Nothing was installed. (Set MERIDIAN_INSTALL_ALLOW_UNVERIFIED=1 to skip verification at your own risk.)"
  EXPECTED="$(awk -v name="$BINARY" '{ f = $2; sub(/^\*/, "", f); if (f == name) { print tolower($1); exit } }' "$SUMS")"
  if ! printf '%s' "$EXPECTED" | grep -Eq '^[0-9a-f]{64}$'; then
    fail "SHA256SUMS has no valid checksum entry for ${BINARY}, so the download cannot be verified. Nothing was installed. (Set MERIDIAN_INSTALL_ALLOW_UNVERIFIED=1 to skip verification at your own risk.)"
  fi
  ACTUAL="$(sha256_of "$TMP")" \
    || fail "neither sha256sum nor shasum is available to verify the download. Nothing was installed."
  if [ "$ACTUAL" != "$EXPECTED" ]; then
    fail "SHA-256 MISMATCH for ${BINARY}: expected ${EXPECTED}, got ${ACTUAL}. The download was deleted and nothing was installed."
  fi
  echo "Checksum verified (sha256 ${ACTUAL})."
fi

chmod 755 "$TMP"
mv -f "$TMP" "$DEST"
echo "Running installer..."
"$DEST" "$@"
