"""The pure rules behind the Sessions tab: the active window, the one timestamp format, the project tree and its labels,
the search, and the summary line. No Qt here. test_sessions_panel.py covers what the window shows.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from meridian_desktop import sessions_client as sc
from meridian_desktop.sessions_client import SessionRow
from meridian_desktop.ui.sessions_panel import (
    LAST_SEEN_MEANING,
    MAX_GROUPS,
    Project,
    ProjectGroup,
    choose_project,
    last_seen_tip,
    picker_choices,
    project_label,
    project_tree,
    row_cells,
    row_matches,
    status_tip,
    summary_text,
)

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
LOCAL = "http://127.0.0.1:7878"
PID = "5787cc92-ba7d-4788-b17c-28ab7938b839"
PID2 = "11111111-2222-4333-8444-555555555555"


def project(project_id, name, parent=""):
    return Project(project_id=project_id, name=name, parent_id=parent)


# ----------------------------------------------------------------- active or idle


def test_the_window_is_ten_minutes_as_the_server_sets_it():
    assert sc.ACTIVE_WINDOW_MINUTES == 10


@pytest.mark.parametrize(
    ("seconds_ago", "expected"),
    [
        (9 * 60, sc.ACTIVE),  # nine minutes: inside the window
        (10 * 60 - 1, sc.ACTIVE),  # one second short of the window
        (10 * 60, sc.IDLE),  # exactly ten minutes: the server's check is strict, so this is idle
        (11 * 60, sc.IDLE),  # eleven minutes: outside the window
        (-30, sc.ACTIVE),  # a clock running behind the server still counts as just seen
    ],
)
def test_a_session_is_active_only_while_last_seen_is_inside_the_ten_minute_window(seconds_ago, expected):
    seen = (NOW - timedelta(seconds=seconds_ago)).isoformat()
    assert sc.session_state(seen, NOW) == expected


def test_the_server_sqlite_time_is_read_as_utc_for_the_window():
    # The server writes last_seen with SQLite datetime('now'): UTC text with no offset.
    assert sc.session_state("2026-11-02 11:51:00", NOW) == sc.ACTIVE  # nine minutes
    assert sc.session_state("2026-11-02 11:49:00", NOW) == sc.IDLE  # eleven minutes


def test_a_last_seen_that_cannot_be_read_is_idle():
    assert sc.session_state("", NOW) == sc.IDLE
    assert sc.session_state("not a time", NOW) == sc.IDLE


# ----------------------------------------------------------------- one timestamp format, local time


def test_the_one_timestamp_format_is_local_24_hour_with_seconds():
    when = datetime(2026, 1, 15, 14, 5, 9, tzinfo=timezone.utc)
    text = sc.format_local(when)
    assert sc.TIME_FORMAT == "%Y-%m-%d %H:%M:%S"
    assert text == when.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)


def test_a_naive_server_time_is_utc_and_is_shown_in_local_time():
    expected = sc.format_local(datetime(2026, 1, 15, 14, 5, 9, tzinfo=timezone.utc))
    assert sc.local_time_text("2026-01-15 14:05:09") == expected


def test_a_naive_check_time_is_read_as_utc_too():
    naive = datetime(2026, 1, 15, 14, 5, 9)
    assert sc.format_local(naive) == sc.format_local(naive.replace(tzinfo=timezone.utc))


def test_an_unreadable_last_seen_shows_unknown_not_a_guess():
    assert sc.local_time_text("") == sc.UNKNOWN_TIME_TEXT == "Unknown"
    assert sc.local_time_text("tomorrow-ish") == "Unknown"


# ----------------------------------------------------------------- one client per row


def test_a_row_names_one_client_the_client_type_first_then_the_framework():
    body = json.dumps(
        [
            {"name": "a", "status": "active", "last_seen": "2026-11-02 11:59:00",
             "client_type": "claude-code", "agent_framework": "codex"},
            {"name": "b", "status": "idle", "last_seen": "2026-11-02 11:00:00",
             "client_type": "", "agent_framework": "codex"},
        ]
    ).encode("utf-8")
    result = sc.local_sessions(LOCAL, PID, transport=lambda base, path, timeout: SimpleNamespace(status=200, body=body))
    assert result.state == sc.SESSIONS
    assert [row.client for row in result.rows] == ["claude-code", "codex"]


def test_row_cells_for_an_unreadable_last_seen_is_idle_with_unknown_time():
    row = SessionRow(name="", status="active", last_seen="", client="codex")
    assert row_cells(row, NOW) == ("(unnamed)", "Idle", "Unknown", "codex")


def test_row_cells_for_a_session_inside_the_window_is_active_with_local_time():
    seen = "2026-11-02 11:55:00"
    row = SessionRow(name="s", status="idle", last_seen=seen, client="codex")
    expected_seen = sc.format_local(datetime(2026, 11, 2, 11, 55, tzinfo=timezone.utc))
    assert row_cells(row, NOW) == ("s", "Active", expected_seen, "codex")


# ----------------------------------------------------------------- tooltips


def test_the_last_seen_tooltip_gives_the_meaning_the_local_time_and_the_age():
    seen = (NOW - timedelta(minutes=2)).isoformat()
    tip = last_seen_tip(SessionRow(name="s", status="active", last_seen=seen, client=""), NOW)
    assert tip.startswith(LAST_SEEN_MEANING)
    assert "2 min ago" in tip
    assert sc.local_time_text(seen) in tip


def test_the_last_seen_tooltip_says_when_no_time_was_sent():
    tip = last_seen_tip(SessionRow(name="s", status="active", last_seen="", client=""), NOW)
    assert "No last-seen time was sent." in tip


def test_the_status_tooltip_names_the_server_status_and_the_rule():
    tip = status_tip(SessionRow(name="s", status="idle", last_seen="", client=""))
    assert "Server status: idle." in tip
    assert "10 minutes" in tip
    assert "Server status: not sent." in status_tip(SessionRow(name="s", status="", last_seen="", client=""))


# ----------------------------------------------------------------- search


def test_row_matches_by_name_or_client_ignoring_case_and_outer_spaces():
    row = SessionRow(name="Auth Refactor", status="active", last_seen="", client="Claude-Code")
    assert row_matches(row, "")
    assert row_matches(row, "   ")
    assert row_matches(row, "  auth ")
    assert row_matches(row, "CLAUDE-code")
    assert not row_matches(row, "codex")


def test_the_search_does_not_match_the_status_or_the_last_seen_text():
    row = SessionRow(name="Auth Refactor", status="active", last_seen="2026-11-02 11:55:00", client="codex")
    assert not row_matches(row, "active")
    assert not row_matches(row, "2026")


# ----------------------------------------------------------------- projects, subprojects and labels


def test_a_project_label_is_its_path_from_the_top_project():
    names = {p.project_id: p for p in (project("a", "Top"), project("b", "Mid", "a"), project("d", "Grand", "b"))}
    assert project_label("d", names) == "Top › Mid › Grand"
    assert project_label("a", names) == "Top"


def test_a_project_not_in_the_list_is_named_by_its_id_and_a_missing_parent_is_top_level():
    names = {"x": project("x", "Orphan", "missing-parent")}
    assert project_label("x", names) == "Orphan"
    assert project_label("nowhere", names) == "nowhere"


def test_the_picker_covers_every_project_including_subprojects_sorted_by_path():
    projects = [project("z", "Zeta"), project(PID2, "Core", PID), project(PID, "Alpha")]
    assert picker_choices(projects) == ((PID, "Alpha"), (PID2, "Alpha › Core"), ("z", "Zeta"))


def test_the_tree_lists_the_picked_project_then_its_children_by_name_depth_first():
    projects = [project("c", "Zeta", "a"), project("b", "Mid", "a"), project("a", "Top"), project("d", "Grand", "b")]
    entries, total = project_tree("a", projects)
    assert entries == (
        ("a", "Top", 0),
        ("b", "Top › Mid", 1),
        ("d", "Top › Mid › Grand", 2),
        ("c", "Top › Zeta", 1),
    )
    assert total == 4


def test_a_parent_loop_is_cut_and_each_project_appears_once():
    entries, total = project_tree("a", [project("a", "A", "b"), project("b", "B", "a")])
    assert [pid for pid, _label, _depth in entries] == ["a", "b"]
    assert total == 2


def test_the_tree_is_cut_at_the_group_limit_and_counts_every_project():
    many = [project(PID, "Root")] + [
        project(f"00000000-0000-4000-8000-{index:012d}", f"C{index:03d}", PID) for index in range(40)
    ]
    entries, total = project_tree(PID, many)
    assert len(entries) == MAX_GROUPS
    assert total == 41
    assert entries[0][0] == PID


def test_choosing_a_project_keeps_a_listed_one_and_otherwise_takes_the_first():
    choices = ((PID, "Alpha"), (PID2, "Beta"))
    assert choose_project(PID2, choices) == PID2
    assert choose_project("gone", choices) == PID


# ----------------------------------------------------------------- the summary line


def test_the_summary_counts_groups_names_unread_projects_and_matches():
    rows = (SessionRow(name="a", status="active", last_seen="", client="x"),)
    root = sc.SessionsResult(sc.SESSIONS, "", sc.SOURCE_LOCAL, rows)
    failed = sc.SessionsResult(sc.ERROR, "Broken.", sc.SOURCE_LOCAL)
    groups = (ProjectGroup(PID, "Alpha", 0, root), ProjectGroup(PID2, "Alpha › Core", 1, failed))
    text = summary_text(root, groups, 3, "a", 1, "2026-11-02 12:00:00")
    assert text == (
        "1 session(s) from this computer's Meridian server. "
        "Grouped by project: 2 shown, including subprojects. "
        "Only the first 2 of 3 projects in this tree are read. "
        "1 project(s) could not be read. "
        '1 of 1 match "a". '
        "Checked at 2026-11-02 12:00:00."
    )


def test_the_summary_for_an_unanswered_root_is_its_message():
    root = sc.SessionsResult(sc.NOT_RUNNING, sc.NOT_RUNNING_MESSAGE, sc.SOURCE_LOCAL)
    assert summary_text(root, (), 0, "", 0, "2026-11-02 12:00:00") == (
        f"{sc.NOT_RUNNING_MESSAGE} Checked at 2026-11-02 12:00:00."
    )
