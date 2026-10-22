#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-tray-mac must run on macOS so iconutil and the native PyInstaller bootloader are available." >&2
  exit 2
fi

source_icon="$repo_root/meridian/static/icon-192.png"
iconset="$repo_root/dist/meridian-tray.iconset"
icns="$repo_root/dist/meridian-tray.icns"
rm -rf "$iconset"
mkdir -p "$iconset"
trap 'rm -rf "$iconset"' EXIT

for spec in \
  "16 icon_16x16.png" \
  "32 icon_16x16@2x.png" \
  "32 icon_32x32.png" \
  "64 icon_32x32@2x.png" \
  "128 icon_128x128.png" \
  "256 icon_128x128@2x.png" \
  "256 icon_256x256.png" \
  "512 icon_256x256@2x.png" \
  "512 icon_512x512.png" \
  "1024 icon_512x512@2x.png"; do
  read -r size name <<< "$spec"
  sips -z "$size" "$size" "$source_icon" --out "$iconset/$name" >/dev/null
done

iconutil -c icns "$iconset" -o "$icns"
pyinstaller meridian-tray.spec --clean --noconfirm
