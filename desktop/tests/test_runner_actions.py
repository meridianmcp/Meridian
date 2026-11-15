"""Tests for meridian_desktop/ui/runner_actions.py: the confirmation dialog, RunnerActions, and the launch-time starter.

Offscreen Qt and pytest-qt, as in the other desktop tests. Every outside call is a fake: the runner, Zotero, the
local URL probe, the dialog and the settings file. No dialog is shown, no process starts and no socket opens.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from meridian_desktop.local_client import HEALTH_NOT_RUNNING
from meridian_desktop.runner_controls import CONFIRMED, ControlResult, RunnerControl
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tunnel_status import NOT_RUNNING
from meridian_desktop.ui import runner_actions
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.runner_actions import CoreSetup, LaunchStarts, RunnerActions, control_box, submit
from meridian_desktop.ui.text import SELECTABLE_TEXT
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck

PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"
EXE = "C:/Program Files/Zotero/zotero.exe"


def _no_confirm(parent, title, body, confirm_text):
    raise AssertionError("no confirmation may open in this test")


def _actions(settings_path, **overrides):
    kwargs = dict(confirm=_no_confirm)
    kwargs.update(overrides)
    return RunnerActions(SettingsStore(settings_path), **kwargs)


# ------------------------------------------------------------------ the confirmation dialog


def test_the_dialog_names_the_exact_command_the_scope_and_the_folder(qtbot):
    body = f"The desktop will run this command:\n\n{PY} -m meridian.local_runner stop --scope local\n\nScope: local\nWorking folder: {CWD}"

    box, _confirm = control_box(None, "Stop the local server?", body, "Stop")
    qtbot.addWidget(box)

    assert box.text() == "Stop the local server?"
    assert "meridian.local_runner stop --scope local" in box.informativeText()
    assert "Scope: local" in box.informativeText()
    assert f"Working folder: {CWD}" in box.informativeText()


def test_cancel_is_the_default_and_the_escape_button_so_enter_never_confirms(qtbot):
    box, confirm = control_box(None, "Start the tunnel?", "body", "Start")
    qtbot.addWidget(box)

    cancel = box.defaultButton()
    assert cancel is not confirm
    assert box.escapeButton() is cancel
    assert confirm.text() == "Start"


def test_the_dialog_text_is_plain_and_its_labels_are_selectable(qtbot):
    box, _confirm = control_box(None, "<b>not html</b>", "<i>x</i>", "Start")
    qtbot.addWidget(box)

    assert box.textFormat() == Qt.TextFormat.PlainText
    labels = box.findChildren(QLabel)
    assert labels
    for label in labels:
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.textInteractionFlags() == SELECTABLE_TEXT


class _FakeBox:
    """A stand-in for the dialog: it reports which button a test chose, and never opens a window."""

    def __init__(self, chosen):
        self._chosen = chosen

    def exec(self):
        return 0

    def clickedButton(self):
        return self._chosen


def test_only_the_confirm_button_returns_true_and_cancel_changes_nothing(monkeypatch):
    confirm, cancel = object(), object()

    monkeypatch.setattr(runner_actions, "control_box", lambda *a: (_FakeBox(confirm), confirm))
    assert runner_actions.ask_control(None, "t", "b", "Start") is True

    monkeypatch.setattr(runner_actions, "control_box", lambda *a: (_FakeBox(cancel), confirm))
    assert runner_actions.ask_control(None, "t", "b", "Start") is False

    monkeypatch.setattr(runner_actions, "control_box", lambda *a: (_FakeBox(None), confirm))
    assert runner_actions.ask_control(None, "t", "b", "Start") is False  # closed without a choice


# ------------------------------------------------------------------ RunnerActions


def test_core_reads_the_saved_setup_and_the_three_switches(settings_path):
    SettingsStore(settings_path).save(
        Settings(
            core_python=PY, core_checkout=CWD, local_url="http://127.0.0.1:7999",
            tunnel_control_enabled=True, zotero_autostart=True, local_server_autostart=False,
        )
    )

    core = _actions(settings_path).core()

    assert core == CoreSetup(
        python=PY, cwd=CWD, local_url="http://127.0.0.1:7999",
        tunnel_control_enabled=True, zotero_autostart=True, local_server_autostart=False,
    )
    assert core.core_ready is True


def test_core_is_not_ready_when_either_path_is_blank(settings_path):
    core = _actions(settings_path).core()

    assert core.core_ready is False
    assert core.tunnel_control_enabled is True  # the default is on (contract amendment F)


def test_set_flag_saves_one_switch_and_refuses_an_unknown_name(settings_path):
    actions = _actions(settings_path)

    actions.set_flag("zotero_autostart", True)

    assert actions.core().zotero_autostart is True
    with pytest.raises(ValueError):
        actions.set_flag("core_python", "x")


def test_the_default_control_factory_builds_a_runner_control_with_the_given_paths(settings_path, tmp_path):
    actions = _actions(settings_path, log_path=tmp_path / "log.txt")

    control = actions.control(PY, CWD)

    assert isinstance(control, RunnerControl)


def test_the_launch_deps_probe_the_saved_local_url(settings_path):
    seen = []

    def probe(url):
        seen.append(url)
        return HEALTH_NOT_RUNNING

    actions = _actions(settings_path, probe_local=probe)
    core = CoreSetup(
        PY, CWD, "http://127.0.0.1:7999", False, True, False, confirmed_python=PY, confirmed_cwd=CWD,
    )
    deps = actions.launch_deps(core)

    assert deps.local_health() == HEALTH_NOT_RUNNING
    assert seen == ["http://127.0.0.1:7999"]
    assert deps.core_ready is True


# ------------------------------------------------------------------ the shared job and its delivery


def test_submit_delivers_the_value_with_the_owner_and_generation(qtbot):
    owner = object()
    got = []

    def listener(item):
        if isinstance(item, Delivered) and item.owner is owner:
            got.append(item.value)

    shared_delivery().delivered.connect(listener)
    try:
        submit(owner, 7, lambda: "done")
        qtbot.waitUntil(lambda: bool(got), timeout=5000)
    finally:
        shared_delivery().delivered.disconnect(listener)

    assert got == [(7, ("done", None))]


def test_submit_reduces_a_raised_exception_to_its_class_name(qtbot):
    owner = object()
    got = []

    def listener(item):
        if isinstance(item, Delivered) and item.owner is owner:
            got.append(item.value)

    def boom():
        raise ValueError("secret detail that must not be shown")

    shared_delivery().delivered.connect(listener)
    try:
        submit(owner, 1, boom)
        qtbot.waitUntil(lambda: bool(got), timeout=5000)
    finally:
        shared_delivery().delivered.disconnect(listener)

    assert got == [(1, (None, "ValueError"))]


# ------------------------------------------------------------------ the launch-time starter


class _Reports:
    def __init__(self):
        self.texts: list[str] = []

    def __call__(self, text: str) -> None:
        self.texts.append(text)


def _launch_actions(settings_path, *, starts, probe=API_NOT_RUNNING, made=None, find_zotero=lambda: EXE):
    """Actions whose launch calls are fakes that record into ``starts``. Zotero is found and not running.

    ``made`` collects the (python, folder) each runner control is built with, so a test can see which setup a start used.
    """

    def start_zotero(exe, *, log_path=None):
        starts.append(("zotero", exe, log_path))
        return "Zotero was started"

    def make_control(python, cwd):
        if made is not None:
            made.append((python, cwd))
        return _StatusFake(starts)

    return _actions(
        settings_path,
        find_zotero=find_zotero,
        probe_zotero=lambda: ApiCheck(probe, "message"),
        start_zotero=start_zotero,
        probe_local=lambda url: HEALTH_NOT_RUNNING,
        make_control=make_control,
    )


def _no_spawn(*args, **kwargs):
    raise AssertionError("no runner process may start in this test")


class _StatusFake(RunnerControl):
    """A RunnerControl whose local start and status are fakes. It records the start, and never spawns a process."""

    def __init__(self, starts):
        super().__init__(PY, CWD, spawn=_no_spawn)
        self._starts = starts

    def status(self, scope):
        return ControlResult("status", scope, CONFIRMED, "The local server is not running.", NOT_RUNNING)

    def start(self, scope):
        self._starts.append(("local", scope))
        return ControlResult("start", scope, CONFIRMED, "started", "running", 5)


def test_with_both_switches_off_the_launch_starts_nothing(qtbot, settings_path):
    starts, reports = [], _Reports()
    launch = LaunchStarts(_launch_actions(settings_path, starts=starts), report=reports)

    launch.start()
    qtbot.wait(50)

    assert starts == []
    assert reports.texts == []


def test_with_zotero_on_it_starts_once_and_reports_the_result(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(zotero_autostart=True, confirmed_zotero_exe=EXE))
    starts, reports = [], _Reports()
    launch = LaunchStarts(_launch_actions(settings_path, starts=starts), report=reports)

    launch.start()
    qtbot.waitUntil(lambda: bool(reports.texts), timeout=5000)

    assert [s[0] for s in starts] == ["zotero"]
    assert "Zotero was started once, at launch." in reports.texts[0]


def test_a_second_call_to_start_does_not_run_the_launch_again(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(zotero_autostart=True, confirmed_zotero_exe=EXE))
    starts, reports = [], _Reports()
    launch = LaunchStarts(_launch_actions(settings_path, starts=starts), report=reports)

    launch.start()
    qtbot.waitUntil(lambda: bool(reports.texts), timeout=5000)
    launch.start()
    qtbot.wait(50)

    assert len(starts) == 1


def test_a_launch_result_with_a_foreign_owner_is_not_reported(qtbot, settings_path):
    reports = _Reports()
    LaunchStarts(_actions(settings_path), report=reports)

    shared_delivery().delivered.emit(Delivered(object(), (1, ((), None))))
    qtbot.wait(50)

    assert reports.texts == []


def test_the_local_start_at_launch_runs_only_when_its_switch_is_on(qtbot, settings_path):
    SettingsStore(settings_path).save(
        Settings(
            local_server_autostart=True, core_python=PY, core_checkout=CWD,
            confirmed_core_python=PY, confirmed_core_checkout=CWD,
        )
    )
    starts, reports = [], _Reports()
    launch = LaunchStarts(_launch_actions(settings_path, starts=starts), report=reports)

    launch.start()
    qtbot.waitUntil(lambda: bool(reports.texts), timeout=5000)

    assert ("local", "local") in starts
    assert "The local server was started once, at launch." in reports.texts[0]


# ------------------------------------------------------------------ the confirmed values (contract amendments A and B)

OTHER_PY = "C:/Users/tester/other-env/python.exe"
OTHER_CWD = "C:/Users/tester/Documents/Meridian/other-checkout"
OTHER_EXE = "C:/Users/tester/AppData/Local/Programs/Zotero/zotero.exe"


def test_a_switch_confirmed_with_values_stores_them_in_the_same_save(settings_path):
    actions = _actions(settings_path)

    actions.set_flag("zotero_autostart", True, confirmed={"confirmed_zotero_exe": EXE})

    saved = SettingsStore(settings_path).load().settings
    assert saved.zotero_autostart is True
    assert saved.confirmed_zotero_exe == EXE


def test_set_flag_refuses_a_confirmed_key_it_does_not_know_and_writes_nothing(settings_path):
    actions = _actions(settings_path)

    with pytest.raises(ValueError):
        actions.set_flag("local_server_autostart", True, confirmed={"core_python": PY})

    assert not settings_path.exists()


def test_the_core_setup_reads_the_confirmed_values_apart_from_the_current_ones(settings_path):
    SettingsStore(settings_path).save(
        Settings(core_python=OTHER_PY, core_checkout=OTHER_CWD, confirmed_core_python=PY, confirmed_core_checkout=CWD)
    )

    core = _actions(settings_path).core()

    assert (core.python, core.cwd) == (OTHER_PY, OTHER_CWD)
    assert (core.confirmed_python, core.confirmed_cwd) == (PY, CWD)
    assert core.launch_ready is True


def test_the_launch_is_not_ready_without_a_confirmed_setup_even_when_the_setup_is_set(settings_path):
    SettingsStore(settings_path).save(Settings(core_python=PY, core_checkout=CWD))

    core = _actions(settings_path).core()

    assert core.core_ready is True
    assert core.launch_ready is False


def test_a_launch_start_uses_the_stored_python_and_checkout_not_the_current_ones(qtbot, settings_path):
    SettingsStore(settings_path).save(
        Settings(
            local_server_autostart=True,
            core_python=OTHER_PY,
            core_checkout=OTHER_CWD,
            confirmed_core_python=PY,
            confirmed_core_checkout=CWD,
        )
    )
    starts, reports, made = [], _Reports(), []
    launch = LaunchStarts(_launch_actions(settings_path, starts=starts, made=made), report=reports)

    launch.start()
    qtbot.waitUntil(lambda: bool(reports.texts), timeout=5000)

    assert (PY, CWD) in made
    assert all(pair != (OTHER_PY, OTHER_CWD) for pair in made)
    assert ("local", "local") in starts
    assert "The local server was started once, at launch." in reports.texts[0]


def test_a_local_start_with_no_confirmed_setup_is_skipped_with_a_plain_line(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(local_server_autostart=True, core_python=PY, core_checkout=CWD))
    starts, reports = [], _Reports()
    launch = LaunchStarts(_launch_actions(settings_path, starts=starts), report=reports)

    launch.start()
    qtbot.waitUntil(lambda: bool(reports.texts), timeout=5000)

    assert starts == []
    assert "not confirmed in Settings" in reports.texts[0]


def test_a_zotero_launch_start_runs_only_the_confirmed_program_and_never_searches_for_one(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(zotero_autostart=True, confirmed_zotero_exe=EXE))
    searched = []

    def find_other():
        searched.append(OTHER_EXE)
        return OTHER_EXE

    starts, reports = [], _Reports()
    launch = LaunchStarts(_launch_actions(settings_path, starts=starts, find_zotero=find_other), report=reports)

    launch.start()
    qtbot.waitUntil(lambda: bool(reports.texts), timeout=5000)

    assert [start[:2] for start in starts] == [("zotero", EXE)]
    assert searched == []


def test_a_zotero_launch_start_with_no_confirmed_program_is_skipped_with_a_plain_line(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(zotero_autostart=True))
    starts, reports = [], _Reports()
    launch = LaunchStarts(_launch_actions(settings_path, starts=starts, find_zotero=lambda: EXE), report=reports)

    launch.start()
    qtbot.waitUntil(lambda: bool(reports.texts), timeout=5000)

    assert starts == []
    assert "its program path was not confirmed in Settings" in reports.texts[0]
