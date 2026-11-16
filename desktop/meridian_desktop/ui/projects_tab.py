"""Projects tab: a list of projects and a detail pane for the selected one.

This is Qt code and sits in ui/, so the boundary check allows it. The tab never writes to projects. Its rows come from
data_source.resolve, and every outside read is an injected callable in DataSources. Its one control is the button
"Back up hosted projects to this computer", shown only when a hosted token is saved. The button reads the hosted project
list once, in one run, and writes it to one backup file in the desktop backups folder (projects_backup). The tab states
that folder before the run and names the file after it. Nothing is written to the hosted service.

The resolve runs on the thread pool, so a slow probe never blocks the window. Each load has a
generation number, and a result from an older load is dropped once a newer load has started.

A pool worker never touches a Qt object. It emits its result to one shared delivery object, which
lives on the GUI thread and is never deleted. Each tab receives on the GUI thread and keeps only
its own results, so a tab deleted while its load runs is never touched by the worker.

Subprojects are nested. A row whose parent_project_id names another listed project is drawn under
that project, indented one step per level, as project_tree.arrange orders them. The name cell keeps
the plain name, so copy and selection see the same text as before. The tree is read-only: nothing
on the tab edits a row or a parent link.

Only the fields in projects_reader.PROJECT_COLUMNS reach the table or the detail pane. The
detail pane is built from those fields alone, so executor_config and agent_instructions never
appear, even if a source returned them. Every label and cell is plain text.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QRunnable, QSize, Qt, QThreadPool, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import hosted_client, local_client, project_tree, projects_backup, projects_reader, token_store
from meridian_desktop.data_source import (
    SOURCE_FILE,
    SOURCE_HOSTED,
    SOURCE_SERVER,
    STATE_OK,
    STATE_READ_FAILED,
    DataSources,
    Resolution,
    resolve,
)
from meridian_desktop.projects_reader import PROJECT_COLUMNS
from meridian_desktop.secret_store import SecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

PROBE_TIMEOUT = 2.0
LOADING_TEXT = "Loading projects..."
DETAIL_EMPTY_TEXT = "Select a project to see its details."
NONE_TEXT = "(none)"
BACKUP_BUTTON_TEXT = "Back up hosted projects to this computer"
BACKUP_RUNNING_TEXT = "Backing up hosted projects..."
BACKUP_FAILED_TEXT = "Projects could not be backed up"
SELECTED_BACKUP_BUTTON_TEXT = "Back up selected projects to this computer"
SELECTED_NONE_TEXT = "Select one or more projects in the table first."
SEARCH_PLACEHOLDER_TEXT = "Search projects by name"
SELECT_ALL_TEXT = "Select all shown"
SELECT_NONE_TEXT = "Select none"

FIELD_LABELS = {
    "id": "ID",
    "name": "Name",
    "creator_human_id": "Creator",
    "icon": "Icon",
    "status": "Status",
    "priority": "Priority",
    "parent_project_id": "Parent project",
    "created_at": "Created at",
    "last_backup": "Last backup",
}
# The table columns, in display order. Each one is in PROJECT_COLUMNS, except the last: the newest time this computer
# backed the project up, read from the backups folder. It is not a hosted field.
TABLE_FIELDS = ("name", "status", "priority", "created_at", "last_backup")
LAST_BACKUP_FIELD = "last_backup"
# The name cell stores its tree depth under this role. The delegate reads it to indent the name.
TREE_DEPTH_ROLE = Qt.ItemDataRole.UserRole + 1
TREE_INDENT_PX = 18

SOURCE_TEXT = {
    SOURCE_SERVER: "the local Meridian server",
    SOURCE_FILE: "the database file",
    SOURCE_HOSTED: "hosted Meridian",
}


def default_sources(secrets: SecretStore, *, server_transport: local_client.Transport | None = None) -> DataSources:
    """The data sources the app uses. Every one is real.

    The probe is loopback only (local_client.probe_health). The running-server reader is
    local_client.list_projects: one loopback GET /projects, with no token. The file reader opens
    the database read-only. The token comes from the keychain through token_store, and only in
    hosted mode. The hosted reader is hosted_client.list_projects: one GET /projects with the bearer
    token. ``server_transport`` is for tests: when given, the server read sends through it, not urllib.
    """
    return DataSources(
        probe=lambda url: local_client.probe_health(url, PROBE_TIMEOUT),
        read_server=lambda url: local_client.list_projects(url, transport=server_transport),
        read_file=projects_reader.list_projects,
        get_token=lambda: token_store.get_token(store=secrets),
        list_hosted=hosted_client.list_projects,
    )


def visible_row(row: dict[str, Any]) -> dict[str, Any]:
    """The listed fields of one row, in PROJECT_COLUMNS order. Every other key is dropped."""
    return {key: row.get(key) for key in PROJECT_COLUMNS}


def _table_text(value: Any) -> str:
    return "" if value is None else str(value)


def detail_text(row: dict[str, Any]) -> str:
    """The detail pane for one visible row: a labelled line per listed field."""
    lines = []
    for key in PROJECT_COLUMNS:
        value = row.get(key)
        text = NONE_TEXT if value is None or value == "" else str(value)
        lines.append(f"{FIELD_LABELS[key]}: {text}")
    return "\n".join(lines)


class _TreeNameDelegate(QStyledItemDelegate):
    """Draws a child's name indented by its depth. The cell text is not changed, only where it is drawn.

    The size hint includes the same indent, so the name column is wide enough for an indented name and its text
    is not clipped on the right.
    """

    def paint(self, painter: Any, option: QStyleOptionViewItem, index: Any) -> None:
        depth = index.data(TREE_DEPTH_ROLE)
        if isinstance(depth, int) and depth > 0:
            option = QStyleOptionViewItem(option)
            option.rect = option.rect.adjusted(depth * TREE_INDENT_PX, 0, 0, 0)
        super().paint(painter, option, index)

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        size = super().sizeHint(option, index)
        depth = index.data(TREE_DEPTH_ROLE)
        if isinstance(depth, int) and depth > 0:
            size.setWidth(size.width() + depth * TREE_INDENT_PX)
        return size


@dataclass(frozen=True)
class _Loaded:
    """One finished load: the number it was started with, what the resolver returned, and whether a hosted token is saved.

    ``token_saved`` is None when the load failed before it could tell, so the backup button keeps what it shows. It is
    the value of a Delivered whose owner is the tab's token, so it holds no Qt object.
    """

    generation: int
    outcome: Resolution
    token_saved: bool | None = None


@dataclass(frozen=True)
class _Backed:
    """One finished backup: the status line for it. It holds no Qt object and no token."""

    message: str


def _usable_token(token: object) -> bool:
    return isinstance(token, str) and bool(token.strip())


def _load(store: SettingsStore, sources: DataSources) -> tuple[Resolution, bool]:
    """Runs on a worker: reads the saved settings, resolves the one source the mode uses, and says whether a hosted
    token is saved.

    The token is read at most once. The resolve's own read is reused. A load whose resolve never reads it reads it here.
    """
    read: list[object] = []

    def get_token() -> object:
        if not read:
            read.append(sources.get_token())
        return read[0]

    outcome = resolve(store.load().settings, replace(sources, get_token=get_token))
    try:
        token = get_token()
    except Exception:  # noqa: BLE001 - an unreadable token only hides the backup button
        token = None
    return outcome, _usable_token(token)


def selection_count_text(chosen: int, total: int, shown: int) -> str:
    """The selection count for the backup row, such as "2 of 5 selected". A search that hides rows adds the shown count."""
    text = f"{chosen} of {total} selected"
    if shown < total:
        text += f" ({shown} shown)"
    return text


def _backup_cell(when: object) -> str:
    """The Last backup cell: the UTC time of the newest local backup of the project, or Never backed up."""
    return projects_backup.NEVER_BACKED_UP_TEXT if when is None else projects_backup.backup_time_text(when)


def _backup_work(store: SettingsStore, sources: DataSources, only_ids: list[str] | None = None) -> _Backed:
    """Runs on a worker: backs up all the hosted projects in one run, through the same hosted reader the tab uses."""
    settings = store.load().settings
    token = sources.get_token()
    if not _usable_token(token):
        return _Backed(projects_backup.NO_TOKEN_TEXT)
    try:
        written = projects_backup.backup_hosted(
            sources.list_hosted,
            settings.hosted_url,
            str(token),
            folder=projects_backup.backup_dir(),
            taken_at=projects_backup.utc_now(),
            only_ids=None if only_ids is None else set(only_ids),
        )
    except projects_backup.NoUsableProjects:
        # An empty answer writes nothing, so the backups already on disk are still there.
        return _Backed(projects_backup.NO_PROJECTS_TEXT)
    return _Backed(written.message)


class _LoadJob(QRunnable):
    """Run one load on the thread pool and deliver its _Loaded to the shared delivery object.

    The job holds plain data only: an owner token, a generation number, the work, and the shared delivery object.
    It never holds the tab, so the worker never touches a tab, even one that was deleted while the load ran. A failure
    becomes a read_failed Resolution that names the exception class only, so a raw exception, and any value inside it,
    never reaches the UI thread.
    """

    def __init__(
        self,
        owner: object,
        generation: int,
        work: Callable[[], tuple[Resolution, bool]],
        delivery: Delivery,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._owner = owner
        self._generation = generation
        self._work = work
        self._delivery = delivery

    def run(self) -> None:
        token_saved: bool | None
        try:
            outcome, token_saved = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failed = f"Projects could not be loaded ({exc.__class__.__name__})."
            outcome = Resolution(STATE_READ_FAILED, None, message=failed)
            token_saved = None
        try:
            self._delivery.delivered.emit(Delivered(self._owner, _Loaded(self._generation, outcome, token_saved)))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class _BackupJob(QRunnable):
    """Run one backup on the thread pool and deliver its _Backed to the shared delivery object.

    Like _LoadJob, it holds plain data only and never the tab. A failure is named in plain words: the hosted client's own
    fixed text, or the exception class only. No token, response body or path reaches the UI as an error.
    """

    def __init__(self, owner: object, work: Callable[[], _Backed], delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._owner = owner
        self._work = work
        self._delivery = delivery

    def run(self) -> None:
        try:
            report = self._work()
        except hosted_client.HostedError as exc:
            report = _Backed(f"{BACKUP_FAILED_TEXT}. {exc}")
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            report = _Backed(f"{BACKUP_FAILED_TEXT} ({exc.__class__.__name__}).")
        try:
            self._delivery.delivered.emit(Delivered(self._owner, report))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class ProjectsTab(QWidget):
    """Projects from the resolver, and the detail of the selected row. Read-only."""

    def __init__(self, settings_store: SettingsStore, sources: DataSources, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("projects-tab")
        self._settings = settings_store
        self._sources = sources
        self._generation = 0
        # Identifies this tab's loads. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._rows: list[dict[str, Any]] = []
        self._depths: list[int] = []
        # The backup controls exist only once a hosted token has been seen saved, so a tab without a token has none.
        # A backup runs one at a time.
        self._backup_widgets: tuple[QPushButton, QLabel, QLabel] | None = None
        self._backup_running = False
        self._selected_backup_button: QPushButton | None = None
        # Every backup-row widget, so one visibility rule covers them all. The row is built once a token is seen.
        self._backup_extras: list[QWidget] = []
        self._select_all_button: QPushButton | None = None
        self._select_none_button: QPushButton | None = None
        self._selection_count: QLabel | None = None
        self._search: QLineEdit | None = None
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        self._status = make_selectable(QLabel(LOADING_TEXT, self))
        self._status.setObjectName("projects-status")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        # A box that only hides the rows whose name does not match. It never edits or sends anything.
        self._search = QLineEdit(self)
        self._search.setObjectName("projects-search")
        self._search.setPlaceholderText(SEARCH_PLACEHOLDER_TEXT)
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)

        self._table = QTableWidget(0, len(TABLE_FIELDS), self)
        self._table.setObjectName("projects-table")
        self._table.setHorizontalHeaderLabels([FIELD_LABELS[field] for field in TABLE_FIELDS])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Ctrl or Shift click selects several projects, so a backup can be run for just those.
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._table.setItemDelegateForColumn(0, _TreeNameDelegate(self._table))
        self._table.itemSelectionChanged.connect(self._show_detail)
        enable_table_copy(self._table)

        self._detail = make_selectable(QLabel(DETAIL_EMPTY_TEXT))
        self._detail.setObjectName("projects-detail")
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setWordWrap(True)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        detail_scroll = QScrollArea(self)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self._detail)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._table)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    # ----------------------------------------------------------- loading

    def refresh(self) -> None:
        """Start a load of the rows. A load still running is superseded, and its result is dropped."""
        self._generation += 1
        self._status.setText(LOADING_TEXT)
        # The job gets the store, the sources and the shared delivery object, not self: a worker never holds the tab.
        work = functools.partial(_load, self._settings, self._sources)
        QThreadPool.globalInstance().start(_LoadJob(self._owner, self._generation, work, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. Another tab's result, or a load a newer one has superseded, is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        if isinstance(item.value, _Backed):
            self._show_backup(item.value)
            return
        loaded = item.value
        if not isinstance(loaded, _Loaded) or loaded.generation != self._generation:
            return
        if loaded.token_saved is not None:
            self._offer_backup(loaded.token_saved)
        self._show(loaded.outcome)

    def _show(self, outcome: Resolution) -> None:
        if outcome.state != STATE_OK:
            # Every non-ok state shows the resolver's message, and no rows.
            self._rows = []
            self._depths = []
            self._fill()
            self._status.setText(outcome.message)
            return
        arranged = project_tree.arrange([visible_row(row) for row in outcome.rows])
        self._rows = [row for row, _depth in arranged]
        self._depths = [depth for _row, depth in arranged]
        self._fill()
        where = SOURCE_TEXT.get(outcome.source or "", "Meridian")
        if self._rows:
            self._status.setText(f"Showing {len(self._rows)} project(s) from {where}.")
        else:
            self._status.setText(f"No projects found in {where}.")

    def _fill(self) -> None:
        self._table.setRowCount(len(self._rows))
        backed = self._last_backups()
        for row_index, (row, depth) in enumerate(zip(self._rows, self._depths)):
            for column, field in enumerate(TABLE_FIELDS):
                if field == LAST_BACKUP_FIELD:
                    text = _backup_cell(backed.get(str(row.get("id") or "")))
                else:
                    text = _table_text(row.get(field))
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(TREE_DEPTH_ROLE, depth)
                self._table.setItem(row_index, column, item)
        # The search hides rows that do not match, and a hidden row is never selected. The first shown row is selected.
        self._apply_filter()
        self._table.clearSelection()
        first_shown = next((row for row in range(len(self._rows)) if not self._table.isRowHidden(row)), None)
        if first_shown is not None:
            self._table.selectRow(first_shown)
        self._show_detail()

    # ----------------------------------------------------------- detail

    @Slot()
    def _show_detail(self) -> None:
        self._update_selection_controls()
        selected = sorted({item.row() for item in self._table.selectedItems()})
        if not selected or selected[0] >= len(self._rows):
            self._detail.setText(DETAIL_EMPTY_TEXT)
            return
        self._detail.setText(detail_text(self._rows[selected[0]]))

    # ----------------------------------------------------------- backup

    def _offer_backup(self, saved: bool) -> None:
        """Show the backup controls while a hosted token is saved, and hide them when none is.

        The widgets are built the first time a token is seen saved. A tab that never sees one never builds them. The
        destination line is set on each call, so it names the folder the backup would be written to now.
        """
        if self._backup_widgets is None:
            if not saved:
                return
            self._backup_widgets = self._build_backup_row()
        button, destination, status = self._backup_widgets
        destination.setText(projects_backup.destination_text(projects_backup.backup_dir()))
        for widget in (button, destination, status, *self._backup_extras):
            widget.setVisible(saved)
        self._update_selection_controls()

    def _build_backup_row(self) -> tuple[QPushButton, QLabel, QLabel]:
        column = QVBoxLayout()
        row = QHBoxLayout()
        button = QPushButton(BACKUP_BUTTON_TEXT, self)
        button.setObjectName("projects-backup-button")
        button.clicked.connect(lambda _checked=False: self._start_backup())
        row.addWidget(button)
        selected = QPushButton(SELECTED_BACKUP_BUTTON_TEXT, self)
        selected.setObjectName("projects-backup-selected")
        selected.setEnabled(False)
        selected.clicked.connect(lambda _checked=False: self._start_backup(self._selected_ids()))
        self._selected_backup_button = selected
        row.addWidget(selected)
        select_all = QPushButton(SELECT_ALL_TEXT, self)
        select_all.setObjectName("projects-select-all")
        select_all.clicked.connect(lambda _checked=False: self._select_all_shown())
        self._select_all_button = select_all
        row.addWidget(select_all)
        select_none = QPushButton(SELECT_NONE_TEXT, self)
        select_none.setObjectName("projects-select-none")
        select_none.clicked.connect(lambda _checked=False: self._table.clearSelection())
        self._select_none_button = select_none
        row.addWidget(select_none)
        count = make_selectable(QLabel("", self))
        count.setObjectName("projects-selection-count")
        count.setTextFormat(Qt.TextFormat.PlainText)
        self._selection_count = count
        row.addWidget(count)
        row.addStretch(1)
        self._backup_extras.extend((selected, select_all, select_none, count))
        destination = make_selectable(QLabel("", self))
        destination.setObjectName("projects-backup-destination")
        destination.setTextFormat(Qt.TextFormat.PlainText)
        destination.setWordWrap(True)
        status = make_selectable(QLabel("", self))
        status.setObjectName("projects-backup-status")
        status.setTextFormat(Qt.TextFormat.PlainText)
        status.setWordWrap(True)
        column.addLayout(row)
        column.addWidget(destination)
        column.addWidget(status)
        # Below the status line, above the table.
        self.layout().insertLayout(1, column)
        return button, destination, status

    def _start_backup(self, only_ids: list[str] | None = None) -> None:
        """Start one backup run: every hosted project, or only ``only_ids``. A press while one runs does nothing."""
        if self._backup_running or self._backup_widgets is None:
            return
        button, _destination, status = self._backup_widgets
        if only_ids is not None and not only_ids:
            status.setText(SELECTED_NONE_TEXT)
            return
        self._backup_running = True
        button.setEnabled(False)
        if self._selected_backup_button is not None:
            self._selected_backup_button.setEnabled(False)
        status.setText(BACKUP_RUNNING_TEXT)
        # The job gets the store and the sources, not self: a worker never holds the tab.
        work = functools.partial(_backup_work, self._settings, self._sources, only_ids)
        QThreadPool.globalInstance().start(_BackupJob(self._owner, work, shared_delivery()))

    def _selected_ids(self) -> list[str]:
        """The ids of the projects selected in the table, in table order. Only real rows count."""
        rows = sorted({index.row() for index in self._table.selectionModel().selectedRows()})
        ids = []
        for row_index in rows:
            if row_index < len(self._rows) and self._rows[row_index].get("id"):
                ids.append(str(self._rows[row_index]["id"]))
        return ids

    def _update_selection_controls(self) -> None:
        """Keep the selection controls in step with the table.

        The selected-backup button is on only when a project is selected and no backup is running. Select all is on
        when the search shows a row. The count says how many listed projects are selected, and how many the search shows.
        """
        chosen = len(self._selected_ids())
        if self._selected_backup_button is not None:
            self._selected_backup_button.setEnabled(chosen > 0 and not self._backup_running)
        if self._select_all_button is not None:
            self._select_all_button.setEnabled(self._shown_count() > 0)
        if self._selection_count is not None:
            self._selection_count.setText(selection_count_text(chosen, len(self._rows), self._shown_count()))

    def _shown_count(self) -> int:
        """How many listed rows the search leaves shown."""
        return sum(1 for row in range(len(self._rows)) if not self._table.isRowHidden(row))

    @Slot(str)
    def _apply_filter(self, _text: str = "") -> None:
        """Hide the rows whose name does not contain the search text, ignoring case. A hidden row is deselected."""
        needle = self._search.text().strip().casefold() if self._search is not None else ""
        hidden: list[int] = []
        for row_index, row in enumerate(self._rows):
            name = str(row.get("name") or "")
            is_hidden = bool(needle) and needle not in name.casefold()
            self._table.setRowHidden(row_index, is_hidden)
            if is_hidden:
                hidden.append(row_index)
        self._set_rows_selected(hidden, selected=False)
        self._update_selection_controls()

    def _set_rows_selected(self, rows: list[int], *, selected: bool) -> None:
        """Select or deselect whole rows in one change. An empty list changes nothing."""
        if not rows:
            return
        model = self._table.model()
        last_column = model.columnCount() - 1
        ranges = QItemSelection()
        for row_index in rows:
            ranges.select(model.index(row_index, 0), model.index(row_index, last_column))
        flag = QItemSelectionModel.SelectionFlag.Select if selected else QItemSelectionModel.SelectionFlag.Deselect
        self._table.selectionModel().select(ranges, flag | QItemSelectionModel.SelectionFlag.Rows)

    @Slot()
    def _select_all_shown(self) -> None:
        """Select every row the search shows, and no hidden row."""
        shown = [row for row in range(len(self._rows)) if not self._table.isRowHidden(row)]
        self._table.clearSelection()
        self._set_rows_selected(shown, selected=True)

    def _last_backups(self) -> dict[str, object]:
        """The newest local backup time for each project id, from the backups folder. Nothing is sent or read hosted."""
        return projects_backup.last_backed_up(projects_backup.backup_dir())

    def _refresh_backup_column(self) -> None:
        """Set the Last backup cell of every row again, after a backup has been written."""
        backed = self._last_backups()
        column = TABLE_FIELDS.index(LAST_BACKUP_FIELD)
        for row_index, row in enumerate(self._rows):
            item = self._table.item(row_index, column)
            if item is not None:
                item.setText(_backup_cell(backed.get(str(row.get("id") or ""))))

    def _show_backup(self, report: _Backed) -> None:
        self._backup_running = False
        if self._backup_widgets is None:
            return
        button, _destination, status = self._backup_widgets
        button.setEnabled(True)
        status.setText(report.message)
        self._update_selection_controls()
        self._refresh_backup_column()

    # ----------------------------------------------------------- first show

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Each time the tab is shown, reload, so a server or file that started since then is seen.
        self.refresh()


__all__ = [
    "FIELD_LABELS",
    "TABLE_FIELDS",
    "TREE_DEPTH_ROLE",
    "ProjectsTab",
    "default_sources",
    "detail_text",
    "visible_row",
]
