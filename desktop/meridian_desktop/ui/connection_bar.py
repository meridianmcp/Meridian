"""The connection bar: the strip across the top of the main window. Qt code, so it sits in ui/.

It states, in plain words and one colour per state, who is signed in and until when, what the desktop is connected to
(the hosted URL, with its state), and the local server when one is live. The check is connection_status.read_connection,
run on the thread pool. This widget only shows its result. It checks when the main window starts it, every five minutes,
and when the main window asks after a sign-in or a sign-out. It starts and stops no process. Its sign-in button only asks
the main window to start the Settings sign-in.

The job holds the store, the secret store, the transport, the probe and the clock, never the bar. Its result comes
back to _on_delivered on the GUI thread through the shared delivery object, and a result from an earlier check is dropped.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from meridian_desktop import connection_status as status
from meridian_desktop.hosted_client import Transport
from meridian_desktop.secret_store import SecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import make_selectable

CHECK_INTERVAL_MS = 5 * 60 * 1000

# (background, edge, text) per state. The state word in each line says the state too, so colour is not the only cue.
_STYLES = {
    status.STATE_CONNECTED: ("#e6f4ea", "#1e7b34", "#0b3d1a"),
    status.STATE_WARNING: ("#fff3cd", "#b26a00", "#5c3500"),
    status.STATE_RECONNECTING: ("#e7f0fd", "#1d5fd1", "#15397a"),
    status.STATE_NOT_CONNECTED: ("#fde7e7", "#c62828", "#7f1313"),
    status.STATE_CHECKING: ("#f1f1f1", "#8a8a8a", "#333333"),
}


@dataclass(frozen=True)
class _CheckDone:
    """One finished check: its generation, and either the snapshot with the hosted URL it used, or a failure name."""

    generation: int
    hosted_url: str
    snapshot: status.ConnectionSnapshot | None
    failure: str | None


def _checker(
    store: SettingsStore,
    secrets: SecretStore,
    transport: Transport | None,
    probe: Callable[[str], str],
    clock: Callable[[], float],
) -> Callable[[], tuple[str, status.ConnectionSnapshot]]:
    """The check the job runs. It reads Settings at the moment it runs, so a change in Settings applies at once."""

    def check() -> tuple[str, status.ConnectionSnapshot]:
        settings = store.load().settings
        snapshot = status.read_connection(
            hosted_url=settings.hosted_url,
            local_url=settings.local_url,
            secrets=secrets,
            transport=transport,
            probe=probe,
            clock=clock,
        )
        return settings.hosted_url, snapshot

    return check


class _CheckJob(QRunnable):
    """Run one connection check on the thread pool and deliver a _CheckDone. A check that raises is reduced to its class name."""

    def __init__(
        self,
        generation: int,
        work: Callable[[], tuple[str, status.ConnectionSnapshot]],
        owner: object,
        delivery: Delivery,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        try:
            hosted_url, snapshot = self._work()
            done = _CheckDone(self._generation, hosted_url, snapshot, None)
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            done = _CheckDone(self._generation, "", None, exc.__class__.__name__)
        try:
            self._delivery.delivered.emit(Delivered(self._owner, done))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class ConnectionBar(QFrame):
    """The top-of-window status strip. Three lines: signed in, hosted connection, local server.

    When the check says a sign-in would change something, a Sign in with browser button shows beside the first line. Clicking
    it emits signInRequested, and the main window starts the same sign-in as the Settings button.
    """

    signInRequested = Signal()

    def __init__(
        self,
        *,
        settings_store: SettingsStore,
        secrets: SecretStore,
        probe: Callable[[str], str],
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("connection-bar")
        self._store = settings_store
        self._secrets = secrets
        self._probe = probe
        self._transport = transport
        self._clock = clock
        # Identifies this bar's checks. A plain object, so a job holds it without holding the bar.
        self._owner = object()
        self._generation = 0
        self._hosted_url = settings_store.load().settings.hosted_url

        self._headline = make_selectable(QLabel(self))
        self._headline.setObjectName("connection-bar-headline")
        headline_font = self._headline.font()
        headline_font.setBold(True)
        headline_font.setPointSize(headline_font.pointSize() + 3)
        self._headline.setFont(headline_font)
        self._hosted = make_selectable(QLabel(self))
        self._hosted.setObjectName("connection-bar-hosted")
        self._local = make_selectable(QLabel(self))
        self._local.setObjectName("connection-bar-local")
        for label in (self._hosted, self._local):
            font = label.font()
            font.setBold(True)
            label.setFont(font)

        self._sign_in = QPushButton("Sign in with browser", self)
        self._sign_in.setObjectName("connection-bar-sign-in")
        self._sign_in.setVisible(False)
        self._sign_in.clicked.connect(self._request_sign_in)

        lines = QVBoxLayout(self)
        lines.setContentsMargins(12, 8, 12, 8)
        headline_row = QHBoxLayout()
        headline_row.addWidget(self._headline)
        headline_row.addWidget(self._sign_in)
        headline_row.addStretch(1)
        lines.addLayout(headline_row)
        detail = QHBoxLayout()
        detail.addWidget(self._hosted)
        detail.addSpacing(24)
        detail.addWidget(self._local)
        detail.addStretch(1)
        lines.addLayout(detail)

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        shared_delivery().delivered.connect(self._on_delivered)
        self._apply(status.checking_view(hosted_url=self._hosted_url))

    def start(self) -> None:
        """Run the first check now and then every five minutes. The main window calls this once, after it is built."""
        self._timer.start()
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        """Run one check on the pool. A result from an earlier check is dropped when it arrives."""
        self._generation += 1
        work = _checker(self._store, self._secrets, self._transport, self._probe, self._clock)
        QThreadPool.globalInstance().start(_CheckJob(self._generation, work, self._owner, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        """Runs on the GUI thread. A result for another bar, or from an earlier check, is dropped."""
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _CheckDone) or done.generation != self._generation:
            return
        if done.snapshot is None:
            self._apply(status.failed_view(hosted_url=self._hosted_url, failure=done.failure or "unknown"))
            return
        self._hosted_url = done.hosted_url
        self._apply(status.connection_view(done.snapshot, hosted_url=done.hosted_url))

    @Slot()
    def _request_sign_in(self) -> None:
        """The sign-in button was clicked. The main window opens Settings and starts its browser sign-in."""
        self.signInRequested.emit()

    def _apply(self, view: status.ConnectionView) -> None:
        """Show one view: its three lines, the sign-in button when the view offers it, and the colours of its state.

        The state is also a property, for tests.
        """
        background, edge, text = _STYLES[view.state]
        self._headline.setText(view.headline)
        self._headline.setVisible(bool(view.headline))
        self._hosted.setText(view.hosted)
        self._local.setText(view.local)
        self._sign_in.setVisible(view.sign_in_offered)
        self.setProperty("state", view.state)
        self.setStyleSheet(
            f"#connection-bar {{ background: {background}; border-left: 8px solid {edge}; border-radius: 4px; }}"
            f" #connection-bar QLabel {{ color: {text}; }}"
        )
