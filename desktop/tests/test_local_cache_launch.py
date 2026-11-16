"""Tests for the launch-time local cache sync (ui/app.py: _sync_local_cache, and where main calls it).

The Qt application, window, tray and timer are stubbed, as test_desktop_log.py does, so main returns instead of running
the event loop. Every settings file is in pytest's temp dir. The sync itself is a spy, so no client config is written.
"""

from __future__ import annotations

import json
import logging

import pytest

from meridian_desktop import local_cache
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui import app as ui_app


class _FakeQApp:
    @staticmethod
    def instance():
        return None

    def __init__(self, *args):
        pass

    def setApplicationName(self, name):  # noqa: N802 - Qt naming
        pass

    def setQuitOnLastWindowClosed(self, flag):  # noqa: N802 - Qt naming
        pass

    def exec(self):
        return 0


class _FakeWindow:
    def __init__(self, *args, **kwargs):
        pass

    def show(self):
        pass

    def isVisible(self):  # noqa: N802 - Qt naming
        return True

    def show_launch_notes(self, text):
        pass


class _FakeTray:
    def __init__(self, *args):
        pass

    def show(self):
        pass


class _FakeTimer:
    @staticmethod
    def singleShot(ms, callback):  # noqa: N802 - Qt naming
        pass


@pytest.fixture
def stubbed_qt(monkeypatch):
    monkeypatch.setattr(ui_app, "QApplication", _FakeQApp)
    monkeypatch.setattr(ui_app, "MainWindow", _FakeWindow)
    monkeypatch.setattr(ui_app, "MeridianTray", _FakeTray)
    monkeypatch.setattr(ui_app, "QTimer", _FakeTimer)
    monkeypatch.setattr(ui_app, "start_desktop_log", lambda *args, **kwargs: None)


@pytest.fixture
def syncs(monkeypatch, stubbed_qt):
    """Record each launch-time sync instead of running it."""
    calls = []
    monkeypatch.setattr(ui_app, "_sync_local_cache", lambda store: calls.append(store))
    return calls


@pytest.fixture
def sync_spy(monkeypatch):
    """Record each call to the real sync, and answer with one line, so no client config is touched."""
    calls = []

    def fake_sync(**kwargs):
        calls.append(kwargs)
        return ["a line"]

    monkeypatch.setattr(local_cache, "sync_local_cache", fake_sync)
    return calls


def test_a_real_start_runs_the_local_cache_sync(syncs, tmp_path):
    assert ui_app.main([], settings_path=tmp_path / "settings.json", first_run=False) == 0
    assert len(syncs) == 1


def test_a_smoke_run_never_runs_the_local_cache_sync(syncs, tmp_path, capsys):
    assert ui_app.main(["--smoke"], settings_path=tmp_path / "settings.json", first_run=False) == 0
    assert syncs == []
    assert "smoke ok" in capsys.readouterr().out


def test_the_sync_uses_the_saved_disabled_tools_and_core_checkout(tmp_path, sync_spy):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"disabled_tools": ["debug:step_over"], "core_checkout": str(tmp_path)}), encoding="utf-8"
    )
    ui_app._sync_local_cache(SettingsStore(path))
    assert len(sync_spy) == 1
    assert tuple(sync_spy[0]["disabled"]) == ("debug:step_over",)
    assert sync_spy[0]["core_checkout"] == str(tmp_path)


def test_a_settings_file_with_a_problem_skips_the_sync_and_logs_why(tmp_path, sync_spy, caplog):
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=ui_app.LOCAL_CACHE_LOGGER):
        ui_app._sync_local_cache(SettingsStore(path))
    assert sync_spy == []
    assert any("settings file has a problem" in record.getMessage() for record in caplog.records)


def test_a_failing_sync_is_logged_and_never_stops_the_app(tmp_path, monkeypatch, caplog):
    def broken(**kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(local_cache, "sync_local_cache", broken)
    with caplog.at_level(logging.WARNING, logger=ui_app.LOCAL_CACHE_LOGGER):
        ui_app._sync_local_cache(SettingsStore(tmp_path / "settings.json"))
    assert any("local cache not updated (OSError)" in record.getMessage() for record in caplog.records)


def test_the_sync_lines_are_logged_at_info(tmp_path, sync_spy, caplog):
    with caplog.at_level(logging.INFO, logger=ui_app.LOCAL_CACHE_LOGGER):
        ui_app._sync_local_cache(SettingsStore(tmp_path / "settings.json"))
    assert any(record.getMessage() == "a line" for record in caplog.records)
