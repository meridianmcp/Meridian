"""The Tools panel: which Meridian tools each AI client has set up, and whether the local server and tunnel are live.

It shows the rows that tools_status.read_tools_status returns: each Meridian entry in each client's
settings, the local Meridian server and the tunnel. Each row says whether the tool is configured and
whether it is live, with one plain sentence on what to do.

Every read runs on the thread pool, through the injected status provider, so the window stays responsive.
The panel reads once when it is first shown, and again each time Refresh is pressed. A second press while
a read is running does nothing. There is no start, stop or install control, and no tool is started or stopped.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRunnable, QThreadPool, QTimer, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.tools_status import ToolsStatus, status_line
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

HEADING_TEXT = "Tools"
INTRO_TEXT = (
    "Which Meridian tools are set up for each AI client, and whether the local Meridian server and the tunnel "
    "are live. The AI clients start their own tools, so this panel cannot see whether those are running. "
    "No tool is started or stopped."
)
REFRESH_TEXT = "Refresh"
WAITING_TEXT = "Press Refresh to read the tools. No tool is started or stopped."
CHECKING_TEXT = "Checking the tools. No tool is started or stopped."
NO_RESULT_TEXT = "The tools check gave no result."
# A read that has not finished after this many seconds stops holding Refresh. The read is not cancelled: if it
# finishes, its answer still shows, unless a newer Refresh has replaced it. Nothing else changes.
STALL_SECONDS = 60
STALLED_TEXT = (
    "The tools check has taken more than {} seconds. It may be waiting on the Meridian core. "
    "Press Refresh to check again. No tool is started or stopped."
).format(STALL_SECONDS)
COLUMNS = ("Client", "Tool", "Status", "Configured", "Live", "What to do")


class _RefreshJob(QRunnable):
    """Run one status read on the thread pool and hand the ToolsStatus back to the panel."""

    def __init__(self, provider: Callable[[], ToolsStatus], deliver: Callable[[object], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._provider = provider
        self._deliver = deliver

    def run(self) -> None:
        try:
            status: object = self._provider()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            # The class name only: an exception's text could hold a path or a value.
            status = ToolsStatus(rows=(), message=f"The tools check failed ({exc.__class__.__name__}).")
        try:
            self._deliver(status)
        except RuntimeError:
            pass  # the panel was deleted while the read ran


class ToolsPanel(QWidget):
    """The Tools page: one read-only table of tool rows, a status line with the time checked, and Refresh."""

    def __init__(self, status_provider: Callable[[], ToolsStatus], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tools-panel")
        self._provider = status_provider
        self._busy = False
        self._read_once = False
        # Each read takes the next number. A result with an older number is dropped, so a stalled read cannot
        # overwrite the answer of a newer Refresh.
        self._generation = 0
        # Identifies this panel's reads. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        self._stall = QTimer(self)
        self._stall.setSingleShot(True)
        self._stall.setInterval(int(STALL_SECONDS * 1000))
        self._stall.timeout.connect(self._on_stalled)

        heading = make_selectable(QLabel(HEADING_TEXT))
        heading.setObjectName("tools-heading")
        intro = make_selectable(QLabel(INTRO_TEXT))
        intro.setObjectName("tools-intro")
        intro.setWordWrap(True)

        self._refresh = QPushButton(REFRESH_TEXT)
        self._refresh.setObjectName("tools-refresh")
        self._refresh.clicked.connect(lambda _checked=False: self.refresh())

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setObjectName("tools-table")
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._table.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._table)

        self._status = make_selectable(QLabel(WAITING_TEXT))
        self._status.setObjectName("tools-status")
        self._status.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(heading)
        top.addStretch(1)
        top.addWidget(self._refresh)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(intro)
        layout.addWidget(self._table)
        layout.addWidget(self._status)

        shared_delivery().delivered.connect(self._on_delivered)

    # ----------------------------------------------------------- reading

    def refresh(self) -> None:
        """Rerun the reads on the thread pool. A call while a read is running does nothing."""
        if self._busy:
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._refresh.setEnabled(False)
        self._status.setText(CHECKING_TEXT)
        self._stall.start()
        # The job holds the provider, the owner token and the shared delivery object. It never holds this panel.
        owner = self._owner
        delivery = shared_delivery()
        QThreadPool.globalInstance().start(
            _RefreshJob(
                self._provider,
                lambda status: delivery.delivered.emit(Delivered(owner, (generation, status))),
            )
        )

    @Slot()
    def _on_stalled(self) -> None:
        """The read has taken too long. Refresh is pressable again, and the status says so. The read is not cancelled."""
        if not self._busy:
            return
        self._busy = False
        self._refresh.setEnabled(True)
        self._status.setText(STALLED_TEXT)

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel is dropped here, and so is one a newer Refresh superseded.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        if not isinstance(item.value, tuple) or len(item.value) != 2:
            return
        generation, status = item.value
        if generation != self._generation:
            return
        self._stall.stop()
        self._busy = False
        self._refresh.setEnabled(True)
        if not isinstance(status, ToolsStatus):
            status = ToolsStatus(rows=(), message=NO_RESULT_TEXT)
        self._show(status)

    def _show(self, status: ToolsStatus) -> None:
        self._table.setRowCount(0)
        for row in status.rows:
            index = self._table.rowCount()
            self._table.insertRow(index)
            for column, text in enumerate(row.cells()):
                self._table.setItem(index, column, QTableWidgetItem(text))
        self._status.setText(status_line(status))

    # ----------------------------------------------------------- events

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._read_once:
            self._read_once = True
            self.refresh()


__all__ = ["COLUMNS", "REFRESH_TEXT", "ToolsPanel"]
