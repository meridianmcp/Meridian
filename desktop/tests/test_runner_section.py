"""Tests for the Settings tab's runner block (meridian_desktop/ui/runner_section.py): the two switches and the local state.

The switches ask the owner before they turn on, and Cancel leaves them off. The local state is read through a fake
spawn when the block is shown. No dialog opens, no runner process starts, no Zotero is found on this computer, and
the settings file is a temp path.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

from meridian_desktop.core_locator import FOUND, NOT_FOUND, SOURCE_SEARCHED, LocatedCore
from meridian_desktop.runner_controls import RunnerControl, describe_command
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.runner_actions import CHOOSE_CANCELLED_TEXT, RunnerActions
from meridian_desktop.ui.runner_section import (
    CHECKING_TEXT,
    LOCAL_CANCELLED_TEXT,
    NO_CORE_TEXT,
    NOT_CHECKED_TEXT,
    RunnerSection,
    ZOTERO_CANCELLED_TEXT,
    ZOTERO_NOT_INSTALLED_TEXT,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT
from meridian_desktop.zotero_client import command_line

PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"
EXE = "C:/Program Files/Zotero/zotero.exe"


class Spawn:
    """A fake for subprocess.run. It returns its canned replies in order, or raises a canned error."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.calls: list[list] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        reply = self._replies.pop(0) if self._replies else subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=json.dumps({"scope": "local", "child": {"state": "not_started"}}).encode(),
        )
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Confirm:
    """A fake for the confirmation dialog. It answers in order and records each question."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, parent, title, body, confirm_text):
        self.asked.append((title, body, confirm_text))
        return self._answers.pop(0) if self._answers else False


def _section(qtbot, settings_path, *, confirm, spawn=None, find_zotero=lambda: EXE, log=None):
    spawn = spawn if spawn is not None else Spawn()
    actions = RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python, cwd: RunnerControl(python, cwd, spawn=spawn, log_path=log, clock=lambda: _when()),
        confirm=confirm,
        find_zotero=find_zotero,
    )
    section = RunnerSection(actions)
    qtbot.addWidget(section)
    return section, spawn


def _when():
    return dt.datetime(2026, 10, 22, 17, 0, 0, tzinfo=dt.timezone.utc)


def _box(section, name) -> QCheckBox:
    return section.findChild(QCheckBox, name)


def _message(section) -> str:
    return section.findChild(QLabel, "runner-autostart-message").text()


def _saved(settings_path) -> Settings:
    return SettingsStore(settings_path).load().settings


def _core(settings_path, **overrides):
    values = dict(core_python=PY, core_checkout=CWD)
    values.update(overrides)
    SettingsStore(settings_path).save(Settings(**values))


# ------------------------------------------------------------------ the switches


def test_both_switches_start_off_and_building_the_block_asks_nothing(qtbot, settings_path):
    confirm = Confirm()
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    assert _box(section, "settings-zotero-autostart").isChecked() is False
    assert _box(section, "settings-local-autostart").isChecked() is False
    assert confirm.asked == []


def test_turning_zotero_on_shows_the_command_and_cancel_leaves_it_off(qtbot, settings_path):
    confirm = Confirm(False)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section, "settings-zotero-autostart").click()

    assert len(confirm.asked) == 1
    title, body, _confirm_text = confirm.asked[0]
    assert title == "Start Zotero when the desktop opens?"
    assert command_line(EXE) in body
    assert f"Working folder: {str(Path(EXE).parent)}" in body
    assert "127.0.0.1:23119" in body
    assert _box(section, "settings-zotero-autostart").isChecked() is False
    assert _saved(settings_path).zotero_autostart is False
    assert _message(section) == ZOTERO_CANCELLED_TEXT


def test_confirming_zotero_on_saves_it_and_keeps_the_box_checked(qtbot, settings_path):
    confirm = Confirm(True)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section, "settings-zotero-autostart").click()

    assert _box(section, "settings-zotero-autostart").isChecked() is True
    assert _saved(settings_path).zotero_autostart is True
    assert "Saved. Zotero will start" in _message(section)


def test_zotero_that_is_not_installed_is_refused_without_a_dialog(qtbot, settings_path):
    confirm = Confirm(True)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm, find_zotero=lambda: None)

    _box(section, "settings-zotero-autostart").click()

    assert confirm.asked == []
    assert _box(section, "settings-zotero-autostart").isChecked() is False
    assert _message(section) == ZOTERO_NOT_INSTALLED_TEXT
    assert _saved(settings_path).zotero_autostart is False


def test_turning_zotero_off_saves_at_once_and_asks_nothing(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(zotero_autostart=True))
    confirm = Confirm()
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)
    assert _box(section, "settings-zotero-autostart").isChecked() is True

    _box(section, "settings-zotero-autostart").click()

    assert confirm.asked == []
    assert _box(section, "settings-zotero-autostart").isChecked() is False
    assert _saved(settings_path).zotero_autostart is False


def test_turning_the_local_server_on_without_the_core_is_refused_without_a_dialog(qtbot, settings_path):
    confirm = Confirm(True)
    section, spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section, "settings-local-autostart").click()

    assert confirm.asked == []
    assert spawn.calls == []
    assert _box(section, "settings-local-autostart").isChecked() is False
    assert _saved(settings_path).local_server_autostart is False


def test_turning_the_local_server_on_names_the_exact_start_command(qtbot, settings_path):
    _core(settings_path)
    confirm = Confirm(True)
    section, spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section, "settings-local-autostart").click()

    title, body, _confirm_text = confirm.asked[0]
    assert title == "Start the local server when the desktop opens?"
    assert describe_command(PY, "start", "local") in body
    assert "Scope: local" in body
    assert f"Working folder: {CWD}" in body
    assert _saved(settings_path).local_server_autostart is True
    assert spawn.calls == []  # turning the switch on starts nothing: the start happens at launch, not here


def test_cancelling_the_local_server_switch_reverts_it_and_saves_nothing(qtbot, settings_path):
    _core(settings_path)
    confirm = Confirm(False)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section, "settings-local-autostart").click()

    assert _box(section, "settings-local-autostart").isChecked() is False
    assert _saved(settings_path).local_server_autostart is False
    assert _message(section) == LOCAL_CANCELLED_TEXT


def test_refresh_flags_shows_the_saved_switches_without_asking(qtbot, settings_path):
    confirm = Confirm()
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)
    SettingsStore(settings_path).save(Settings(zotero_autostart=True, local_server_autostart=True))

    section.refresh_flags()

    assert _box(section, "settings-zotero-autostart").isChecked() is True
    assert _box(section, "settings-local-autostart").isChecked() is True
    assert confirm.asked == []


# ------------------------------------------------------------------ the local state


def test_the_local_state_is_read_when_the_block_is_shown_and_shows_the_runner_answer(qtbot, settings_path):
    _core(settings_path)
    spawn = Spawn(subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"scope": "local", "child": {"state": "running", "pid": 3}}).encode(),
    ))
    section, _spawn = _section(qtbot, settings_path, confirm=Confirm(), spawn=spawn)
    state = section.findChild(QLabel, "runner-local-state")
    assert state.text() == NOT_CHECKED_TEXT

    section.show()
    qtbot.waitUntil(lambda: state.text() not in (NOT_CHECKED_TEXT, CHECKING_TEXT), timeout=5000)

    assert state.text() == "The local server is running."
    assert len(spawn.calls) == 1
    assert spawn.calls[0][3] == "status"


def test_without_the_core_the_local_state_is_not_read_and_no_process_starts(qtbot, settings_path):
    spawn = Spawn()
    section, _spawn = _section(qtbot, settings_path, confirm=Confirm(), spawn=spawn)

    section.show()

    assert section.findChild(QLabel, "runner-local-state").text() == NO_CORE_TEXT
    assert spawn.calls == []


def test_an_unknown_child_state_is_never_shown_as_running(qtbot, settings_path):
    _core(settings_path)
    spawn = Spawn(subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"scope": "local", "child": {"state": "unknown"}}).encode(),
    ))
    section, _spawn = _section(qtbot, settings_path, confirm=Confirm(), spawn=spawn)
    state = section.findChild(QLabel, "runner-local-state")

    section.show()
    qtbot.waitUntil(lambda: state.text() not in (NOT_CHECKED_TEXT, CHECKING_TEXT), timeout=5000)

    assert "unknown" in state.text()
    assert "The local server is running" not in state.text()


def test_a_status_read_that_times_out_is_not_shown_as_running(qtbot, settings_path):
    _core(settings_path)
    spawn = Spawn(subprocess.TimeoutExpired(cmd="x", timeout=10))
    section, _spawn = _section(qtbot, settings_path, confirm=Confirm(), spawn=spawn)
    state = section.findChild(QLabel, "runner-local-state")

    section.show()
    qtbot.waitUntil(lambda: state.text() not in (NOT_CHECKED_TEXT, CHECKING_TEXT), timeout=5000)

    assert state.text().startswith("Not confirmed")
    assert "is running" not in state.text()


def test_the_runner_block_labels_are_selectable(qtbot, settings_path):
    section, _spawn = _section(qtbot, settings_path, confirm=Confirm())

    for label in section.findChildren(QLabel):
        assert label.textInteractionFlags() == SELECTABLE_TEXT, label.objectName()


# ------------------------------------------------------------------ the confirmed values (contract amendments A and B)


def test_confirming_zotero_on_records_the_exact_program_the_dialog_named(qtbot, settings_path):
    confirm = Confirm(True)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section, "settings-zotero-autostart").click()

    body = confirm.asked[0][1]
    assert command_line(EXE) in body
    saved = _saved(settings_path)
    assert saved.confirmed_zotero_exe == EXE
    assert saved.zotero_autostart is True


def test_confirming_the_local_server_on_records_the_python_and_folder_the_dialog_showed(qtbot, settings_path):
    _core(settings_path)
    confirm = Confirm(True)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section, "settings-local-autostart").click()

    body = confirm.asked[0][1]
    assert describe_command(PY, "start", "local") in body
    saved = _saved(settings_path)
    assert saved.confirmed_core_python == PY
    assert saved.confirmed_core_checkout == CWD
    assert saved.local_server_autostart is True


def test_cancelling_either_switch_records_no_confirmed_value(qtbot, settings_path):
    _core(settings_path)
    confirm = Confirm(False, False)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section, "settings-zotero-autostart").click()
    _box(section, "settings-local-autostart").click()

    saved = _saved(settings_path)
    assert saved.confirmed_zotero_exe == ""
    assert saved.confirmed_core_python == ""
    assert saved.confirmed_core_checkout == ""


# ------------------------------------------------------------------ the local block follows the core search


def _core_actions(settings_path, *, locate, choose_folder=lambda parent, start: ""):
    """RunnerActions with a fake core search and folder chooser. Nothing starts a process or opens a dialog."""
    return RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python, cwd: RunnerControl(
            python, cwd, spawn=Spawn(), log_path=settings_path.parent / "log.txt", clock=lambda: _when(),
        ),
        confirm=Confirm(),
        locate=locate,
        choose_folder=choose_folder,
    )


def test_the_local_block_offers_the_choose_button_while_the_core_is_not_found_and_says_why(qtbot, settings_path):
    actions = _core_actions(settings_path, locate=lambda store: LocatedCore(NOT_FOUND, reason="No folder."))
    section = RunnerSection(actions)
    qtbot.addWidget(section)

    actions.find_core()
    qtbot.waitUntil(lambda: actions.located is not None, timeout=5000)

    assert section.findChild(QPushButton, "local-start").isEnabled() is False
    fix = section.findChild(QPushButton, "local-fix")
    assert fix.isVisibleTo(section) and fix.text() == "Choose the Meridian core folder"
    assert "not found" in section.findChild(QLabel, "local-control-note").text()


def test_the_local_block_turns_on_when_the_search_finds_the_core(qtbot, settings_path, tmp_path):
    checkout = tmp_path / "Meridian" / "repository"
    python = tmp_path / "python.exe"
    python.write_bytes(b"MZ")
    actions = _core_actions(
        settings_path,
        locate=lambda store: LocatedCore(FOUND, str(checkout), str(python), SOURCE_SEARCHED),
    )
    section = RunnerSection(actions)
    qtbot.addWidget(section)
    assert section.findChild(QPushButton, "local-start").isEnabled() is False

    actions.find_core()

    qtbot.waitUntil(lambda: section.findChild(QPushButton, "local-start").isEnabled(), timeout=5000)
    assert SettingsStore(settings_path).load().settings.core_checkout == str(checkout)


def test_a_search_at_start_puts_the_local_answer_on_the_line_while_settings_is_hidden(qtbot, settings_path, tmp_path):
    """The search at start finishes while Settings is hidden. The local line still shows the runner's answer, not the default."""
    checkout = tmp_path / "Meridian" / "repository"
    python = tmp_path / "python.exe"
    python.write_bytes(b"MZ")
    spawn = Spawn(
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"scope": "local", "child": {"state": "running", "pid": 3}}).encode(),
        ),
    )
    actions = RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python_, cwd: RunnerControl(
            python_, cwd, spawn=spawn, log_path=settings_path.parent / "log.txt", clock=lambda: _when(),
        ),
        confirm=Confirm(),
        locate=lambda store: LocatedCore(FOUND, str(checkout), str(python), SOURCE_SEARCHED),
    )
    section = RunnerSection(actions)
    qtbot.addWidget(section)
    state = section.findChild(QLabel, "runner-local-state")
    assert not section.isVisible()

    actions.find_core()

    qtbot.waitUntil(lambda: state.text() not in (NOT_CHECKED_TEXT, CHECKING_TEXT), timeout=5000)
    assert state.text() == "The local server is running."
    assert len(spawn.calls) == 1 and spawn.calls[0][3] == "status"


def test_a_cancelled_choice_in_the_local_block_leaves_the_controls_off(qtbot, settings_path):
    actions = _core_actions(
        settings_path,
        locate=lambda store: LocatedCore(NOT_FOUND, reason="No folder."),
        choose_folder=lambda parent, start: "",
    )
    section = RunnerSection(actions)
    qtbot.addWidget(section)

    qtbot.mouseClick(section.findChild(QPushButton, "local-fix"), Qt.MouseButton.LeftButton)

    assert section.findChild(QPushButton, "local-start").isEnabled() is False
    assert section.findChild(QLabel, "local-control-result").text() == CHOOSE_CANCELLED_TEXT
