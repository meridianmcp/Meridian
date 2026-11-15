"""The restart notice: a strip above the tabs that shows when code or the local server changed on disk. GUI thread only.

Every CHECK_INTERVAL_MS the check runs on the GUI thread. It stats the tracked files, makes one process query for a server
this window started, and spends at most MAX_ENTRIES_PER_TICK steps on unreadable folders and files from that server.
The first walk of that folder runs on the thread that confirmed the start. Dismiss hides the strip. It comes
back only when the set of changed files or the server state is different from the set the owner dismissed. When the
notice clears, the dismissed set is forgotten too, so a later change of the same kind is shown again.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from meridian_desktop.restart_check import Restart, RestartWatch, banner_text
from meridian_desktop.ui.text import make_selectable

CHECK_INTERVAL_MS = 30_000
DISMISS_TEXT = "Dismiss"


class RestartBanner(QFrame):
    def __init__(self, watch: RestartWatch, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("restart-banner")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._watch = watch
        self._dismissed_key: tuple[tuple[str, ...], bool] | None = None
        self._restart = Restart()

        self._label = make_selectable(QLabel(self))
        self._label.setWordWrap(True)
        self._dismiss = QPushButton(DISMISS_TEXT, self)
        self._dismiss.setObjectName("restart-banner-dismiss")
        self._dismiss.clicked.connect(self._on_dismiss)
        row = QHBoxLayout(self)
        row.addWidget(self._label, 1)
        row.addWidget(self._dismiss)

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.check_now)
        self._timer.start()
        self.hide()

    @property
    def restart(self) -> Restart:
        """The result of the last check."""
        return self._restart

    def check_now(self) -> None:
        """Compare the disk with the baseline and show or hide the strip. The timer calls this every interval."""
        self._restart = self._watch.check()
        if not self._restart.needed:
            # The notice has cleared, so a later change is new even if it matches one the owner dismissed before.
            self._dismissed_key = None
            self.hide()
            return
        if self._key(self._restart) == self._dismissed_key:
            self.hide()
            return
        self._label.setText(banner_text(self._restart))
        self.show()

    def _on_dismiss(self) -> None:
        self._dismissed_key = self._key(self._restart)
        self.hide()

    @staticmethod
    def _key(restart: Restart) -> tuple[tuple[str, ...], bool]:
        return (restart.code, restart.server)
