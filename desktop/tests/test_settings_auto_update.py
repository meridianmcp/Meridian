"""The auto_update setting: its defaults, its round trip through the settings file, and the Settings checkbox that edits it.

No real settings file is read or written: every path is under pytest's temp dir.
"""

from __future__ import annotations

import json

from PySide6.QtWidgets import QCheckBox

from _fakes import MemoryKeyring
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.settings_tab import SettingsTab


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_auto_update_defaults_to_on_without_a_warning(settings_path):
    assert Settings().auto_update is True
    result = SettingsStore(settings_path).load()
    assert result.settings.auto_update is True
    assert result.warning is None


def test_auto_update_round_trips_off_and_back_on(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(auto_update=False))
    assert _read(settings_path)["auto_update"] is False

    result = SettingsStore(settings_path).load()
    assert result.settings == Settings(auto_update=False)
    assert result.warning is None

    store.save(Settings(auto_update=True))
    assert "auto_update" not in _read(settings_path)  # on is the default, so no key is written
    assert SettingsStore(settings_path).load().settings == Settings(auto_update=True)


def test_a_file_with_no_auto_update_key_reads_as_on(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"check_updates": True}), encoding="utf-8")
    assert SettingsStore(settings_path).load().settings.auto_update is True


def test_auto_update_that_is_not_true_or_false_warns_and_stays_on(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"auto_update": "no"}), encoding="utf-8")
    result = SettingsStore(settings_path).load()
    assert result.settings.auto_update is True
    assert result.warning is not None and "auto_update" in result.warning


def test_auto_update_does_not_disturb_the_other_saved_keys(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(check_updates=False, auto_update=False, autostart=True))
    saved = _read(settings_path)
    assert saved["check_updates"] is False
    assert saved["auto_update"] is False
    assert saved["autostart"] is True


def _tab(qtbot, settings_path):
    tab = SettingsTab(SettingsStore(settings_path), secrets=KeyringSecretStore(backend=MemoryKeyring()))
    qtbot.addWidget(tab)
    return tab


def test_the_settings_checkbox_shows_the_saved_value(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(auto_update=False))
    tab = _tab(qtbot, settings_path)
    box = tab.findChild(QCheckBox, "auto-update-box")
    assert box is not None
    assert box.isChecked() is False


def test_the_settings_checkbox_saves_each_change(qtbot, settings_path):
    tab = _tab(qtbot, settings_path)
    box = tab.findChild(QCheckBox, "auto-update-box")
    assert box.isChecked() is True

    box.setChecked(False)
    assert SettingsStore(settings_path).load().settings.auto_update is False

    box.setChecked(True)
    assert SettingsStore(settings_path).load().settings.auto_update is True


def test_showing_the_saved_value_does_not_save_it_again(qtbot, settings_path, monkeypatch):
    tab = _tab(qtbot, settings_path)
    saves: list[Settings] = []
    original_save = tab._store.save

    def counting_save(settings):
        saves.append(settings)
        original_save(settings)

    monkeypatch.setattr(tab._store, "save", counting_save)
    tab.refresh()
    assert saves == []
