"""Tests for the Sessions tab (meridian_desktop/ui/sessions_panel.py), with pytest-qt. Offscreen Qt, no network.

Both transports are fakes, so no socket is opened and no keychain is read. Each fake records its calls and the
thread it ran on, so a test can prove that a refresh runs off the UI thread, that nothing is sent before the tab is
shown, and that a refused address sends nothing. The token is checked against every widget's visible text.
"""

import gc
import json
import re
import threading
import weakref
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QLabel, QPushButton, QTableWidget, QWidget

from _shared_delivery import emit, recording_deliveries, visible_state
from meridian_desktop import hosted_client, sessions_client as sc
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.ui.sessions_panel import (
    ACTIVE_COLOR,
    AGENT_PRESS_TEXT,
    COLUMNS,
    IDLE_COLOR,
    LOADING_TEXT,
    MAX_GROUPS,
    NO_PROJECTS_TEXT,
    PRESS_REFRESH_TEXT,
    PROJECTS_FAILED_TEXT,
    READ_FAILED_TEXT,
    REFRESH_MS,
    Project,
    SessionsPanel,
    choose_project,
    project_tree,
    row_cells,
    status_text,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT

LOCAL = "http://127.0.0.1:7878"
HOSTED = "https://usemeridian.us"
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
PID2 = "11111111-2222-4333-8444-555555555555"
SENTINEL_TOKEN = "sk_meridian_PANELSENTINEL0123456789"
SECRET_SUMMARY = "SECRET-SUMMARY-TEXT-do-not-show"


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def session(**overrides):
    row = {
        "id": "sess-1",
        "project_id": PID,
        "name": "auth-refactor",
        "status": "active",
        "last_seen": ago(hours=2),
        "created_at": ago(hours=3),
        "session_summary": {"notes": SECRET_SUMMARY},
        "agent_framework": "claude_code",
        "client_type": "claude-code",
    }
    row.update(overrides)
    return row


def body_of(rows):
    return json.dumps(rows).encode("utf-8")


class FakeLocal:
    """A local transport. It records the base, the path and the thread. It can block until released."""

    def __init__(self, status=200, body=b"[]", error=None, release=None):
        self.status = status
        self.body = body
        self.error = error
        self.release = release
        self.calls = []
        self.threads = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        self.threads.append(threading.current_thread())
        if self.release is not None:
            self.release.wait(5)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status=self.status, body=self.body)


class FakeHosted:
    """A hosted transport. It records each request and the thread it ran on."""

    def __init__(self, status=200, body=b"[]"):
        self.status = status
        self.body = body
        self.calls = []
        self.threads = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        self.threads.append(threading.current_thread())
        return hosted_client.Reply(self.status, self.body)


class ScriptedLocal:
    """A local transport that returns one body per call. The first call can be held until released."""

    def __init__(self, bodies, gate=None):
        self.bodies = list(bodies)
        self.gate = gate
        self.paths = []

    def __call__(self, base_url, path, timeout):
        self.paths.append(path)
        index = len(self.paths)
        if index == 1 and self.gate is not None:
            self.gate.wait(5)
        return SimpleNamespace(status=200, body=self.bodies[index - 1])


def build_panel(qtbot, *, local, hosted, local_url=LOCAL, hosted_value=(HOSTED, None), projects=((PID, "Alpha"),),
                project_provider=None, hosted_provider=None, mode="local"):
    """Build the panel with fake providers. Returns (panel, counts): how many times each provider was called.

    ``mode`` is the mode saved in Settings. The mode provider runs on the worker, so counts.mode also shows each
    timer tick that reached the worker.
    """
    counts = SimpleNamespace(projects=0, hosted=0, mode=0)

    def list_projects():
        counts.projects += 1
        return project_provider() if project_provider is not None else list(projects)

    def hosted_pair():
        counts.hosted += 1
        return hosted_value

    def mode_value():
        counts.mode += 1
        return mode

    panel = SessionsPanel(
        lambda: local_url,
        hosted_provider if hosted_provider is not None else hosted_pair,
        list_projects,
        mode_value,
        local_transport=local,
        hosted_transport=hosted,
    )
    qtbot.addWidget(panel)
    panel.hide()
    return panel, counts


def make_panel(qtbot, *, local, hosted=None, local_url=LOCAL, hosted_value=(HOSTED, None),
               projects=((PID, "Alpha"),), project_provider=None, mode="local"):
    hosted = hosted if hosted is not None else FakeHosted()
    panel, _counts = build_panel(
        qtbot, local=local, hosted=hosted, local_url=local_url, hosted_value=hosted_value,
        projects=projects, project_provider=project_provider, mode=mode,
    )
    return panel, hosted


def settle(qtbot, panel):
    """Wait until no refresh is running. The panel clears its in-flight marker when the latest result lands."""
    qtbot.waitUntil(lambda: panel._in_flight is None, timeout=5000)  # noqa: SLF001 - test hook


def table_of(panel):
    return panel.findChild(QTableWidget, "sessions-table")


def status_of(panel):
    return panel.findChild(QLabel, "sessions-status").text()


def picker_of(panel):
    return panel.findChild(QComboBox, "sessions-project-picker")


def all_visible_text(panel):
    """Every string the user can read in the panel: labels, the status line, the picker and every table cell."""
    parts = [label.text() for label in panel.findChildren(QLabel)]
    combo = picker_of(panel)
    parts.extend(combo.itemText(index) for index in range(combo.count()))
    table = table_of(panel)
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                parts.append(item.text())
    return "\n".join(parts)


def is_header_row(table, row):
    """A project header row spans every column. A session row spans one."""
    return table.columnSpan(row, 0) > 1


def session_cells(panel):
    """Each session row as its four cell texts, in column order. Project header rows are left out."""
    table = table_of(panel)
    return [
        tuple(table.item(row, column).text() for column in range(table.columnCount()))
        for row in range(table.rowCount())
        if not is_header_row(table, row)
    ]


def header_texts(panel):
    """The text of each project header row, in table order."""
    table = table_of(panel)
    return [table.item(row, 0).text() for row in range(table.rowCount()) if is_header_row(table, row)]


def header_text(label, count, collapsed=False):
    """A header row's text: the arrow (open or closed), the project's path label, and its session count."""
    arrow = "▸" if collapsed else "▾"
    return f"{arrow} {label}  ({count})"


def project(pid, name, parent=""):
    return Project(project_id=pid, name=name, parent_id=parent)


# ----------------------------------------------------------------- nothing sent before the tab is shown


def test_nothing_is_sent_when_the_panel_is_built_but_not_shown(qtbot):
    local = FakeLocal()
    panel, hosted = make_panel(qtbot, local=local)
    assert local.calls == []
    assert hosted.calls == []
    assert not panel._timer.isActive()  # noqa: SLF001


def test_showing_the_tab_refreshes_once_and_starts_the_timer(qtbot):
    local = FakeLocal(body=body_of([session()]))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.calls) == 1)
    settle(qtbot, panel)
    assert panel._timer.isActive()  # noqa: SLF001
    assert panel._timer.interval() == REFRESH_MS == 5000  # noqa: SLF001


def test_hiding_the_tab_stops_the_timer_and_a_tick_while_hidden_sends_nothing(qtbot):
    local = FakeLocal()
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    panel.hide()
    assert not panel._timer.isActive()  # noqa: SLF001
    count = len(local.calls)
    qtbot.wait(50)
    assert len(local.calls) == count  # the stopped timer sends nothing while the tab is hidden


def test_a_tick_is_skipped_while_a_refresh_is_still_running(qtbot):
    release = threading.Event()
    local = FakeLocal(release=release)
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.calls) == 1)
    panel._on_tick()  # noqa: SLF001
    panel._on_tick()  # noqa: SLF001
    qtbot.wait(50)
    assert len(local.calls) == 1
    release.set()
    settle(qtbot, panel)
    panel._on_tick()  # noqa: SLF001
    qtbot.waitUntil(lambda: len(local.calls) == 2)


# ----------------------------------------------------------------- off the UI thread


def test_refresh_runs_off_the_ui_thread(qtbot):
    local = FakeLocal(body=body_of([session()]))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.threads) == 1)
    settle(qtbot, panel)
    assert local.threads[0] is not threading.main_thread()


# ----------------------------------------------------------------- rows and the status line


def test_rows_show_name_state_local_last_seen_and_client(qtbot):
    # A fixed old time, so the row is idle whatever day the test runs.
    seen = datetime(2020, 1, 1, 9, 30, 15, tzinfo=timezone.utc)
    local = FakeLocal(body=body_of([session(name="auth-refactor", last_seen=seen.isoformat(), client_type="claude-code")]))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    table = table_of(panel)
    assert [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())] == list(COLUMNS)
    assert session_cells(panel) == [("auth-refactor", "Idle", sc.format_local(seen), "claude-code")]
    assert header_texts(panel) == [header_text("Alpha", "1 session(s)")]
    assert "1 session(s) from this computer's Meridian server" in status_of(panel)


def test_session_summary_is_never_shown(qtbot):
    local = FakeLocal(body=body_of([session()]))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    assert SECRET_SUMMARY not in all_visible_text(panel)


def test_empty_list_says_no_sessions_right_now(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=b"[]"))
    panel.show()
    settle(qtbot, panel)
    assert session_cells(panel) == []
    assert header_texts(panel) == [header_text("Alpha", "0 session(s)")]
    assert "No sessions from this computer's Meridian server right now." in status_of(panel)


def test_unnamed_row_reads_as_unnamed(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body_of([session(name="")])))
    panel.show()
    settle(qtbot, panel)
    assert session_cells(panel)[0][0] == "(unnamed)"


def test_tables_are_read_only_and_the_only_buttons_are_the_two_refreshes(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body_of([session()])))
    panel.show()
    settle(qtbot, panel)
    assert table_of(panel).editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    agents_table = panel.findChild(QTableWidget, "sessions-agents-table")
    assert agents_table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    names = sorted(button.objectName() for button in panel.findChildren(QPushButton))
    assert names == ["sessions-agents-refresh", "sessions-refresh"]


def test_every_label_is_selectable(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body_of([session()])))
    panel.show()
    settle(qtbot, panel)
    labels = panel.findChildren(QLabel)
    assert labels
    for label in labels:
        assert label.textInteractionFlags() & SELECTABLE_TEXT == SELECTABLE_TEXT, label.objectName()


def test_no_text_says_not_built_yet(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(error=LocalClientError("not_running", "x")))
    panel.show()
    settle(qtbot, panel)
    assert "not built yet" not in all_visible_text(panel).lower()


def test_the_agent_section_lists_nothing_on_show_and_never_claims_a_connection(qtbot):
    # The agent list is read only when its own Refresh button is pressed. Showing the tab lists no process.
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body_of([session()])))
    panel.show()
    settle(qtbot, panel)
    assert panel.findChild(QLabel, "sessions-agents-status").text() == AGENT_PRESS_TEXT
    assert "Not connected" not in all_visible_text(panel)
    # The only session row is the one session the local server sent, never a process.
    assert len(session_cells(panel)) == 1


# ----------------------------------------------------------------- picker


def test_picker_lists_projects_and_selects_the_first(qtbot):
    local = FakeLocal(body=b"[]")
    panel, _hosted = make_panel(qtbot, local=local, projects=((PID, "Alpha"), (PID2, "Beta")))
    panel.show()
    settle(qtbot, panel)
    combo = picker_of(panel)
    assert [combo.itemText(i) for i in range(combo.count())] == ["Alpha", "Beta"]
    assert combo.currentData() == PID
    assert f"/projects/{PID}/sessions" in local.calls[0][1]


def test_switching_project_reads_only_the_local_server_for_the_new_project(qtbot):
    local = FakeLocal(body=b"[]")
    panel, counts = build_panel(qtbot, local=local, hosted=FakeHosted(), projects=((PID, "Alpha"), (PID2, "Beta")))
    panel.show()
    settle(qtbot, panel)
    projects_read = counts.projects
    picker_of(panel).setCurrentIndex(1)
    qtbot.waitUntil(lambda: len(local.calls) == 2)
    settle(qtbot, panel)
    assert f"/projects/{PID2}/sessions" in local.calls[-1][1]
    assert counts.projects == projects_read  # the project list is not read again
    assert counts.hosted == 0


def test_no_projects_says_so_and_sends_nothing(qtbot):
    local = FakeLocal()
    panel, _hosted = make_panel(qtbot, local=local, projects=())
    panel.show()
    settle(qtbot, panel)
    assert status_of(panel) == NO_PROJECTS_TEXT
    assert local.calls == []


class ProjectsUnavailable(Exception):
    """Stands in for the window's exception. Its class name must never reach the status line."""


@pytest.mark.parametrize("failure", [RuntimeError("db path with secret"), ProjectsUnavailable("/home/x/secret.db")])
def test_a_failed_project_list_says_so_in_plain_words_and_sends_nothing(qtbot, failure):
    def broken():
        raise failure

    local = FakeLocal()
    panel, _hosted = make_panel(qtbot, local=local, project_provider=broken)
    panel.show()
    settle(qtbot, panel)
    assert status_of(panel) == PROJECTS_FAILED_TEXT
    assert type(failure).__name__ not in all_visible_text(panel)
    assert "secret" not in all_visible_text(panel)
    assert local.calls == []


# ----------------------------------------------------------------- sources and typed states


def test_local_not_running_without_a_token_does_not_touch_hosted(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    panel, hosted = make_panel(qtbot, local=local, hosted_value=(HOSTED, None))
    panel.show()
    settle(qtbot, panel)
    assert "not running on this computer" in status_of(panel)
    assert table_of(panel).rowCount() == 0
    assert hosted.calls == []


def test_local_not_running_with_a_token_reads_hosted_and_names_the_source(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session(name="from-hosted")]))
    panel, hosted = make_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert len(hosted.calls) == 1
    request, _timeout = hosted.calls[0]
    assert request.full_url.startswith(HOSTED + "/projects/")
    assert request.get_header("Authorization") == f"Bearer {SENTINEL_TOKEN}"
    assert session_cells(panel)[0][0] == "from-hosted"
    assert "1 session(s) from hosted Meridian" in status_of(panel)


def test_the_token_is_never_shown_in_any_visible_text(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session()]))
    panel, _hosted = make_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert SENTINEL_TOKEN not in all_visible_text(panel)


def test_the_token_is_not_shown_when_hosted_refuses_it(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(status=401)
    panel, _hosted = make_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert "did not accept" in status_of(panel)
    assert SENTINEL_TOKEN not in all_visible_text(panel)


def test_a_non_loopback_local_address_is_refused_and_nothing_is_sent(qtbot):
    local = FakeLocal()
    hosted = FakeHosted()
    panel, hosted = make_panel(qtbot, local=local, hosted=hosted, local_url="http://10.0.0.5:7878",
                               hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert local.calls == []
    assert hosted.calls == []
    assert "not on this computer" in status_of(panel)


def test_a_missing_route_shows_its_message_and_no_rows(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(status=404))
    panel.show()
    settle(qtbot, panel)
    assert table_of(panel).rowCount() == 0
    assert "no sessions route" in status_of(panel)


def test_a_failed_refresh_keeps_the_tab_usable(qtbot):
    local = FakeLocal(error=RuntimeError("socket blew up"))
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    assert "could not be checked" in status_of(panel)
    assert "blew up" not in all_visible_text(panel)


def test_a_failed_sessions_read_says_so_in_plain_words_and_names_no_class(qtbot):
    class KeychainError(Exception):
        pass

    def broken_hosted():
        raise KeychainError("token store locked")

    local = FakeLocal(error=LocalClientError("not_running", "x"))
    panel, _counts = build_panel(qtbot, local=local, hosted=FakeHosted(), hosted_provider=broken_hosted)
    panel.show()
    settle(qtbot, panel)
    assert status_of(panel).startswith(READ_FAILED_TEXT)
    assert "KeychainError" not in all_visible_text(panel)
    assert "locked" not in all_visible_text(panel)


# ----------------------------------------------------------------- when the reads happen, and what each one reads


def test_a_show_makes_one_project_read_and_one_sessions_read(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session(name="from-hosted")]))
    panel, counts = build_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    assert counts.projects == 1
    assert len(local.calls) == 1  # the sessions read asks the local server first
    assert counts.hosted == 1
    assert len(hosted.calls) == 1  # and then the hosted host, once


def test_in_hosted_mode_three_timer_ticks_make_no_hosted_call(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session()]))
    panel, counts = build_panel(
        qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN), mode="hosted",
    )
    panel.show()
    settle(qtbot, panel)
    projects, hosted_reads = counts.projects, counts.hosted
    hosted_calls, local_calls = len(hosted.calls), len(local.calls)
    mode_reads = counts.mode
    for _ in range(3):
        panel._timer.timeout.emit()  # noqa: SLF001 - the timer's real connection
        settle(qtbot, panel)
    assert counts.mode == mode_reads + 3  # each tick ran, and only read the saved mode
    assert counts.projects == projects
    assert counts.hosted == hosted_reads
    assert len(hosted.calls) == hosted_calls
    assert len(local.calls) == local_calls


def test_a_refresh_press_makes_exactly_one_project_read_and_one_sessions_read(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session()]))
    panel, counts = build_panel(
        qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN), mode="hosted",
    )
    panel.show()
    settle(qtbot, panel)
    projects, hosted_reads = counts.projects, counts.hosted
    hosted_calls, local_calls = len(hosted.calls), len(local.calls)
    button = panel.findChild(QPushButton, "sessions-refresh")
    button.click()
    settle(qtbot, panel)
    assert counts.projects == projects + 1
    assert counts.hosted == hosted_reads + 1
    assert len(local.calls) == local_calls + 1
    assert len(hosted.calls) == hosted_calls + 1
    assert button.isEnabled()


def test_a_second_show_makes_no_read(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session()]))
    panel, counts = build_panel(
        qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN), mode="hosted",
    )
    panel.show()
    settle(qtbot, panel)
    panel.hide()
    before = (counts.projects, counts.hosted, len(local.calls), len(hosted.calls))
    panel.show()
    qtbot.wait(50)
    assert (counts.projects, counts.hosted, len(local.calls), len(hosted.calls)) == before
    assert panel._timer.isActive()  # noqa: SLF001


def test_a_local_tick_reads_only_the_local_server_for_the_picked_project(qtbot):
    local = FakeLocal(body=body_of([session()]))
    hosted = FakeHosted()
    panel, counts = build_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    projects, local_calls = counts.projects, len(local.calls)
    for _ in range(2):
        panel._timer.timeout.emit()  # noqa: SLF001
        settle(qtbot, panel)
    assert len(local.calls) == local_calls + 2
    assert all(f"/projects/{PID}/sessions" in path for _base, path, _timeout in local.calls[local_calls:])
    assert counts.projects == projects  # the timer never reads the project list
    assert counts.hosted == 0
    assert hosted.calls == []


def test_a_local_tick_that_gets_no_answer_changes_nothing_and_asks_hosted_nothing(qtbot):
    local = FakeLocal(body=body_of([session(name="kept")]))
    hosted = FakeHosted()
    panel, counts = build_panel(qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN))
    panel.show()
    settle(qtbot, panel)
    status_before = status_of(panel)
    local.error = LocalClientError("not_running", "x")  # the server stops answering
    hosted_reads = counts.hosted
    panel._timer.timeout.emit()  # noqa: SLF001
    settle(qtbot, panel)
    assert len(local.calls) == 2
    assert counts.hosted == hosted_reads
    assert hosted.calls == []
    assert status_of(panel) == status_before
    assert session_cells(panel)[0][0] == "kept"


def test_a_project_change_outside_local_mode_reads_nothing_and_says_press_refresh(qtbot):
    local = FakeLocal(error=LocalClientError("not_running", "x"))
    hosted = FakeHosted(body=body_of([session()]))
    panel, counts = build_panel(
        qtbot, local=local, hosted=hosted, hosted_value=(HOSTED, SENTINEL_TOKEN), mode="hosted",
        projects=((PID, "Alpha"), (PID2, "Beta")),
    )
    panel.show()
    settle(qtbot, panel)
    before = (counts.projects, counts.hosted, len(local.calls), len(hosted.calls))
    picker_of(panel).setCurrentIndex(1)
    settle(qtbot, panel)
    assert (counts.projects, counts.hosted, len(local.calls), len(hosted.calls)) == before
    assert status_of(panel) == PRESS_REFRESH_TEXT
    assert table_of(panel).rowCount() == 0


def test_a_project_change_with_no_local_answer_keeps_the_press_refresh_line(qtbot):
    local = FakeLocal(body=body_of([session()]))
    panel, _counts = build_panel(qtbot, local=local, hosted=FakeHosted(), projects=((PID, "Alpha"), (PID2, "Beta")))
    panel.show()
    settle(qtbot, panel)
    local.error = LocalClientError("not_running", "x")
    picker_of(panel).setCurrentIndex(1)
    settle(qtbot, panel)
    assert status_of(panel) == PRESS_REFRESH_TEXT
    assert table_of(panel).rowCount() == 0


def test_the_refresh_button_is_disabled_while_a_full_read_runs(qtbot):
    release = threading.Event()
    local = FakeLocal(body=b"[]", release=release)
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.calls) == 1)
    button = panel.findChild(QPushButton, "sessions-refresh")
    assert not button.isEnabled()
    release.set()
    settle(qtbot, panel)
    assert button.isEnabled()


# ----------------------------------------------------------------- stale results


def test_a_stale_result_is_dropped_when_a_newer_refresh_has_started(qtbot):
    gate = threading.Event()
    local = ScriptedLocal([body_of([session(name="old-run")]), body_of([session(name="new-run")])], gate=gate)
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    qtbot.waitUntil(lambda: len(local.paths) == 1)
    panel.refresh()
    qtbot.waitUntil(lambda: _first_name(panel) == "new-run")
    gate.set()
    qtbot.wait(150)
    settle(qtbot, panel)
    assert _first_name(panel) == "new-run"


def _first_name(panel):
    """The name of the only session row, or "" when there is not exactly one."""
    cells = session_cells(panel)
    return cells[0][0] if len(cells) == 1 else ""


def test_a_result_from_another_tab_or_from_a_superseded_read_is_dropped(qtbot):
    """A sibling tab's answer, and this tab's own answer from a read that a newer read superseded, show nothing.

    Two tabs each read their own sessions. The first read of each carries generation 1. The sibling's answer reaches
    this tab at that same generation, so this tab drops it by owner alone. This tab then reads again, and its first
    answer is replayed: it carries this tab's own owner with a superseded generation, so it is dropped by generation
    alone. The second rows are what the user sees throughout, and they are the control that this tab's own current
    answer is accepted.
    """
    own = ScriptedLocal([body_of([session(name="own-first")]), body_of([session(name="own-second")])])
    sibling_local = ScriptedLocal([body_of([session(name="sibling-first")])])
    with recording_deliveries() as emitted:
        panel, _hosted = make_panel(qtbot, local=own)
        panel.show()
        qtbot.waitUntil(lambda: _first_name(panel) == "own-first", timeout=5000)
        own_first = emitted[0]
        shown_first = visible_state(panel)
        sibling, _sibling_hosted = make_panel(qtbot, local=sibling_local)
        sibling.show()
        qtbot.waitUntil(lambda: _first_name(sibling) == "sibling-first", timeout=5000)
        assert len(emitted) == 2
        qtbot.wait(20)  # the sibling's answer has reached every tab that listens on the shared object
        assert visible_state(panel) == shown_first
        panel.refresh()
        qtbot.waitUntil(lambda: _first_name(panel) == "own-second", timeout=5000)
        shown_second = visible_state(panel)
        assert shown_second != shown_first
        emit(own_first)  # this tab's answer from the first read, which the second read superseded
        qtbot.wait(20)
    assert visible_state(panel) == shown_second


# ----------------------------------------------------------------- pure helpers


def test_choose_project_keeps_a_listed_selection_and_otherwise_takes_the_first():
    choices = ((PID, "Alpha"), (PID2, "Beta"))
    assert choose_project(PID2, choices) == PID2
    assert choose_project("gone", choices) == PID
    assert choose_project("", ()) == ""


def test_status_text_for_each_state():
    rows = (sc.SessionRow(name="a", status="active", last_seen="", client=""),)
    assert status_text(sc.SessionsResult(sc.SESSIONS, "", sc.SOURCE_LOCAL, rows), "12:00:00") == (
        "1 session(s) from this computer's Meridian server. Checked at 12:00:00."
    )
    assert status_text(sc.SessionsResult(sc.SESSIONS, "", sc.SOURCE_HOSTED), "12:00:00") == (
        "No sessions from hosted Meridian right now. Checked at 12:00:00."
    )
    assert status_text(sc.SessionsResult(sc.NO_PROJECT, "Pick one."), "12:00:00") == "Pick one."
    assert status_text(sc.SessionsResult(sc.ERROR, "Broken.", sc.SOURCE_LOCAL), "12:00:00") == (
        "Broken. Checked at 12:00:00."
    )


def test_row_cells_orders_the_four_columns_with_state_local_time_and_one_client():
    now = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
    row = sc.SessionRow(name="", status="idle", last_seen="2026-10-22 11:55:00", client="codex")
    # Five minutes ago is inside the 10-minute window, so the row is Active, whatever the server status says.
    expected_seen = sc.format_local(datetime(2026, 10, 22, 11, 55, 0, tzinfo=timezone.utc))
    assert row_cells(row, now) == ("(unnamed)", "Active", expected_seen, "codex")


def test_the_table_uses_single_row_selection(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal())
    table = table_of(panel)
    assert table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    assert table.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert table.horizontalHeader().stretchLastSection()


def test_loading_text_is_shown_before_the_first_result(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal())
    assert status_of(panel) == LOADING_TEXT


@pytest.mark.parametrize("base", [None, "", "https://example.com"])
def test_bad_local_address_never_makes_a_request(qtbot, base):
    local = FakeLocal()
    panel, _hosted = make_panel(qtbot, local=local, local_url=base)
    panel.show()
    settle(qtbot, panel)
    assert local.calls == []


def test_a_read_still_running_when_its_tab_is_deleted_does_not_keep_the_tab(qtbot):
    """A tab deleted while its read still runs is freed on the GUI thread, because the read holds no tab.

    The read used to hold the tab through a closure. When the pool ended the read on a worker, the worker dropped
    the last reference, and the tab was freed off the GUI thread. The collector is off, so only reference counting
    can free the tab here.
    """
    entered = threading.Event()
    gate = threading.Event()

    def projects_held_at_the_gate():
        entered.set()
        gate.wait(5)
        return [(PID, "Alpha")]

    gc.disable()
    try:
        container = QWidget()
        panel = SessionsPanel(
            lambda: LOCAL,
            lambda: (HOSTED, None),
            projects_held_at_the_gate,
            lambda: "local",
            local_transport=FakeLocal(),
            hosted_transport=FakeHosted(),
            parent=container,
        )
        panel.refresh()
        assert entered.wait(5)  # the read has started and is held at the gate
        ref = weakref.ref(panel)
        shiboken6.delete(container)  # the tab's widgets are deleted on the GUI thread while the read still runs
        del panel
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
    finally:
        gate.set()
        gc.enable()


# ----------------------------------------------------------------- search


def two_sessions():
    return body_of(
        [
            session(name="Auth-Refactor", client_type="claude-code", last_seen=ago(minutes=1)),
            session(name="docs-pass", client_type="codex", last_seen=ago(minutes=1)),
        ]
    )


def names_of(panel):
    return [cells[0] for cells in session_cells(panel)]


def test_search_filters_rows_by_name_and_by_client_ignoring_case(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=two_sessions()))
    panel.show()
    settle(qtbot, panel)
    assert names_of(panel) == ["Auth-Refactor", "docs-pass"]
    panel._search.setText("AUTH")  # noqa: SLF001 - the search box is the test's input
    assert names_of(panel) == ["Auth-Refactor"]
    panel._search.setText("codex")  # noqa: SLF001
    assert names_of(panel) == ["docs-pass"]
    assert '1 of 2 match "codex".' in status_of(panel)


def test_search_reads_nothing_hides_an_empty_group_and_clearing_restores_it(qtbot):
    local = FakeLocal(body=two_sessions())
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    reads = len(local.calls)
    panel._search.setText("nothing-matches")  # noqa: SLF001
    assert len(local.calls) == reads
    assert session_cells(panel) == []
    assert header_texts(panel) == []  # the only group has no match, so its header goes too
    assert '0 of 2 match "nothing-matches".' in status_of(panel)
    panel._search.setText("")  # noqa: SLF001
    assert names_of(panel) == ["Auth-Refactor", "docs-pass"]
    assert header_texts(panel) == [header_text("Alpha", "2 session(s)")]
    assert "match" not in status_of(panel)


# ----------------------------------------------------------------- subprojects and groups

PID3 = "22222222-3333-4444-8555-666666666666"
PID4 = "33333333-4444-4555-8666-777777777777"
# Alpha, with Core under it and Parser under Core. Beta is a separate top-level project.
TREE = ((PID, "Alpha", ""), (PID2, "Core", PID), (PID3, "Parser", PID2), (PID4, "Beta", ""))
ALPHA_CORE = "Alpha › Core"
ALPHA_CORE_PARSER = "Alpha › Core › Parser"


class RoutedLocal:
    """A local transport that answers each project from its own body or status, and records the project ids asked."""

    def __init__(self, bodies, statuses=None):
        self.bodies = bodies
        self.statuses = statuses or {}
        self.asked = []

    def __call__(self, base_url, path, timeout):
        project_id = path.split("/projects/")[1].split("/")[0]
        self.asked.append(project_id)
        return SimpleNamespace(status=self.statuses.get(project_id, 200), body=self.bodies.get(project_id, b"[]"))


def tree_panel(qtbot, statuses=None):
    bodies = {
        PID: body_of([session(name="root-run", last_seen=ago(minutes=1))]),
        PID2: body_of([session(name="core-run", last_seen=ago(minutes=1))]),
        PID3: body_of([session(name="parser-run", last_seen=ago(hours=3))]),
        PID4: body_of([session(name="beta-run", last_seen=ago(minutes=1))]),
    }
    routed = RoutedLocal(bodies, statuses)
    panel, _hosted = make_panel(qtbot, local=routed, projects=TREE)
    panel.show()
    settle(qtbot, panel)
    return panel, routed


def header_row(panel, label):
    """The row of the header whose path label is exactly ``label``."""
    table = table_of(panel)
    for row in range(table.rowCount()):
        if is_header_row(table, row) and table.item(row, 0).text().split("  (")[0][2:] == label:
            return row
    raise AssertionError(f"no header row for {label!r}")


def click_header(panel, label):
    table_of(panel).cellClicked.emit(header_row(panel, label), 0)


def test_subprojects_are_grouped_under_the_picked_project_in_tree_order(qtbot):
    panel, routed = tree_panel(qtbot)
    assert header_texts(panel) == [
        header_text("Alpha", "1 session(s)"),
        header_text(ALPHA_CORE, "1 session(s)"),
        header_text(ALPHA_CORE_PARSER, "1 session(s)"),
    ]
    assert names_of(panel) == ["root-run", "core-run", "parser-run"]
    assert routed.asked[:3] == [PID, PID2, PID3]  # the picked project, then its subprojects in tree order
    assert PID4 not in routed.asked  # Beta is another top-level project, not part of Alpha's tree
    assert "Grouped by project: 3 shown, including subprojects." in status_of(panel)


def test_the_project_picker_covers_subprojects_by_their_path(qtbot):
    panel, _routed = tree_panel(qtbot)
    combo = picker_of(panel)
    assert [combo.itemText(index) for index in range(combo.count())] == [
        "Alpha",
        ALPHA_CORE,
        ALPHA_CORE_PARSER,
        "Beta",
    ]


def test_a_collapsed_group_hides_its_subtree_and_stays_collapsed_across_a_refresh(qtbot):
    panel, _routed = tree_panel(qtbot)
    click_header(panel, ALPHA_CORE)
    assert header_texts(panel) == [
        header_text("Alpha", "1 session(s)"),
        header_text(ALPHA_CORE, "1 session(s)", collapsed=True),
    ]
    assert names_of(panel) == ["root-run"]  # Core's rows and its subproject Parser are hidden with it
    panel.refresh()
    settle(qtbot, panel)
    assert names_of(panel) == ["root-run"]  # still collapsed after a full read
    click_header(panel, ALPHA_CORE)
    assert names_of(panel) == ["root-run", "core-run", "parser-run"]


def test_enter_and_space_on_a_selected_header_collapse_and_expand_it(qtbot):
    panel, _routed = tree_panel(qtbot)
    table = table_of(panel)
    table.setCurrentCell(header_row(panel, ALPHA_CORE), 0)
    qtbot.keyClick(table, Qt.Key.Key_Return)
    assert names_of(panel) == ["root-run"]
    qtbot.keyClick(table, Qt.Key.Key_Space)  # the header is still selected after the redraw
    assert names_of(panel) == ["root-run", "core-run", "parser-run"]


def test_a_subproject_that_cannot_be_read_keeps_its_header_with_the_reason(qtbot):
    panel, _routed = tree_panel(qtbot, statuses={PID2: 404})
    assert header_texts(panel) == [
        header_text("Alpha", "1 session(s)"),
        f"▾ {ALPHA_CORE}  (could not be read)",
        header_text(ALPHA_CORE_PARSER, "1 session(s)"),
    ]
    assert names_of(panel) == ["root-run", "parser-run"]  # the other subprojects still show
    table = table_of(panel)
    assert table.item(header_row(panel, ALPHA_CORE), 0).toolTip() == sc.MISSING_ROUTE_MESSAGE
    assert "1 project(s) could not be read." in status_of(panel)


def test_a_search_keeps_only_the_groups_with_a_match(qtbot):
    panel, _routed = tree_panel(qtbot)
    panel._search.setText("parser")  # noqa: SLF001
    assert header_texts(panel) == [header_text(ALPHA_CORE_PARSER, "1 of 1 session(s)")]
    assert names_of(panel) == ["parser-run"]


def test_the_tree_is_cut_at_the_group_limit_and_says_so(qtbot):
    many = [(PID, "Root", "")] + [
        (f"00000000-0000-4000-8000-{index:012d}", f"C{index:03d}", PID) for index in range(40)
    ]
    entries, total = project_tree(PID, [project(*row) for row in many])
    assert len(entries) == MAX_GROUPS
    assert total == 41
    assert entries[0][0] == PID


# ----------------------------------------------------------------- active, idle and the legend


def test_active_rows_are_green_idle_rows_grey_and_the_legend_says_so(qtbot):
    # Nine minutes is inside the 10-minute window, so Active. Eleven minutes is outside it, so Idle.
    local = FakeLocal(
        body=body_of(
            [
                session(name="nine", last_seen=ago(minutes=9)),
                session(name="eleven", last_seen=ago(minutes=11)),
            ]
        )
    )
    panel, _hosted = make_panel(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    table = table_of(panel)
    states = {cells[0]: cells[1] for cells in session_cells(panel)}
    assert states == {"nine": "Active", "eleven": "Idle"}
    rows = {table.item(row, 0).text(): row for row in range(table.rowCount()) if not is_header_row(table, row)}
    assert table.item(rows["nine"], 1).foreground().color().name() == ACTIVE_COLOR
    assert table.item(rows["eleven"], 1).foreground().color().name() == IDLE_COLOR
    legend_active = panel.findChild(QLabel, "sessions-legend-active")
    legend_idle = panel.findChild(QLabel, "sessions-legend-idle")
    assert "Active" in legend_active.text()
    assert f"{sc.ACTIVE_WINDOW_MINUTES} minutes" in legend_active.text()
    assert "Idle" in legend_idle.text()
    assert ACTIVE_COLOR in legend_active.styleSheet()
    assert IDLE_COLOR in legend_idle.styleSheet()


def test_last_seen_tooltip_says_it_is_the_last_contact_not_the_last_keystroke(qtbot):
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body_of([session(name="one", last_seen=ago(minutes=2))])))
    panel.show()
    settle(qtbot, panel)
    table = table_of(panel)
    row = next(index for index in range(table.rowCount()) if not is_header_row(table, index))
    tip = table.item(row, 2).toolTip()
    assert "contacted the server, through a tool call or keepalive" in tip
    assert "It is not the last keystroke." in tip
    assert "Server status: active" in table.item(row, 1).toolTip()


def test_one_client_is_shown_per_row(qtbot):
    body = body_of([session(name="both", client_type="claude-code", agent_framework="codex", last_seen=ago(minutes=1))])
    panel, _hosted = make_panel(qtbot, local=FakeLocal(body=body))
    panel.show()
    settle(qtbot, panel)
    assert session_cells(panel)[0][3] == "claude-code"


# ----------------------------------------------------------------- one timestamp format, local time


def test_every_timestamp_in_the_tab_is_local_time_in_one_format(qtbot):
    # The server writes last_seen as SQLite UTC text with no offset.
    panel, _hosted = make_panel(
        qtbot, local=FakeLocal(body=body_of([session(name="old", last_seen="2020-01-01 09:30:15")]))
    )
    panel.show()
    settle(qtbot, panel)
    expected_local = sc.format_local(datetime(2020, 1, 1, 9, 30, 15, tzinfo=timezone.utc))
    assert session_cells(panel)[0][2] == expected_local
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", expected_local)
    # Row 0 is the project header, so the one session row is row 1.
    assert expected_local in table_of(panel).item(1, 2).toolTip()
    checked = re.search(r"Checked at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.", status_of(panel))
    assert checked is not None
    # The check time is this computer's local wall clock, not UTC: it is within a couple of minutes of datetime.now().
    checked_at = datetime.strptime(checked.group(1), sc.TIME_FORMAT)
    assert abs((datetime.now() - checked_at).total_seconds()) < 120
