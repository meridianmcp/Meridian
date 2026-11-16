"""Tests for the Sessions tab's two additions: the "Only projects with active sessions" switch and its saved value,
and the agent-program section. The settings are a temporary file. The panel uses offscreen Qt and fake transports.
The process parser and the folder match use fake process data. The one Windows test starts a child Python process it
owns, reads that child's working folder, and stops the child again. Nothing else is started or stopped.
"""

import json
import ntpath
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QTableWidget

from meridian_desktop import agent_processes as ap
from meridian_desktop import sessions_client as sc
from meridian_desktop.agent_processes import AgentListing, AgentProcess, ProcessEntry
from meridian_desktop.restart_check import GONE
from meridian_desktop.settings_store import SettingsStore, SettingsUnreadable
from meridian_desktop.ui.sessions_panel import (
    AGENT_COLUMNS,
    AGENT_PRESS_TEXT,
    ONLY_ACTIVE_NOT_SAVED_TEXT,
    ONLY_ACTIVE_TEXT,
    ProjectGroup,
    SessionsPanel,
    agent_cells,
    agents_summary,
    started_text,
    summary_text,
)

LOCAL = "http://127.0.0.1:7878"
HOSTED = "https://usemeridian.us"
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
PID2 = "11111111-2222-4333-8444-555555555555"
PID3 = "22222222-3333-4444-8555-666666666666"
PID4 = "33333333-4444-4555-8666-777777777777"
# Alpha, with Core under it and Parser under Core. Beta is a separate top-level project and is not in Alpha's tree.
TREE = ((PID, "Alpha", ""), (PID2, "Core", PID), (PID3, "Parser", PID2), (PID4, "Beta", ""))
ALPHA = "Alpha"
ALPHA_CORE = "Alpha › Core"
ALPHA_CORE_PARSER = "Alpha › Core › Parser"
WINDOWS_64 = sys.platform == "win32" and sys.maxsize > 2**32
FOLDERS = {"Meridian": r"C:\Users\me\Documents\Meridian", "Meridian core": r"C:\Users\me\Documents\Meridian\repository"}


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def session(name, last_seen):
    return {
        "id": f"id-{name}",
        "name": name,
        "status": "active",
        "last_seen": last_seen,
        "client_type": "claude-code",
        "agent_framework": "claude_code",
    }


def body_of(rows):
    return json.dumps(rows).encode("utf-8")


# Each project in Alpha's tree answers with one session. Core and Parser are idle, Alpha is active, Parser is active.
# These are functions, not constants: "active" is judged against the clock, so the timestamps must be taken when a test
# runs. A constant built at import goes stale during a long suite run and makes the active sessions look idle.
def mixed():
    return {
        PID: body_of([session("root-run", ago(minutes=1))]),
        PID2: body_of([session("core-idle", ago(hours=3))]),
        PID3: body_of([session("parser-run", ago(minutes=2))]),
        PID4: body_of([session("beta-idle", ago(hours=5))]),
    }


def all_idle():
    return {
        PID: body_of([session("root-idle", ago(hours=2))]),
        PID2: body_of([session("core-idle", ago(hours=3))]),
        PID3: body_of([session("parser-idle", ago(hours=4))]),
    }


class Routed:
    """A local transport that answers each project from its own body or status, and records the project ids asked."""

    def __init__(self, bodies, statuses=None):
        self.bodies = bodies
        self.statuses = statuses or {}
        self.asked = []

    def __call__(self, base_url, path, timeout):
        project_id = path.split("/projects/")[1].split("/")[0]
        self.asked.append(project_id)
        return SimpleNamespace(status=self.statuses.get(project_id, 200), body=self.bodies.get(project_id, b"[]"))


class NoHosted:
    """A hosted transport that records any request. The tests here never expect one."""

    def __init__(self):
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        raise AssertionError("the hosted host must not be read in these tests")


def build(qtbot, *, local, only_active=False, saver=None, lister=None, folders=None):
    panel = SessionsPanel(
        lambda: LOCAL,
        lambda: (HOSTED, None),
        lambda: list(TREE),
        lambda: "local",
        local_transport=local,
        hosted_transport=NoHosted(),
        only_active=only_active,
        on_only_active_changed=saver,
        project_folders=folders,
        agent_lister=lister,
    )
    qtbot.addWidget(panel)
    panel.hide()
    return panel


def settle(qtbot, panel):
    qtbot.waitUntil(lambda: panel._in_flight is None, timeout=5000)  # noqa: SLF001 - test hook


def table_of(panel):
    return panel.findChild(QTableWidget, "sessions-table")


def header_labels(panel):
    """The path label of each project header row, in table order, without its arrow and session count."""
    table = table_of(panel)
    labels = []
    for row in range(table.rowCount()):
        if table.columnSpan(row, 0) > 1:
            labels.append(table.item(row, 0).text()[2:].split("  (")[0])
    return labels


def header_texts(panel):
    table = table_of(panel)
    return [table.item(row, 0).text() for row in range(table.rowCount()) if table.columnSpan(row, 0) > 1]


def session_names(panel):
    table = table_of(panel)
    return [table.item(row, 0).text() for row in range(table.rowCount()) if table.columnSpan(row, 0) == 1]


def status_of(panel):
    return panel.findChild(QLabel, "sessions-status").text()


def switch_of(panel):
    return panel.findChild(QCheckBox, "sessions-only-active")


def visible_text(panel):
    parts = [label.text() for label in panel.findChildren(QLabel)]
    table = table_of(panel)
    for row in range(table.rowCount()):
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                parts.append(item.text())
    return "\n".join(parts)


# ----------------------------------------------------------------- the saved switch


def test_the_switch_is_off_by_default_and_writes_nothing_by_default(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.load().settings.sessions_only_active_projects is False
    store.set_sessions_only_active(False)
    assert "sessions_only_active_projects" not in json.loads(path.read_text(encoding="utf-8"))


def test_the_switch_round_trips_through_the_settings_file(tmp_path):
    path = tmp_path / "settings.json"
    saved = SettingsStore(path).set_sessions_only_active(True)
    assert saved.sessions_only_active_projects is True
    assert json.loads(path.read_text(encoding="utf-8"))["sessions_only_active_projects"] is True
    assert SettingsStore(path).load().settings.sessions_only_active_projects is True
    assert SettingsStore(path).set_sessions_only_active(False).sessions_only_active_projects is False


def test_a_switch_value_that_is_not_true_or_false_reads_as_off_with_a_warning(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"sessions_only_active_projects": "yes"}), encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.settings.sessions_only_active_projects is False
    assert "sessions_only_active_projects must be true or false" in (result.warning or "")


def test_saving_the_switch_keeps_every_other_field(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mode": "hosted", "local_url": "http://127.0.0.1:9999"}), encoding="utf-8")
    SettingsStore(path).set_sessions_only_active(True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mode"] == "hosted"
    assert data["local_url"] == "http://127.0.0.1:9999"
    assert data["sessions_only_active_projects"] is True


def test_an_unreadable_settings_file_is_copied_aside_and_never_rebuilt(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")  # a JSON list, not an object: nothing in it can be trusted
    with pytest.raises(SettingsUnreadable):
        SettingsStore(path).set_sessions_only_active(True)
    assert path.read_text(encoding="utf-8") == "[1, 2, 3]"
    assert (tmp_path / "settings.json.unreadable.bak").is_file()


# ----------------------------------------------------------------- the switch: the status line and the panel


def test_the_summary_says_how_many_projects_the_switch_hid():
    rows = (sc.SessionRow(name="a", status="idle", last_seen="", client=""),)
    root = sc.SessionsResult(sc.SESSIONS, "", sc.SOURCE_LOCAL, rows)
    groups = (ProjectGroup(PID, ALPHA, 0, root),)
    text = summary_text(root, groups, 1, "", 1, "12:00:00", inactive_hidden=2)
    assert text == (
        "1 session(s) from this computer's Meridian server. "
        "2 project(s) hidden: no active sessions. "
        "Checked at 12:00:00."
    )


def test_the_switch_is_off_on_build_and_building_or_showing_saves_nothing(qtbot):
    saves = []
    panel = build(qtbot, local=Routed(mixed()), saver=lambda value: saves.append(value) or True)
    panel.show()
    settle(qtbot, panel)
    assert switch_of(panel).text() == ONLY_ACTIVE_TEXT
    assert not switch_of(panel).isChecked()
    assert saves == []
    assert header_labels(panel) == [ALPHA, ALPHA_CORE, ALPHA_CORE_PARSER]


def test_switched_on_it_hides_idle_projects_and_switched_off_it_shows_them_again(qtbot):
    local = Routed(mixed())
    panel = build(qtbot, local=local)
    panel.show()
    settle(qtbot, panel)
    asked = len(local.asked)
    switch_of(panel).setChecked(True)
    assert header_labels(panel) == [ALPHA, ALPHA_CORE_PARSER]
    assert session_names(panel) == ["root-run", "parser-run"]
    assert "1 project(s) hidden: no active sessions." in status_of(panel)
    switch_of(panel).setChecked(False)
    assert header_labels(panel) == [ALPHA, ALPHA_CORE, ALPHA_CORE_PARSER]
    assert session_names(panel) == ["root-run", "core-idle", "parser-run"]
    assert len(local.asked) == asked  # the switch reads nothing


def test_the_switch_starts_on_when_the_window_says_so_and_hides_at_once(qtbot):
    saves = []
    panel = build(qtbot, local=Routed(mixed()), only_active=True, saver=lambda value: saves.append(value) or True)
    panel.show()
    settle(qtbot, panel)
    assert switch_of(panel).isChecked()
    assert header_labels(panel) == [ALPHA, ALPHA_CORE_PARSER]
    assert saves == []


def test_each_change_of_the_switch_goes_through_the_window_callback(qtbot):
    saves = []
    panel = build(qtbot, local=Routed(mixed()), saver=lambda value: saves.append(value) or True)
    panel.show()
    settle(qtbot, panel)
    switch_of(panel).setChecked(True)
    switch_of(panel).setChecked(False)
    assert saves == [True, False]
    assert ONLY_ACTIVE_NOT_SAVED_TEXT not in status_of(panel)


def test_a_switch_that_cannot_be_saved_still_filters_and_says_it_holds_for_this_session(qtbot):
    panel = build(qtbot, local=Routed(mixed()), saver=lambda value: False)
    panel.show()
    settle(qtbot, panel)
    switch_of(panel).setChecked(True)
    assert header_labels(panel) == [ALPHA, ALPHA_CORE_PARSER]
    assert ONLY_ACTIVE_NOT_SAVED_TEXT in status_of(panel)


def test_a_save_that_raises_counts_as_not_saved_and_its_words_stay_out_of_the_window(qtbot):
    def broken(_value):
        raise OSError("disk full at C:\\secret\\settings.json")

    panel = build(qtbot, local=Routed(mixed()), saver=broken)
    panel.show()
    settle(qtbot, panel)
    switch_of(panel).setChecked(True)
    assert ONLY_ACTIVE_NOT_SAVED_TEXT in status_of(panel)
    assert "secret" not in visible_text(panel)
    assert "OSError" not in visible_text(panel)


def test_a_project_that_could_not_be_read_stays_under_the_switch_with_its_reason(qtbot):
    panel = build(qtbot, local=Routed(mixed(), statuses={PID2: 404}))
    panel.show()
    settle(qtbot, panel)
    switch_of(panel).setChecked(True)
    assert f"▾ {ALPHA_CORE}  (could not be read)" in header_texts(panel)
    assert header_labels(panel) == [ALPHA, ALPHA_CORE, ALPHA_CORE_PARSER]


def test_with_no_active_session_anywhere_the_table_is_empty_and_says_how_many_are_hidden(qtbot):
    panel = build(qtbot, local=Routed(all_idle()), only_active=True)
    panel.show()
    settle(qtbot, panel)
    assert header_labels(panel) == []
    assert session_names(panel) == []
    assert "3 project(s) hidden: no active sessions." in status_of(panel)


# ----------------------------------------------------------------- the agent section: pure helpers


def test_only_claude_and_codex_programs_are_agents_whatever_the_case_or_the_suffix():
    entries = [
        ProcessEntry(10, 1, "Claude.exe"),
        ProcessEntry(11, 1, "claude"),
        ProcessEntry(12, 1, "CODEX.EXE"),
        ProcessEntry(13, 1, "node.exe"),
        ProcessEntry(14, 1, "claude-helper.exe"),
        ProcessEntry(15, 1, "codex.cmd"),
    ]
    assert [(entry.pid, label) for entry, label in ap.find_agents(entries)] == [
        (10, "Claude"),
        (11, "Claude"),
        (12, "Codex"),
    ]


def test_helper_processes_of_the_same_program_are_listed_too_so_no_process_is_hidden():
    # A Claude Code run started inside Claude Desktop is a child of the same program name, so it must not be dropped.
    entries = [
        ProcessEntry(20, 1, "Claude.exe"),
        ProcessEntry(21, 20, "claude.exe"),
        ProcessEntry(22, 20, "claude.exe"),
    ]
    assert [entry.pid for entry, _label in ap.find_agents(entries)] == [20, 21, 22]


def test_the_agents_come_in_process_id_order_whatever_the_snapshot_order():
    entries = [ProcessEntry(30, 1, "codex.exe"), ProcessEntry(5, 1, "claude.exe")]
    assert [entry.pid for entry, _label in ap.find_agents(entries)] == [5, 30]


def test_a_working_folder_inside_a_known_folder_names_its_project_and_the_longest_folder_wins():
    assert ap.project_for_folder(r"C:\Users\me\Documents\Meridian\repository\worktrees\x", FOLDERS) == "Meridian core"
    assert ap.project_for_folder(r"C:\Users\me\Documents\Meridian\docs", FOLDERS) == "Meridian"


def test_the_folder_match_ignores_case_and_slash_direction_but_not_a_shared_name_prefix():
    assert ap.project_for_folder("c:/users/ME/documents/meridian/docs/", FOLDERS) == "Meridian"
    assert ap.project_for_folder(r"C:\Users\me\Documents\Meridian2", FOLDERS) == ""


def test_no_known_folder_or_no_working_folder_means_no_project():
    assert ap.project_for_folder(r"C:\Users\me\Documents\Meridian\docs", {}) == ""
    assert ap.project_for_folder("", FOLDERS) == ""
    assert ap.project_for_folder(r"D:\elsewhere", FOLDERS) == ""


def test_a_start_time_is_shown_in_the_one_local_format_and_an_unknown_one_says_unknown():
    started_ns = 1_700_000_000_000_000_000
    expected = sc.format_local(datetime.fromtimestamp(started_ns / 1e9, tz=timezone.utc))
    assert started_text(started_ns) == expected
    assert started_text(None) == sc.UNKNOWN_TIME_TEXT


def test_an_agent_row_names_empty_folder_and_project_in_plain_words():
    agent = AgentProcess(program="Codex", pid=3, started_ns=None, folder="", project="")
    assert agent_cells(agent) == ("Codex", "3", sc.UNKNOWN_TIME_TEXT, ap.NOT_READ_TEXT, ap.NO_MATCH_TEXT)


def test_the_agent_summary_counts_programs_and_names_the_missing_folders():
    listing = AgentListing(ap.STATE_OK, "", (AgentProcess("Claude", 1, None, "", ""),))
    assert agents_summary(listing, "12:00:00", False) == (
        "1 agent program(s) running on this computer. "
        "No project folder is saved yet, so no program is matched to a project. "
        "Checked at 12:00:00."
    )
    assert agents_summary(AgentListing(ap.STATE_OK), "12:00:00", True) == (
        "No claude or codex program is running on this computer. Checked at 12:00:00."
    )


# ----------------------------------------------------------------- the agent section: the listing with fake data


def fake_snapshot(entries):
    return lambda: list(entries)


def test_the_listing_gives_each_agent_its_start_time_working_folder_and_project():
    entries = [ProcessEntry(7, 1, "claude.exe"), ProcessEntry(8, 1, "node.exe"), ProcessEntry(9, 1, "codex.exe")]
    listing = ap.list_agent_programs(
        {"Meridian": r"C:\work\meridian"},
        supported=True,
        snapshot=fake_snapshot(entries),
        folder_of=lambda pid: {7: r"C:\work\meridian\app"}.get(pid, ""),
        started_of=lambda pid: {7: 1_700_000_000_000_000_000}.get(pid),
    )
    assert listing.state == ap.STATE_OK
    assert listing.agents == (
        AgentProcess("Claude", 7, 1_700_000_000_000_000_000, r"C:\work\meridian\app", "Meridian"),
        AgentProcess("Codex", 9, None, "", ""),
    )


def test_a_process_that_exited_after_the_snapshot_is_left_out():
    listing = ap.list_agent_programs(
        {},
        supported=True,
        snapshot=fake_snapshot([ProcessEntry(7, 1, "claude.exe"), ProcessEntry(8, 1, "claude.exe")]),
        folder_of=lambda pid: "",
        started_of=lambda pid: GONE if pid == 7 else 1000,
    )
    assert [agent.pid for agent in listing.agents] == [8]


def test_off_windows_the_listing_says_so_and_reads_no_process():
    calls = []

    def snapshot():
        calls.append("snapshot")
        return ()

    listing = ap.list_agent_programs({}, supported=False, snapshot=snapshot)
    assert listing.state == ap.STATE_UNSUPPORTED
    assert listing.message == ap.UNSUPPORTED_TEXT
    assert calls == []


def test_a_failed_snapshot_says_so_in_fixed_words_and_keeps_the_exception_text_out():
    def broken():
        raise OSError("C:\\secret\\path token=abc")

    listing = ap.list_agent_programs({}, supported=True, snapshot=broken)
    assert listing.state == ap.STATE_ERROR
    assert listing.message == ap.READ_FAILED_TEXT
    assert "secret" not in listing.message
    assert listing.agents == ()


def test_a_reader_that_raises_leaves_its_cell_empty_and_keeps_the_row():
    def boom(_pid):
        raise RuntimeError("nope")

    listing = ap.list_agent_programs(
        {"P": r"C:\x"},
        supported=True,
        snapshot=fake_snapshot([ProcessEntry(3, 1, "codex.exe")]),
        folder_of=boom,
        started_of=boom,
    )
    assert listing.agents == (AgentProcess("Codex", 3, None, "", ""),)


def test_an_unreadable_pid_gives_no_folder():
    assert ap.read_working_folder(0) == ""
    assert ap.read_working_folder(-5) == ""


@pytest.mark.skipif(not WINDOWS_64, reason="reads a 64-bit process on 64-bit Windows")
def test_the_working_folder_and_the_snapshot_of_a_real_child_process(tmp_path):
    folder = tmp_path / "child-folder"
    folder.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=str(folder),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        seen = ""
        deadline = time.monotonic() + 15
        while not seen and time.monotonic() < deadline:
            seen = ap.read_working_folder(child.pid)
            if not seen:
                time.sleep(0.1)
        assert ntpath.normcase(seen) == ntpath.normcase(str(folder))
        assert any(entry.pid == child.pid for entry in ap.snapshot_processes())
    finally:
        child.kill()  # the test's own child, and nothing else
        child.wait(timeout=15)


# ----------------------------------------------------------------- the agent section: the panel


def agent_panel(qtbot, lister, folders=None):
    return build(qtbot, local=Routed(mixed()), lister=lister, folders=folders)


def test_the_agent_list_is_read_only_when_refresh_is_pressed_and_never_on_show(qtbot):
    calls = []

    def lister(folders):
        calls.append((threading.current_thread(), dict(folders)))
        return AgentListing(
            ap.STATE_OK, "", (AgentProcess("Claude", 42, 1_700_000_000_000_000_000, r"C:\work\app", "Meridian"),)
        )

    panel = agent_panel(qtbot, lister, folders={"Meridian": r"C:\work"})
    panel.show()
    settle(qtbot, panel)
    assert calls == []
    assert panel.findChild(QLabel, "sessions-agents-status").text() == AGENT_PRESS_TEXT
    button = panel.findChild(QPushButton, "sessions-agents-refresh")
    button.click()
    qtbot.waitUntil(lambda: len(calls) == 1 and panel._agent_in_flight is None, timeout=5000)  # noqa: SLF001
    assert calls[0][0] is not threading.main_thread()
    assert calls[0][1] == {"Meridian": r"C:\work"}
    table = panel.findChild(QTableWidget, "sessions-agents-table")
    assert [table.horizontalHeaderItem(index).text() for index in range(table.columnCount())] == list(AGENT_COLUMNS)
    assert [table.item(0, column).text() for column in range(table.columnCount())] == [
        "Claude",
        "42",
        started_text(1_700_000_000_000_000_000),
        r"C:\work\app",
        "Meridian",
    ]
    assert "1 agent program(s) running on this computer." in panel.findChild(QLabel, "sessions-agents-status").text()
    assert button.isEnabled()


def test_a_second_press_while_a_listing_runs_starts_nothing_new(qtbot):
    gate = threading.Event()
    calls = []

    def lister(_folders):
        calls.append(1)
        gate.wait(5)
        return AgentListing(ap.STATE_OK, "", ())

    panel = agent_panel(qtbot, lister)
    panel.show()
    settle(qtbot, panel)
    panel.refresh_agents()
    panel.refresh_agents()
    qtbot.waitUntil(lambda: len(calls) == 1, timeout=5000)
    assert not panel.findChild(QPushButton, "sessions-agents-refresh").isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: panel._agent_in_flight is None, timeout=5000)  # noqa: SLF001
    assert len(calls) == 1
    assert panel.findChild(QPushButton, "sessions-agents-refresh").isEnabled()


def _unsupported(_folders):
    return AgentListing(ap.STATE_UNSUPPORTED, ap.UNSUPPORTED_TEXT)


def _raises(_folders):
    raise RuntimeError("boom with C:\\secret")


@pytest.mark.parametrize(("lister", "expected"), [(_unsupported, ap.UNSUPPORTED_TEXT), (_raises, ap.READ_FAILED_TEXT)])
def test_a_listing_that_fails_shows_its_fixed_words_and_no_rows(qtbot, lister, expected):
    panel = agent_panel(qtbot, lister)
    panel.show()
    settle(qtbot, panel)
    panel.refresh_agents()
    qtbot.waitUntil(lambda: panel._agent_in_flight is None, timeout=5000)  # noqa: SLF001
    assert panel.findChild(QLabel, "sessions-agents-status").text() == expected
    assert panel.findChild(QTableWidget, "sessions-agents-table").rowCount() == 0
    assert "secret" not in visible_text(panel)


def test_with_no_project_folder_saved_the_agent_status_says_so(qtbot):
    panel = agent_panel(qtbot, lambda _folders: AgentListing(ap.STATE_OK, "", ()))
    panel.show()
    settle(qtbot, panel)
    panel.refresh_agents()
    qtbot.waitUntil(lambda: panel._agent_in_flight is None, timeout=5000)  # noqa: SLF001
    status = panel.findChild(QLabel, "sessions-agents-status").text()
    assert "No claude or codex program is running on this computer." in status
    assert "No project folder is saved yet" in status
