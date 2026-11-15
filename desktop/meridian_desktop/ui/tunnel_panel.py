"""The Tunnel panel: the one place in the desktop for the tunnel. Qt code, so it sits in ui/.

It shows, top to bottom:

- Status: running or not, and when the tunnel was last checked. One read-only pass runs on the thread pool when the
  panel is first shown, and again when Check now is pressed or a Start or Stop has finished. The pass reads the status
  call and then the tail of the tunnel log.
- Start, Stop and Restart: the existing tunnel commands, through ScopeControls. Each one asks the owner first, in the same
  dialog as the rest of the runner controls, and the gate (tunnel_control_enabled and the core setup) turns them off with
  the reason shown. The Home tunnel card has a Start of its own, through the same ScopeControls; Stop and Restart are only here.
- Lanes: the direct lane and the tunnel lane. The tunnel lane shows whether it is live, and each lane gets one line on
  what it needs.
- Slots: the tunnel's slot ports, one row each, with the plugin each serves and the last connect-only port check on
  127.0.0.1 (listening or not). The check runs when Check now is pressed.
- Recent log: the last 50 lines of the tunnel log, already redacted. The panel only reads it.
- Custom connectors: the tunnel opt-in (off by default), and each saved connector with its tunnel flag.

The words and rows come from meridian_desktop/tunnel_panel_model.py. Saves go through the settings store, and a settings
file with a problem is never written over. Nothing here starts a process on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThreadPool, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import custom_connectors
from meridian_desktop import tunnel_panel_model as model
from meridian_desktop.custom_connectors import ConnectorError
from meridian_desktop.log_tail import LogTail, read_tunnel_log_tail
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.runner_controls import SCOPE_TUNNEL
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tunnel_status import TunnelStatus, read_tunnel_status
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.scope_controls import ScopeControls, tunnel_gate
from meridian_desktop.ui.text import enable_table_copy, make_selectable
from meridian_desktop.slot_ports import SlotPortResult, probe_slot_ports
from meridian_desktop.ui.tunnel_check import TunnelReadJob

NOT_SAVED_TEXT = "Not saved: the settings file could not be written."
PROBLEM_TEXT = "Not saved: the settings file has a problem. Fix or remove that problem first, then try again."
SAVED_TEXT = "Saved."
# The slot table and the log pane are capped so the panel does not grow past the tab. Each one scrolls inside its cap.
SLOT_TABLE_MAX_HEIGHT = 220
LOG_VIEW_MAX_HEIGHT = 180


def _text(parent: QWidget, name: str, text: str = "") -> QLabel:
    """A selectable label with a fixed object name. Plain text, wrapped, so a long line never cuts off."""
    label = make_selectable(QLabel(text, parent))
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


def _heading(parent: QWidget, name: str, text: str) -> QLabel:
    label = _text(parent, name, text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def _slot_table(parent: QWidget) -> QTableWidget:
    """The slot table. Its rows are the slot ports. Each state is not checked until a check fills it in."""
    table = QTableWidget(0, len(model.SLOT_COLUMNS), parent)
    table.setObjectName("tunnelpanel-slots")
    table.setHorizontalHeaderLabels(list(model.SLOT_COLUMNS))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setMaximumHeight(SLOT_TABLE_MAX_HEIGHT)
    enable_table_copy(table)  # read-only: Ctrl+C and the right-click Copy action copy the selected cells
    _fill_slot_table(table, model.slot_rows())
    return table


def _fill_slot_table(table: QTableWidget, rows: tuple) -> None:
    """Set the slot table's rows. The rows are the slot ports, in their fixed order, and only the cell text changes."""
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for column, text in enumerate((row.slot, str(row.port), row.serves, row.state)):
            table.setItem(row_index, column, QTableWidgetItem(text))


class TunnelPanel(QWidget):
    """The Tunnel section. Build it once; call refresh() to show the saved choices again.

    ``status_call`` is RunnerClient.status, or a fake in tests. It is called with the tunnel scope only, on the thread
    pool, and it is read-only. ``log_path`` is the tunnel log to show; None means the path the core writes (see
    log_tail.tunnel_log_path), and a test passes a temp file. The panel holds the settings store and the runner actions,
    never a window.
    """

    def __init__(
        self,
        store: SettingsStore,
        actions: RunnerActions,
        status_call: Callable[[str], RunnerResult],
        parent: QWidget | None = None,
        log_path: Path | None = None,
        port_probe: Callable[[], Sequence[SlotPortResult]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("tunnel-panel")
        self._store = store
        self._status_call = status_call
        self._log_path = log_path
        # None means the real connect-only port check. A test passes a fake; it is read at each check.
        self._port_probe = port_probe
        self._busy = False
        self._generation = 0
        self._read_once = False
        # Identifies this panel's checks. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        self._state: str | None = None
        shared_delivery().delivered.connect(self._on_delivered)

        heading = _heading(self, "tunnelpanel-heading", model.PANEL_TITLE)
        intro = _text(self, "tunnelpanel-intro", model.PANEL_INTRO)

        # Status: the headline, the check time and the detail, then Check now.
        self._headline = _heading(self, "tunnelpanel-headline", model.NOT_CHECKED_HEADLINE)
        self._checked = _text(self, "tunnelpanel-checked")
        self._detail = _text(self, "tunnelpanel-detail", model.PANEL_CHECK_HINT)
        self._check_button = QPushButton(model.CHECK_BUTTON_TEXT, self)
        self._check_button.setObjectName("tunnelpanel-check")
        self._check_button.clicked.connect(lambda _checked=False: self.check())
        status_row = QHBoxLayout()
        status_row.addWidget(self._check_button)
        status_row.addStretch(1)

        # Start, Stop and Restart reuse the existing tunnel commands. ScopeControls asks first, as the runner controls do.
        self._controls = ScopeControls(SCOPE_TUNNEL, actions, tunnel_gate, prefix="tunnel", parent=self)
        self._controls.actionFinished.connect(self.check)

        # Lanes.
        self._lanes_heading = _heading(self, "tunnelpanel-lanes-heading", model.LANES_HEADING)
        self._direct_lane = _text(self, "tunnelpanel-lane-direct")
        self._tunnel_lane = _text(self, "tunnelpanel-lane-tunnel")

        # Slots: the tunnel's slot ports, one row each. Each state is the last port check, or not checked yet.
        self._slots_heading = _heading(self, "tunnelpanel-slots-heading", model.SLOTS_HEADING)
        self._slots_intro = _text(self, "tunnelpanel-slots-intro", model.SLOTS_INTRO)
        self._slots = _slot_table(self)

        # Custom connectors: the opt-in, then each saved connector with its tunnel flag.
        self._connectors_heading = _heading(self, "tunnelpanel-connectors-heading", model.CONNECTORS_HEADING)
        self._connectors_intro = _text(self, "tunnelpanel-connectors-intro", model.CONNECTORS_INTRO)
        self._opt_in = QCheckBox(model.OPT_IN_TEXT, self)
        self._opt_in.setObjectName("tunnelpanel-opt-in")
        self._opt_in.toggled.connect(self._on_opt_in_toggled)
        self._connector_box = QWidget(self)
        self._connector_box.setObjectName("tunnelpanel-connector-list")
        self._connector_column = QVBoxLayout(self._connector_box)
        self._connector_column.setContentsMargins(0, 0, 0, 0)
        self._connectors_empty = _text(self, "tunnelpanel-connectors-empty", model.NO_CONNECTORS_TEXT)

        self._message = _text(self, "tunnelpanel-message")
        self._message.setVisible(False)

        # Recent log: the last lines of the tunnel log, read with each check. The pane is read-only and selectable.
        self._log_heading = _heading(self, "tunnelpanel-log-heading", model.LOG_HEADING)
        self._log_intro = _text(self, "tunnelpanel-log-intro", model.LOG_INTRO)
        self._log_message = _text(self, "tunnelpanel-log-message", model.LOG_NOT_READ_TEXT)
        self._log_view = QPlainTextEdit(self)
        self._log_view.setObjectName("tunnelpanel-log")
        self._log_view.setReadOnly(True)
        make_selectable(self._log_view)  # after setReadOnly, which otherwise allows mouse selection only
        self._log_view.setMaximumHeight(LOG_VIEW_MAX_HEIGHT)

        column = QVBoxLayout(self)
        column.addWidget(heading)
        column.addWidget(intro)
        column.addWidget(self._headline)
        column.addWidget(self._checked)
        column.addWidget(self._detail)
        column.addLayout(status_row)
        column.addWidget(self._controls)
        column.addWidget(self._lanes_heading)
        column.addWidget(self._direct_lane)
        column.addWidget(self._tunnel_lane)
        column.addWidget(self._slots_heading)
        column.addWidget(self._slots_intro)
        column.addWidget(self._slots)
        column.addWidget(self._connectors_heading)
        column.addWidget(self._connectors_intro)
        column.addWidget(self._opt_in)
        column.addWidget(self._connector_box)
        column.addWidget(self._connectors_empty)
        column.addWidget(self._message)
        column.addWidget(self._log_heading)
        column.addWidget(self._log_intro)
        column.addWidget(self._log_message)
        column.addWidget(self._log_view)

        self._render_lanes()
        self.refresh()

    # ----------------------------------------------------------- the status check

    def check(self) -> None:
        """Read the tunnel status and the tail of the tunnel log once, on the thread pool. A press while a read runs does
        nothing. Both reads are read-only."""
        if self._busy:
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._check_button.setEnabled(False)
        self._headline.setText(model.CHECKING_TEXT)
        self._checked.setText("")
        # The job holds the status call, the log path, this panel's owner token and the shared delivery object.
        # It never holds the panel.
        owner = self._owner
        delivery = shared_delivery()
        status_call = self._status_call
        log_path = self._log_path
        port_probe = self._port_probe if self._port_probe is not None else probe_slot_ports
        QThreadPool.globalInstance().start(
            TunnelReadJob(
                lambda: read_tunnel_status(status_call),
                lambda: read_tunnel_log_tail(log_path),
                lambda payload: delivery.delivered.emit(Delivered(owner, (generation, payload))),
                ports_work=port_probe,
            )
        )

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        """Runs on the GUI thread. A result for another panel, or one a newer check superseded, is dropped here."""
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        if not isinstance(item.value, tuple) or len(item.value) != 2:
            return
        generation, payload = item.value
        if generation != self._generation:
            return
        status, failure, tail, ports = (
            payload if isinstance(payload, tuple) and len(payload) == 4 else (None, None, None, None)
        )
        if status is not None and not isinstance(status, TunnelStatus):
            status, failure = None, None
        if tail is not None and not isinstance(tail, LogTail):
            tail = None
        if isinstance(ports, list):
            ports = [port for port in ports if isinstance(port, SlotPortResult)]
        else:
            ports = None
        self._busy = False
        self._check_button.setEnabled(True)
        self._show_status(status, failure)
        self._show_log(tail)
        self._show_slots(ports)

    def _show_status(self, status: TunnelStatus | None, failure: str | None) -> None:
        shown = model.panel_status(status, failure)
        self._headline.setText(shown.headline)
        self._checked.setText(shown.checked)
        self._detail.setText(shown.detail)
        self._state = status.state if status is not None else None
        self._render_lanes()

    def _show_slots(self, results: list[SlotPortResult] | None) -> None:
        """Show the last port check in the slot table. None means the check gave no results, so the rows say not checked."""
        _fill_slot_table(self._slots, model.slot_rows(results))

    def _show_log(self, tail: LogTail | None) -> None:
        """Show the last lines of the log read, and say why there are none. The newest line stays in view."""
        self._log_view.setPlainText("\n".join(model.log_lines(tail)))
        scroll = self._log_view.verticalScrollBar()
        scroll.setValue(scroll.maximum())
        message = model.log_message(tail)
        self._log_message.setText(message)
        self._log_message.setVisible(bool(message))

    def _render_lanes(self) -> None:
        direct, tunnel = model.lanes(self._state)
        self._direct_lane.setText(f"{direct.name}. {direct.live} {direct.needs}")
        self._tunnel_lane.setText(f"{tunnel.name}. {tunnel.live} {tunnel.needs}")

    # ----------------------------------------------------------- showing the saved choices

    def refresh(self) -> None:
        """Show the saved choices again. Showing them saves nothing, and reads no status."""
        settings = self._store.load().settings
        self._opt_in.blockSignals(True)
        self._opt_in.setChecked(settings.custom_tunnel_opt_in)
        self._opt_in.blockSignals(False)
        self._render_connectors(settings.custom_connectors, settings.custom_tunnel_opt_in)
        self._controls.refresh_enabled()

    def _render_connectors(self, raw: tuple, opt_in: bool) -> None:
        self._clear_connectors()
        rows = model.connector_rows(raw, opt_in)
        for row in rows:
            frame = QWidget(self._connector_box)
            frame.setObjectName(f"tunnelpanel-connector-{row.index}")
            column = QVBoxLayout(frame)
            column.setContentsMargins(0, 4, 0, 4)
            name = _heading(frame, f"tunnelpanel-connector-{row.index}-name", row.name or "(unnamed)")
            note = _text(frame, f"tunnelpanel-connector-{row.index}-note", row.note)
            exposed = QCheckBox(model.CONNECTOR_TUNNEL_TEXT, frame)
            exposed.setObjectName(f"tunnelpanel-connector-{row.index}-exposed")
            exposed.setChecked(row.tunnel_exposed)
            exposed.setEnabled(row.usable)
            exposed.toggled.connect(lambda checked, i=row.index: self._on_exposed_toggled(i, checked))
            column.addWidget(name)
            column.addWidget(note)
            column.addWidget(exposed)
            self._connector_column.addWidget(frame)
        self._connectors_empty.setVisible(not rows)

    def _clear_connectors(self) -> None:
        while self._connector_column.count():
            item = self._connector_column.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)  # detached at once, so a lookup by name finds only the new rows
                widget.deleteLater()

    def _show(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    # ----------------------------------------------------------- saving

    def _save_changes(self, **changes: object) -> bool:
        """Save the changed fields through the store. Every other field in the file is kept. Refused while the file has
        a problem, so nothing is written over a file the app could not read. The saved choices show again either way."""
        result = self._store.load()
        if result.warning is not None:
            self._show(PROBLEM_TEXT)
            self.refresh()
            return False
        try:
            self._store.save(replace(result.settings, **changes))
        except OSError:
            self._show(NOT_SAVED_TEXT)
            self.refresh()
            return False
        self._show(SAVED_TEXT)
        self.refresh()
        return True

    def _on_opt_in_toggled(self, checked: bool) -> None:
        self._save_changes(custom_tunnel_opt_in=bool(checked))

    def _on_exposed_toggled(self, index: int, checked: bool) -> None:
        raw = self._store.load().settings.custom_connectors
        try:
            updated = custom_connectors.set_tunnel_exposed(raw, index, checked)
        except ConnectorError as exc:
            self._show(str(exc))
            self.refresh()
            return
        self._save_changes(custom_connectors=updated)

    # ----------------------------------------------------------- events

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._read_once:
            self._read_once = True
            self.check()


__all__ = ["NOT_SAVED_TEXT", "PROBLEM_TEXT", "SAVED_TEXT", "TunnelPanel"]
