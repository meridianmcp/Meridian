"""Zotero tab: whether Zotero is installed, running and answering, a Start Zotero button, and a library search.

This is Qt code and sits in ui/, so the boundary check allows it. The tab makes no request when it
is built. It checks Zotero when first shown and when Refresh is pressed. Each check, each search and
the start action run on the thread pool, so the UI never blocks. Zotero starts only after the owner
picks Start Zotero in a dialog whose default button is Cancel. Results are plain text (PlainText
labels and plain table cells), so Zotero's text is never read as HTML. The results table is read-only
and has the shared Copy action (Ctrl+C or the right-click menu). The save section at the foot sends lines of
reading-list items through the Meridian Zotero bridge. The bridge check runs with the other checks, and Save runs on the
thread pool as well. A line whose outcome is unknown is held back until the person checks Zotero and presses I checked Zotero.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import zotero_bridge, zotero_client, zotero_journal, zotero_scope
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable
from meridian_desktop.zotero_client import (
    API_DISABLED,
    API_NOT_RUNNING,
    API_ON,
    SEARCH_LIMIT,
    ZoteroError,
    ZoteroItem,
    ZoteroStatus,
    command_line,
)

INTRO_TEXT = (
    "Zotero is a separate app that runs on this computer. Searches and status read its local API on 127.0.0.1:23119. "
    "Saves go through the Meridian Zotero bridge on 127.0.0.1:23125, which is the only thing that writes."
)
NOT_CHECKED_TEXT = "Zotero: not checked yet. Press Refresh to check."
CHECKING_TEXT = "Checking Zotero..."
CHECK_FAILED_TEXT = "The check failed unexpectedly ({})."
INSTALLED_TEXT = "Installed: yes, at {}"
NOT_INSTALLED_TEXT = (
    "Installed: not found in the usual install folders. Install Zotero from zotero.org, then press Refresh."
)
PROCESS_RUNNING_TEXT = "Zotero process: running"
PROCESS_STOPPED_TEXT = "Zotero process: not running"
PROCESS_UNKNOWN_TEXT = "Zotero process: could not be checked on this computer"
API_TEXT = {
    API_ON: "Local API: on",
    API_DISABLED: "Local API: off (Zotero has it turned off)",
    API_NOT_RUNNING: "Local API: not answering (Zotero is not running)",
}
API_ERROR_TEXT = "Local API: could not be checked"
START_HINT_READY = (
    "Start Zotero opens Zotero on this computer after you confirm. The desktop changes no Zotero setting."
)
START_HINT_RUNNING = (
    "Zotero is already running, so there is nothing to start. If the local API is off, "
    "turn it on in Zotero under Settings, then Advanced."
)
START_HINT_NOT_INSTALLED = (
    "Zotero is not installed in a usual folder, so the desktop cannot start it. "
    "Install Zotero from zotero.org, then press Refresh."
)
START_CANCELLED_TEXT = "Start cancelled. Nothing was started and no file was written."
STARTING_TEXT = "Starting Zotero..."
API_READY_TEXT = "Local API answering: yes"
API_NOT_READY_TEXT = "Local API answering: not yet. Zotero can take a little while to open. Press Refresh in a moment."
SEARCH_PLACEHOLDER = "Search your Zotero library by title, author or year"
SEARCH_HINT = "Type a search term, then press Search or Enter."
BLANK_TEXT = "Type a search term first. Nothing was sent to Zotero."
SEARCHING_TEXT = "Searching your Zotero library..."
NO_RESULTS_TEXT = "No items in your Zotero library match \"{}\"."
RESULTS_TEXT = "{} result(s) for \"{}\"."
LIMIT_TEXT = " Showing the first {}; narrow the search to see fewer."
RESULT_COLUMNS = ("Title", "Authors", "Year", "Zotero key")
SCOPE_HEADING = "Project scope: the Zotero collection each project searches"
SCOPE_INTRO = (
    "Pick a project, then a library and a collection in your Zotero. Searches for that project read only that "
    "collection. A project with no collection searches your whole library. Saves go into the project's collection "
    "when it is in My Library and the bridge plugin is 0.1.4 or newer. Group collections cannot take saves yet."
)
SCOPE_PROJECT_LABEL = "Project"
SCOPE_LIBRARY_LABEL = "Library"
SCOPE_COLLECTION_LABEL = "Collection"
SCOPE_NO_PROJECTS_TEXT = "No projects were found, so no collection can be set."
SCOPE_NO_SOURCE_TEXT = "Projects are not available in this window, so no collection can be set."
SCOPE_LOADING_TEXT = "Loading..."
SCOPE_NO_LIBRARY_TEXT = "Pick a library first."
SCOPE_NO_COLLECTIONS_TEXT = "This library has no collections."
SCOPE_SAVE_TEXT = "Use this collection"
SCOPE_CLEAR_TEXT = "Clear collection"
SCOPE_RELOAD_TEXT = "Reload libraries"
SCOPE_NEED_TEXT = "Pick a project, a library and a collection first. Nothing was saved."
SCOPE_SAVED_TEXT = "Saved. Searches for {} now read the collection '{}' in {}."
SCOPE_CLEARED_TEXT = "Cleared. Searches for {} now read the whole library."
SCOPE_NO_PROJECT_TEXT = "Pick a project to see or set its collection."
SCOPE_CURRENT_BOUND = "{} searches the collection '{}' in {}."
SCOPE_CURRENT_UNBOUND = "{} searches your whole library. No collection is set."
SCOPE_SEARCH_IN = " Searched the collection '{}' in {} for this project."
SCOPE_SEARCH_WHOLE = " This project has no collection set, so the search covered your whole library."
SCOPE_SEARCHING_TEXT = "Searching the collection '{}' in {}..."
LIBRARIES_FAILED_TEXT = "The Zotero libraries could not be read. Press Reload libraries to try again."
COLLECTIONS_FAILED_TEXT = "The collections could not be read."
NOT_SAVED_TEXT = "Not saved: the scope file could not be written ({})."
SAVE_HEADING = "Save to Zotero"
SAVE_INTRO = (
    "One item per line, as Title | URL. The items go into your Zotero library through the Meridian Zotero bridge. "
    "Nothing is deleted."
)
SAVE_PLACEHOLDER = "Title | https://example.org/page"
SAVE_BUTTON_TEXT = "Save to Zotero"
SAVING_TEXT = "Saving to Zotero through the Meridian bridge..."
SAVE_NO_RESULT_TEXT = (
    "The save ended without a result, so every line is held back. Check Zotero before you save again."
)
SAVE_HELD_TEXT = "Save is off until you check Zotero for the held lines and press I checked Zotero."
HELD_HEADING = (
    "Held back: these may already be in Zotero, so they are not retried. Check Zotero, then press I checked Zotero."
)
HELD_BUTTON_TEXT = "I checked Zotero"
HELD_RELEASED_TEXT = "Released. Nothing was retried."
STILL_WRITING_TEXT = (
    "Released the lines Zotero confirmed. Lines still being written in another window stay held until they finish."
)
BRIDGE_CHECKING_TEXT = "Bridge: checking..."
JOURNAL_LOADING_TEXT = "The save journal is still loading. Save is off until it is read."
JOURNAL_CHECK_FAILED_TEXT = "The save journal could not be checked, so saving is off."
WRITING_ELSEWHERE_TEXT = (
    "Another window is still writing a line, so it stays held until that write ends. "
    "If no other window is open, restart the desktop."
)
# A busy journal is tried again after this long, so a window that opened while another held the lock recovers.
JOURNAL_RETRY_MS = 2000
# The kinds of read the panel runs. Each kind has at most one read in flight.
STATUS = "status"
SEARCH = "search"
ACTION = "action"
BRIDGE = "bridge"
SAVE = "save"
JOURNAL = "journal"
CLEAR = "clear"
PROJECTS = "projects"
LIBRARIES = "libraries"
COLLECTIONS = "collections"


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def process_text(running: bool | None) -> str:
    if running is True:
        return PROCESS_RUNNING_TEXT
    if running is False:
        return PROCESS_STOPPED_TEXT
    return PROCESS_UNKNOWN_TEXT


def api_text(state: str) -> str:
    return API_TEXT.get(state, API_ERROR_TEXT)


def results_text(query: str, count: int) -> str:
    """The line above the results. It names the query and says when the list is cut at the limit."""
    if count == 0:
        return NO_RESULTS_TEXT.format(query)
    text = RESULTS_TEXT.format(count, query)
    if count >= SEARCH_LIMIT:
        text += LIMIT_TEXT.format(SEARCH_LIMIT)
    return text


def start_dialog(parent: QWidget | None, command: str, folder: str) -> tuple[QMessageBox, QPushButton, QPushButton]:
    """The Start Zotero confirmation, built but not shown. Its default button is Cancel, so Enter does not start Zotero.

    The command and the working folder are shown as plain text, so the owner sees exactly what will run.
    """
    box = QMessageBox(parent)
    box.setWindowTitle("Start Zotero")
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText("Start Zotero on this computer?")
    box.setInformativeText(
        f"The desktop will run this command:\n\n{command}\n\n"
        f"Working folder: {folder}\n\n"
        "Zotero opens in its own window. The desktop changes no Zotero setting."
    )
    start = box.addButton("Start Zotero", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    return box, start, cancel


def ask_start(parent: QWidget | None, command: str, folder: str) -> bool:
    """Show the confirmation. True only when Start Zotero is chosen. Closing the dialog is a no."""
    box, start, _cancel = start_dialog(parent, command, folder)
    box.exec()
    return box.clickedButton() is start


@dataclass(frozen=True)
class _Done:
    """One finished read: its kind, the number it was started with, and the (result, failure_text) its job made."""

    kind: str
    generation: int
    payload: tuple


def _launch(client: Any, exe: str) -> tuple[str, bool]:
    """Start Zotero, then ask the local API once. Runs on a worker thread, and only after a yes."""
    line = client.start_zotero(exe)
    check = client.probe_api()
    return line, check.state == API_ON


class _Job(QRunnable):
    """Run ``work`` on the thread pool and deliver (result, failure_text).

    A ZoteroError keeps its fixed message. Any other exception is reduced to its class name, so
    no traceback reaches the UI.
    """

    def __init__(self, work: Callable[[], Any], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        result: Any = None
        failure: str | None = None
        try:
            result = self._work()
        except ZoteroError as exc:
            failure = exc.message
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = CHECK_FAILED_TEXT.format(exc.__class__.__name__)
        try:
            self._deliver((result, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class _JournalProgress:
    """Writes each item's outcome to the journal as it happens. It runs on the save's worker thread.

    An item is recorded as writing before its POST, and as landed, unknown or refused once its outcome is known. A quit in
    between leaves the item writing, and the next launch holds it.
    """

    def __init__(self, journal: zotero_journal.SaveJournal, lines: list[str]) -> None:
        self._journal = journal
        self._lines = lines
        self._ids: dict[int, str] = {}

    def started(self, index: int) -> None:
        self._ids[index] = self._journal.begin(self._lines[index])

    def finished(self, index: int, outcome: str) -> None:
        self._journal.finish(self._ids[index], outcome)


@dataclass(frozen=True)
class _JournalState:
    """What a read or a release of the journal found: the open journal, the held entries if the read finished, and any
    problem. ``held_lines`` names the lines a backup holds, when the journal cannot be read."""

    journal: zotero_journal.SaveJournal | None
    held: tuple[zotero_journal.JournalEntry, ...] | None
    problem: str | None = None
    busy: bool = False
    held_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SaveDone:
    """What a save run decided, on the worker thread: the result text, how much of the box to keep, the held entries once
    the run has written them, and any journal problem."""

    message: str
    keep_from: int
    held: tuple[zotero_journal.JournalEntry, ...] | None
    problem: str | None = None
    busy: bool = False


def _journal_state(path: Path, journal: zotero_journal.SaveJournal | None) -> _JournalState:
    """Open the journal if it is not open yet, and read what is held. Runs on a worker thread."""
    try:
        if journal is None:
            journal = zotero_journal.SaveJournal(path, read_timeout=zotero_journal.UI_READ_TIMEOUT_SECONDS)
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _JournalState(journal, None, exc.message, exc.busy, exc.held_lines)
    return _JournalState(journal, tuple(held))


def _clear_held(journal: zotero_journal.SaveJournal, entry_ids: list[str]) -> _JournalState:
    """Release the unknown entries the person checked, then read what is still held. Runs on a worker thread."""
    try:
        journal.clear(entry_ids)
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _JournalState(journal, None, exc.message, exc.busy, exc.held_lines)
    return _JournalState(journal, tuple(held))


def _hold_line(journal: zotero_journal.SaveJournal, line: str) -> tuple[str | None, bool]:
    """Make sure ``line`` is held. Returns the problem and whether it was busy; both empty when the line is held."""
    try:
        if any(entry.line == line for entry in journal.held()):
            return None, False
        journal.record_unknown([line])
    except zotero_journal.JournalError as exc:
        return exc.message, exc.busy
    return None, False


def _hold_lines(journal: zotero_journal.SaveJournal, lines: list[str]) -> tuple[str | None, bool]:
    """Hold every line of a save that produced no result. Returns the problem and whether it was busy."""
    try:
        journal.record_unknown(lines)
    except zotero_journal.JournalError as exc:
        return exc.message, exc.busy
    return None, False


def _record_save(journal: zotero_journal.SaveJournal, lines: list[str], result: Any) -> _SaveDone:
    """Hold what the save left unknown, then read what is held. A line leaves the box only when its journal entry was
    written, so a line whose entry failed stays in the box and is not posted again."""
    problem, busy = None, False
    if result is None:
        problem, busy = _hold_lines(journal, lines)
        keep_from = 0 if problem is not None else len(lines)
        base = SAVE_NO_RESULT_TEXT
    elif result.state == zotero_bridge.UNKNOWN and result.saved < len(lines):
        problem, busy = _hold_line(journal, lines[result.saved])
        keep_from = result.saved if problem is not None else result.saved + 1
        base = result.message
    else:
        keep_from = result.saved
        base = result.message
    message = base if problem is None else f"{problem} {base}"
    try:
        held = tuple(journal.held())
    except zotero_journal.JournalError as exc:
        return _SaveDone(message, keep_from, None, problem or exc.message, busy if problem else exc.busy)
    return _SaveDone(message, keep_from, held, problem, busy)


GROUP_FOLDER_TEXT = (
    "Nothing was saved. This project's folder is in a group library, and the Zotero bridge files items only into "
    "My Library. Choose a folder in My Library on the Zotero tab, then try again."
)
FOLDER_GONE_TEXT = (
    "Nothing was saved. This project's folder is no longer in the Zotero scope file, so the save would not go there. "
    "Choose the folder again on the Zotero tab, or clear it there to save without one, then try again."
)


SCOPE_UNREADABLE_SAVE_TEXT = (
    "Nothing was saved. The Zotero scope file could not be read, so the save cannot tell which folder this project "
    "uses. Fix or move that file in the desktop's settings folder, then try again."
)
SCOPE_SKIPPED_SAVE_TEXT = (
    "Nothing was saved. This project's folder entry in the Zotero scope file is not valid, so the save cannot tell "
    "where it goes. Choose the folder again on the Zotero tab, then try again."
)


def _save_placement(
    current: zotero_scope.ScopeRead,
    bound: frozenset[str] | set[str],
    project_id: str,
) -> tuple[str | None, str]:
    """(the collection key a save files into, a refusal). A project with no folder gives (None, "").

    ``current`` is the scope file read at save time, and it decides where a save goes. ``bound`` holds every project this
    window has seen the file bind, or hold an unusable entry for, and only its own clear removes one. A save refuses when
    the file is unreadable, since it cannot tell whether the project has a folder. It refuses when the file's entry for
    the project is not valid. It refuses when a bound project is no longer bound in the file, so nothing goes to the
    bridge's default collection by mistake. A group folder refuses, since the bridge files items only into My Library.
    """
    if current.unreadable:
        return None, SCOPE_UNREADABLE_SAVE_TEXT
    if not project_id:
        return None, ""
    if project_id in current.skipped:
        return None, SCOPE_SKIPPED_SAVE_TEXT
    scope = current.scopes.get(project_id)
    if scope is None:
        return (None, FOLDER_GONE_TEXT) if project_id in bound else (None, "")
    if scope.library_type != zotero_scope.LIBRARY_USER:
        return None, GROUP_FOLDER_TEXT
    return scope.collection_key, ""


def _run_save(
    journal: zotero_journal.SaveJournal,
    bridge: Any,
    items: list,
    lines: list[str],
    collection_key: str | None = None,
) -> _SaveDone:
    """One save, on a worker thread: the guard, the bridge, and the journal entries for what it did.

    The guard reads the journal under its lock, so a window whose list is out of date still refuses a line another window
    is writing. _JournalProgress writes each item's entry as the item starts and as its outcome is known.
    """
    try:
        held = journal.held()
    except zotero_journal.JournalError as exc:
        return _SaveDone(exc.message, 0, None, exc.message, exc.busy)
    if held:
        return _SaveDone(SAVE_HELD_TEXT, 0, tuple(held))
    progress = _JournalProgress(journal, lines)
    try:
        # A project with a folder files its saves there. Without one the bridge's own choice stands, as before.
        placed = {} if collection_key is None else {"collection_key": collection_key}
        result = bridge.save_items(items, progress=progress, **placed)
    except Exception:  # noqa: BLE001 - a save that raises has no result, so every line is held back
        result = None
    return _record_save(journal, lines, result)


class ZoteroPanel(QWidget):
    """The Zotero tab: status, Start Zotero behind a confirmation, and a read-only library search."""

    def __init__(
        self,
        client: Any = zotero_client,
        confirm: Callable[[QWidget, str, str], bool] | None = None,
        parent: QWidget | None = None,
        bridge: Any = zotero_bridge,
        journal_path: Path | None = None,
        projects: Callable[[], Sequence[tuple[str, str, str]]] | None = None,
        scopes_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("zotero-panel")
        self._client = client
        self._bridge = bridge
        # The Meridian projects as (id, name, parent id), read on a worker. None means this window has no project list.
        self._projects_source = projects
        # Which collection each project searches. Read once here, then kept in step with the file by every save and clear.
        self._scopes_path = zotero_scope.default_path() if scopes_path is None else scopes_path
        self._scope_read = zotero_scope.read_scopes(self._scopes_path)
        # The projects this window has seen bound, or with an entry the file could not use. A save for one of them that the
        # file no longer binds is refused until this window chooses or clears its folder. Only a clear removes a project.
        self._bound_projects: set[str] = set()
        self._learn_bindings(self._scope_read)
        self._libraries: list[zotero_client.ZoteroLibrary] = []
        self._collections_by_key: dict[str, str] = {}
        self._pending_collection_key = ""
        self._pending_scope_note = ""
        self._action_text = ""
        self._load_texts: dict[str, str] = {}
        self._scope_loaded_once = False
        self._saving = False
        self._pending_lines: list[str] = []
        # The save journal. It is opened on a worker thread, so until it is open and read, Save is off.
        self._journal: zotero_journal.SaveJournal | None = None
        self._journal_path = zotero_journal.default_path() if journal_path is None else journal_path
        self._journal_loaded = False
        # A journal failure that stays for the rest of this run, so Save stays off. A busy journal is tried again.
        self._journal_problem: str | None = None
        self._journal_busy: str | None = None
        self._journal_retry = QTimer(self)
        self._journal_retry.setSingleShot(True)
        self._journal_retry.timeout.connect(self._start_journal_read)
        # The held entries from the last read that finished. A read that fails keeps them on screen.
        self._held_entries: list[zotero_journal.JournalEntry] = []
        self._confirm = ask_start if confirm is None else confirm
        self._exe: str | None = None
        self._running: bool | None = None
        self._checking = False
        self._searching = False
        self._acting = False
        self._probed_on_show = False
        self._pending_query = ""
        # Identifies this panel's reads. A plain object, so a job can hold it without holding the panel.
        self._owner = object()
        # Each read takes the next number, and each kind keeps the number of its latest read. A result from an older
        # read of the same kind is dropped. Reads of different kinds never supersede each other.
        self._generation = 0
        self._latest: dict[str, int] = {}
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()
        self._start_journal_read()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "zotero-intro", INTRO_TEXT))

        self._installed = _label(self, "zotero-installed", NOT_CHECKED_TEXT)
        self._process = _label(self, "zotero-process", "")
        self._api = _label(self, "zotero-api", "")
        self._api_detail = _label(self, "zotero-api-detail", "")
        self._start_hint = _label(self, "zotero-start-hint", "")
        for label in (self._installed, self._process, self._api, self._api_detail, self._start_hint):
            layout.addWidget(label)

        actions = QHBoxLayout()
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.setObjectName("zotero-refresh")
        self._start_button = QPushButton("Start Zotero...", self)
        self._start_button.setObjectName("zotero-start")
        self._start_button.setEnabled(False)
        actions.addWidget(self._refresh_button)
        actions.addWidget(self._start_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._start_result = _label(self, "zotero-start-result", "")
        layout.addWidget(self._start_result)

        self._build_scope(layout)

        search_row = QHBoxLayout()
        self._search_input = QLineEdit(self)
        self._search_input.setObjectName("zotero-search-input")
        self._search_input.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search_button = QPushButton("Search", self)
        self._search_button.setObjectName("zotero-search-button")
        search_row.addWidget(self._search_input, 1)
        search_row.addWidget(self._search_button)
        layout.addLayout(search_row)

        self._search_status = _label(self, "zotero-search-status", SEARCH_HINT)
        layout.addWidget(self._search_status)

        self._results = QTableWidget(0, len(RESULT_COLUMNS), self)
        self._results.setObjectName("zotero-results")
        self._results.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self._results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._results.verticalHeader().setVisible(False)
        self._results.horizontalHeader().setStretchLastSection(True)
        enable_table_copy(self._results)
        layout.addWidget(self._results, 1)

        save_box = QVBoxLayout()
        save_box.addWidget(_label(self, "zotero-save-heading", SAVE_HEADING))
        save_box.addWidget(_label(self, "zotero-save-intro", SAVE_INTRO))
        self._save_input = QPlainTextEdit(self)
        self._save_input.setObjectName("zotero-save-input")
        self._save_input.setPlaceholderText(SAVE_PLACEHOLDER)
        self._save_input.setMinimumHeight(90)
        save_box.addWidget(self._save_input)
        save_row = QHBoxLayout()
        self._save_button = QPushButton(SAVE_BUTTON_TEXT, self)
        self._save_button.setObjectName("zotero-save-button")
        self._bridge_state = _label(self, "zotero-bridge-state", BRIDGE_CHECKING_TEXT)
        save_row.addWidget(self._save_button)
        save_row.addWidget(self._bridge_state, 1)
        save_box.addLayout(save_row)
        self._save_result = _label(self, "zotero-save-result", "")
        save_box.addWidget(self._save_result)
        self._held_heading = _label(self, "zotero-held-heading", HELD_HEADING)
        self._held_lines = QPlainTextEdit(self)
        self._held_lines.setObjectName("zotero-held-lines")
        self._held_lines.setReadOnly(True)
        make_selectable(self._held_lines)
        self._held_lines.setMinimumHeight(60)
        self._held_note = _label(self, "zotero-held-note", "")
        self._held_button = QPushButton(HELD_BUTTON_TEXT, self)
        self._held_button.setObjectName("zotero-held-checked")
        for widget in (self._held_heading, self._held_lines, self._held_note, self._held_button):
            save_box.addWidget(widget)
            widget.setVisible(False)
        layout.addLayout(save_box)

        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        self._start_button.clicked.connect(self._on_start_clicked)
        self._search_button.clicked.connect(self._on_search_clicked)
        self._search_input.returnPressed.connect(self._on_search_clicked)
        self._save_button.clicked.connect(self._on_save_clicked)
        self._held_button.clicked.connect(self._on_held_checked_clicked)

    # ----------------------------------------------------------- project scope

    def _build_scope(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label(self, "zotero-scope-heading", SCOPE_HEADING))
        layout.addWidget(_label(self, "zotero-scope-intro", SCOPE_INTRO))
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self._scope_project = QComboBox(self)
        self._scope_project.setObjectName("zotero-scope-project")
        self._scope_library = QComboBox(self)
        self._scope_library.setObjectName("zotero-scope-library")
        self._scope_collection = QComboBox(self)
        self._scope_collection.setObjectName("zotero-scope-collection")
        rows = (
            (SCOPE_PROJECT_LABEL, self._scope_project),
            (SCOPE_LIBRARY_LABEL, self._scope_library),
            (SCOPE_COLLECTION_LABEL, self._scope_collection),
        )
        for row, (caption, combo) in enumerate(rows):
            grid.addWidget(_label(self, f"zotero-scope-caption-{row}", caption), row, 0)
            grid.addWidget(combo, row, 1)
        layout.addLayout(grid)

        actions = QHBoxLayout()
        self._scope_save = QPushButton(SCOPE_SAVE_TEXT, self)
        self._scope_save.setObjectName("zotero-scope-save")
        self._scope_clear = QPushButton(SCOPE_CLEAR_TEXT, self)
        self._scope_clear.setObjectName("zotero-scope-clear")
        self._scope_reload = QPushButton(SCOPE_RELOAD_TEXT, self)
        self._scope_reload.setObjectName("zotero-scope-reload")
        for button in (self._scope_save, self._scope_clear, self._scope_reload):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._scope_current = _label(self, "zotero-scope-current", SCOPE_NO_PROJECT_TEXT)
        self._scope_note = _label(self, "zotero-scope-note", "")
        self._scope_note.setVisible(False)
        self._scope_status = _label(self, "zotero-scope-status", "")
        self._scope_status.setVisible(False)
        for widget in (self._scope_current, self._scope_note, self._scope_status):
            layout.addWidget(widget)

        self._scope_project.currentIndexChanged.connect(self._on_scope_project_changed)
        self._scope_library.currentIndexChanged.connect(self._on_scope_library_changed)
        self._scope_save.clicked.connect(self._on_scope_save_clicked)
        self._scope_clear.clicked.connect(self._on_scope_clear_clicked)
        self._scope_reload.clicked.connect(self._load_libraries)
        self._scope_library.setEnabled(False)
        self._scope_collection.setEnabled(False)

    def _scope_message(self, text: str) -> None:
        """An action's message (save, clear, a refused save). It shows above the load messages until the next change."""
        self._action_text = text
        self._render_scope_status()

    def _set_load_text(self, source: str, text: str) -> None:
        """One load's message (projects, libraries or collections). Each load keeps its own, so one cannot erase another."""
        self._load_texts[source] = text
        self._render_scope_status()

    def _render_scope_status(self) -> None:
        text = self._action_text or " ".join(item for item in self._load_texts.values() if item)
        self._scope_status.setText(text)
        self._scope_status.setVisible(bool(text))

    def _selected_project(self) -> str:
        value = self._scope_project.currentData()
        return value if isinstance(value, str) else ""

    def _scope_for(self, project_id: str) -> zotero_scope.ProjectScope | None:
        return self._scope_read.scopes.get(project_id) if project_id else None

    def _learn_bindings(self, read: zotero_scope.ScopeRead) -> None:
        """Remember every project the file binds or holds an unusable entry for. Only a clear forgets one."""
        self._bound_projects.update(read.scopes)
        self._bound_projects.update(read.skipped)

    def _current_library(self) -> zotero_client.ZoteroLibrary | None:
        key = self._scope_library.currentData()
        if not isinstance(key, str):
            return None
        for library in self._libraries:
            if f"{library.library_type}:{library.library_id}" == key:
                return library
        return None

    def _select_library(self, library_type: str, library_id: str) -> None:
        index = self._scope_library.findData(f"{library_type}:{library_id}")
        if index < 0:
            return
        self._scope_library.blockSignals(True)  # a binding shown on selection must not start a second load
        self._scope_library.setCurrentIndex(index)
        self._scope_library.blockSignals(False)

    def _update_current_label(self) -> None:
        project_id = self._selected_project()
        if not project_id:
            self._scope_current.setText(SCOPE_NO_PROJECT_TEXT)
            return
        name = self._scope_project.currentText()
        scope = self._scope_for(project_id)
        if scope is None:
            self._scope_current.setText(SCOPE_CURRENT_UNBOUND.format(name))
        else:
            self._scope_current.setText(SCOPE_CURRENT_BOUND.format(name, scope.collection_name, scope.library_name))

    def _load_projects(self) -> None:
        if self._projects_source is None:
            self._set_load_text("projects", SCOPE_NO_SOURCE_TEXT)
            return
        self._start_read(PROJECTS, self._projects_source)

    def _load_libraries(self) -> None:
        client = self._client
        self._set_load_text("libraries", SCOPE_LOADING_TEXT)
        self._start_read(LIBRARIES, lambda: client.list_libraries())

    def _load_collections(self) -> None:
        library = self._current_library()
        self._collections_by_key = {}
        self._scope_collection.setEnabled(False)
        self._scope_collection.blockSignals(True)
        self._scope_collection.clear()
        self._scope_collection.blockSignals(False)
        if library is None:
            self._scope_collection.addItem(SCOPE_NO_LIBRARY_TEXT)
            return
        self._scope_collection.addItem(SCOPE_LOADING_TEXT)
        client = self._client
        self._start_read(COLLECTIONS, lambda: client.list_collections(library))

    def _on_projects_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None:
            self._set_load_text("projects", failure)
            return
        triples = [item for item in (result or []) if isinstance(item, tuple) and len(item) == 3]
        ordered = sorted(triples, key=lambda item: (item[1] or item[0]).lower())
        chosen = self._selected_project()
        self._scope_project.blockSignals(True)
        self._scope_project.clear()
        for project_id, name, _parent in ordered:
            self._scope_project.addItem(name or project_id, project_id)
        index = self._scope_project.findData(chosen) if chosen else -1
        self._scope_project.setCurrentIndex(index if index >= 0 else 0)
        self._scope_project.blockSignals(False)
        self._set_load_text("projects", "" if ordered else SCOPE_NO_PROJECTS_TEXT)
        self._on_scope_project_changed(self._scope_project.currentIndex())

    def _on_libraries_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None or not isinstance(result, tuple) or len(result) != 2:
            self._libraries = []
            self._scope_library.blockSignals(True)
            self._scope_library.clear()
            self._scope_library.blockSignals(False)
            self._scope_library.setEnabled(False)
            self._scope_note.setVisible(False)
            self._set_load_text("libraries", failure or LIBRARIES_FAILED_TEXT)
            self._load_collections()
            return
        libraries, note = result
        self._libraries = list(libraries)
        chosen = self._scope_library.currentData()
        self._scope_library.blockSignals(True)
        self._scope_library.clear()
        for library in self._libraries:
            self._scope_library.addItem(library.name, f"{library.library_type}:{library.library_id}")
        index = self._scope_library.findData(chosen) if isinstance(chosen, str) else -1
        self._scope_library.setCurrentIndex(index if index >= 0 else 0)
        self._scope_library.blockSignals(False)
        self._scope_library.setEnabled(bool(self._libraries))
        self._scope_note.setText(note)
        self._scope_note.setVisible(bool(note))
        self._set_load_text("libraries", "")
        self._on_scope_project_changed(0)

    def _on_collections_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._scope_collection.blockSignals(True)
        self._scope_collection.clear()
        self._collections_by_key = {}
        if failure is not None:
            self._scope_collection.addItem(COLLECTIONS_FAILED_TEXT)
            self._scope_collection.blockSignals(False)
            self._set_load_text("collections", failure)
            return
        rows = zotero_client.collection_rows(list(result or []))
        if not rows:
            self._scope_collection.addItem(SCOPE_NO_COLLECTIONS_TEXT)
            self._scope_collection.blockSignals(False)
            return
        for collection, depth in rows:
            self._collections_by_key[collection.key] = collection.name
            self._scope_collection.addItem("    " * depth + collection.name, collection.key)
        wanted = self._pending_collection_key
        self._pending_collection_key = ""
        index = self._scope_collection.findData(wanted) if wanted else -1
        if index >= 0:
            self._scope_collection.setCurrentIndex(index)
        self._scope_collection.setEnabled(True)
        self._scope_collection.blockSignals(False)
        self._set_load_text("collections", "")

    def _on_scope_project_changed(self, _index: int = 0) -> None:
        project_id = self._selected_project()
        self._scope_message("")  # an earlier save or clear message is about another project now
        self._update_current_label()
        scope = self._scope_for(project_id)
        if scope is not None:
            self._select_library(scope.library_type, scope.library_id)
            self._pending_collection_key = scope.collection_key
        else:
            self._pending_collection_key = ""
        self._load_collections()

    def _on_scope_library_changed(self, _index: int = 0) -> None:
        self._pending_collection_key = ""
        self._load_collections()

    def _on_scope_save_clicked(self) -> None:
        project_id = self._selected_project()
        library = self._current_library()
        key = self._scope_collection.currentData()
        if not project_id or library is None or not isinstance(key, str) or not self._scope_collection.isEnabled():
            self._scope_message(SCOPE_NEED_TEXT)
            return
        name = self._collections_by_key.get(key, "")
        try:
            scope = zotero_scope.ProjectScope(
                project_id=project_id,
                library_type=library.library_type,
                library_id=library.library_id,
                library_name=library.name,
                collection_key=key,
                collection_name=name,
            )
            read = zotero_scope.save_scope(scope, self._scopes_path)
        except zotero_scope.ScopeError as exc:
            self._scope_message(exc.message)
            return
        except OSError as exc:
            self._scope_message(NOT_SAVED_TEXT.format(exc.__class__.__name__))
            return
        self._scope_read = read
        self._learn_bindings(read)
        message = SCOPE_SAVED_TEXT.format(self._scope_project.currentText(), name, library.name)
        self._scope_message(f"{message} {read.warning}" if read.warning else message)
        self._update_current_label()

    def _on_scope_clear_clicked(self) -> None:
        project_id = self._selected_project()
        if not project_id:
            self._scope_message(SCOPE_NO_PROJECT_TEXT)
            return
        try:
            read = zotero_scope.clear_scope(project_id, self._scopes_path)
        except zotero_scope.ScopeError as exc:
            self._scope_message(exc.message)
            return
        except OSError as exc:
            self._scope_message(NOT_SAVED_TEXT.format(exc.__class__.__name__))
            return
        self._scope_read = read
        self._learn_bindings(read)
        self._bound_projects.discard(project_id)
        message = SCOPE_CLEARED_TEXT.format(self._scope_project.currentText())
        self._scope_message(f"{message} {read.warning}" if read.warning else message)
        self._update_current_label()

    # ----------------------------------------------------------- status

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if not self._probed_on_show:
            self._probed_on_show = True
            self._refresh()
            self._load_projects()
            self._load_libraries()

    @Slot()
    def _on_refresh_clicked(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if self._checking:
            return
        self._checking = True
        self._refresh_button.setEnabled(False)
        self._start_button.setEnabled(False)
        self._installed.setText(CHECKING_TEXT)
        self._process.setText("")
        self._api.setText("")
        self._api_detail.setText("")
        self._start_hint.setText("")
        self._start_read(STATUS, self._client.status)
        self._start_bridge_check()

    def _start_bridge_check(self) -> None:
        self._bridge_state.setText(BRIDGE_CHECKING_TEXT)
        self._start_read(BRIDGE, self._bridge.bridge_status)

    def _start_read(self, kind: str, work: Callable[[], Any]) -> None:
        """Start one read on the thread pool. Its result comes back to _on_delivered, tagged with kind and number."""
        self._generation += 1
        generation = self._generation
        self._latest[kind] = generation
        # The deliver closure captures only the owner, the kind, the number and the delivery object, never the panel.
        owner = self._owner
        delivery = shared_delivery()

        def deliver(payload: tuple) -> None:
            delivery.delivered.emit(Delivered(owner, _Done(kind, generation, payload)))

        QThreadPool.globalInstance().start(_Job(work, deliver))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another panel is dropped here, and so is one a newer read of its kind
        # has superseded.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if not isinstance(done, _Done) or done.generation != self._latest.get(done.kind):
            return
        if done.kind == STATUS:
            self._on_status_ready(done.payload)
        elif done.kind == SEARCH:
            self._on_search_ready(done.payload)
        elif done.kind == ACTION:
            self._on_action_ready(done.payload)
        elif done.kind == BRIDGE:
            self._on_bridge_ready(done.payload)
        elif done.kind == SAVE:
            self._on_save_ready(done.payload)
        elif done.kind == JOURNAL:
            self._on_journal_ready(done.payload)
        elif done.kind == CLEAR:
            self._on_clear_ready(done.payload)
        elif done.kind == PROJECTS:
            self._on_projects_ready(done.payload)
        elif done.kind == LIBRARIES:
            self._on_libraries_ready(done.payload)
        elif done.kind == COLLECTIONS:
            self._on_collections_ready(done.payload)

    def _on_status_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._checking = False
        self._refresh_button.setEnabled(True)
        if failure is not None or not isinstance(result, ZoteroStatus):
            self._exe = None
            self._running = None
            self._installed.setText(NOT_CHECKED_TEXT)
            self._process.setText("")
            self._api.setText(API_ERROR_TEXT)
            self._api_detail.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            self._start_hint.setText("")
            self._start_button.setEnabled(False)
            return
        self._show_status(result)

    def _show_status(self, status: ZoteroStatus) -> None:
        self._exe = status.exe
        self._running = status.process_running
        self._installed.setText(NOT_INSTALLED_TEXT if status.exe is None else INSTALLED_TEXT.format(status.exe))
        self._process.setText(process_text(status.process_running))
        self._api.setText(api_text(status.api.state))
        self._api_detail.setText(status.api.message)
        self._update_start_button()

    def _update_start_button(self) -> None:
        if self._exe is None:
            hint, enabled = START_HINT_NOT_INSTALLED, False
        elif self._running is True:
            hint, enabled = START_HINT_RUNNING, False
        else:
            hint, enabled = START_HINT_READY, True
        self._start_hint.setText(hint)
        self._start_button.setEnabled(enabled and not self._acting)

    # ----------------------------------------------------------- start

    @Slot()
    def _on_start_clicked(self) -> None:
        exe = self._exe
        if exe is None or self._acting or self._running is True:
            return
        command = command_line(exe)
        folder = str(Path(exe).parent)
        if not self._confirm(self, command, folder):
            self._start_result.setText(START_CANCELLED_TEXT)
            return
        self._acting = True
        self._start_button.setEnabled(False)
        self._start_result.setText(STARTING_TEXT)
        self._start_read(ACTION, functools.partial(_launch, self._client, exe))

    def _on_action_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._acting = False
        if failure is not None or not isinstance(result, tuple):
            self._start_result.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            self._update_start_button()
            return
        line, ready = result
        ready_text = API_READY_TEXT if ready else API_NOT_READY_TEXT
        self._start_result.setText(f"{line}\n{ready_text}")
        self._refresh()

    # ----------------------------------------------------------- search

    @Slot()
    def _on_search_clicked(self) -> None:
        query = self._search_input.text().strip()
        if not query:
            self._search_status.setText(BLANK_TEXT)
            return
        if self._searching:
            return
        self._searching = True
        self._pending_query = query
        self._search_button.setEnabled(False)
        self._results.setRowCount(0)
        project_id = self._selected_project()
        scope = self._scope_for(project_id)
        if scope is None:
            # With a project picked but no collection set, the note says so; with no project picked, nothing is added.
            self._pending_scope_note = SCOPE_SEARCH_WHOLE if project_id else ""
            self._search_status.setText(SEARCHING_TEXT)
            self._start_read(SEARCH, functools.partial(self._client.search_items, query))
            return
        client = self._client
        library = zotero_client.ZoteroLibrary(scope.library_type, scope.library_id, scope.library_name)
        key = scope.collection_key
        self._pending_scope_note = SCOPE_SEARCH_IN.format(scope.collection_name, scope.library_name)
        self._search_status.setText(SCOPE_SEARCHING_TEXT.format(scope.collection_name, scope.library_name))
        self._start_read(SEARCH, lambda: client.search_collection_items(query, library, key))

    def _on_search_ready(self, payload: tuple) -> None:
        result, failure = payload
        self._searching = False
        self._search_button.setEnabled(True)
        if failure is not None:
            self._results.setRowCount(0)
            self._search_status.setText(failure)
            return
        items: list[ZoteroItem] = list(result or [])
        self._fill_results(items)
        self._search_status.setText(results_text(self._pending_query, len(items)) + self._pending_scope_note)

    def _fill_results(self, items: list[ZoteroItem]) -> None:
        self._results.setRowCount(len(items))
        for row, item in enumerate(items):
            for column, value in enumerate((item.title, item.authors, item.year, item.key)):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._results.setItem(row, column, cell)

    # ----------------------------------------------------------- bridge and save

    def _on_bridge_ready(self, payload: tuple) -> None:
        result, failure = payload
        if failure is not None or not isinstance(result, zotero_bridge.BridgeStatus):
            self._bridge_state.setText(failure or CHECK_FAILED_TEXT.format("NoResult"))
            return
        self._bridge_state.setText(result.message)

    # ----------------------------------------------------------- journal

    def _start_journal_read(self) -> None:
        """Open the journal if it is not open yet, and read what is held. The read runs on a worker thread."""
        self._start_read(JOURNAL, functools.partial(_journal_state, self._journal_path, self._journal))

    def _on_journal_ready(self, payload: tuple) -> None:
        state, failure = payload
        if failure is not None or not isinstance(state, _JournalState):
            state = _JournalState(self._journal, None, failure or JOURNAL_CHECK_FAILED_TEXT)
        self._apply_journal_state(state)

    def _apply_journal_state(self, state: _JournalState) -> None:
        """Show what a read of the journal found. A read that did not finish keeps the held entries of the last one that did."""
        self._journal_loaded = True
        if state.journal is not None:
            self._journal = state.journal
        if state.problem is None:
            self._journal_busy = None
        else:
            self._note_journal_problem(state.problem, state.busy)
            self._save_result.setText(state.problem)
        if state.held is not None:
            self._show_held(state.held)
        elif state.held_lines and self._journal is None:
            # The journal cannot be read, so the lines its backup holds are shown. Nothing can release them from here.
            self._show_held_lines(state.held_lines, can_clear=False)
        self._update_save_enabled()

    def _note_journal_problem(self, problem: str, busy: bool) -> None:
        """A busy journal is tried again in a moment. Any other problem stays for the rest of this run."""
        if busy:
            self._journal_busy = problem
            self._journal_retry.start(JOURNAL_RETRY_MS)
        else:
            self._journal_problem = problem
        self._update_save_enabled()

    def _show_held(self, entries: Sequence[zotero_journal.JournalEntry]) -> None:
        """Show the held lines, read from the journal on disk. They survive quitting, so they show again on every launch."""
        self._held_entries = list(entries)
        self._show_held_lines([entry.line for entry in self._held_entries], can_clear=bool(self._held_entries))
        writing = any(entry.outcome == zotero_journal.WRITING for entry in self._held_entries)
        self._held_note.setText(WRITING_ELSEWHERE_TEXT if writing else "")
        self._held_note.setVisible(writing)
        self._update_save_enabled()

    def _show_held_lines(self, lines: Sequence[str], *, can_clear: bool) -> None:
        self._held_lines.setPlainText("\n".join(lines))
        shown = bool(lines)
        self._held_heading.setVisible(shown)
        self._held_lines.setVisible(shown)
        self._held_button.setVisible(shown and can_clear)

    def _update_save_enabled(self) -> None:
        """Save is off while the journal is not open or cannot be used, while it is busy, while any line is held, and while
        a save is running."""
        self._save_button.setEnabled(
            self._journal is not None
            and self._journal_problem is None
            and self._journal_busy is None
            and not self._held_entries
            and not self._saving
        )

    @Slot()
    def _on_held_checked_clicked(self) -> None:
        if self._journal is None:
            return
        entry_ids = [entry.id for entry in self._held_entries]
        self._start_read(CLEAR, functools.partial(_clear_held, self._journal, entry_ids))

    def _on_clear_ready(self, payload: tuple) -> None:
        state, failure = payload
        if failure is not None or not isinstance(state, _JournalState):
            state = _JournalState(self._journal, None, failure or JOURNAL_CHECK_FAILED_TEXT)
        self._apply_journal_state(state)
        if state.problem is None:
            still_writing = any(entry.outcome == zotero_journal.WRITING for entry in state.held or ())
            self._save_result.setText(STILL_WRITING_TEXT if still_writing else HELD_RELEASED_TEXT)

    @Slot()
    def _on_save_clicked(self) -> None:
        if self._saving:
            return
        if self._journal is None:
            self._save_result.setText(self._journal_problem or self._journal_busy or JOURNAL_LOADING_TEXT)
            return
        if self._journal_problem is not None:
            self._save_result.setText(self._journal_problem)
            return
        text = self._save_input.toPlainText()
        try:
            items = self._bridge.parse_lines(text)
        except zotero_bridge.BridgeInputError as exc:
            self._save_result.setText(exc.message)
            return
        # parse_lines makes one item per non-blank line, in order, so these lines line up with the items.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # The file decides where a save goes. This window's copy and its label follow the file, and the projects the file
        # binds are remembered, so a later save still notices a folder that has gone.
        current = zotero_scope.read_scopes(self._scopes_path)
        self._scope_read = current
        self._learn_bindings(current)
        self._update_current_label()
        collection_key, refusal = _save_placement(current, self._bound_projects, self._selected_project())
        if refusal:
            self._save_result.setText(refusal)
            return
        self._pending_lines = lines
        self._saving = True
        self._save_input.setReadOnly(True)
        self._update_save_enabled()
        self._save_result.setText(SAVING_TEXT)
        # The guard, the bridge and every journal write run on the worker, so the window never waits on the journal lock.
        self._start_read(
            SAVE,
            functools.partial(_run_save, self._journal, self._bridge, items, lines, collection_key),
        )

    def _on_save_ready(self, payload: tuple) -> None:
        done, failure = payload
        lines, self._pending_lines = self._pending_lines, []
        self._saving = False
        self._save_input.setReadOnly(False)
        if failure is not None or not isinstance(done, _SaveDone):
            # The save ended without a result the worker could record. Every line stays in the box, and the journal is read
            # again, so that whatever it did record is held.
            self._save_input.setPlainText("\n".join(lines))
            self._save_result.setText(SAVE_NO_RESULT_TEXT)
            self._start_journal_read()
            self._update_save_enabled()
            self._start_bridge_check()
            return
        # The lines before keep_from landed, or their journal entry was written, so they leave the box. The rest stay.
        self._save_input.setPlainText("\n".join(lines[done.keep_from :]))
        self._save_result.setText(done.message)
        if done.problem is not None:
            self._note_journal_problem(done.problem, done.busy)
        if done.held is not None:
            self._show_held(done.held)
        self._update_save_enabled()
        self._start_bridge_check()


__all__ = [
    "API_TEXT",
    "CHECKING_TEXT",
    "NOT_CHECKED_TEXT",
    "START_CANCELLED_TEXT",
    "ZoteroPanel",
    "api_text",
    "ask_start",
    "process_text",
    "results_text",
    "start_dialog",
]
