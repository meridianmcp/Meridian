"""System tray icon with Show and Quit. The icon is generated at runtime
(a solid QPixmap), so the repo ships no binary assets.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

ICON_SIZE = 64
ICON_COLOR = "#2B6CB0"


def make_icon(size: int = ICON_SIZE) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(ICON_COLOR))
    return QIcon(pixmap)


class MeridianTray(QSystemTrayIcon):
    """Tray icon whose menu offers Show (restore the window) and Quit."""

    def __init__(
        self,
        window: QMainWindow,
        app: QApplication,
        parent: object | None = None,
    ) -> None:
        super().__init__(make_icon(), parent)
        self._window = window
        self.setToolTip("Meridian")

        self._menu = QMenu()
        show_action = self._menu.addAction("Show")
        show_action.triggered.connect(self._show_window)
        quit_action = self._menu.addAction("Quit")
        quit_action.triggered.connect(app.quit)
        self.setContextMenu(self._menu)

    def _show_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()
