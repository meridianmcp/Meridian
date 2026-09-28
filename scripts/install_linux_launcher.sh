#!/usr/bin/env bash
# Meridian desktop launcher installer for Linux (sprint item 52cdabaa)
#
# Installs a .desktop application-menu entry for Meridian, and (optionally)
# a systemd --user unit so the Meridian server autostarts at login. Mirrors
# the OS-detection / systemd-unit pattern already shipped by
# install_tunnel.sh and install_watcher.sh in this same directory.
#
# Deliberately NOT an AppImage or .deb/.rpm package: Meridian already runs
# cleanly from a normal Python env on Linux (pinned decision b6a8f317 --
# fresh WSL Ubuntu-20.04, `pixi install` -> `python -m meridian` -> /health
# 200, zero Windows-only workarounds needed). There is no packaging problem
# to solve here -- the gap this script closes is pure UX: no icon, no
# app-menu entry, no autostart. AppImage would solve a relocatable-single-
# binary problem Meridian doesn't have (and still couldn't own .desktop/
# systemd registration itself); .deb/.rpm would mean maintaining two package
# formats plus signing/repo infrastructure, disproportionate to the actual
# gap. See sprint item 52cdabaa for the full rationale.
#
# Note (best-effort, not required by this script): a real system-tray ICON
# (as opposed to this app-menu launcher) would go through pystray, which on
# Linux auto-selects among three backends (AppIndicator > GTK > Xorg
# fallback) -- AppIndicator specifically is not installed by default on all
# distros. This script does not install a tray icon, so that dependency
# does not apply here; it is called out for whoever picks up an actual
# Linux tray-icon follow-up.
#
# Usage:
#   curl -fsSL https://usemeridian.us/install_linux_launcher.sh | bash
#   # or, to also enable autostart via systemd --user:
#   MERIDIAN_AUTOSTART=1 bash install_linux_launcher.sh

set -euo pipefail

# -- detect OS ----------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
  Linux*) : ;;
  *)
    echo "This installer is Linux-only."
    echo "macOS: see install_tunnel.sh (LaunchAgent) and the meridian-tray-mac build."
    echo "Windows: see install-windows.ps1."
    exit 1
    ;;
esac

# -- locate meridian ------------------------------------------------------
MERIDIAN_BIN="$(command -v meridian || true)"
if [ -z "$MERIDIAN_BIN" ]; then
  echo "error: 'meridian' was not found on PATH."
  echo "Install it first:  pip install meridian-server   (or:  pixi install)"
  exit 1
fi

# -- resolve target paths ------------------------------------------------------
APPS_DIR="$HOME/.local/share/applications"
ICONS_DIR="$HOME/.local/share/icons"
DESKTOP_FILE="$APPS_DIR/meridian.desktop"
ICON_NAME="meridian"
ICON_DEST="$ICONS_DIR/${ICON_NAME}.png"

mkdir -p "$APPS_DIR"

# -- resolve an icon (best-effort; a missing icon must never block install) --
ICON_SRC=""
if command -v python3 >/dev/null 2>&1; then
  ICON_SRC="$(python3 - <<'PYEOF' 2>/dev/null || true
import os

try:
    import meridian
except Exception:
    raise SystemExit(0)

candidate = os.path.join(os.path.dirname(meridian.__file__), "static", "icon-512.png")
print(candidate if os.path.isfile(candidate) else "")
PYEOF
)"
fi

if [ -n "$ICON_SRC" ]; then
  mkdir -p "$ICONS_DIR"
  cp -f "$ICON_SRC" "$ICON_DEST"
  ICON_VALUE="$ICON_NAME"
else
  # No bundled icon found (e.g. an unusual install layout) -- fall back to a
  # generic, always-present themed icon name rather than leaving Icon=
  # empty or pointing at a dangling path.
  ICON_VALUE="utilities-terminal"
fi

# -- write the .desktop launcher -----------------------------------------------
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=Meridian
Comment=Meridian coordination server and dashboard
Exec=${MERIDIAN_BIN}
Icon=${ICON_VALUE}
Terminal=false
Categories=Development;Utility;
StartupNotify=false
EOF
chmod 644 "$DESKTOP_FILE"

echo ""
echo "Meridian desktop launcher installed: $DESKTOP_FILE"
echo "It should now appear in your application menu (some desktop environments"
echo "need a re-login, or: update-desktop-database ~/.local/share/applications)."

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

# -- optional systemd --user unit for autostart --------------------------------
if [ "${MERIDIAN_AUTOSTART:-0}" = "1" ]; then
  SERVICE_DIR="$HOME/.config/systemd/user"
  SERVICE_FILE="$SERVICE_DIR/meridian.service"

  mkdir -p "$SERVICE_DIR"

  cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Meridian coordination server
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=${MERIDIAN_BIN}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable meridian.service
  systemctl --user start meridian.service

  echo ""
  echo "Meridian installed as systemd user service 'meridian' (autostart enabled)."
  echo "Tip: run 'loginctl enable-linger $USER' so it keeps running without an active login."
  echo ""
  echo "To check status: systemctl --user status meridian"
  echo "To uninstall:    systemctl --user disable --now meridian && rm '${SERVICE_FILE}'"
else
  echo ""
  echo "Autostart not enabled. Re-run with MERIDIAN_AUTOSTART=1 to also install a"
  echo "systemd --user unit that starts Meridian automatically at login."
fi
