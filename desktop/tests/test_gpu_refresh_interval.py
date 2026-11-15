"""The GPUs tab's Auto refresh: the saved choice, Off, the interval the timer runs at, and the settings file.

No real command runs: the panel gets the recording runner from test_gpu_jobs_panel.
"""

import json

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QLabel

from meridian_desktop.settings_store import DEFAULT_GPU_REFRESH_SECONDS, SettingsStore
from meridian_desktop.ui.gpu_jobs_panel import INTERVAL_NOT_SAVED_TEXT, GpuJobsPanel

from test_gpu_jobs_panel import RUNPOD, RecordingRunner, wait_checked


def _panel(qtbot, **kwargs):
    panel = GpuJobsPanel(lambda: (), lambda: RUNPOD, run=RecordingRunner(), **kwargs)
    qtbot.addWidget(panel)
    return panel


def _combo(panel):
    return panel.findChild(QComboBox, "gpu-refresh-interval")


def test_the_default_interval_is_five_minutes(qtbot):
    panel = _panel(qtbot)
    assert DEFAULT_GPU_REFRESH_SECONDS == 300
    assert panel.findChild(QTimer).interval() == 300_000
    assert _combo(panel).currentData() == 300


def test_off_stops_the_timer_and_it_never_runs_while_visible(qtbot):
    panel = _panel(qtbot)
    timer = panel.findChild(QTimer)
    _combo(panel).setCurrentIndex(_combo(panel).findData(0))
    panel.show()
    qtbot.waitExposed(panel)
    assert timer.isActive() is False
    wait_checked(qtbot, panel)  # the read on first show still happens, as it always did
    panel.hide()


def test_a_chosen_interval_sets_the_timer_and_runs_while_visible(qtbot):
    saved = []
    panel = _panel(qtbot, on_refresh_changed=lambda seconds: saved.append(seconds) or True)
    _combo(panel).setCurrentIndex(_combo(panel).findData(60))
    assert saved == [60]
    timer = panel.findChild(QTimer)
    assert timer.interval() == 60_000
    panel.show()
    qtbot.waitExposed(panel)
    assert timer.isActive() is True
    panel.hide()
    assert timer.isActive() is False


def test_a_choice_that_cannot_be_saved_says_it_holds_for_this_session_only(qtbot):
    panel = _panel(qtbot, on_refresh_changed=lambda seconds: False)
    _combo(panel).setCurrentIndex(_combo(panel).findData(900))
    assert panel.findChild(QLabel, "gpu-refresh-note").text() == INTERVAL_NOT_SAVED_TEXT


def test_the_saved_choice_is_used_when_the_tab_is_built(qtbot):
    panel = _panel(qtbot, refresh_seconds=900)
    assert panel.findChild(QTimer).interval() == 900_000
    assert _combo(panel).currentData() == 900


def test_an_unlisted_saved_choice_falls_back_to_five_minutes(qtbot):
    panel = _panel(qtbot, refresh_seconds=5)
    assert panel.findChild(QTimer).interval() == 300_000
    assert _combo(panel).currentData() == 300


def test_settings_default_to_five_minutes_keep_a_listed_choice_and_write_no_key_for_the_default(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.load().settings.gpu_refresh_seconds == 300
    store.set_gpu_refresh_seconds(60)
    assert SettingsStore(path).load().settings.gpu_refresh_seconds == 60
    store.set_gpu_refresh_seconds(300)
    assert "gpu_refresh_seconds" not in json.loads(path.read_text(encoding="utf-8"))


def test_an_unlisted_interval_is_refused_and_a_bad_file_value_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    with pytest.raises(ValueError):
        store.set_gpu_refresh_seconds(5)
    path.write_text(json.dumps({"gpu_refresh_seconds": 5}), encoding="utf-8")
    assert SettingsStore(path).load().settings.gpu_refresh_seconds == 300
