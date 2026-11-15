"""Save one PNG per main tab of the desktop window, for quick visual checks on this machine.

Usage:
    python tools/ui_snapshot.py <out_dir>

The window is built offscreen. Arial is loaded from C:/Windows/Fonts/ARIAL.TTF when that file is there, and set as
the application font. The settings file is a temporary file, and the secrets are an in-memory store, so the real
settings and the real OS keychain are never read or written. No update check runs, because no transport is passed.

The tool waits for Home's boot checks, saves 00-home-after-boot.png, then saves one PNG for each tab in order, and
prints the path of every file it wrote. It starts no event loop and exits when the last PNG is saved.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

ARIAL_FILE = Path("C:/Windows/Fonts/ARIAL.TTF")
FONT_POINT_SIZE = 9
WINDOW_SIZE = (1280, 900)
BOOT_WAIT_MS = 8000
SETTLE_SECONDS = 0.3


class MemorySecrets:
    """An in-memory keychain backend. It holds values in a dict, so the OS keychain is never touched."""

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._values.pop((service, username), None)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "tab"


def pump(app, seconds: float) -> None:
    """Run queued Qt events for a short time, so results that arrive from the thread pool are shown."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def load_font(app) -> str:
    """Set Arial as the application font when the file is there. Returns the family that was set."""
    from PySide6.QtGui import QFont, QFontDatabase

    family = "Arial"
    if ARIAL_FILE.is_file():
        font_id = QFontDatabase.addApplicationFont(str(ARIAL_FILE))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if families:
            family = families[0]
    app.setFont(QFont(family, FONT_POINT_SIZE))
    return family


def save(window, path: Path) -> Path:
    window.grab().save(str(path), "PNG")
    return path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python tools/ui_snapshot.py <out_dir>", file=sys.stderr)
        return 2
    out_dir = Path(argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    from meridian_desktop.secret_store import KeyringSecretStore
    from meridian_desktop.settings_store import SettingsStore
    from meridian_desktop.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    load_font(app)

    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="meridian-ui-snapshot-") as temp_dir:
        store = SettingsStore(Path(temp_dir) / "settings.json")
        secrets = KeyringSecretStore(backend=MemorySecrets())
        window = MainWindow(store, secret_store=secrets, update_transport=None)
        window.resize(*WINDOW_SIZE)
        window.show()

        # Home's boot checks run on the thread pool. Wait for them to finish, then let their results paint.
        QThreadPool.globalInstance().waitForDone(BOOT_WAIT_MS)
        pump(app, 0.5)
        written.append(save(window, out_dir / "00-home-after-boot.png"))

        for index in range(window.tabs.count()):
            window.tabs.setCurrentIndex(index)
            # Each tab reads its data on the thread pool when it is shown. Wait for those reads, so the PNG is not a
            # loading state, then let their results paint.
            QThreadPool.globalInstance().waitForDone(BOOT_WAIT_MS)
            pump(app, SETTLE_SECONDS)
            name = slug(window.tabs.tabText(index))
            written.append(save(window, out_dir / f"{index + 1:02d}-{name}.png"))

        window.close()
        QThreadPool.globalInstance().waitForDone(BOOT_WAIT_MS)

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
