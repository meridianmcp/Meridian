"""Tests for the Start, Stop and Restart widget (meridian_desktop/ui/scope_controls.py), through the tunnel and local scopes.

Every outside effect is a fake: the runner spawn, the confirmation dialog and the settings file (a temp path). No
dialog opens, no runner process starts, no socket opens, and the logs go to pytest's temp directory. The widget
runs under pytest-qt on the offscreen platform, as the other desktop tests do.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from meridian_desktop.core_locator import FOUND, NOT_FOUND, SOURCE_SEARCHED, LocatedCore
from meridian_desktop.runner_controls import RunnerControl, build_argv, describe_command
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.runner_actions import CHOOSE_CANCELLED_TEXT, CHOSEN_TEXT, CoreSetup, RunnerActions
from meridian_desktop.ui.scope_controls import (
    CORE_NOT_SET_TEXT,
    TUNNEL_OFF_TEXT,
    ScopeControls,
    local_gate,
    tunnel_gate,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT

PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"
STAMP = dt.datetime(2026, 10, 22, 17, 0, 0, tzinfo=dt.timezone.utc)


class Spawn:
    """A fake for subprocess.run. It returns its canned replies in order, or raises a canned error. It can be held."""

    def __init__(self, *replies, gate: threading.Event | None = None):
        self._replies = list(replies)
        self._gate = gate
        self._lock = threading.Lock()
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        with self._lock:
            self.calls.append((list(argv), dict(kwargs)))
            reply = self._replies.pop(0) if self._replies else _done(0, _status("tunnel", "stopped"))
        if self._gate is not None:
            self._gate.wait(5)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def verbs(self) -> list[str]:
        with self._lock:
            return [argv[3] for argv, _ in self.calls]


class Confirm:
    """A fake for the confirmation dialog. It answers in order and records each question it was asked."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, parent, title, body, confirm_text):
        self.asked.append((title, body, confirm_text))
        return self._answers.pop(0) if self._answers else False


def _done(code, payload=None, *, stdout=None, stderr=b""):
    body = stdout if stdout is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=body, stderr=stderr)


def _status(scope, state, pid=808):
    return {"scope": scope, "generated_at": 1.0, "child": {"state": state, "pid": pid}}


def _settings(settings_path, **overrides):
    values = dict(core_python=PY, core_checkout=CWD, tunnel_control_enabled=True)
    values.update(overrides)
    SettingsStore(settings_path).save(Settings(**values))


def _actions(settings_path, spawn, confirm, log_path):
    return RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python, cwd: RunnerControl(
            python, cwd, spawn=spawn, log_path=log_path, clock=lambda: STAMP,
        ),
        confirm=confirm,
    )


def _widget(qtbot, actions, gate=tunnel_gate, scope="tunnel", prefix="tunnel"):
    widget = ScopeControls(scope, actions, gate, prefix=prefix)
    qtbot.addWidget(widget)
    return widget


def _button(widget, name) -> QPushButton:
    return widget.findChild(QPushButton, name)


def _label(widget, name) -> QLabel:
    return widget.findChild(QLabel, name)


# ------------------------------------------------------------------ the gates


def test_the_tunnel_buttons_are_disabled_while_the_switch_is_off(qtbot, settings_path, tmp_path):
    _settings(settings_path, tunnel_control_enabled=False)
    widget = _widget(qtbot, _actions(settings_path, Spawn(), Confirm(), tmp_path / "log.txt"))

    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart"):
        assert _button(widget, name).isEnabled() is False, name
    assert _label(widget, "tunnel-control-note").text() == TUNNEL_OFF_TEXT


def test_the_tunnel_buttons_are_disabled_when_the_core_is_not_set_even_if_the_switch_is_on(qtbot, settings_path, tmp_path):
    _settings(settings_path, core_python="", core_checkout="")
    widget = _widget(qtbot, _actions(settings_path, Spawn(), Confirm(), tmp_path / "log.txt"))

    assert _button(widget, "tunnel-start").isEnabled() is False
    assert _label(widget, "tunnel-control-note").text() == CORE_NOT_SET_TEXT


def test_the_tunnel_buttons_are_enabled_when_the_switch_is_on_and_the_core_is_set(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    widget = _widget(qtbot, _actions(settings_path, Spawn(), Confirm(), tmp_path / "log.txt"))

    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart"):
        assert _button(widget, name).isEnabled() is True, name
    assert _label(widget, "tunnel-control-note").isHidden()


def test_the_local_buttons_need_only_the_core_to_be_set(qtbot, settings_path, tmp_path):
    _settings(settings_path, tunnel_control_enabled=False)
    widget = _widget(
        qtbot, _actions(settings_path, Spawn(), Confirm(), tmp_path / "log.txt"),
        gate=local_gate, scope="local", prefix="local",
    )

    assert _button(widget, "local-restart").isEnabled() is True


def test_the_gate_functions_say_why_when_they_refuse():
    off = CoreSetup(PY, CWD, "http://127.0.0.1:7878", False, False, False)
    unset = CoreSetup("", "", "http://127.0.0.1:7878", True, False, False)

    assert tunnel_gate(off) == (False, TUNNEL_OFF_TEXT)
    assert tunnel_gate(unset) == (False, CORE_NOT_SET_TEXT)
    assert tunnel_gate(CoreSetup(PY, CWD, "x", True, False, False)) == (True, "")
    assert local_gate(unset) == (False, CORE_NOT_SET_TEXT)


def test_the_labels_are_selectable_plain_text(qtbot, settings_path, tmp_path):
    _settings(settings_path, tunnel_control_enabled=False)
    widget = _widget(qtbot, _actions(settings_path, Spawn(), Confirm(), tmp_path / "log.txt"))

    for label in widget.findChildren(QLabel):
        assert label.textInteractionFlags() == SELECTABLE_TEXT, label.objectName()


# ------------------------------------------------------------------ start, stop and cancel


def test_start_asks_first_naming_the_exact_command_scope_and_folder_then_runs_it(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn, confirm = Spawn(_done(0, _status("tunnel", "running", pid=909))), Confirm(True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    title, body, confirm_text = confirm.asked[0]
    assert title == "Start the tunnel?"
    assert confirm_text == "Start"
    assert describe_command(PY, "start", "tunnel") in body
    assert "Scope: tunnel" in body
    assert f"Working folder: {CWD}" in body
    assert spawn.calls[0][0] == build_argv(PY, "start", "tunnel")
    assert spawn.calls[0][1]["shell"] is False
    assert "running" in _label(widget, "tunnel-control-result").text()
    assert "pid 909" in _label(widget, "tunnel-control-result").text()


def test_cancelling_the_start_dialog_changes_nothing(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn, confirm = Spawn(), Confirm(False)
    log = tmp_path / "log.txt"
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, log))

    _button(widget, "tunnel-start").click()

    assert len(confirm.asked) == 1
    assert spawn.calls == []
    assert not log.exists()
    assert _label(widget, "tunnel-control-result").text() == "Start cancelled. Nothing was started."


def test_stop_asks_then_stops_and_the_stop_is_confirmed_when_the_runner_says_so(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn, confirm = Spawn(_done(0, _status("tunnel", "stopped", pid=5))), Confirm(True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))

    _button(widget, "tunnel-stop").click()
    qtbot.waitUntil(lambda: _label(widget, "tunnel-control-result").text() == "The tunnel is stopped.", timeout=5000)

    assert confirm.asked[0][0] == "Stop the tunnel?"
    assert spawn.verbs == ["stop"]


def test_a_stop_the_runner_still_reports_running_is_shown_as_not_confirmed(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn, confirm = Spawn(_done(0, _status("tunnel", "running", pid=6))), Confirm(True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))

    _button(widget, "tunnel-stop").click()
    qtbot.waitUntil(lambda: "Not confirmed" in _label(widget, "tunnel-control-result").text(), timeout=5000)


def test_a_start_that_times_out_is_not_confirmed_and_is_not_retried(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn = Spawn(subprocess.TimeoutExpired(cmd="x", timeout=30))
    confirm = Confirm(True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: "Not confirmed" in _label(widget, "tunnel-control-result").text(), timeout=5000)

    assert "Nothing was retried" in _label(widget, "tunnel-control-result").text()
    assert len(spawn.calls) == 1
    assert _button(widget, "tunnel-start").isEnabled() is True  # the page is usable again after the timeout


def test_the_buttons_are_disabled_while_a_call_runs_and_enabled_after(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    gate = threading.Event()
    spawn = Spawn(_done(0, _status("tunnel", "running", pid=7)), gate=gate)
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(True), tmp_path / "log.txt"))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: len(spawn.calls) == 1, timeout=5000)
    assert _button(widget, "tunnel-start").isEnabled() is False
    assert _button(widget, "tunnel-stop").isEnabled() is False
    gate.set()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)
    assert _button(widget, "tunnel-start").isEnabled() is True


# ------------------------------------------------------------------ restart: stop, then start, each confirmed


def test_restart_asks_twice_and_runs_stop_then_start_in_that_order(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn = Spawn(_done(0, _status("tunnel", "stopped", pid=10)), _done(0, _status("tunnel", "running", pid=11)))
    confirm = Confirm(True, True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-restart").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    assert [question[0] for question in confirm.asked] == [
        "Restart the tunnel: stop it first?",
        "Restart the tunnel: start it now?",
    ]
    assert spawn.verbs == ["stop", "start"]
    assert "pid 11" in _label(widget, "tunnel-control-result").text()


def test_cancelling_the_first_restart_question_changes_nothing(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn, confirm = Spawn(), Confirm(False)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))

    _button(widget, "tunnel-restart").click()

    assert len(confirm.asked) == 1
    assert spawn.calls == []
    assert _label(widget, "tunnel-control-result").text() == "Restart cancelled. Nothing was stopped or started."


def test_cancelling_the_second_restart_question_leaves_the_tunnel_stopped_and_starts_nothing(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn = Spawn(_done(0, _status("tunnel", "stopped", pid=12)))
    confirm = Confirm(True, False)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-restart").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    assert spawn.verbs == ["stop"]
    assert "Start cancelled. The tunnel is stopped." in _label(widget, "tunnel-control-result").text()


def test_a_restart_does_not_start_when_its_stop_is_not_confirmed(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn = Spawn(_done(0, _status("tunnel", "running", pid=13)))
    confirm = Confirm(True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-restart").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    assert len(confirm.asked) == 1  # the start question is never asked
    assert spawn.verbs == ["stop"]
    assert "Restart stopped: the stop was not confirmed, so nothing was started." in _label(widget, "tunnel-control-result").text()


def test_a_restart_stop_that_times_out_is_not_retried_and_starts_nothing(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn = Spawn(subprocess.TimeoutExpired(cmd="x", timeout=15))
    confirm = Confirm(True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-restart").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    assert spawn.verbs == ["stop"]
    assert len(confirm.asked) == 1
    assert "Not confirmed" in _label(widget, "tunnel-control-result").text()


def test_a_local_restart_names_the_local_scope_in_its_questions(qtbot, settings_path, tmp_path):
    _settings(settings_path, tunnel_control_enabled=False)
    spawn = Spawn(_done(0, _status("local", "stopped", pid=14)), _done(0, _status("local", "running", pid=15)))
    confirm = Confirm(True, True)
    widget = _widget(
        qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"),
        gate=local_gate, scope="local", prefix="local",
    )
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "local-restart").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    assert confirm.asked[0][0] == "Restart the local server: stop it first?"
    assert "Scope: local" in confirm.asked[0][1]
    assert "Scope: local" in confirm.asked[1][1]
    assert spawn.verbs == ["stop", "start"]
    assert spawn.calls[1][0] == build_argv(PY, "start", "local")


def test_each_call_is_logged_once_with_no_secret(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    log = tmp_path / "log.txt"
    spawn = Spawn(_done(0, _status("tunnel", "running", pid=16)))
    widget = _widget(qtbot, _actions(settings_path, spawn, Confirm(True), log))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-start").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    assert log.read_text(encoding="utf-8").splitlines() == [
        "2026-10-22T17:00:00+00:00 start scope=tunnel outcome=confirmed pid=16",
    ]


class SwitchOffAfterStop(Spawn):
    """A runner fake that turns the tunnel switch off after its first call, as if the owner did so while the stop ran."""

    def __init__(self, settings_path, *replies):
        super().__init__(*replies)
        self._settings_path = settings_path

    def __call__(self, argv, **kwargs):
        result = super().__call__(argv, **kwargs)
        SettingsStore(self._settings_path).save(Settings(core_python=PY, core_checkout=CWD, tunnel_control_enabled=False))
        return result


class SwitchOffWhileAsked(Confirm):
    """A dialog fake that turns the tunnel switch off once the restart's start question has been answered."""

    def __init__(self, settings_path, *answers):
        super().__init__(*answers)
        self._settings_path = settings_path

    def __call__(self, parent, title, body, confirm_text):
        answer = super().__call__(parent, title, body, confirm_text)
        if len(self.asked) == 2:
            SettingsStore(self._settings_path).save(Settings(core_python=PY, core_checkout=CWD, tunnel_control_enabled=False))
        return answer


def test_a_restart_does_not_ask_to_start_when_the_tunnel_switch_is_turned_off_after_the_stop(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn = SwitchOffAfterStop(settings_path, _done(0, _status("tunnel", "stopped", pid=18)))
    confirm = Confirm(True, True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-restart").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    assert [question[0] for question in confirm.asked] == ["Restart the tunnel: stop it first?"]
    assert spawn.verbs == ["stop"]
    assert "Restart stopped before the start, so nothing was started." in _label(widget, "tunnel-control-result").text()
    assert _button(widget, "tunnel-start").isEnabled() is False
    assert _label(widget, "tunnel-control-note").text() == TUNNEL_OFF_TEXT


def test_a_restart_does_not_start_when_the_tunnel_switch_is_turned_off_while_the_start_is_asked(qtbot, settings_path, tmp_path):
    _settings(settings_path)
    spawn = Spawn(_done(0, _status("tunnel", "stopped", pid=19)))
    confirm = SwitchOffWhileAsked(settings_path, True, True)
    widget = _widget(qtbot, _actions(settings_path, spawn, confirm, tmp_path / "log.txt"))
    finished = []
    widget.actionFinished.connect(lambda: finished.append(True))

    _button(widget, "tunnel-restart").click()
    qtbot.waitUntil(lambda: bool(finished), timeout=5000)

    assert len(confirm.asked) == 2  # the start question was asked, then the switch went off before the start ran
    assert spawn.verbs == ["stop"]
    assert "Restart stopped before the start, so nothing was started." in _label(widget, "tunnel-control-result").text()


def test_the_tunnel_buttons_are_enabled_by_default_once_the_core_is_set(qtbot, settings_path, tmp_path):
    SettingsStore(settings_path).save(Settings(core_python=PY, core_checkout=CWD))  # no switch key: the default is on
    widget = _widget(qtbot, _actions(settings_path, Spawn(), Confirm(), tmp_path / "log.txt"))

    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart"):
        assert _button(widget, name).isEnabled() is True, name


# ------------------------------------------------------------------ the one-step fix next to a greyed control


def _fix(widget, prefix="tunnel") -> QPushButton:
    return widget.findChild(QPushButton, f"{prefix}-fix")


def _located_actions(settings_path, *, locate, choose_folder=lambda parent, start: "", spawn=None, tmp_path=None):
    """RunnerActions with a fake core search and a fake folder chooser. Nothing starts a process or opens a dialog."""
    return RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python, cwd: RunnerControl(
            python, cwd, spawn=spawn or Spawn(), log_path=(tmp_path or settings_path.parent) / "log.txt", clock=lambda: STAMP,
        ),
        confirm=Confirm(),
        locate=locate,
        choose_folder=choose_folder,
    )


def test_the_fix_button_is_shown_only_while_the_gate_is_off_and_names_the_one_thing_that_enables_it(qtbot, settings_path, tmp_path):
    _settings(settings_path, core_python="", core_checkout="")
    widget = _widget(qtbot, _actions(settings_path, Spawn(), Confirm(), tmp_path / "log.txt"))
    assert _fix(widget).isVisibleTo(widget) is True
    assert _fix(widget).text() == "Choose the Meridian core folder"
    assert _fix(widget).isEnabled() is True

    _settings(settings_path)
    widget.refresh_enabled()
    assert _fix(widget).isVisibleTo(widget) is False  # the core is set and the switch is on, so nothing needs a fix

    _settings(settings_path, tunnel_control_enabled=False)
    widget.refresh_enabled()
    assert _fix(widget).text() == "Turn on the tunnel controls"


def test_choose_the_core_folder_turns_the_controls_on_once_the_search_finds_the_core(qtbot, settings_path, tmp_path):
    _settings(settings_path, core_python="", core_checkout="")
    checkout = tmp_path / "chosen" / "repository"
    checkout.mkdir(parents=True)
    (checkout / "meridian").mkdir()
    (checkout / "meridian" / "__init__.py").write_text("", encoding="utf-8")
    python = tmp_path / "env" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"MZ")
    actions = _located_actions(
        settings_path,
        locate=lambda store: LocatedCore(FOUND, str(checkout), str(python), SOURCE_SEARCHED),
        choose_folder=lambda parent, start: str(checkout),
    )
    widget = _widget(qtbot, actions)
    assert _button(widget, "tunnel-start").isEnabled() is False

    qtbot.mouseClick(_fix(widget), Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: _button(widget, "tunnel-start").isEnabled(), timeout=5000)
    assert _label(widget, "tunnel-control-result").text() == CHOSEN_TEXT
    assert SettingsStore(settings_path).load().settings.core_python == str(python)


def test_a_cancelled_folder_choice_keeps_the_controls_off_and_the_fix_button_there(qtbot, settings_path, tmp_path):
    _settings(settings_path, core_python="", core_checkout="")
    actions = _located_actions(settings_path, locate=lambda s: LocatedCore(NOT_FOUND, reason="none"), choose_folder=lambda p, s: "")
    widget = _widget(qtbot, actions)

    qtbot.mouseClick(_fix(widget), Qt.MouseButton.LeftButton)

    assert _label(widget, "tunnel-control-result").text() == CHOOSE_CANCELLED_TEXT
    assert _button(widget, "tunnel-start").isEnabled() is False
    assert _fix(widget).isVisibleTo(widget) is True


def test_the_turn_on_button_sets_the_switch_and_the_buttons_come_on(qtbot, settings_path, tmp_path):
    _settings(settings_path, tunnel_control_enabled=False)
    widget = _widget(qtbot, _actions(settings_path, Spawn(), Confirm(), tmp_path / "log.txt"))

    qtbot.mouseClick(_fix(widget), Qt.MouseButton.LeftButton)

    assert SettingsStore(settings_path).load().settings.tunnel_control_enabled is True
    assert _button(widget, "tunnel-start").isEnabled() is True
    assert _fix(widget).isVisibleTo(widget) is False


def test_the_gate_reads_the_setup_again_when_the_core_setup_changes(qtbot, settings_path, tmp_path):
    _settings(settings_path, core_python="", core_checkout="")
    actions = _located_actions(settings_path, locate=lambda s: LocatedCore(NOT_FOUND, reason="none"))
    widget = _widget(qtbot, actions)
    assert _button(widget, "tunnel-start").isEnabled() is False

    _settings(settings_path)  # the core is saved somewhere else, for example by the client setup section
    actions.notify_core_changed()

    assert _button(widget, "tunnel-start").isEnabled() is True
