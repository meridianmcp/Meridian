"""Research tab: a read-only list of research runs on the local server, with each run's state and timestamps.

This is Qt code and sits in ui/, so the boundary check allows it. The panel makes no request when it is
built. It reads the runs when first shown and again when Refresh is pressed. The read runs on the thread
pool, and the server address comes from local_url_provider, which is also called on the pool, so the UI
thread never blocks. Results are plain text (PlainText labels and plain table cells). The runs table is
read-only and copyable with the shared Copy action (Ctrl+C or the right-click menu).

The panel has one button, Refresh. It starts, cancels, promotes and submits nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, Slot
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

from meridian_desktop import outputs_client
from meridian_desktop.outputs_client import ERROR, ResearchResult, ResearchRun
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

INTRO_TEXT = (
    "Research runs on the local Meridian server, shown read-only. Each run shows its state and when it "
    "started, last changed and finished. This tab cannot start, cancel or promote a run."
)
NOT_CHECKED_TEXT = "Research runs: not checked yet. Press Refresh to check."
CHECKING_TEXT = "Checking research runs..."
CHECK_FAILED_TEXT = "The check failed unexpectedly ({})."
NOT_REPORTED = "not reported"
RUN_COLUMNS = ("Run", "Provider", "State", "Started", "Updated", "Completed")


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def run_row(run: ResearchRun) -> tuple[str, ...]:
    """The table cells for one run, in RUN_COLUMNS order. A field the server did not report reads as 'not reported'."""
    return (
        run.run_id,
        run.provider or NOT_REPORTED,
        run.state or NOT_REPORTED,
        run.started_at or NOT_REPORTED,
        run.updated_at or NOT_REPORTED,
        run.completed_at or NOT_REPORTED,
    )


@dataclass(frozen=True)
class _Answer:
    """One finished read: the generation it was started under, and either its result or its failure text."""

    generation: int
    result: Any
    failure: str | None


class _Job(QRunnable):
    """Run ``work`` on the thread pool and deliver an _Answer to the shared delivery object.

    The job holds only its work, its owner token, its generation and the shared delivery object. It never holds the
    panel, so a panel deleted while the read runs is never kept alive by the pool. Exceptions reduce to a class name.
    """

    def __init__(self, owner: object, generation: int, work: Callable[[], Any], delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._owner = owner
        self._generation = generation
        self._work = work
        self._delivery = delivery

    def run(self) -> None:
        result: Any = None
        failure: str | None = None
        try:
            result = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = CHECK_FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._delivery.delivered.emit(Delivered(self._owner, _Answer(self._generation, result, failure)))
        except RuntimeError:
            pass  # the shared delivery object is never deleted, so this is only a guard


def _list_runs(local_url_provider: Callable[[], str]) -> ResearchResult:
    """Read the server address from the provider, then list the runs. Runs on a worker thread."""
    url = local_url_provider()
    return outputs_client.list_research_runs(url)


class ResearchPanel(QWidget):
    """The Research tab: the runs the local server reports, with Refresh and nothing else."""

    def __init__(self, local_url_provider: Callable[[], str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("research-panel")
        self._local_url_provider = local_url_provider
        self._checking = False
        self._probed_on_show = False
        # Identifies this panel's reads. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "research-intro", INTRO_TEXT))

        self._status = _label(self, "research-status", NOT_CHECKED_TEXT)
        layout.addWidget(self._status)

        actions = QHBoxLayout()
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.setObjectName("research-refresh")
        actions.addWidget(self._refresh_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._runs = QTableWidget(0, len(RUN_COLUMNS), self)
        self._runs.setObjectName("research-runs")
        self._runs.setHorizontalHeaderLabels(list(RUN_COLUMNS))
        self._runs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._runs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._runs.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._runs.verticalHeader().setVisible(False)
        self._runs.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._runs)
        layout.addWidget(self._runs, 1)

        self._refresh_button.clicked.connect(self._on_refresh_clicked)

    # ----------------------------------------------------------- read

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._probed_on_show:
            self._probed_on_show = True
            self._refresh()

    @Slot()
    def _on_refresh_clicked(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if self._checking:
            return
        self._checking = True
        self._generation += 1
        generation = self._generation
        self._refresh_button.setEnabled(False)
        self._status.setText(CHECKING_TEXT)
        # The job holds the provider, the owner token, its generation and the shared delivery object. It never holds
        # this panel.
        work = partial(_list_runs, self._local_url_provider)
        QThreadPool.globalInstance().start(_Job(self._owner, generation, work, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel is dropped here, and so is one a newer read superseded.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        answer = item.value
        if not isinstance(answer, _Answer) or answer.generation != self._generation:
            return
        self._finish(answer)

    def _finish(self, answer: _Answer) -> None:
        self._checking = False
        self._refresh_button.setEnabled(True)
        if answer.failure is not None or not isinstance(answer.result, ResearchResult):
            self._show(ResearchResult(ERROR, answer.failure or CHECK_FAILED_TEXT.format("NoResult")))
            return
        self._show(answer.result)

    def _show(self, result: ResearchResult) -> None:
        self._status.setText(result.message)
        self._runs.setRowCount(len(result.runs))
        for row, run in enumerate(result.runs):
            for column, value in enumerate(run_row(run)):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._runs.setItem(row, column, cell)


__all__ = [
    "NOT_CHECKED_TEXT",
    "ResearchPanel",
    "run_row",
]
