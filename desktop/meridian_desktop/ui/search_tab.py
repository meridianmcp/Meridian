"""Search tab: the code, outputs and research the local Meridian server holds, as lists grouped by where they come from.

Qt code, so it sits in ui/. The tab has no search box. Code and outputs have no list route on the local server yet, so
each says "No data source yet" and sends nothing. Research runs are read from the Local URL in Settings when the tab is
first shown and when Refresh is pressed, and they are grouped by provider. The kind filter only chooses which lists
show. It sends nothing.

A read runs on the thread pool, so the window never blocks. A second press of Refresh while a read runs does nothing,
and an answer from a superseded read is dropped. Results and messages are plain text (PlainText labels and plain table
cells), so server text is never read as HTML. The table is read-only and has the shared Copy action (Ctrl+C or the
right-click menu). A group's header row spans the table, so a copied selection keeps the group's name.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any, NamedTuple

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop.search_kinds import (
    KIND_LABELS,
    KIND_RESEARCH,
    KINDS,
    STATE_EMPTY,
    STATE_ERROR,
    STATE_NO_SOURCE,
    STATE_NOT_CHECKED,
    STATE_NOT_CONNECTED,
    STATE_RESULTS,
    KindList,
    ListOutcome,
    no_source_list,
    not_checked_list,
    read_lists,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import submit
from meridian_desktop.ui.text import enable_table_copy, make_selectable

# The intro is the owner's text, verbatim for the parts it names.
INTRO_TEXT = (
    "Code, outputs and research from the local Meridian server, as lists grouped by where they come from. "
    "This tab has no search box. Research runs are read from the Local URL in Settings when this tab is first "
    "shown and when Refresh is pressed. Code and outputs have no list on the server yet, so they send nothing."
)
ALL_KINDS_TEXT = "All kinds"
REFRESH_TEXT = "Refresh"
READING_TEXT = "Reading the research runs from the local Meridian server..."
FAILED_TEXT = "Research: the runs could not be read ({}). Press Refresh to try again."
NOT_REPORTED = "not reported"
RESULT_COLUMNS = ("Item", "State", "Started", "Updated", "Completed")
FILTER_ITEMS = (
    (ALL_KINDS_TEXT, ""),
    (KIND_LABELS["code"], "code"),
    (KIND_LABELS["outputs"], "outputs"),
    (KIND_LABELS["research"], "research"),
)
# The short words a header row uses when a kind has no groups to show.
STATE_WORDS = {
    STATE_NO_SOURCE: "no data source yet",
    STATE_NOT_CONNECTED: "not connected",
    STATE_NOT_CHECKED: "not checked yet",
    STATE_EMPTY: "nothing to show yet",
    STATE_ERROR: "could not be read",
    STATE_RESULTS: "results",
}


class TableLine(NamedTuple):
    """One table row. A header row carries one cell that spans every column. A data row carries one cell per column."""

    cells: tuple[str, ...]
    header: bool


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def kinds_for(choice: str) -> tuple[str, ...]:
    """The kinds one filter choice shows: every kind for All kinds, or the one kind named."""
    return KINDS if choice not in KINDS else (choice,)


def default_list(kind: str) -> KindList:
    """The list a kind shows before any read: no-source kinds say so, and research says it is not checked yet."""
    if kind == KIND_RESEARCH:
        return not_checked_list(kind)
    return no_source_list(kind)


def shown_lists(lists: Mapping[str, KindList], choice: str) -> tuple[KindList, ...]:
    """The lists for the chosen kinds, in filter order. A kind with no read yet shows its default list."""
    return tuple(lists.get(kind) or default_list(kind) for kind in kinds_for(choice))


def status_text(lists: Sequence[KindList]) -> str:
    """One line per shown kind, in the order the kinds are listed."""
    return "\n".join(item.line for item in lists)


def table_lines(lists: Sequence[KindList]) -> tuple[TableLine, ...]:
    """The table rows for the shown lists. Each group is a header row, then its runs. A kind with no groups is one header row."""
    lines: list[TableLine] = []
    for item in lists:
        label = KIND_LABELS[item.kind]
        if not item.groups:
            lines.append(TableLine((f"{label}: {STATE_WORDS.get(item.state, 'not available')}",), True))
            continue
        for group in item.groups:
            lines.append(TableLine((f"{label}: {group.title} ({len(group.rows)} run(s))",), True))
            for row in group.rows:
                cells = (
                    row.item,
                    row.state or NOT_REPORTED,
                    row.started or NOT_REPORTED,
                    row.updated or NOT_REPORTED,
                    row.completed or NOT_REPORTED,
                )
                lines.append(TableLine(cells, False))
    return tuple(lines)


def _read(url_provider: Callable[[], str], transports: Mapping[str, Any] | None) -> ListOutcome:
    """Runs on a worker thread. The Local URL is read here, so the UI thread does no settings I/O."""
    return read_lists(url_provider(), KINDS, transports=transports)


class SearchTab(QWidget):
    """The Search tab: the code, outputs and research lists, a kind filter, Refresh, and one read-only table."""

    def __init__(
        self,
        local_url_provider: Callable[[], str],
        parent: QWidget | None = None,
        *,
        transports: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("search-tab")
        self._url_provider = local_url_provider
        self._transports = dict(transports or {})
        self._reading = False
        self._probed_on_show = False
        # The last read, by kind. Code and outputs are never read, so they are not in it.
        self._lists: dict[str, KindList] = {}
        # Identifies this tab's reads. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()
        self._render()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "search-intro", INTRO_TEXT))

        row = QHBoxLayout()
        self._kind = QComboBox(self)
        self._kind.setObjectName("search-kind")
        for text, value in FILTER_ITEMS:
            self._kind.addItem(text, value)
        self._refresh_button = QPushButton(REFRESH_TEXT, self)
        self._refresh_button.setObjectName("search-refresh")
        row.addWidget(self._kind)
        row.addStretch(1)
        row.addWidget(self._refresh_button)
        layout.addLayout(row)

        self._status = _label(self, "search-status", "")
        layout.addWidget(self._status)

        self._results = QTableWidget(0, len(RESULT_COLUMNS), self)
        self._results.setObjectName("search-results")
        self._results.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self._results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.verticalHeader().setVisible(False)
        self._results.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._results)
        layout.addWidget(self._results, 1)

        self._refresh_button.clicked.connect(lambda _checked=False: self._refresh())
        self._kind.currentIndexChanged.connect(lambda _index: self._render())

    # ----------------------------------------------------------- read

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._probed_on_show:
            self._probed_on_show = True
            self._refresh()

    def _refresh(self) -> None:
        if self._reading:
            return
        self._reading = True
        self._generation += 1
        generation = self._generation
        self._refresh_button.setEnabled(False)
        self._status.setText(READING_TEXT)
        # The job holds the URL provider, the transports, the owner token and the shared delivery. It never holds this tab.
        work = partial(_read, self._url_provider, self._transports)
        submit(self._owner, generation, work)

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab, or one a newer read superseded, is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (value, failure) = item.value
        if generation != self._generation:
            return
        self._finish(value, failure)

    def _finish(self, value: object, failure: str | None) -> None:
        self._reading = False
        self._refresh_button.setEnabled(True)
        if failure is not None or not isinstance(value, ListOutcome):
            self._lists[KIND_RESEARCH] = KindList(KIND_RESEARCH, STATE_ERROR, FAILED_TEXT.format(failure or "no result"))
        else:
            self._lists = {item.kind: item for item in value.kinds}
        self._render()

    # ----------------------------------------------------------- show

    def _render(self) -> None:
        shown = shown_lists(self._lists, self._kind.currentData() or "")
        self._status.setText(status_text(shown))
        self._fill(table_lines(shown))

    def _fill(self, lines: Sequence[TableLine]) -> None:
        self._results.clearSpans()
        self._results.setRowCount(len(lines))
        columns = self._results.columnCount()
        for row, line in enumerate(lines):
            if line.header:
                cell = QTableWidgetItem(line.cells[0])
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                font = QFont(cell.font())
                font.setBold(True)
                cell.setFont(font)
                self._results.setItem(row, 0, cell)
                self._results.setSpan(row, 0, 1, columns)
                continue
            for column, text in enumerate(line.cells):
                cell = QTableWidgetItem(text)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._results.setItem(row, column, cell)


__all__ = [
    "ALL_KINDS_TEXT",
    "INTRO_TEXT",
    "RESULT_COLUMNS",
    "SearchTab",
    "TableLine",
    "default_list",
    "kinds_for",
    "shown_lists",
    "status_text",
    "table_lines",
]
