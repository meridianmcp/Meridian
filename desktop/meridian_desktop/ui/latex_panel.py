"""LaTeX engine panel for the Writing tab: one probe of the local engine, with both versions shown.

This is Qt code and sits in ui/, so the boundary check allows it. The panel makes no
request when it is built. It probes when it is first shown and again when Check again
is pressed. The probe runs on the thread pool, and the engine address comes from the
settings file, so the UI thread never blocks. Results are plain text (PlainText labels),
so engine text is never read as HTML.

The panel shows running or not running, the engine and extension versions when the engine
answers, what the probe found, and one sentence on how to start the engine.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from meridian_desktop import latex_client
from meridian_desktop.latex_client import PROBE_TIMEOUT, EngineStatus
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import make_selectable

UNCHECKED_TEXT = "LaTeX engine: not checked yet"
CHECKING_TEXT = "LaTeX engine: checking..."
CONNECTED_TEXT = "LaTeX engine: running."
NOT_CONNECTED_TEXT = "LaTeX engine: not running."
VERSION_UNAVAILABLE = "not available"
FAILED_TEXT = "The check failed unexpectedly ({})."
UNCHECKED_START = (
    "start the LaTeX engine you installed on this computer if it is not running, then press Check again."
)


@dataclass(frozen=True)
class _Probe:
    """One probe's result, with the port it checked. The port is text, and it never holds the host."""

    port: str
    status: EngineStatus | None


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _probe(settings: SettingsStore, client: Any) -> _Probe:
    """Read the engine address from settings, then probe it. Runs on a worker thread.

    A module-level function over the settings store and the client only, so the job never holds the panel.
    """
    url = settings.load().settings.engine_url
    status = client.probe_engine(url, PROBE_TIMEOUT)
    return _Probe(port=latex_client.port_text(url), status=status)


def engine_version_text(status: EngineStatus | None) -> str:
    """The engine version line. Versions are shown only for a connected engine."""
    if status is None or not status.connected:
        return f"Engine version: {VERSION_UNAVAILABLE}"
    if status.engine_version is None:
        # The engine has no version field, so the probe reports None and the panel says so.
        return f"Engine version: {VERSION_UNAVAILABLE}"
    return f"Engine version: {status.engine_version}"


def extension_version_text(status: EngineStatus | None) -> str:
    """The extension version line. Versions are shown only for a connected engine."""
    if status is None or not status.connected:
        return f"Extension build: {VERSION_UNAVAILABLE}"
    return f"Extension build: {status.extension_version}"


def engine_start_text(status: EngineStatus | None, port: str) -> str:
    """One plain sentence on how to start the engine, for what the probe found.

    ``port`` is the port the probe checked. It is used only for the states that name it.
    """
    if status is None:
        return "start the LaTeX engine you installed on this computer, then press Check again."
    if status.connected:
        return "it is already running, so there is nothing to start."
    if status.state == latex_client.ENGINE_NOT_RUNNING:
        return (
            f"start the LaTeX engine you installed on this computer so it listens on port {port}, "
            "then press Check again."
        )
    if status.state == latex_client.ENGINE_NOT_ANSWERING:
        return (
            f"stop the other program on port {port}, then start the LaTeX engine you installed "
            "and press Check again."
        )
    if status.message == latex_client.NOT_LOOPBACK_MESSAGE:
        return (
            "change the engine address in the desktop settings to one on this computer, "
            "such as http://127.0.0.1:8471, then press Check again."
        )
    return "check that the LaTeX engine you installed is running on this computer, then press Check again."


class _ProbeJob(QRunnable):
    """Run the probe on the thread pool and deliver (probe, failure_text).

    The probe maps its own loopback errors to a status. Any other exception is reduced
    to its class name here, in the worker, so a raw exception never reaches the UI thread.
    """

    def __init__(self, work: Callable[[], Any], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        probe: Any = None
        failure: str | None = None
        try:
            probe = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._deliver((probe, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class LatexPanel(QWidget):
    """The LaTeX section of the Writing tab: running or not, with both versions side by side."""

    def __init__(
        self,
        settings_store: SettingsStore,
        client: Any = latex_client,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("latex-panel")
        self._settings = settings_store
        self._client = client
        self._probed_on_show = False
        self._probing = False
        # Identifies this panel's probes. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        # Each probe takes the next number. A result with an older number is dropped.
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self._status = _label(self, "latex-status", UNCHECKED_TEXT)
        layout.addWidget(self._status)

        self._detail = _label(self, "latex-detail", "")
        layout.addWidget(self._detail)

        self._start = _label(self, "latex-start", f"How to start: {UNCHECKED_START}")
        layout.addWidget(self._start)

        versions = QHBoxLayout()
        self._engine_version = _label(self, "latex-engine-version", engine_version_text(None))
        self._extension_version = _label(self, "latex-extension-version", extension_version_text(None))
        versions.addWidget(self._engine_version)
        versions.addWidget(self._extension_version)
        versions.addStretch(1)
        layout.addLayout(versions)

        action_row = QHBoxLayout()
        self._check_button = QPushButton("Check again", self)
        self._check_button.setObjectName("latex-check")
        action_row.addWidget(self._check_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self._check_button.clicked.connect(self._on_check_clicked)

    # ----------------------------------------------------------- probe

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._probed_on_show:
            self._probed_on_show = True
            self._start_probe()

    @Slot()
    def _on_check_clicked(self) -> None:
        self._start_probe()

    def _start_probe(self) -> None:
        if self._probing:
            return
        self._probing = True
        self._generation += 1
        generation = self._generation
        self._check_button.setEnabled(False)
        self._status.setText(CHECKING_TEXT)
        self._detail.setText("")
        # The job holds the probe's inputs (the settings store and the client), this panel's owner token and the
        # shared delivery object. It never holds the panel, and the deliver closure captures no widget.
        work = functools.partial(_probe, self._settings, self._client)
        owner = self._owner
        delivery = shared_delivery()

        def deliver(payload: tuple) -> None:
            delivery.delivered.emit(Delivered(owner, (generation, payload)))

        QThreadPool.globalInstance().start(_ProbeJob(work, deliver))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel, or for an older probe, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, payload = item.value
        if generation != self._generation:
            return
        self._on_probe_finished(payload)

    def _on_probe_finished(self, payload: tuple) -> None:
        probe, failure = payload
        self._probing = False
        self._check_button.setEnabled(True)
        if failure is not None or not isinstance(probe, _Probe) or not isinstance(probe.status, EngineStatus):
            self._show_result(None, failure or FAILED_TEXT.format("NoResult"), "")
            return
        self._show_result(probe.status, probe.status.message, probe.port)

    def _show_result(self, status: EngineStatus | None, detail: str, port: str) -> None:
        connected = status is not None and status.connected
        self._status.setText(CONNECTED_TEXT if connected else NOT_CONNECTED_TEXT)
        self._detail.setText(detail)
        self._start.setText(f"How to start: {engine_start_text(status, port)}")
        self._engine_version.setText(engine_version_text(status))
        self._extension_version.setText(extension_version_text(status))


__all__ = [
    "CONNECTED_TEXT",
    "NOT_CONNECTED_TEXT",
    "UNCHECKED_TEXT",
    "LatexPanel",
    "engine_start_text",
    "engine_version_text",
    "extension_version_text",
]
