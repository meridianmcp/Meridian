"""Tests for the hosted-tunnel launch switch (amendment G): the Settings switch and the saved flag.

The switch asks the owner before it turns on, names the exact tunnel command and the working folder claude.ai can
reach, and starts nothing when it is turned on. It is refused outside Hosted mode, without a confirmed core,
and while the tunnel buttons are off. No dialog opens for a refusal, no runner process starts, and the settings file
is a temp path.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from PySide6.QtWidgets import QCheckBox, QLabel

from meridian_desktop.launch_starts import TUNNEL_MODES
from meridian_desktop.runner_controls import RunnerControl, describe_command
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.runner_section import (
    LOCAL_NOT_SET_TEXT,
    SWITCH_OFF_TEXT,
    RunnerSection,
    TUNNEL_CANCELLED_TEXT,
    TUNNEL_CONTROLS_OFF_TEXT,
    TUNNEL_MODE_NOT_SET_TEXT,
    TUNNEL_ON_TEXT,
    TUNNEL_QUESTION,
)
PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"


class Spawn:
    """A fake for subprocess.run. The switch itself never spawns, so any call is recorded and answered as not running."""

    def __init__(self):
        self.calls: list[list] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        payload = json.dumps({"scope": "tunnel", "child": {"state": "not_started"}}).encode()
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=payload)


class Confirm:
    """A fake for the confirmation dialog. It answers in order and records each question."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.asked: list[tuple[str, str, str]] = []

    def __call__(self, parent, title, body, confirm_text):
        self.asked.append((title, body, confirm_text))
        return self._answers.pop(0) if self._answers else False


def _section(qtbot, settings_path, *, confirm):
    spawn = Spawn()
    actions = RunnerActions(
        SettingsStore(settings_path),
        make_control=lambda python, cwd: RunnerControl(python, cwd, spawn=spawn, log_path=None),
        confirm=confirm,
        find_zotero=lambda: None,
    )
    section = RunnerSection(actions)
    qtbot.addWidget(section)
    return section, spawn


def _box(section) -> QCheckBox:
    return section.findChild(QCheckBox, "settings-tunnel-autostart")


def _message(section) -> str:
    return section.findChild(QLabel, "runner-autostart-message").text()


def _saved(settings_path) -> Settings:
    return SettingsStore(settings_path).load().settings


def _core(settings_path, **overrides):
    values = dict(core_python=PY, core_checkout=CWD, mode="hosted")
    values.update(overrides)
    SettingsStore(settings_path).save(Settings(**values))


# ------------------------------------------------------------------ the saved flag


def test_the_tunnel_flag_is_off_by_default_and_round_trips_through_the_file(settings_path):
    assert Settings().tunnel_autostart is False

    SettingsStore(settings_path).save(Settings(tunnel_autostart=True))
    assert SettingsStore(settings_path).load().settings.tunnel_autostart is True
    assert json.loads(settings_path.read_text(encoding="utf-8"))["tunnel_autostart"] is True

    SettingsStore(settings_path).save(Settings())
    assert "tunnel_autostart" not in json.loads(settings_path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ the switch


def test_the_tunnel_switch_starts_off_and_building_the_block_asks_nothing(qtbot, settings_path):
    confirm = Confirm()
    section, spawn = _section(qtbot, settings_path, confirm=confirm)

    assert _box(section).isChecked() is False
    assert confirm.asked == []
    assert spawn.calls == []


@pytest.mark.parametrize("mode", ["local"])
def test_turning_the_tunnel_on_outside_hosted_mode_is_refused_without_a_dialog(qtbot, settings_path, mode):
    _core(settings_path, mode=mode)
    confirm = Confirm(True)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section).click()

    assert _box(section).isChecked() is False
    assert _message(section) == TUNNEL_MODE_NOT_SET_TEXT
    assert confirm.asked == []
    assert _saved(settings_path).tunnel_autostart is False


@pytest.mark.parametrize("mode", sorted(TUNNEL_MODES))
def test_turning_the_tunnel_on_names_the_exact_command_and_the_folder_claude_ai_can_reach(qtbot, settings_path, mode):
    _core(settings_path, mode=mode)
    confirm = Confirm(True)
    section, spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section).click()

    title, body, _confirm_text = confirm.asked[0]
    assert title == TUNNEL_QUESTION
    assert describe_command(PY, "start", "tunnel") in body
    assert "Scope: tunnel" in body
    assert f"Working folder: {CWD}" in body
    assert "claude.ai can reach the working folder" in body
    saved = _saved(settings_path)
    assert saved.tunnel_autostart is True
    assert saved.confirmed_core_python == PY
    assert saved.confirmed_core_checkout == CWD
    assert spawn.calls == []  # turning the switch on starts nothing: the start happens at launch, not here
    assert _message(section) == TUNNEL_ON_TEXT


def test_cancelling_the_tunnel_switch_reverts_it_and_saves_nothing(qtbot, settings_path):
    _core(settings_path)
    confirm = Confirm(False)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section).click()

    assert _box(section).isChecked() is False
    assert _saved(settings_path).tunnel_autostart is False
    assert _message(section) == TUNNEL_CANCELLED_TEXT


def test_the_tunnel_switch_is_refused_while_the_tunnel_buttons_are_off(qtbot, settings_path):
    _core(settings_path, tunnel_control_enabled=False)
    confirm = Confirm(True)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section).click()

    assert _box(section).isChecked() is False
    assert _message(section) == TUNNEL_CONTROLS_OFF_TEXT
    assert confirm.asked == []
    assert _saved(settings_path).tunnel_autostart is False


def test_turning_the_tunnel_on_without_the_core_is_refused_without_a_dialog(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(mode="hosted"))
    confirm = Confirm(True)
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)

    _box(section).click()

    assert _box(section).isChecked() is False
    assert _message(section) == LOCAL_NOT_SET_TEXT
    assert confirm.asked == []


def test_turning_the_tunnel_off_saves_at_once_and_asks_nothing(qtbot, settings_path):
    _core(settings_path, tunnel_autostart=True)
    confirm = Confirm()
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)
    assert _box(section).isChecked() is True

    _box(section).click()

    assert _box(section).isChecked() is False
    assert _saved(settings_path).tunnel_autostart is False
    assert _message(section) == SWITCH_OFF_TEXT
    assert confirm.asked == []


def test_refresh_flags_shows_the_saved_tunnel_switch_without_asking(qtbot, settings_path):
    confirm = Confirm()
    section, _spawn = _section(qtbot, settings_path, confirm=confirm)
    SettingsStore(settings_path).save(Settings(tunnel_autostart=True))

    section.refresh_flags()

    assert _box(section).isChecked() is True
    assert confirm.asked == []
