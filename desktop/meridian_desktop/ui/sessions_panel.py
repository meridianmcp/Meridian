"""Read-only Sessions tab: a project picker, a search box, a Refresh button, a legend, and a table of sessions.

This is Qt code and sits in ui/, so the boundary check allows it. The panel has no start, stop, restart, send or
edit control. Its inputs are the project picker, the search box, the "Only projects with active sessions" switch and
the two Refresh buttons.

The table is grouped by project. The picked project is the first group. Its subprojects (projects whose parent is the
picked project, at any depth) follow as further groups, each under a header row. A header row collapses or expands its
group when it is clicked, or when it is selected and Enter or Space is pressed. Collapsed groups stay collapsed across
the timer's refreshes. The search box filters the rows by session name or client. It reads nothing.

Every read runs on the thread pool, so a slow server never blocks the window. Each read has a generation number,
and a result from an older read is dropped once a newer one has started. There are two kinds of read:

- A full read lists the projects, then reads the picked project. Its subprojects are read only when the picked project
  answered with sessions. The sessions come from the local server first. The hosted host is asked only when the local
  server is not running and a hosted token is saved. The Refresh button is disabled while a full read runs.
- A local read asks the local server only, for the picked project and its subprojects, and only in local mode. It uses
  the project list that the last full read kept, so it never lists the projects. The 5-second timer starts one on each
  tick while the tab is visible, and a change of project starts one. A local server that does not answer gives nothing
  new, so the table and the status line keep what they show.

Outside local mode a change of project clears the table and says to press Refresh. It reads nothing. The status
line names the source the rows came from.

The switch "Only projects with active sessions" hides every project whose sessions are all idle. It reads nothing, and a
project that could not be read stays, with its reason. Its value is saved through a callback the window passes in. When
the save fails, the switch still works for this session and the status line says so.

The agent section lists the claude and codex programs running on this computer, from agent_processes.py. It reads only
when its Refresh button is pressed, on the thread pool, and it never stops, suspends or writes to a process. A
process's working folder is shown when it can be read, and its project when a known project folder holds that folder.

A session is active while it was last seen less than 10 minutes ago, the server's own window. Active rows are green and
idle rows grey, and a legend says so. Last seen is shown in local time in one format. Its tooltip says what last seen
means: the last time the session contacted the server, through a tool call or keepalive. It is not the last keystroke.

The four providers are called on the worker thread, never on the UI thread:
- local_url_provider() -> str: the local Meridian server address from Settings.
- hosted_provider() -> (hosted_url, token or None): the hosted address from Settings and the saved token from the
  keychain. The panel passes the token to the client for one request and keeps no copy. It is never shown.
- project_provider() -> sequence of (project_id, name) or (project_id, name, parent_id) tuples: fills the picker and
  the subproject tree. Only a full read calls it.
- mode_provider() -> str: the mode saved in Settings. Only "local" allows a local read.

Only names, status, last seen (as a local time) and the client name reach the table. Session summaries, task log bodies
and notes are never read by the client, so they cannot reach this panel.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QEvent, QRunnable, Qt, QThreadPool, QTimer, Slot
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import agent_processes, hosted_client
from meridian_desktop.agent_processes import AgentListing, AgentProcess, list_agent_programs
from meridian_desktop.sessions_client import (
    ACTIVE,
    ACTIVE_WINDOW_MINUTES,
    ERROR,
    IDLE,
    NO_PROJECT,
    NOT_RUNNING,
    SESSIONS,
    SOURCE_HOSTED,
    SOURCE_LOCAL,
    UNKNOWN_TIME_TEXT,
    SessionRow,
    SessionsResult,
    format_local,
    hosted_sessions,
    last_seen_age,
    local_sessions,
    local_time_text,
    parse_timestamp,
    session_state,
)
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

REFRESH_MS = 5000
LOCAL_MODE = "local"
COLUMNS = ("Name", "Status", "Last seen", "Client")
# A tree larger than this reads only its first groups, so the 5-second timer stays cheap on the local server.
MAX_GROUPS = 25
MAX_PATH_DEPTH = 10
PATH_SEPARATOR = " › "  # a single right angle quotation mark, as in "Alpha › Core"
ARROW_OPEN = "▾"  # down-pointing small triangle
ARROW_CLOSED = "▸"  # right-pointing small triangle
BULLET = "●"  # black circle
INTRO_TEXT = (
    "Sessions for the chosen project and its subprojects, grouped by project. "
    "Read-only: names, status, last seen and client only."
)
PROJECT_CAPTION = "Project"
SEARCH_PLACEHOLDER = "Search by session name or client"
SEARCH_ACCESSIBLE_NAME = "Search sessions by name or client"
REFRESH_TEXT = "Refresh"
LOADING_TEXT = "Loading sessions..."
PICK_TEXT = "Pick a project to see its sessions."
PRESS_REFRESH_TEXT = "Press Refresh to read this project's sessions."
NO_PROJECTS_TEXT = "No projects are available to pick. Create a project in Meridian first."
PROJECTS_FAILED_TEXT = "The projects could not be read. Check the local server or the database file in Settings."
READ_FAILED_TEXT = "The sessions could not be read. Check the local server or the hosted token in Settings."
ONLY_ACTIVE_TEXT = "Only projects with active sessions"
ONLY_ACTIVE_NOT_SAVED_TEXT = "The switch could not be saved, so it holds for this session only."
AGENT_CAPTION = "Agent programs on this computer"
AGENT_INTRO_TEXT = (
    "The claude and codex programs running here, read-only. Their command lines are not read, and nothing is stopped."
)
AGENT_PRESS_TEXT = "Press Refresh to list the claude and codex programs running on this computer."
AGENT_LOADING_TEXT = "Listing agent programs..."
AGENT_NO_FOLDERS_TEXT = "No project folder is saved yet, so no program is matched to a project."
AGENT_NONE_TEXT = "No claude or codex program is running on this computer."
AGENT_COLUMNS = ("Program", "PID", "Started", "Working folder", "Project")
AGENT_TABLE_MAX_HEIGHT = 160
UNNAMED_TEXT = "(unnamed)"
SOURCE_TEXT = {SOURCE_LOCAL: "this computer's Meridian server", SOURCE_HOSTED: "hosted Meridian"}
STATE_TEXT = {ACTIVE: "Active", IDLE: "Idle"}
ACTIVE_COLOR = "#1e7b34"
IDLE_COLOR = "#5f6368"
HEADER_COLOR = "#f1f3f4"
LEGEND_ACTIVE_TEXT = f"{BULLET} Active: seen within the last {ACTIVE_WINDOW_MINUTES} minutes"
LEGEND_IDLE_TEXT = f"{BULLET} Idle: not seen within the last {ACTIVE_WINDOW_MINUTES} minutes"
LAST_SEEN_MEANING = (
    "Last seen is the last time this session contacted the server, through a tool call or keepalive. "
    "It is not the last keystroke."
)
NO_LAST_SEEN_TEXT = "No last-seen time was sent."
GROUP_TIP = "Click to collapse or expand this project's sessions. Enter or Space does the same on a selected header."
# The table keeps a project id on each header cell under this role. Session cells never carry it.
_GROUP_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_TOGGLE_KEYS = (int(Qt.Key.Key_Return), int(Qt.Key.Key_Enter), int(Qt.Key.Key_Space))


@dataclass(frozen=True)
class Project:
    """One project: its id, its name, and the id of its parent. ``parent_id`` is "" for a top-level project."""

    project_id: str
    name: str
    parent_id: str = ""


@dataclass(frozen=True)
class ProjectGroup:
    """One group of the table: a project, its path label such as "Alpha › Core", its depth (0 for the picked project),
    and the result of reading its sessions."""

    project_id: str
    label: str
    depth: int
    result: SessionsResult


@dataclass(frozen=True)
class _Snapshot:
    """One finished read.

    ``projects`` is None when the project list was not read in this pass, so the picker and the tree are left alone.
    ``root`` is None when there is nothing new to show: a local read whose server did not answer, or one that ran
    outside local mode. ``groups`` is empty unless the picked project answered with sessions.
    """

    generation: int
    projects: tuple[Project, ...] | None
    project_id: str
    root: SessionsResult | None
    groups: tuple[ProjectGroup, ...] = ()
    total_projects: int = 0


# ----------------------------------------------------------------- pure helpers


def choose_project(selected: str, choices: Sequence[tuple[str, str]]) -> str:
    """Keep the selected project when it is still listed. Otherwise take the first one. Empty when none is listed."""
    ids = [pid for pid, _name in choices]
    if selected in ids:
        return selected
    return ids[0] if ids else ""


def _index(projects: Sequence[Project]) -> dict[str, Project]:
    return {project.project_id: project for project in projects}


def project_label(project_id: str, projects: Mapping[str, Project]) -> str:
    """The project's path, such as "Alpha › Core › Parser", from the top project down. A project not in the list is
    named by its id. A parent loop is cut where it first repeats."""
    parts: list[str] = []
    seen: set[str] = set()
    current = project_id
    while current in projects and current not in seen and len(parts) < MAX_PATH_DEPTH:
        seen.add(current)
        project = projects[current]
        parts.append(project.name or project.project_id)
        current = project.parent_id
    if not parts:
        return project_id
    return PATH_SEPARATOR.join(reversed(parts))


def picker_choices(projects: Sequence[Project]) -> tuple[tuple[str, str], ...]:
    """(project_id, path label) for every project, subprojects included, sorted by path. This fills the picker."""
    names = _index(projects)
    pairs = [(project.project_id, project_label(project.project_id, names)) for project in projects]
    return tuple(sorted(pairs, key=lambda pair: (pair[1].casefold(), pair[0])))


def project_tree(root_id: str, projects: Sequence[Project]) -> tuple[tuple[tuple[str, str, int], ...], int]:
    """The picked project and all its subprojects, depth first, as (project_id, path label, depth) in table order.

    Children are listed by name. A project appears once even when the parent links form a loop. The first entry is
    always the picked project. The result is cut at MAX_GROUPS. The second value counts every project in the tree.
    """
    names = _index(projects)
    children: dict[str, list[Project]] = {}
    for project in projects:
        parent = project.parent_id
        if parent and parent != project.project_id and parent in names:
            children.setdefault(parent, []).append(project)
    for siblings in children.values():
        siblings.sort(key=lambda project: (project.name.casefold(), project.project_id))
    order: list[tuple[str, str, int]] = []
    seen = {root_id}

    def visit(project_id: str, depth: int) -> None:
        order.append((project_id, project_label(project_id, names), depth))
        for child in children.get(project_id, ()):
            if child.project_id not in seen:
                seen.add(child.project_id)
                visit(child.project_id, depth + 1)

    visit(root_id, 0)
    return tuple(order[:MAX_GROUPS]), len(order)


def row_matches(row: SessionRow, query: str) -> bool:
    """True when the search text is inside the session's name or client, ignoring case. Empty text matches every row."""
    needle = query.strip().casefold()
    if not needle:
        return True
    return needle in row.name.casefold() or needle in row.client.casefold()


def status_text(result: SessionsResult, checked_at: str) -> str:
    """The status line for one result. ``checked_at`` is the one local timestamp the tab shows."""
    if result.state == NO_PROJECT:
        return result.message
    if result.state == SESSIONS:
        where = SOURCE_TEXT.get(result.source, "Meridian")
        if result.rows:
            return f"{len(result.rows)} session(s) from {where}. Checked at {checked_at}."
        return f"No sessions from {where} right now. Checked at {checked_at}."
    return f"{result.message} Checked at {checked_at}."


def summary_text(
    root: SessionsResult,
    groups: Sequence[ProjectGroup],
    total_projects: int,
    query: str,
    matches: int,
    checked_at: str,
    *,
    inactive_hidden: int = 0,
) -> str:
    """The status line for a read of the picked project and its subprojects, after the search and the active-only switch
    have been applied. ``inactive_hidden`` counts the projects the switch hid."""
    if root.state != SESSIONS:
        return status_text(root, checked_at)
    where = SOURCE_TEXT.get(root.source, "Meridian")
    total = sum(len(group.result.rows) for group in groups if group.result.state == SESSIONS)
    parts = [f"{total} session(s) from {where}." if total else f"No sessions from {where} right now."]
    if len(groups) > 1:
        parts.append(f"Grouped by project: {len(groups)} shown, including subprojects.")
    if total_projects > len(groups):
        parts.append(f"Only the first {len(groups)} of {total_projects} projects in this tree are read.")
    if inactive_hidden:
        parts.append(f"{inactive_hidden} project(s) hidden: no active sessions.")
    unread = sum(1 for group in groups if group.result.state != SESSIONS)
    if unread:
        parts.append(f"{unread} project(s) could not be read.")
    needle = query.strip()
    if needle:
        parts.append(f'{matches} of {total} match "{needle}".')
    parts.append(f"Checked at {checked_at}.")
    return " ".join(parts)


def row_cells(row: SessionRow, now: datetime) -> tuple[str, str, str, str]:
    """The four table cells for one session, in COLUMNS order.

    Status is Active or Idle from the last-seen time. Last seen is a local timestamp in the one format. The client is
    the one client name the session sent, so a row never lists two.
    """
    state = session_state(row.last_seen, now)
    return (row.name or UNNAMED_TEXT, STATE_TEXT[state], local_time_text(row.last_seen), row.client)


def status_tip(row: SessionRow) -> str:
    """The tooltip of a session's status: the server's own status and what Active and Idle mean here."""
    server = row.status or "not sent"
    return (
        f"Server status: {server}. Active means last seen less than {ACTIVE_WINDOW_MINUTES} minutes ago. "
        f"Idle means not seen in that time."
    )


def last_seen_tip(row: SessionRow, now: datetime) -> str:
    """The tooltip of a session's last-seen cell: what last seen means, and the local time and age of the contact."""
    if parse_timestamp(row.last_seen) is None:
        return f"{LAST_SEEN_MEANING} {NO_LAST_SEEN_TEXT}"
    return f"{LAST_SEEN_MEANING} Local time {local_time_text(row.last_seen)}, {last_seen_age(row.last_seen, now)}."


def row_is_active(row: SessionRow, now: datetime) -> bool:
    """True when the session is Active by the last-seen rule the table shows."""
    return session_state(row.last_seen, now) == ACTIVE


def started_text(started_ns: int | None) -> str:
    """A process start time in the one local format, or UNKNOWN_TIME_TEXT when it is not known."""
    if started_ns is None:
        return UNKNOWN_TIME_TEXT
    try:
        return format_local(datetime.fromtimestamp(started_ns / 1_000_000_000, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return UNKNOWN_TIME_TEXT


def agent_cells(agent: AgentProcess) -> tuple[str, str, str, str, str]:
    """The five cells of one agent row, in AGENT_COLUMNS order. An empty folder or project reads in plain words."""
    return (
        agent.program,
        str(agent.pid),
        started_text(agent.started_ns),
        agent.folder or agent_processes.NOT_READ_TEXT,
        agent.project or agent_processes.NO_MATCH_TEXT,
    )


def agents_summary(listing: AgentListing, checked_at: str, has_folders: bool) -> str:
    """The status line for a listing that worked: how many programs, whether any project folder is known, the time."""
    count = len(listing.agents)
    parts = [AGENT_NONE_TEXT if count == 0 else f"{count} agent program(s) running on this computer."]
    if not has_folders:
        parts.append(AGENT_NO_FOLDERS_TEXT)
    parts.append(f"Checked at {checked_at}.")
    return " ".join(parts)


def _plain(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = make_selectable(QLabel(text, parent))
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _group_of(item: QTableWidgetItem | None) -> str:
    """The project id of a header cell, or "" for any other cell."""
    if item is None:
        return ""
    value = item.data(_GROUP_ROLE)
    return value if isinstance(value, str) else ""


# ----------------------------------------------------------------- worker


class _RefreshJob(QRunnable):
    """Run one read on the thread pool and deliver a _Snapshot.

    A failure becomes an error result with fixed text. The text names no exception class and no value, so a raw
    exception never reaches the UI thread.
    """

    def __init__(self, generation: int, work: Callable[[], _Snapshot], deliver: Callable[[_Snapshot], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        try:
            snapshot = self._work()
        except Exception:  # noqa: BLE001 - a worker must never raise into the pool
            failed = SessionsResult(ERROR, READ_FAILED_TEXT)
            snapshot = _Snapshot(self._generation, None, "", failed)
        try:
            self._deliver(snapshot)
        except RuntimeError:
            pass  # the panel was deleted while the read ran


# ----------------------------------------------------------------- reads


@dataclass(frozen=True)
class _AgentRead:
    """One finished agent listing, with the generation of the press that started it."""

    generation: int
    listing: AgentListing


class _AgentJob(QRunnable):
    """Run one agent listing on the thread pool and deliver an _AgentRead.

    A failure becomes an error listing with fixed text, so no raw exception reaches the UI thread.
    """

    def __init__(
        self,
        generation: int,
        work: Callable[[], AgentListing],
        deliver: Callable[[_AgentRead], None],
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        try:
            listing = self._work()
        except Exception:  # noqa: BLE001 - a worker must never raise into the pool
            listing = AgentListing(agent_processes.STATE_ERROR, agent_processes.READ_FAILED_TEXT)
        try:
            self._deliver(_AgentRead(self._generation, listing))
        except RuntimeError:
            pass  # the panel was deleted while the listing ran


class _Reads:
    """The reads the Sessions tab runs on the worker thread, through the providers Settings gives it.

    It holds no widget. A read that is still running when the tab is deleted therefore never keeps the tab
    alive, and never touches it. The tab holds one of these and hands its reads to the thread pool.
    """

    def __init__(
        self,
        local_url_provider: Callable[[], str],
        hosted_provider: Callable[[], tuple[str, str | None]],
        project_provider: Callable[[], Sequence[tuple[str, ...]]],
        mode_provider: Callable[[], str],
        local_transport: Any,
        hosted_transport: hosted_client.Transport | None,
    ) -> None:
        self._local_url = local_url_provider
        self._hosted = hosted_provider
        self._projects = project_provider
        self._mode = mode_provider
        self._local_transport = local_transport
        self._hosted_transport = hosted_transport

    def collect_full(self, generation: int, selected: str) -> _Snapshot:
        """Runs on a worker. Lists the projects once, then reads the chosen project and its subprojects."""
        projects, failure = self._read_projects()
        chosen = selected if projects is None else choose_project(selected, picker_choices(projects))
        if not chosen:
            if failure:
                message = failure
            elif projects == ():
                message = NO_PROJECTS_TEXT
            else:
                message = PICK_TEXT
            return _Snapshot(generation, projects, "", SessionsResult(NO_PROJECT, message))
        root, groups, total = self._read_tree(chosen, projects or (), self._read_sessions)
        return _Snapshot(generation, projects, chosen, root, groups, total)

    def collect_local(self, generation: int, selected: str, projects: tuple[Project, ...]) -> _Snapshot:
        """Runs on a worker. A local read: the local server only, for the picked project and its subprojects, and only
        in local mode.

        It never lists the projects and never calls the hosted host. It uses ``projects``, the list the last full read
        kept. When the local server does not answer, there is nothing new to show, so the result has no root and the
        table and status line keep what they show.
        """
        if not selected or self._mode() != LOCAL_MODE:
            return _Snapshot(generation, None, "", None)

        def read_local(project_id: str) -> SessionsResult:
            return local_sessions(self._local_url(), project_id, transport=self._local_transport)

        root, groups, total = self._read_tree(selected, projects, read_local)
        if root.state == NOT_RUNNING:
            return _Snapshot(generation, None, selected, None)
        return _Snapshot(generation, None, selected, root, groups, total)

    def _read_tree(
        self,
        root_id: str,
        projects: Sequence[Project],
        read: Callable[[str], SessionsResult],
    ) -> tuple[SessionsResult, tuple[ProjectGroup, ...], int]:
        """Read the picked project. When it answered with sessions, read each of its subprojects too, in tree order.

        A project that did not answer with sessions makes no further request, so a refused or failing server is asked
        once. Returns (the picked project's result, the groups, the number of projects in the tree).
        """
        root = read(root_id)
        if root.state != SESSIONS:
            return root, (), 0
        entries, total = project_tree(root_id, projects)
        groups = [ProjectGroup(root_id, entries[0][1], 0, root)]
        for project_id, label, depth in entries[1:]:
            groups.append(ProjectGroup(project_id, label, depth, read(project_id)))
        return root, tuple(groups), total

    def _read_projects(self) -> tuple[tuple[Project, ...] | None, str]:
        """(projects, "") on success. (None, message) when the project list could not be read."""
        try:
            found: list[Project] = []
            for item in self._projects():
                project_id = item[0] if len(item) > 0 else None
                if not isinstance(project_id, str) or not project_id.strip():
                    continue
                name = item[1] if len(item) > 1 else ""
                parent = item[2] if len(item) > 2 else ""
                cleaned = project_id.strip()
                found.append(Project(cleaned, _plain(name) or cleaned, _plain(parent)))
        except Exception:  # noqa: BLE001 - a failed read is named in plain words, never by class or value
            return None, PROJECTS_FAILED_TEXT
        return tuple(found), ""

    def _read_sessions(self, project_id: str) -> SessionsResult:
        """The local server first. Hosted only when the local server is not running and a token is saved."""
        local = local_sessions(self._local_url(), project_id, transport=self._local_transport)
        if local.state != NOT_RUNNING:
            return local
        hosted_url, token = self._hosted()
        if not isinstance(token, str) or not token.strip():
            return local
        return hosted_sessions(hosted_url, project_id, token, transport=self._hosted_transport)


# ----------------------------------------------------------------- panel


class SessionsPanel(QWidget):
    """The Sessions tab: the picked project's sessions, grouped by subproject. Read on first show and on Refresh, and
    locally on a timer."""

    def __init__(
        self,
        local_url_provider: Callable[[], str],
        hosted_provider: Callable[[], tuple[str, str | None]],
        project_provider: Callable[[], Sequence[tuple[str, ...]]],
        mode_provider: Callable[[], str],
        parent: QWidget | None = None,
        *,
        local_transport: Any = None,
        hosted_transport: hosted_client.Transport | None = None,
        only_active: bool = False,
        on_only_active_changed: Callable[[bool], bool] | None = None,
        project_folders: Mapping[str, str] | None = None,
        agent_lister: Callable[[Mapping[str, str]], AgentListing] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sessions-panel")
        self._reads = _Reads(
            local_url_provider,
            hosted_provider,
            project_provider,
            mode_provider,
            local_transport,
            hosted_transport,
        )
        # Identifies this tab's reads. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        self._in_flight: int | None = None
        self._read_once = False
        # The last project list, kept for the local reads, and the last result, kept so a search or a collapse can
        # redraw the table without a read.
        self._projects: tuple[Project, ...] = ()
        self._root: SessionsResult | None = None
        self._groups: tuple[ProjectGroup, ...] = ()
        self._total_projects = 0
        self._checked_at: datetime | None = None
        self._collapsed: set[str] = set()
        # The only-active switch: its starting value, its save callback, and the note shown when a save fails.
        self._only_active_initial = bool(only_active)
        self._only_active_saver = on_only_active_changed
        self._only_active_note = ""
        # The agent section: the folders that name a project, the lister, and the last listing and its generation.
        self._project_folders: dict[str, str] = dict(project_folders or {})
        self._agent_lister = agent_lister if agent_lister is not None else list_agent_programs
        self._agent_generation = 0
        self._agent_in_flight: int | None = None
        self._agent_listing: AgentListing | None = None
        self._agent_checked_at: datetime | None = None
        shared_delivery().delivered.connect(self._on_delivered)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._on_tick)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "sessions-intro", INTRO_TEXT))

        picker_row = QHBoxLayout()
        picker_row.addWidget(_label(self, "sessions-project-caption", PROJECT_CAPTION))
        self._combo = QComboBox(self)
        self._combo.setObjectName("sessions-project-picker")
        self._combo.currentIndexChanged.connect(self._on_project_changed)
        picker_row.addWidget(self._combo, 1)
        self._refresh = QPushButton(REFRESH_TEXT, self)
        self._refresh.setObjectName("sessions-refresh")
        self._refresh.clicked.connect(lambda _checked=False: self.refresh())
        picker_row.addWidget(self._refresh)
        layout.addLayout(picker_row)

        self._search = QLineEdit(self)
        self._search.setObjectName("sessions-search")
        self._search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search.setAccessibleName(SEARCH_ACCESSIBLE_NAME)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)
        layout.addWidget(self._search)

        self._only_active = QCheckBox(ONLY_ACTIVE_TEXT, self)
        self._only_active.setObjectName("sessions-only-active")
        self._only_active.setChecked(self._only_active_initial)
        # Connected after the starting value is set, so building the tab saves nothing.
        self._only_active.toggled.connect(self._on_only_active_toggled)
        layout.addWidget(self._only_active)

        self._status = _label(self, "sessions-status", LOADING_TEXT)
        layout.addWidget(self._status)

        legend = QHBoxLayout()
        self._legend_active = _label(self, "sessions-legend-active", LEGEND_ACTIVE_TEXT)
        self._legend_active.setStyleSheet(f"color: {ACTIVE_COLOR};")
        self._legend_idle = _label(self, "sessions-legend-idle", LEGEND_IDLE_TEXT)
        self._legend_idle.setStyleSheet(f"color: {IDLE_COLOR};")
        legend.addWidget(self._legend_active)
        legend.addWidget(self._legend_idle)
        legend.addStretch(1)
        layout.addLayout(legend)

        self._table = QTableWidget(0, len(COLUMNS), self)
        self._table.setObjectName("sessions-table")
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.installEventFilter(self)
        enable_table_copy(self._table)
        layout.addWidget(self._table, 1)
        self._build_agents(layout)

    def _build_agents(self, layout: QVBoxLayout) -> None:
        """The agent section under the sessions table: a caption, a note, Refresh with its status line, and a table."""
        layout.addWidget(_label(self, "sessions-agents-caption", AGENT_CAPTION))
        layout.addWidget(_label(self, "sessions-agents-intro", AGENT_INTRO_TEXT))
        row = QHBoxLayout()
        self._agents_refresh = QPushButton(REFRESH_TEXT, self)
        self._agents_refresh.setObjectName("sessions-agents-refresh")
        self._agents_refresh.clicked.connect(lambda _checked=False: self.refresh_agents())
        row.addWidget(self._agents_refresh)
        self._agents_status = _label(self, "sessions-agents-status", AGENT_PRESS_TEXT)
        row.addWidget(self._agents_status, 1)
        layout.addLayout(row)

        self._agents_table = QTableWidget(0, len(AGENT_COLUMNS), self)
        self._agents_table.setObjectName("sessions-agents-table")
        self._agents_table.setHorizontalHeaderLabels(list(AGENT_COLUMNS))
        self._agents_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._agents_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._agents_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._agents_table.verticalHeader().setVisible(False)
        self._agents_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._agents_table.horizontalHeader().setStretchLastSection(True)
        self._agents_table.setMaximumHeight(AGENT_TABLE_MAX_HEIGHT)
        enable_table_copy(self._agents_table)
        layout.addWidget(self._agents_table)

    # ----------------------------------------------------------- reads

    def refresh(self) -> None:
        """A full read now: the project list, then the picked project and its subprojects, through the mode chosen in
        Settings.

        A read still running is superseded, and its result is dropped. The Refresh button is disabled until this
        read ends.
        """
        self._start(full=True)

    def refresh_agents(self) -> None:
        """List the agent programs now, on the thread pool. Nothing else is read, and the list is never read on a timer.

        A press while a listing is still running does nothing: the button is disabled until that listing ends.
        """
        if self._agent_in_flight is not None:
            return
        self._agent_generation += 1
        generation = self._agent_generation
        self._agent_in_flight = generation
        self._agents_refresh.setEnabled(False)
        self._agents_status.setText(AGENT_LOADING_TEXT)

        # The job holds the lister, a copy of the folders and the shared delivery object. It never holds this tab.
        lister = self._agent_lister
        folders = dict(self._project_folders)
        owner = self._owner
        delivery = shared_delivery()

        def work() -> AgentListing:
            return lister(folders)

        def deliver(read: _AgentRead) -> None:
            delivery.delivered.emit(Delivered(owner, read))

        QThreadPool.globalInstance().start(_AgentJob(generation, work, deliver))

    def _on_agents_delivered(self, read: _AgentRead) -> None:
        # Runs on the GUI thread. A listing that a newer press has superseded is dropped.
        if read.generation != self._agent_generation:
            return
        self._agent_in_flight = None
        self._agents_refresh.setEnabled(True)
        self._agent_listing = read.listing
        self._agent_checked_at = datetime.now(timezone.utc)
        self._render_agents()

    def _render_agents(self) -> None:
        """Draw the last agent listing. It reads nothing."""
        listing = self._agent_listing
        if listing is None:
            return
        table = self._agents_table
        table.setRowCount(0)
        if listing.state != agent_processes.STATE_OK:
            self._agents_status.setText(listing.message)
            return
        for agent in listing.agents:
            index = table.rowCount()
            table.insertRow(index)
            for column, text in enumerate(agent_cells(agent)):
                table.setItem(index, column, QTableWidgetItem(text))
        checked = format_local(self._agent_checked_at) if self._agent_checked_at is not None else ""
        self._agents_status.setText(agents_summary(listing, checked, bool(self._project_folders)))

    def _start(self, *, full: bool) -> None:
        """Start one read on the thread pool. It supersedes a read still running, whose result is then dropped."""
        self._generation += 1
        generation = self._generation
        self._in_flight = generation
        selected = str(self._combo.currentData() or "")
        if full:
            self._refresh.setEnabled(False)

        # The job holds the reads, the owner token, the shared delivery object and the project list. It never holds
        # this tab.
        reads = self._reads
        owner = self._owner
        delivery = shared_delivery()
        known = self._projects

        def work() -> _Snapshot:
            if full:
                return reads.collect_full(generation, selected)
            return reads.collect_local(generation, selected, known)

        def deliver(snapshot: _Snapshot) -> None:
            delivery.delivered.emit(Delivered(owner, snapshot))

        QThreadPool.globalInstance().start(_RefreshJob(generation, work, deliver))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab is dropped here, and so is one a newer read has superseded.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        if isinstance(item.value, _AgentRead):
            self._on_agents_delivered(item.value)
            return
        snapshot = item.value
        if snapshot.generation != self._generation:
            return  # a newer read started after this one
        self._in_flight = None
        self._refresh.setEnabled(True)
        if snapshot.projects is not None:
            self._projects = snapshot.projects
            self._fill_picker(snapshot.projects, snapshot.project_id)
        if snapshot.root is not None:
            self._root = snapshot.root
            self._groups = snapshot.groups
            self._total_projects = snapshot.total_projects
            self._checked_at = datetime.now(timezone.utc)
            self._render()

    def _fill_picker(self, projects: Sequence[Project], chosen: str) -> None:
        choices = picker_choices(projects)
        self._combo.blockSignals(True)
        try:
            current = [
                (self._combo.itemData(index), self._combo.itemText(index)) for index in range(self._combo.count())
            ]
            if current != list(choices):
                self._combo.clear()
                for pid, label in choices:
                    self._combo.addItem(label, pid)
            self._combo.setCurrentIndex(self._combo.findData(chosen) if chosen else -1)
        finally:
            self._combo.blockSignals(False)

    # ----------------------------------------------------------- drawing

    def _render(self) -> None:
        """Draw the last result: the groups, the rows that match the search, and the status line. It reads nothing."""
        if self._root is None:
            return
        now = datetime.now(timezone.utc)
        query = self._search.text()
        needle = query.strip()
        only_active = self._only_active.isChecked()
        table = self._table
        # Keep the header the user had selected, and the scroll position, across the redraw.
        selected = _group_of(table.currentItem())
        scroll = table.verticalScrollBar().value()
        table.setRowCount(0)
        matches = 0
        inactive_hidden = 0
        headers: dict[str, int] = {}
        hidden_depth: int | None = None  # the depth of a collapsed group whose subtree is hidden
        for group in self._groups:
            answered = group.result.state == SESSIONS
            rows = group.result.rows if answered else ()
            visible = [row for row in rows if row_matches(row, query)]
            matches += len(visible)
            if hidden_depth is not None and group.depth > hidden_depth:
                continue  # a subproject of a collapsed project is hidden with it
            hidden_depth = None
            if needle and answered and not visible:
                continue  # a search hides a group with nothing in it
            if only_active and answered and not any(row_is_active(row, now) for row in rows):
                inactive_hidden += 1
                continue  # the switch hides a project with no active session; a project that was not read stays
            headers[group.project_id] = self._add_header(group, len(rows), len(visible), bool(needle))
            if group.project_id in self._collapsed:
                hidden_depth = group.depth
                continue
            for row in visible:
                self._add_session(row, now)
        table.verticalScrollBar().setValue(scroll)
        if selected in headers:
            table.setCurrentCell(headers[selected], 0)
        checked = format_local(self._checked_at) if self._checked_at is not None else ""
        status = summary_text(
            self._root,
            self._groups,
            self._total_projects,
            query,
            matches,
            checked,
            inactive_hidden=inactive_hidden,
        )
        self._status.setText(f"{status} {self._only_active_note}" if self._only_active_note else status)

    def _add_header(self, group: ProjectGroup, total: int, visible: int, filtering: bool) -> int:
        """Add one project header row and return its index."""
        index = self._table.rowCount()
        self._table.insertRow(index)
        arrow = ARROW_CLOSED if group.project_id in self._collapsed else ARROW_OPEN
        if group.result.state == SESSIONS:
            count = f"{visible} of {total} session(s)" if filtering else f"{total} session(s)"
            text = f"{arrow} {group.label}  ({count})"
            tip = GROUP_TIP
        else:
            text = f"{arrow} {group.label}  (could not be read)"
            tip = group.result.message
        item = QTableWidgetItem(text)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setBackground(QBrush(QColor(HEADER_COLOR)))
        item.setToolTip(tip)
        item.setData(_GROUP_ROLE, group.project_id)
        self._table.setItem(index, 0, item)
        self._table.setSpan(index, 0, 1, len(COLUMNS))
        return index

    def _add_session(self, row: SessionRow, now: datetime) -> None:
        index = self._table.rowCount()
        self._table.insertRow(index)
        for column, text in enumerate(row_cells(row, now)):
            self._table.setItem(index, column, QTableWidgetItem(text))
        colour = ACTIVE_COLOR if session_state(row.last_seen, now) == ACTIVE else IDLE_COLOR
        status_item = self._table.item(index, 1)
        status_item.setForeground(QBrush(QColor(colour)))
        status_item.setToolTip(status_tip(row))
        self._table.item(index, 2).setToolTip(last_seen_tip(row, now))

    # ----------------------------------------------------------- events

    @Slot(int, int)
    def _on_cell_clicked(self, row: int, _column: int) -> None:
        project_id = _group_of(self._table.item(row, 0))
        if project_id:
            self._toggle(project_id)

    def _toggle(self, project_id: str) -> None:
        if project_id in self._collapsed:
            self._collapsed.discard(project_id)
        else:
            self._collapsed.add(project_id)
        self._render()

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt naming
        # Enter or Space on a selected group header collapses or expands it, the same as a click.
        if watched is self._table and event.type() == QEvent.Type.KeyPress and event.key() in _TOGGLE_KEYS:
            project_id = _group_of(self._table.currentItem())
            if project_id:
                self._toggle(project_id)
                return True
        return super().eventFilter(watched, event)

    @Slot(str)
    def _on_search_changed(self, _text: str) -> None:
        self._render()

    @Slot(bool)
    def _on_only_active_toggled(self, checked: bool) -> None:
        """Redraw with the switch as it now stands, and save it through the window's callback. Reads nothing."""
        self._only_active_note = ""
        if self._only_active_saver is not None:
            try:
                saved = bool(self._only_active_saver(checked))
            except Exception:  # noqa: BLE001 - a failed save is named in plain words, never by class or value
                saved = False
            if not saved:
                self._only_active_note = ONLY_ACTIVE_NOT_SAVED_TEXT
        self._render()

    @Slot(int)
    def _on_project_changed(self, _index: int) -> None:
        self._root = None
        self._groups = ()
        self._table.setRowCount(0)
        self._status.setText(PRESS_REFRESH_TEXT)
        self._start(full=False)

    @Slot()
    def _on_tick(self) -> None:
        if self._in_flight is None:
            self._start(full=False)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._timer.start()
        if not self._read_once:
            self._read_once = True
            self.refresh()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        super().hideEvent(event)


__all__ = [
    "AGENT_COLUMNS",
    "COLUMNS",
    "LOCAL_MODE",
    "MAX_GROUPS",
    "ONLY_ACTIVE_TEXT",
    "REFRESH_MS",
    "Project",
    "ProjectGroup",
    "SessionsPanel",
    "agent_cells",
    "agents_summary",
    "choose_project",
    "last_seen_tip",
    "picker_choices",
    "project_label",
    "project_tree",
    "row_cells",
    "row_is_active",
    "row_matches",
    "started_text",
    "status_text",
    "status_tip",
    "summary_text",
]
