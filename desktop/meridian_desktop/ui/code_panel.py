"""Code tab: a search box over the local Meridian server's code index, with path:line results and copy.

This is Qt code and sits in ui/, so the boundary check allows it. The tab makes no request when it
is built or shown. A search runs only when Search or Enter is pressed. The Local URL is read and the
search runs on the thread pool, so the UI never blocks. Results and messages are plain text (PlainText
labels and plain table cells), so code from the index is never read as HTML. The results table is
read-only and has the shared Copy action (Ctrl+C or the right-click menu).
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
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import code_client
from meridian_desktop.code_client import (
    BLANK_QUERY_MESSAGE,
    DEFAULT_LIMIT,
    STATE_BLANK_QUERY,
    STATE_NOT_INDEXED,
    STATE_NOT_RUNNING,
    STATE_OK,
    STATE_ROUTE_MISSING,
    UNCHECKED_MESSAGE,
    CodeHit,
    SearchResult,
    Transport,
)
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

SEARCH_LIMIT = DEFAULT_LIMIT
INTRO_TEXT = (
    "Search the code index of the local Meridian server, at the Local URL set in Settings. "
    "This tab contacts that address and nothing else."
)
SEARCH_PLACEHOLDER = "Search the indexed code by name, word or phrase"
SEARCH_HINT = "Type a search term, then press Search or Enter."
SEARCHING_TEXT = "Searching the code index..."
BLANK_TEXT = BLANK_QUERY_MESSAGE
NO_MATCHES_TEXT = 'No code in the index matches "{}".'
RESULTS_TEXT = '{} result(s) for "{}".'
LIMIT_TEXT = " Showing the first {}; narrow the search to see fewer."
NOT_RUNNING_TEXT = (
    "Not connected. The local Meridian server is not running at {}, so there is nothing to search. "
    "Start Meridian on this computer. In a terminal in the Meridian core folder, run "
    "python -m meridian.local_runner start --scope <your scope>. Then press Search again."
)
ROUTE_MISSING_TEXT = (
    "Not connected. Something answers at {}, but it has no code search route (HTTP 404), so that "
    "Meridian is too old for the Code tab. Update Meridian to a build that includes code search, "
    "then press Search again."
)
NOT_INDEXED_TEXT = (
    "The code index has not been built, so there is nothing to search. To build it, run "
    "meridian-codeindex with the repository folder and an index file, for example: "
    "meridian-codeindex <repo folder> index --db-path <index file>. Then press Search again."
)
ERROR_TEXT = "The code search failed. {} Press Search to try again."
SEARCH_FAILED_TEXT = "The code search could not be completed ({})."
RESULT_COLUMNS = ("Location", "Score", "Snippet")
SCORE_FORMAT = "{:.3f}"


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def results_text(query: str, count: int) -> str:
    """The line above the results. It names the query and says when the list is cut at the limit."""
    if count == 0:
        return NO_MATCHES_TEXT.format(query)
    text = RESULTS_TEXT.format(count, query)
    if count >= SEARCH_LIMIT:
        text += LIMIT_TEXT.format(SEARCH_LIMIT)
    return text


def status_text(outcome: SearchResult, query: str, url: str) -> str:
    """The status line for one search outcome. Every state has plain words and, where needed, the fix."""
    state = outcome.state
    if state == STATE_OK:
        return results_text(query, len(outcome.hits))
    if state == STATE_NOT_RUNNING:
        return NOT_RUNNING_TEXT.format(url)
    if state == STATE_ROUTE_MISSING:
        return ROUTE_MISSING_TEXT.format(url)
    if state == STATE_NOT_INDEXED:
        return NOT_INDEXED_TEXT
    if state == STATE_BLANK_QUERY:
        return BLANK_TEXT
    return ERROR_TEXT.format(outcome.detail or UNCHECKED_MESSAGE)


def location_text(hit: CodeHit) -> str:
    """path:line, or the path alone when the server gave no line."""
    return f"{hit.path}:{hit.line}" if hit.line else hit.path


def score_text(score: float | None) -> str:
    return "" if score is None else SCORE_FORMAT.format(score)


def snippet_text(snippet: str) -> str:
    """The snippet on one line, so a table cell and a copied row each stay one line."""
    return " ".join(snippet.split())


@dataclass(frozen=True)
class _Answer:
    """One finished search: the generation it was started under, and either its result or its failure text."""

    generation: int
    result: Any
    failure: str | None


class _SearchJob(QRunnable):
    """Run ``work`` on the thread pool and deliver an _Answer to the shared delivery object.

    The job holds only its work, its owner token, its generation and the shared delivery object. It never holds the
    tab, so a tab deleted while the search runs is never kept alive by the pool. Any exception is reduced to its
    class name, so no traceback or response text reaches the UI.
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
            failure = SEARCH_FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._delivery.delivered.emit(Delivered(self._owner, _Answer(self._generation, result, failure)))
        except RuntimeError:
            pass  # the shared delivery object is never deleted, so this is only a guard


def _search_read(url_provider: Callable[[], str], transport: Transport | None, query: str) -> tuple[str, SearchResult]:
    """Runs on a worker thread. The Local URL is read here, so the UI thread does no settings I/O."""
    url = url_provider()
    return url, code_client.search(url, query, limit=SEARCH_LIMIT, transport=transport)


def _is_answer(result: object) -> bool:
    return isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], SearchResult)


class CodePanel(QWidget):
    """The Code tab: a search box over the local server's code index, with path:line results and copy."""

    def __init__(
        self,
        local_url_provider: Callable[[], str],
        parent: QWidget | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("code-panel")
        self._url_provider = local_url_provider
        self._transport = transport
        self._searching = False
        self._pending_query = ""
        # Identifies this tab's searches. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "code-intro", INTRO_TEXT))

        search_row = QHBoxLayout()
        self._search_input = QLineEdit(self)
        self._search_input.setObjectName("code-search-input")
        self._search_input.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search_button = QPushButton("Search", self)
        self._search_button.setObjectName("code-search-button")
        search_row.addWidget(self._search_input, 1)
        search_row.addWidget(self._search_button)
        layout.addLayout(search_row)

        self._status = _label(self, "code-status", SEARCH_HINT)
        layout.addWidget(self._status)

        self._results = QTableWidget(0, len(RESULT_COLUMNS), self)
        self._results.setObjectName("code-results")
        self._results.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self._results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.verticalHeader().setVisible(False)
        self._results.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._results)
        layout.addWidget(self._results, 1)

        self._search_button.clicked.connect(self._on_search_clicked)
        self._search_input.returnPressed.connect(self._on_search_clicked)

    # ----------------------------------------------------------- search

    @Slot()
    def _on_search_clicked(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            self._results.setRowCount(0)
            self._status.setText(BLANK_TEXT)
            return
        if self._searching:
            return
        self._searching = True
        self._pending_query = query
        self._generation += 1
        generation = self._generation
        self._search_button.setEnabled(False)
        self._results.setRowCount(0)
        self._status.setText(SEARCHING_TEXT)
        # The job holds the provider, the transport, the query, the owner token, its generation and the shared
        # delivery object. It never holds this tab.
        work = partial(_search_read, self._url_provider, self._transport, query)
        QThreadPool.globalInstance().start(_SearchJob(self._owner, generation, work, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab is dropped here, and so is one a newer search superseded.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        answer = item.value
        if not isinstance(answer, _Answer) or answer.generation != self._generation:
            return
        self._finish(answer)

    def _finish(self, answer: _Answer) -> None:
        self._searching = False
        self._search_button.setEnabled(True)
        self._results.setRowCount(0)
        if answer.failure is not None or not _is_answer(answer.result):
            self._status.setText(answer.failure or SEARCH_FAILED_TEXT.format("NoResult"))
            return
        url, outcome = answer.result
        if outcome.state == STATE_OK:
            self._fill_results(outcome.hits)
        self._status.setText(status_text(outcome, self._pending_query, url))

    def _fill_results(self, hits: tuple[CodeHit, ...]) -> None:
        self._results.setRowCount(len(hits))
        for row, hit in enumerate(hits):
            cells = (location_text(hit), score_text(hit.score), snippet_text(hit.snippet))
            for column, value in enumerate(cells):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._results.setItem(row, column, cell)


__all__ = [
    "ERROR_TEXT",
    "NOT_INDEXED_TEXT",
    "NOT_RUNNING_TEXT",
    "ROUTE_MISSING_TEXT",
    "CodePanel",
    "location_text",
    "results_text",
    "score_text",
    "snippet_text",
    "status_text",
]
