"""Tests for the restart notice strip and the window wiring. Offscreen Qt via qtbot.

Most tests use a fake watch, so no file is read. The placement test and the server-start test use a real RestartWatch
with fake readers, and temporary files. No test starts a process: the runner's spawn is a fake.
"""

import json
import os
import subprocess
import sys
import threading
from types import ModuleType

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from meridian_desktop import app_relaunch
from meridian_desktop import runner_controls as rc
from meridian_desktop.restart_check import GONE, Restart, RestartWatch, banner_text
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import main_window as main_window_module
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.restart_banner import CHECK_INTERVAL_MS, RestartBanner
from meridian_desktop.ui.runner_actions import RunnerActions

BASE_NS = 1_700_000_000_000_000_000
NANOS_PER_SECOND = 1_000_000_000
LATER_NS = BASE_NS + 60 * NANOS_PER_SECOND


class FakeWatch:
    """Returns ``next`` from each check. The test sets it before the check it wants."""

    def __init__(self):
        self.next = Restart()
        self.checks = 0

    def check(self):
        self.checks += 1
        return self.next


def _banner(qtbot):
    watch = FakeWatch()
    banner = RestartBanner(watch)
    qtbot.addWidget(banner)
    return banner, watch


def _dismiss(banner):
    return banner.findChild(QPushButton, "restart-banner-dismiss")


def _write(path, text="x = 1\n", mtime=BASE_NS):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(mtime, mtime))
    return str(path)


def _module(name, file):
    module = ModuleType(name)
    module.__file__ = file
    return module


def test_the_banner_starts_hidden_and_checks_every_interval(qtbot):
    banner, _watch = _banner(qtbot)
    assert banner.isHidden()
    assert banner._timer.interval() == CHECK_INTERVAL_MS == 30_000
    assert banner._timer.isActive()


def test_the_timer_runs_the_check_and_shows_the_banner(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/a.py",))
    before = watch.checks
    banner._timer.setInterval(10)  # a short interval, so the real timer fires inside the test
    qtbot.waitUntil(lambda: watch.checks > before and not banner.isHidden(), timeout=5000)
    assert banner.findChild(QLabel).text() == banner_text(watch.next)


def test_a_change_shows_the_banner_with_its_text_and_a_selectable_label(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/ui/main_window.py",))
    banner.check_now()
    assert not banner.isHidden()
    label = banner.findChild(QLabel)
    assert label.text() == banner_text(watch.next)
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse


def test_a_server_only_change_is_shown(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(server=True)
    banner.check_now()
    assert not banner.isHidden()
    assert banner.findChild(QLabel).text() == (
        "Restart needed: the local Meridian server changed on disk since it started."
    )


def test_dismiss_hides_the_banner_and_the_same_change_does_not_bring_it_back(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/a.py",))
    banner.check_now()
    _dismiss(banner).click()
    assert banner.isHidden()
    banner.check_now()
    assert banner.isHidden()


def test_a_new_change_after_dismiss_brings_the_banner_back(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/a.py",))
    banner.check_now()
    _dismiss(banner).click()
    watch.next = Restart(code=("meridian_desktop/a.py", "meridian_desktop/b.py"))
    banner.check_now()
    assert not banner.isHidden()


def test_no_change_hides_a_shown_banner_again(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/a.py",))
    banner.check_now()
    assert not banner.isHidden()
    watch.next = Restart()
    banner.check_now()
    assert banner.isHidden()
    assert banner.restart == Restart()


def test_a_cleared_notice_forgets_a_dismissed_change(qtbot):
    banner, watch = _banner(qtbot)
    watch.next = Restart(code=("meridian_desktop/a.py",))
    banner.check_now()
    _dismiss(banner).click()
    watch.next = Restart()  # the notice clears, so the dismissal is forgotten
    banner.check_now()
    watch.next = Restart(code=("meridian_desktop/a.py",))  # the same change happens again
    banner.check_now()
    assert not banner.isHidden()


def test_the_strip_sits_above_the_tabs_and_shows_a_real_package_change(qtbot, settings_path, tmp_path):
    package_file = _write(tmp_path / "meridian_desktop" / "a.py")
    watch = RestartWatch(
        modules={"meridian_desktop.a": _module("meridian_desktop.a", package_file)},
        frozen=False,
        executable="",
        started_ns_of=lambda pid: GONE,
    )
    window = MainWindow(SettingsStore(settings_path), restart_watch=watch)
    qtbot.addWidget(window)
    banner = window.findChild(RestartBanner)
    layout = window.centralWidget().layout()
    assert 0 <= layout.indexOf(banner) < layout.indexOf(window.tabs)
    assert banner.isHidden()
    banner.check_now()  # nothing has changed yet
    assert banner.isHidden()
    os.utime(package_file, ns=(LATER_NS, LATER_NS))
    banner.check_now()
    assert not banner.isHidden()
    assert banner.findChild(QLabel).text() == (
        "Restart needed: meridian_desktop/a.py changed on disk since this window opened."
    )


# ----------------------------------------------------------- a confirmed start from the window


class _FakeSpawn:
    """Stands in for subprocess.run. It returns one canned reply and records the argv it was given."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self.reply


def _done(payload, code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=json.dumps(payload).encode("utf-8"), stderr=b"")


def test_a_confirmed_local_start_from_the_window_is_watched(qtbot, settings_path, tmp_path):
    checkout = tmp_path / "core"
    server_file = _write(checkout / "meridian" / "server.py")
    store = SettingsStore(settings_path)
    store.save(Settings(mode="local", core_checkout=str(checkout), core_python=sys.executable))
    started = BASE_NS + 10 * NANOS_PER_SECOND
    watch = RestartWatch(
        modules={},
        frozen=False,
        executable="",
        started_ns_of=lambda pid: started if pid == 4321 else GONE,
    )
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "running", "pid": 4321}}))
    actions = RunnerActions(store, spawn=spawn, log_path=tmp_path / "control.log")
    window = MainWindow(store, runner_actions=actions, restart_watch=watch)
    qtbot.addWidget(window)

    # The same call the window's Start button makes: the controls for the saved core, then start.
    result = actions.control(sys.executable, str(checkout)).start("local")

    assert result.outcome == rc.CONFIRMED
    assert len(spawn.calls) == 1
    assert not watch.check().server
    os.utime(server_file, ns=(started + NANOS_PER_SECOND, started + NANOS_PER_SECOND))
    assert watch.check() == Restart(server=True)


def test_a_start_through_a_make_control_factory_is_watched(qtbot, settings_path, tmp_path):
    # A factory that builds the control itself, as a test or another caller might. Its controls still reach the watch.
    checkout = tmp_path / "core"
    server_file = _write(checkout / "meridian" / "server.py")
    store = SettingsStore(settings_path)
    store.save(Settings(mode="local", core_checkout=str(checkout), core_python=sys.executable))
    started = BASE_NS + 10 * NANOS_PER_SECOND
    watch = RestartWatch(
        modules={}, frozen=False, executable="", started_ns_of=lambda pid: started if pid == 4321 else GONE,
    )
    spawn = _FakeSpawn(_done({"scope": "local", "child": {"state": "running", "pid": 4321}}))

    def make_control(python, cwd):
        return rc.RunnerControl(python, cwd, spawn=spawn, log_path=tmp_path / "control.log")

    actions = RunnerActions(store, make_control=make_control)
    window = MainWindow(store, runner_actions=actions, restart_watch=watch)
    qtbot.addWidget(window)

    result = actions.control(sys.executable, str(checkout)).start("local")

    assert result.outcome == rc.CONFIRMED
    assert not watch.check().server
    os.utime(server_file, ns=(started + NANOS_PER_SECOND, started + NANOS_PER_SECOND))
    assert watch.check() == Restart(server=True)


class _HookedControl:
    """A control that can take the start hook, as RunnerControl does."""

    def __init__(self):
        self.hook = None

    def set_on_started(self, callback):
        self.hook = callback


class _UnhookedControl:
    """A control with no set_on_started, so a start hook has nowhere to go."""


def test_a_control_with_set_on_started_takes_the_hook(qtbot, settings_path):
    actions = RunnerActions(SettingsStore(settings_path), make_control=lambda python, cwd: _HookedControl())

    def hook(pid, cwd):
        return None

    actions.set_on_started(hook)
    assert actions.control("python", "core").hook is hook


def test_a_control_without_set_on_started_is_refused_when_a_hook_is_set(qtbot, settings_path):
    actions = RunnerActions(SettingsStore(settings_path), make_control=lambda python, cwd: _UnhookedControl())
    actions.set_on_started(lambda pid, cwd: None)
    with pytest.raises(TypeError, match="set_on_started"):
        actions.control("python", "core")


def test_without_a_hook_a_control_without_set_on_started_is_returned_as_it_is(qtbot, settings_path):
    actions = RunnerActions(SettingsStore(settings_path), make_control=lambda python, cwd: _UnhookedControl())
    assert isinstance(actions.control("python", "core"), _UnhookedControl)


# ----------------------------------------------------------- Restart app and the strip's local Restart in the window

REAL_STARTUP_WATCH = app_relaunch.StartupWatch
REAL_START_NEW_COPY = app_relaunch.start_new_copy


class _WindowWatch(FakeWatch):
    """A FakeWatch the window can hand a confirmed start to. The start is recorded and the check stays as the test sets it."""

    def __init__(self):
        super().__init__()
        self.started = []

    def note_server_started(self, pid, cwd):
        self.started.append(pid)


class _GatedSpawn:
    """Stands in for subprocess.run. A stop waits for the gate, so a test can look at the window mid-action."""

    def __init__(self, gate):
        self.gate = gate
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, argv, **kwargs):
        with self._lock:
            self.calls.append(list(argv))
        if "stop" in argv:
            self.gate.wait(5)
            return _done({"scope": "local", "child": {"state": "stopped"}})
        return _done({"scope": "local", "child": {"state": "running", "pid": 4321}})

    def verbs(self):
        with self._lock:
            return [argv[3] for argv in self.calls]


class _AnswersInOrder:
    """Stands in for the runner confirmation dialog. It answers in order, then No."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, parent, title, body, confirm_text):
        return self.answers.pop(0) if self.answers else False


class _Child:
    """Stands in for the new copy's Popen object."""

    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


def _restart_app_fakes(monkeypatch, *, answer, child=None, failure=None):
    """Fakes the confirm helper, the popen call, the warning and the quit. Returns what each one saw."""
    seen = {"asked": [], "popen": [], "warnings": [], "quit": 0}

    def fake_ask(parent, title, text):
        seen["asked"].append((title, text))
        return answer

    def fake_popen(argv, **kwargs):
        seen["popen"].append(list(argv))
        if failure is not None:
            raise failure
        return child

    def fake_quit():
        seen["quit"] += 1

    monkeypatch.setattr(main_window_module, "ask_yes_no", fake_ask)
    monkeypatch.setattr(
        app_relaunch, "start_new_copy", lambda plan: REAL_START_NEW_COPY(plan, popen=fake_popen, platform="win32")
    )
    # A short hold keeps the test quick. The window still polls it on its timer.
    monkeypatch.setattr(app_relaunch, "StartupWatch", lambda c: REAL_STARTUP_WATCH(c, hold_seconds=0.2))
    monkeypatch.setattr(
        main_window_module.QMessageBox, "warning", lambda parent, title, text: seen["warnings"].append(text)
    )
    monkeypatch.setattr(main_window_module.QApplication, "quit", fake_quit)
    return seen


def _plain_window(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path), restart_watch=_WindowWatch())
    qtbot.addWidget(window)
    return window


def test_restart_app_no_starts_nothing_and_the_question_names_the_launch_starts(qtbot, settings_path, monkeypatch):
    window = _plain_window(qtbot, settings_path)
    seen = _restart_app_fakes(monkeypatch, answer=False, child=_Child(None))
    window._restart_app()
    assert len(seen["asked"]) == 1
    title, text = seen["asked"][0]
    assert title == main_window_module.RESTART_APP_TITLE
    for name in ("Zotero", "local server", "hosted tunnel"):
        assert name in text
    assert seen["popen"] == []
    assert seen["quit"] == 0


def test_restart_app_asks_through_the_confirm_helper_whose_default_is_no():
    from meridian_desktop.ui import confirm

    assert main_window_module.ask_yes_no is confirm.ask_yes_no


def test_restart_app_yes_quits_only_after_the_new_copy_stayed_running(qtbot, settings_path, monkeypatch):
    window = _plain_window(qtbot, settings_path)
    seen = _restart_app_fakes(monkeypatch, answer=True, child=_Child(None))
    window._restart_app()
    assert len(seen["popen"]) == 1
    assert seen["quit"] == 0  # the window is still open right after the start
    # A second press while the new copy is being checked starts nothing more.
    window._restart_app()
    assert len(seen["popen"]) == 1
    qtbot.waitUntil(lambda: seen["quit"] == 1, timeout=3000)
    assert seen["warnings"] == []


def test_restart_app_waits_to_quit_until_the_local_scope_is_not_busy(qtbot, settings_path, monkeypatch):
    window = _plain_window(qtbot, settings_path)
    seen = _restart_app_fakes(monkeypatch, answer=True, child=_Child(None))
    busy = {"on": True}
    monkeypatch.setattr(window._restart_banner_local, "scope_busy", lambda: busy["on"])
    window._restart_app()
    assert len(seen["popen"]) == 1
    # The hold passes while a local action is busy: the window stays open and keeps polling.
    qtbot.wait(600)
    assert seen["quit"] == 0
    assert window._relaunch_timer.isActive()
    window._restart_app()  # a second press during the wait starts no second copy
    assert len(seen["popen"]) == 1
    busy["on"] = False
    qtbot.waitUntil(lambda: seen["quit"] == 1, timeout=3000)
    qtbot.wait(300)
    assert seen["quit"] == 1
    assert not window._relaunch_timer.isActive()
    assert len(seen["popen"]) == 1
    assert seen["warnings"] == []


def test_restart_app_keeps_the_window_open_when_the_new_copy_exits(qtbot, settings_path, monkeypatch):
    window = _plain_window(qtbot, settings_path)
    seen = _restart_app_fakes(monkeypatch, answer=True, child=_Child(1))
    window._restart_app()
    qtbot.waitUntil(lambda: bool(seen["warnings"]), timeout=3000)
    assert seen["warnings"] == [app_relaunch.EXITED_TEXT.format(1)]
    assert seen["quit"] == 0


def test_restart_app_warns_without_a_path_when_the_start_fails(qtbot, settings_path, monkeypatch):
    window = _plain_window(qtbot, settings_path)
    seen = _restart_app_fakes(monkeypatch, answer=True, failure=OSError("C:/secret/place"))
    window._restart_app()
    assert seen["warnings"] == [app_relaunch.FAILED_START_TEXT.format("OSError")]
    assert "secret" not in seen["warnings"][0]
    assert seen["quit"] == 0


def test_sign_in_from_bar_is_a_slot_and_restart_app_is_not():
    meta = MainWindow.staticMetaObject
    assert meta.indexOfMethod("_sign_in_from_bar()") >= 0
    assert meta.indexOfMethod("_restart_app()") < 0


def test_a_strip_restart_keeps_the_strip_disables_restart_app_and_refreshes_settings(qtbot, settings_path, tmp_path):
    gate = threading.Event()
    spawn = _GatedSpawn(gate)
    store = SettingsStore(settings_path)
    store.save(Settings(mode="local", core_checkout=str(tmp_path / "core"), core_python=sys.executable))
    # Yes to the stop, No to the start: the restart ends after its stop.
    actions = RunnerActions(store, spawn=spawn, log_path=tmp_path / "control.log", confirm=_AnswersInOrder(True, False))
    watch = _WindowWatch()
    watch.next = Restart(code=("core.py",), server=True)
    window = MainWindow(store, runner_actions=actions, restart_watch=watch)
    qtbot.addWidget(window)
    banner = window.findChild(RestartBanner)
    banner.check_now()
    restart_app = banner.findChild(QPushButton, "restart-banner-app")
    controls = window._restart_banner_local
    assert restart_app.isEnabled()

    banner.findChild(QPushButton, "restart-banner-local-restart").click()
    qtbot.waitUntil(lambda: "stop" in spawn.verbs(), timeout=3000)
    assert not restart_app.isEnabled()
    # The server record clears when the server stops, so the next check reports nothing. The strip stays.
    watch.next = Restart()
    banner.check_now()
    assert not banner.isHidden()
    assert not controls.isHidden()
    statuses_before = spawn.verbs().count("status")

    gate.set()
    qtbot.waitUntil(lambda: not controls.in_flight(), timeout=3000)
    assert restart_app.isEnabled()
    assert controls.findChild(QLabel, "restart-banner-local-control-result").text()
    # The Settings local state is read again after the strip's action, as after a Settings action.
    qtbot.waitUntil(lambda: spawn.verbs().count("status") > statuses_before, timeout=3000)
