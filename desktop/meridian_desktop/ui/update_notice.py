"""The update banner: a background check at launch and every six hours, and a status-bar bar that says what is happening.

The check runs on the thread pool through updater.check_and_stage. It downloads a release only after its signature
verifies, and it stages the release without touching the shortcuts. While a release downloads, the bar says so. When
it is staged, the bar says it is ready and offers two buttons: "Restart to update" starts the staged exe with
--after-update and then quits this app, and "Later" hides the bar until the next launch. The release page is shown as
plain selectable text, not as a button.

A check runs only when the caller's enabled() is true, when a transport and an install folder were given, and when
this is the frozen bundle. A check never starts while another is running or while a staged update waits for a restart.

The transport, the launcher and the quit callable are injected, so tests pass fakes and make no network call and start
no process. The job holds no widget: it emits to the shared delivery object, as ui/delivery.py describes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from meridian_desktop import updater
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import make_selectable

CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000  # every six hours while the app runs
DOWNLOADING_TEXT = "Downloading Meridian Desktop {version}..."
READY_TEXT = "Meridian Desktop {version} is ready. Restart to update."
RESTART_TEXT = "Restart to update"
LATER_TEXT = "Later"
PAGE_TEXT = "Release page: {url}"
# The result for a check that raised instead of returning. The Home card reads it as an offline check.
CHECK_FAILED_TEXT = "The update check could not finish. Try again later."

Launcher = Callable[[Path, str], None]


def downloading_text(version: str) -> str:
    """The label while a release downloads. The version is plain dotted digits, checked by updater."""
    return DOWNLOADING_TEXT.format(version=version)


def ready_text(version: str) -> str:
    """The label once a release is staged and waits for the restart."""
    return READY_TEXT.format(version=version)


def open_release_page(url: str) -> bool:
    """Open a release page in the browser with QDesktopServices.openUrl. Only the Home tab's Updates card calls this."""
    return bool(QDesktopServices.openUrl(QUrl(url)))


@dataclass(frozen=True)
class _Downloading:
    """A check has found a release and started its download. Delivered before the result."""

    version: str


def _quit_application() -> None:
    app = QApplication.instance()
    if app is not None:
        app.quit()


class _CheckJob(QRunnable):
    """Run one update check on the thread pool and deliver what it reports. A failure delivers None."""

    def __init__(
        self,
        current_version: str,
        transport: updater.Transport,
        install_root: Path,
        public_key: bytes | None,
        deliver: Callable[[object], None],
        record_apps_entry: updater.AppsEntryWriter | None = None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._current = current_version
        self._transport = transport
        self._root = install_root
        self._public_key = public_key
        self._deliver = deliver
        self._record_apps_entry = record_apps_entry

    def run(self) -> None:
        def announce(version: str) -> None:
            self._deliver(_Downloading(version))

        try:
            result: object = updater.check_and_stage(
                self._current,
                self._transport,
                self._root,
                public_key=self._public_key,
                announce=announce,
                record_apps_entry=self._record_apps_entry,
            )
        except Exception:  # noqa: BLE001 - a check must never raise into the pool; nothing is shown for it
            result = None
        try:
            self._deliver(result)
        except RuntimeError:
            pass  # the window was deleted while the check ran


class UpdateNoticeBar(QWidget):
    """The status-bar update bar: hidden unless a release is downloading or staged. See the module docstring.

    The Home tab's Updates card reads the same check through ``result``, ``checking``, ``enabled`` and
    ``release_page_url``, and hears ``resultChanged`` each time a download notice or the final result arrives.
    """

    # Emitted on the GUI thread for each notice this bar receives: a download starting, or the final result.
    resultChanged = Signal()

    def __init__(
        self,
        current_version: str,
        transport: updater.Transport | None,
        *,
        install_root: Path | None = None,
        enabled: Callable[[], bool] | None = None,
        report: Callable[[str], object] | None = None,
        launcher: Launcher | None = None,
        quit_app: Callable[[], None] | None = None,
        public_key: bytes | None = None,
        interval_ms: int = CHECK_INTERVAL_MS,
        parent: QWidget | None = None,
        record_apps_entry: updater.AppsEntryWriter | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("update-notice")
        self._current = current_version
        self._transport = transport
        self._root = Path(install_root) if install_root is not None else None
        self._enabled = enabled if enabled is not None else (lambda: True)
        self._report = report
        self._launcher: Launcher = launcher if launcher is not None else updater.launch_after_update
        self._quit = quit_app if quit_app is not None else _quit_application
        self._public_key = public_key
        self._record_apps_entry = record_apps_entry
        self._busy = False
        self._finished = False
        self._ready: str | None = None  # the staged version the bar offers to restart into
        self._dismissed = False  # Later was pressed: the staged bar stays hidden until the next launch
        self._result: object | None = None  # the last final check result, as the Home card reads it
        # Identifies this bar's results. A plain object, so a job can hold it without holding the bar.
        self._owner = object()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.check_now)
        shared_delivery().delivered.connect(self._on_delivered)

        self._label = make_selectable(QLabel("", self))
        self._label.setObjectName("update-notice-label")
        self._label.setTextFormat(Qt.TextFormat.PlainText)

        self._page = make_selectable(QLabel("", self))
        self._page.setObjectName("update-notice-page")
        self._page.setTextFormat(Qt.TextFormat.PlainText)

        self._restart = QPushButton(RESTART_TEXT, self)
        self._restart.setObjectName("update-notice-restart")
        self._restart.clicked.connect(lambda _checked=False: self.restart())

        self._later = QPushButton(LATER_TEXT, self)
        self._later.setObjectName("update-notice-later")
        self._later.clicked.connect(lambda _checked=False: self.later())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)
        layout.addWidget(self._page)
        layout.addWidget(self._restart)
        layout.addWidget(self._later)
        self.hide()

    # ----------------------------------------------------------- state

    @property
    def check_finished(self) -> bool:
        """True once a check result has arrived, whatever it was."""
        return self._finished

    @property
    def result(self) -> object | None:
        """The last final check result (an updater.UpdateResult), or None before one arrives."""
        return self._result

    @property
    def enabled(self) -> bool:
        """True when a check may run for this window: a transport was given and both update switches are on."""
        return self._transport is not None and self._enabled()

    @property
    def checking(self) -> bool:
        """True while the one check is running."""
        return self._busy

    @property
    def release_page_url(self) -> str:
        """The release page of the staged release, or "" when no release is staged."""
        return updater.release_page_url_for(self._ready) if self._ready is not None else ""

    @property
    def ready_version(self) -> str | None:
        """The staged version the bar offers to restart into. None unless the bar is showing it."""
        return None if self.isHidden() else self._ready

    @property
    def state(self) -> str:
        """"hidden", "downloading" or "ready": what the bar shows now."""
        if self.isHidden():
            return "hidden"
        return "ready" if self._ready is not None else "downloading"

    # ----------------------------------------------------------- check

    def start(self) -> None:
        """Show a staged update that waits for a restart, then check now and every six hours while checks are on."""
        if self._root is not None and updater.running_frozen():
            staged = updater.ready_version(self._root, self._current)
            if staged is not None:
                self._show_ready(staged)
        if self._transport is None:
            return
        self._timer.start()
        self.check_now()

    @Slot()
    def check_now(self) -> None:
        """Run one check on the thread pool. Nothing runs when checks are off, the bar has no transport or install folder,
        a check is already running, or a staged update is waiting for the restart. A source run (not the frozen bundle)
        makes no request: its Updates card gets the refused reason at once, so the card never waits for a result."""
        if self._transport is None or self._root is None or self._busy:
            return
        if not self._enabled():
            return
        if not updater.running_frozen():
            self._finished = True
            self._result = updater.UpdateResult("refused", reason=updater.DEV_RUN_REASON)
            self.resultChanged.emit()
            return
        if updater.ready_version(self._root, self._current) is not None:
            return
        self._busy = True
        # The job holds the version, the transport, the root, the owner token and the shared delivery. Never the bar.
        owner = self._owner
        delivery = shared_delivery()
        QThreadPool.globalInstance().start(
            _CheckJob(
                self._current,
                self._transport,
                self._root,
                self._public_key,
                lambda item: delivery.delivered.emit(Delivered(owner, item)),
                record_apps_entry=self._record_apps_entry,
            )
        )

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another bar is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        value = item.value
        if isinstance(value, _Downloading):
            self._show_downloading(value.version)
            self.resultChanged.emit()
            return
        self._busy = False
        self._finished = True
        if value is None:
            # A check that raised delivers None. The Home card shows it as an offline check, not as "not checked".
            value = updater.UpdateResult("offline", reason=CHECK_FAILED_TEXT)
        self._result = value
        self._show_final(value)
        self.resultChanged.emit()

    def _show_final(self, value: object) -> None:
        if isinstance(value, updater.UpdateResult) and value.status == "update_staged" and value.version:
            self._show_ready(value.version)
            return
        # Up to date, offline, refused, or a failed check: the bar is hidden. A refusal's reason goes to the status bar.
        self._ready = None
        self.hide()
        if isinstance(value, updater.UpdateResult) and value.status == "refused" and value.reason and self._report:
            self._report(value.reason)

    # ----------------------------------------------------------- what the bar shows

    def _show_downloading(self, version: str) -> None:
        self._ready = None
        self._label.setText(downloading_text(version))
        self._page.setText("")
        self._page.hide()
        self._restart.hide()
        self._later.hide()
        self.show()

    def _show_ready(self, version: str) -> None:
        self._ready = version
        if self._dismissed:
            self.hide()
            return
        self._label.setText(ready_text(version))
        self._page.setText(PAGE_TEXT.format(url=updater.release_page_url_for(version)))
        self._page.show()
        self._restart.show()
        self._later.show()
        self.show()

    # ----------------------------------------------------------- buttons

    def restart(self) -> None:
        """Start the staged exe with --after-update <running version>, detached, then quit this app.

        A launch that fails keeps this app running and says why in the bar. The staged update stays for the next try.
        """
        version = self._ready
        if version is None or self._root is None:
            return
        try:
            self._launcher(updater.exe_for(self._root, version), self._current)
        except updater.Refused as exc:
            self._label.setText(str(exc))
            return
        self._quit()

    def later(self) -> None:
        """Hide the staged bar until the next launch. The staged update itself is kept."""
        self._dismissed = True
        self.hide()


__all__ = [
    "CHECK_INTERVAL_MS",
    "LATER_TEXT",
    "PAGE_TEXT",
    "READY_TEXT",
    "RESTART_TEXT",
    "UpdateNoticeBar",
    "downloading_text",
    "open_release_page",
    "ready_text",
]
