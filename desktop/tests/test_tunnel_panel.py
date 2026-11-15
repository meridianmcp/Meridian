"""Tests for the Tunnel panel: meridian_desktop/tunnel_panel_model.py (the words and rows) and ui/tunnel_panel.py.

Every outside effect is a fake. The status call is a fake runner reply, the Start and Stop runner is a fake spawn, and
the confirmation dialog is a fake that answers in order. The settings file is a temp file. No process starts, no
socket opens, and no dialog opens. The widget tests run under pytest-qt on the offscreen platform.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
from PySide6.QtWidgets import QCheckBox, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget, QWidget

from meridian_desktop import log_tail, slot_ports
from meridian_desktop import tunnel_panel_model as model
from meridian_desktop.log_tail import LogTail
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.runner_controls import RunnerControl
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tunnel_status import NOT_RUNNING, RUNNING, UNKNOWN, TunnelStatus
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.scope_controls import TUNNEL_OFF_TEXT
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui import tunnel_panel
from meridian_desktop.ui.tunnel_check import TunnelReadJob
from meridian_desktop.ui.tunnel_panel import PROBLEM_TEXT, TunnelPanel

PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"
STAMP = dt.datetime(2026, 10, 22, 17, 0, 0, tzinfo=dt.timezone.utc)
STAMP_TEXT = "2026-10-22 17:00:00"
CONNECTOR = {
    "name": "notes", "kind": "copy", "transport": "stdio", "enabled": True, "tunnel_exposed": False,
    "command": "notes-cli", "args": ["--serve"],
}


# ------------------------------------------------------------------ fakes


class Spawn:
    """A fake for the runner's spawn. It records each call and answers with a benign status reply."""

    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        body = json.dumps({"scope": "tunnel", "generated_at": 1.0, "child": {"state": "stopped", "pid": None}})
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=body.encode("utf-8"), stderr=b"")


class Confirm:
    """A fake for the confirmation dialog. It answers in order and records each question."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, parent, title, body, confirm_text):
        self.asked.append((title, body, confirm_text))
        return self._answers.pop(0) if self._answers else False


def runner_reply(state: str):
    """A status call that answers with the given child state for the tunnel scope, as the runner does."""

    def call(scope: str) -> RunnerResult:
        payload = {"scope": scope, "generated_at": 1.0, "child": {"state": state, "pid": 808}}
        return RunnerResult(verb="status", ok=True, payload=payload)

    return call


def raising_call(scope: str) -> RunnerResult:
    raise RuntimeError("the runner could not be reached")


def _status(state: str, reason: str = "The runner reports the tunnel process as running.") -> TunnelStatus:
    return TunnelStatus(state=state, reason=reason, checked_at=STAMP)


def _settings(settings_path, **overrides):
    values = dict(core_python=PY, core_checkout=CWD, tunnel_control_enabled=True)
    values.update(overrides)
    SettingsStore(settings_path).save(Settings(**values))


def _actions(settings_path, spawn, confirm, tmp_path):
    return RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python, cwd: RunnerControl(
            python, cwd, spawn=spawn, log_path=tmp_path / "runner-control.log", clock=lambda: STAMP,
        ),
        confirm=confirm,
    )


def _panel(qtbot, settings_path, tmp_path, status_call=None, spawn=None, confirm=None, log_path=None):
    spawn = spawn if spawn is not None else Spawn()
    confirm = confirm if confirm is not None else Confirm()
    actions = _actions(settings_path, spawn, confirm, tmp_path)
    panel = TunnelPanel(
        SettingsStore(settings_path),
        actions,
        status_call if status_call is not None else runner_reply("running"),
        log_path=log_path if log_path is not None else tmp_path / "tunnel.log",  # a temp file, never the real log
    )
    qtbot.addWidget(panel)
    return panel, spawn, confirm


def _label(panel, name) -> QLabel:
    return panel.findChild(QLabel, name)


def _box(panel, name) -> QCheckBox:
    return panel.findChild(QCheckBox, name)


def _button(panel, name) -> QPushButton:
    return panel.findChild(QPushButton, name)


def _check_and_wait(qtbot, panel, until_text: str, name: str = "tunnelpanel-headline") -> None:
    panel.check()
    qtbot.waitUntil(lambda: _label(panel, name).text() == until_text, timeout=5000)


# ------------------------------------------------------------------ the status words (no Qt)


def test_a_running_tunnel_reads_process_running_with_its_check_time():
    shown = model.panel_status(_status(RUNNING))
    assert shown.headline == model.RUNNING_HEADLINE == "Tunnel process running"
    assert shown.checked == f"Checked at {STAMP_TEXT}."
    assert shown.detail == "The runner reports the tunnel process as running."


def test_a_stopped_tunnel_reads_not_connected_and_says_how_to_start_it():
    shown = model.panel_status(_status(NOT_RUNNING, "The runner reports the tunnel process as stopped."))
    assert shown.headline == "Not connected"
    assert shown.checked == f"Checked at {STAMP_TEXT}."
    assert shown.detail.startswith("The runner reports the tunnel process as stopped.")
    assert model.PANEL_START_HINT in shown.detail


def test_an_unknown_or_failed_check_never_reads_as_running():
    unknown = model.panel_status(_status(UNKNOWN, "The runner could not verify whether the tunnel process is running."))
    assert unknown.headline == model.UNKNOWN_HEADLINE
    assert unknown.headline != model.RUNNING_HEADLINE

    failed = model.panel_status(None, "RuntimeError")
    assert failed.headline == model.UNKNOWN_HEADLINE
    assert failed.checked == "The check did not finish, so no time was recorded."
    assert "RuntimeError" in failed.detail
    assert failed.headline != model.RUNNING_HEADLINE


def test_the_tunnel_lane_is_live_only_while_the_tunnel_runs():
    _direct, tunnel = model.lanes(RUNNING)
    assert tunnel.live == model.TUNNEL_LANE_LIVE
    _direct, tunnel = model.lanes(NOT_RUNNING)
    assert tunnel.live == model.TUNNEL_LANE_NOT_LIVE
    _direct, tunnel = model.lanes(UNKNOWN)
    assert tunnel.live == model.TUNNEL_LANE_UNKNOWN
    _direct, tunnel = model.lanes(None)
    assert tunnel.live == model.TUNNEL_LANE_UNKNOWN


def test_the_direct_lane_is_never_shown_as_live_by_this_panel():
    for state in (RUNNING, NOT_RUNNING, UNKNOWN, None):
        direct, _tunnel = model.lanes(state)
        assert direct.live == model.DIRECT_LANE_LIVE
        assert "Live" not in direct.live


def test_each_lane_says_what_it_needs():
    direct, tunnel = model.lanes(RUNNING)
    assert direct.name == "Direct lane"
    assert "does not need this desktop or the tunnel" in direct.needs
    assert tunnel.name == "Tunnel lane"
    assert "needs this desktop or the tunnel running" in tunnel.needs


def test_the_tunnel_panel_holds_no_tool_group_switches(qtbot, settings_path, tmp_path):
    # The tool groups are the MCP tools switches in Settings. They are not part of the tunnel panel.
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    names = [box.objectName() for box in panel.findChildren(QCheckBox)]
    assert not [name for name in names if name.startswith("tunnelpanel-group-")]


def test_the_tunnel_opt_in_is_off_by_default():
    assert model.OPT_IN_DEFAULT is False
    assert Settings().custom_tunnel_opt_in is False


def test_a_connector_says_why_claude_ai_cannot_reach_it_through_the_tunnel():
    assert model.connector_text(True, False, opt_in=False) == model.NOT_REACHABLE_OPT_IN_OFF
    assert model.connector_text(True, False, opt_in=True) == model.NOT_REACHABLE_NOT_MARKED
    assert model.connector_text(True, True, opt_in=False) == model.NOT_REACHABLE_OPT_IN_OFF
    assert model.connector_text(True, True, opt_in=True) == model.REACHABLE_TEXT


def test_an_unusable_connector_is_listed_without_a_tunnel_switch():
    rows = model.connector_rows([CONNECTOR, {"name": "bad name!", "kind": "copy"}], opt_in=False)
    assert [row.name for row in rows] == ["notes", "bad name!"]
    assert rows[0].usable is True
    assert rows[1].usable is False
    assert rows[1].tunnel_exposed is False
    assert rows[1].note


# ------------------------------------------------------------------ the widget


def test_status_shows_process_running_when_the_runner_reports_the_tunnel_running(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=runner_reply("running"))
    _check_and_wait(qtbot, panel, model.RUNNING_HEADLINE)
    assert _label(panel, "tunnelpanel-checked").text().startswith("Checked at ")
    assert "Live." in _label(panel, "tunnelpanel-lane-tunnel").text()


def test_status_shows_not_connected_when_the_runner_reports_the_tunnel_stopped(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=runner_reply("stopped"))
    _check_and_wait(qtbot, panel, "Not connected")
    assert model.PANEL_START_HINT in _label(panel, "tunnelpanel-detail").text()
    assert "Not live" in _label(panel, "tunnelpanel-lane-tunnel").text()


def test_a_status_call_that_raises_is_not_shown_as_running(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=raising_call)
    _check_and_wait(qtbot, panel, model.UNKNOWN_HEADLINE)
    assert _label(panel, "tunnelpanel-headline").text() != model.RUNNING_HEADLINE


def test_start_stop_and_restart_are_the_tunnel_controls(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    _settings(settings_path)  # the core is set and the switch is on, so the buttons are on
    panel.refresh()
    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart"):
        assert _button(panel, name).isEnabled() is True, name
        assert _button(panel, name).isHidden() is False, name


def test_the_tunnel_controls_are_the_only_start_and_stop_buttons_in_the_panel(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    controls = [b for b in panel.findChildren(QPushButton) if b.text().split()[:1] in (["Start"], ["Stop"], ["Restart"])]
    assert sorted(b.objectName() for b in controls) == ["tunnel-restart", "tunnel-start", "tunnel-stop"]


def test_start_asks_first_and_runs_nothing_when_the_owner_says_no(qtbot, settings_path, tmp_path):
    spawn, confirm = Spawn(), Confirm(False)
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, spawn=spawn, confirm=confirm)
    _settings(settings_path)
    panel.refresh()

    _button(panel, "tunnel-start").click()

    assert len(confirm.asked) == 1
    title, body, confirm_text = confirm.asked[0]
    assert "tunnel" in title.lower()
    assert confirm_text == "Start"
    assert spawn.calls == []


def test_the_start_button_is_off_while_the_tunnel_switch_is_off(qtbot, settings_path, tmp_path):
    _settings(settings_path, tunnel_control_enabled=False)
    panel, spawn, confirm = _panel(qtbot, settings_path, tmp_path)
    assert _button(panel, "tunnel-start").isEnabled() is False
    assert _label(panel, "tunnel-control-note").text() == TUNNEL_OFF_TEXT
    assert confirm.asked == [] and spawn.calls == []


def test_the_opt_in_is_off_by_default_and_saves_only_when_turned_on(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    assert _box(panel, "tunnelpanel-opt-in").isChecked() is False
    assert not settings_path.exists() or "custom_tunnel_opt_in" not in settings_path.read_text(encoding="utf-8")

    _box(panel, "tunnelpanel-opt-in").click()
    assert SettingsStore(settings_path).load().settings.custom_tunnel_opt_in is True
    assert _box(panel, "tunnelpanel-opt-in").isChecked() is True

    _box(panel, "tunnelpanel-opt-in").click()
    assert SettingsStore(settings_path).load().settings.custom_tunnel_opt_in is False
    assert "custom_tunnel_opt_in" not in settings_path.read_text(encoding="utf-8")


def test_a_connector_tunnel_flag_saves_and_says_why_it_is_not_reachable(qtbot, settings_path, tmp_path):
    _settings(settings_path, custom_connectors=(CONNECTOR,))
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    assert _label(panel, "tunnelpanel-connector-0-name").text() == "notes"
    assert _label(panel, "tunnelpanel-connector-0-note").text() == model.NOT_REACHABLE_OPT_IN_OFF
    assert _box(panel, "tunnelpanel-connector-0-exposed").isChecked() is False

    _box(panel, "tunnelpanel-connector-0-exposed").click()

    saved = SettingsStore(settings_path).load().settings.custom_connectors
    assert saved[0]["tunnel_exposed"] is True
    assert _box(panel, "tunnelpanel-connector-0-exposed").isChecked() is True


def test_no_connectors_line_shows_when_none_are_saved(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    assert _label(panel, "tunnelpanel-connectors-empty").isHidden() is False
    assert _label(panel, "tunnelpanel-connectors-empty").text() == model.NO_CONNECTORS_TEXT


def test_a_settings_file_with_a_problem_is_not_written_over(qtbot, settings_path, tmp_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{not json", encoding="utf-8")
    before = settings_path.read_text(encoding="utf-8")
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)

    _box(panel, "tunnelpanel-opt-in").click()

    assert settings_path.read_text(encoding="utf-8") == before
    assert _label(panel, "tunnelpanel-message").text() == PROBLEM_TEXT


def _tunnel_window(qtbot, settings_path, tmp_path):
    from meridian_desktop.ui.main_window import MainWindow

    spawn, confirm = Spawn(), Confirm()
    window = MainWindow(
        SettingsStore(settings_path),
        tunnel_runner=type("Runner", (), {"status": staticmethod(runner_reply("stopped"))})(),
        runner_actions=_actions(settings_path, spawn, confirm, tmp_path),
    )
    qtbot.addWidget(window)
    return window


def test_the_tunnel_panel_is_its_own_tab_and_settings_has_no_copy(qtbot, settings_path, tmp_path):
    window = _tunnel_window(qtbot, settings_path, tmp_path)
    index = next(i for i in range(window.tabs.count()) if window.tabs.tabText(i) == "Tunnel")
    page = window.tabs.widget(index).widget()
    assert isinstance(page, TunnelPanel)
    assert page.objectName() == "tunnel-panel"
    assert window._tunnel_panel is page
    settings_index = next(i for i in range(window.tabs.count()) if window.tabs.tabText(i) == "Settings")
    settings_page = window.tabs.widget(settings_index)
    assert settings_page.findChild(TunnelPanel) is None


def test_the_tunnel_tab_page_is_a_scroll_area_like_the_zotero_and_latex_tabs(qtbot, settings_path, tmp_path):
    window = _tunnel_window(qtbot, settings_path, tmp_path)
    tabs = {window.tabs.tabText(i): window.tabs.widget(i) for i in range(window.tabs.count())}
    tunnel_page = tabs["Tunnel"]
    assert isinstance(tunnel_page, QScrollArea)
    assert tunnel_page.widgetResizable() is True
    assert isinstance(tunnel_page.widget(), TunnelPanel)
    # The Zotero and LaTeX tabs are scroll areas too, so the lower part of the panel is reachable by scrolling.
    assert isinstance(tabs["Zotero"], QScrollArea)
    assert isinstance(tabs["LaTeX"], QScrollArea)


# ------------------------------------------------------------------ the slot table and the recent log (no Qt)


def _numbered(count: int) -> tuple[str, ...]:
    return tuple(f"line {n}" for n in range(1, count + 1))


def _cells(table: QTableWidget, row: int) -> list[str]:
    return [table.item(row, column).text() for column in range(table.columnCount())]


def test_the_slot_table_has_one_row_per_slot_port_and_is_not_checked_until_a_check():
    rows = model.slot_rows()
    assert [(row.slot, row.port) for row in rows] == list(slot_ports.SLOT_PORTS)
    assert len(rows) == 15
    assert all(row.state == model.SLOT_STATE_NOT_CHECKED for row in rows)


def test_a_port_check_says_listening_or_not_listening_and_never_healthy():
    results = [slot_ports.SlotPortResult("fs", 8808, True), slot_ports.SlotPortResult("code", 8809, False)]
    rows = {row.slot: row for row in model.slot_rows(results)}
    assert rows["fs"].state == model.SLOT_LISTENING and rows["fs"].serves == "filesystem"
    assert rows["code"].state == model.SLOT_NOT_LISTENING and rows["code"].serves == "code-intel"
    assert rows["extract"].state == model.SLOT_STATE_NOT_CHECKED, "a slot the check did not report is not checked"
    assert rows["p0"].serves == model.SLOT_NOT_ASSIGNED
    assert not any(word in row.state.lower() for row in rows.values() for word in ("healthy", "working", "connected"))


def test_the_recent_log_shows_only_the_last_fifty_lines():
    tail = LogTail(log_tail.STATUS_OK, Path("tunnel.log"), _numbered(120))
    assert model.LOG_TAIL_LINES == 50
    assert model.log_lines(tail) == _numbered(120)[-50:]
    assert model.log_lines(tail)[0] == "line 71"
    short = LogTail(log_tail.STATUS_OK, Path("tunnel.log"), _numbered(3))
    assert model.log_lines(short) == _numbered(3)


def test_a_log_with_no_lines_to_show_says_why():
    path = Path("tunnel.log")
    assert model.log_lines(None) == () and model.log_message(None) == model.LOG_FAILED_TEXT

    missing = LogTail(log_tail.STATUS_NO_LOG_YET, path, (), log_tail.NO_LOG_YET_TEXT)
    assert model.log_lines(missing) == ()
    assert model.log_message(missing) == log_tail.NO_LOG_YET_TEXT

    unreadable = LogTail(log_tail.STATUS_UNREADABLE, path, (), log_tail.UNREADABLE_TEXT)
    assert model.log_lines(unreadable) == ()
    assert model.log_message(unreadable) == log_tail.UNREADABLE_TEXT

    empty = LogTail(log_tail.STATUS_OK, path, ())
    assert model.log_lines(empty) == ()
    assert model.log_message(empty) == model.LOG_EMPTY_TEXT

    with_lines = LogTail(log_tail.STATUS_OK, path, ("a line",))
    assert model.log_message(with_lines) == ""

    unknown_status = LogTail("something-else", path, ("a line",))
    assert model.log_lines(unknown_status) == ()
    assert model.log_message(unknown_status) == model.LOG_FAILED_TEXT


def test_the_read_job_delivers_the_status_and_the_log_tail_together():
    delivered: list[tuple] = []
    tail = LogTail(log_tail.STATUS_OK, Path("tunnel.log"), ("a line",))
    TunnelReadJob(lambda: _status(RUNNING), lambda: tail, delivered.append).run()
    assert delivered == [(_status(RUNNING), None, tail, None)]


def test_a_status_read_that_raises_is_delivered_with_its_class_name_only():
    delivered: list[tuple] = []

    def failing_status() -> TunnelStatus:
        raise RuntimeError("a detail the panel must not show")

    TunnelReadJob(failing_status, lambda: None, delivered.append).run()
    status, failure, tail, _ports = delivered[0]
    assert status is None and failure == "RuntimeError" and tail is None
    assert "a detail the panel must not show" not in repr(delivered)


def test_a_log_read_that_raises_is_delivered_as_not_read():
    delivered: list[tuple] = []

    def failing_log() -> LogTail:
        raise OSError("the disk went away")

    TunnelReadJob(lambda: _status(NOT_RUNNING), failing_log, delivered.append).run()
    status, failure, tail, _ports = delivered[0]
    assert status.state == NOT_RUNNING and failure is None and tail is None
    assert model.log_message(tail) == model.LOG_FAILED_TEXT


def test_a_read_job_whose_delivery_is_gone_does_not_raise():
    def gone(_payload) -> None:
        raise RuntimeError("the shared delivery object was deleted")

    TunnelReadJob(lambda: _status(RUNNING), lambda: None, gone).run()


# ------------------------------------------------------------------ the slot table and the recent log (the widget)


def _expected_slot_cells(state_of):
    return [
        [slot, str(port), model.SLOT_SERVES.get(slot, model.SLOT_NOT_ASSIGNED), state_of(slot)]
        for slot, port in slot_ports.SLOT_PORTS
    ]


def test_the_slot_table_fills_in_the_port_check_on_a_check(qtbot, settings_path, tmp_path, monkeypatch):
    results = [slot_ports.SlotPortResult(slot, port, slot == "fs") for slot, port in slot_ports.SLOT_PORTS]
    monkeypatch.setattr(tunnel_panel, "probe_slot_ports", lambda: results)
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=runner_reply("running"))
    table = panel.findChild(QTableWidget, "tunnelpanel-slots")
    assert table.rowCount() == len(slot_ports.SLOT_PORTS) == 15
    assert [_cells(table, row) for row in range(table.rowCount())] == _expected_slot_cells(
        lambda slot: model.SLOT_STATE_NOT_CHECKED
    )

    _check_and_wait(qtbot, panel, model.RUNNING_HEADLINE)
    assert [_cells(table, row) for row in range(table.rowCount())] == _expected_slot_cells(
        lambda slot: model.SLOT_LISTENING if slot == "fs" else model.SLOT_NOT_LISTENING
    )


def test_a_port_check_that_raises_leaves_every_slot_not_checked(qtbot, settings_path, tmp_path, monkeypatch):
    def failing_probe():
        raise OSError("a detail the slot table must not show")

    monkeypatch.setattr(tunnel_panel, "probe_slot_ports", failing_probe)
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=runner_reply("running"))
    table = panel.findChild(QTableWidget, "tunnelpanel-slots")
    _check_and_wait(qtbot, panel, model.RUNNING_HEADLINE)
    assert [_cells(table, row) for row in range(table.rowCount())] == _expected_slot_cells(
        lambda slot: model.SLOT_STATE_NOT_CHECKED
    )
    assert "a detail the slot table must not show" not in repr([_cells(table, r) for r in range(table.rowCount())])


def test_the_recent_log_shows_the_last_fifty_lines_of_a_temp_file_with_secrets_redacted(qtbot, settings_path, tmp_path):
    log = tmp_path / "tunnel.log"
    body = [f"line {n}" for n in range(1, 120)] + ["connected with token=s3cr3t"]
    log.write_text("\n".join(body) + "\n", encoding="utf-8")
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=runner_reply("running"), log_path=log)

    _check_and_wait(qtbot, panel, model.RUNNING_HEADLINE)

    view = panel.findChild(QPlainTextEdit, "tunnelpanel-log")
    expected = [f"line {n}" for n in range(71, 120)] + ["connected with token=[redacted]"]
    assert view.toPlainText().split("\n") == expected
    assert len(view.toPlainText().split("\n")) == 50
    assert "s3cr3t" not in view.toPlainText()
    assert view.isReadOnly() is True
    assert _label(panel, "tunnelpanel-log-message").isHidden() is True


def test_a_missing_tunnel_log_says_no_log_yet_and_shows_no_lines(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(
        qtbot, settings_path, tmp_path, status_call=runner_reply("running"), log_path=tmp_path / "absent" / "tunnel.log",
    )
    assert _label(panel, "tunnelpanel-log-message").text() == model.LOG_NOT_READ_TEXT

    _check_and_wait(qtbot, panel, model.RUNNING_HEADLINE)

    assert panel.findChild(QPlainTextEdit, "tunnelpanel-log").toPlainText() == ""
    assert _label(panel, "tunnelpanel-log-message").text() == log_tail.NO_LOG_YET_TEXT
    assert _label(panel, "tunnelpanel-log-message").isHidden() is False


def test_a_tunnel_log_that_cannot_be_read_says_so(qtbot, settings_path, tmp_path):
    # A directory is not a readable file, so the log read reports it as unreadable and the panel shows that.
    panel, _spawn, _confirm = _panel(
        qtbot, settings_path, tmp_path, status_call=runner_reply("running"), log_path=tmp_path,
    )

    _check_and_wait(qtbot, panel, model.RUNNING_HEADLINE)

    assert panel.findChild(QPlainTextEdit, "tunnelpanel-log").toPlainText() == ""
    assert _label(panel, "tunnelpanel-log-message").text() == log_tail.UNREADABLE_TEXT


def test_the_log_is_read_again_on_each_check(qtbot, settings_path, tmp_path):
    log = tmp_path / "tunnel.log"
    log.write_text("first\nsecond\n", encoding="utf-8")
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path, status_call=runner_reply("running"), log_path=log)
    view = panel.findChild(QPlainTextEdit, "tunnelpanel-log")
    _check_and_wait(qtbot, panel, model.RUNNING_HEADLINE)
    assert view.toPlainText() == "first\nsecond"

    with log.open("a", encoding="utf-8") as handle:
        handle.write("third\n")
    panel.check()

    qtbot.waitUntil(lambda: view.toPlainText() == "first\nsecond\nthird", timeout=5000)


def test_a_delivered_log_that_is_not_a_tail_is_shown_as_not_read(qtbot, settings_path, tmp_path):
    panel, _spawn, _confirm = _panel(qtbot, settings_path, tmp_path)
    # The panel's own owner token and generation, with a log value that is not a LogTail: it must not be shown.
    shared_delivery().delivered.emit(Delivered(panel._owner, (panel._generation, (None, None, "not a log tail"))))
    assert _label(panel, "tunnelpanel-log-message").text() == model.LOG_FAILED_TEXT
    assert panel.findChild(QPlainTextEdit, "tunnelpanel-log").toPlainText() == ""


@pytest.fixture(autouse=True)
def _no_real_runner(monkeypatch):
    """The panel's only runner calls are the fakes above. A test that reached the real runner would fail here."""
    from meridian_desktop import runner_client

    def refuse(*args, **kwargs):
        raise AssertionError("the real runner was started by a Tunnel panel test")

    monkeypatch.setattr(runner_client.RunnerClient, "run", refuse)
