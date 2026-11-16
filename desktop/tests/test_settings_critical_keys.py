"""Tests for the critical key marks in settings.json (settings_store.Settings.critical_keys). Pure Python, no Qt.

Only names are stored in the settings file. A mark never sits with a value, and these tests check that no value is written here.
"""

from __future__ import annotations

import json

from meridian_desktop.settings_store import Settings, SettingsStore


def test_critical_keys_round_trip_through_the_settings_file(settings_path):
    store = SettingsStore(settings_path)

    store.save(Settings(key_names=("OpenAI", "Anthropic"), critical_keys=("Anthropic",)))

    assert json.loads(settings_path.read_text(encoding="utf-8"))["critical_keys"] == ["Anthropic"]
    loaded = SettingsStore(settings_path).load()
    assert loaded.settings.critical_keys == ("Anthropic",)
    assert loaded.warning is None


def test_a_file_with_no_critical_marks_is_written_without_the_key(settings_path):
    store = SettingsStore(settings_path)

    store.save(Settings(key_names=("OpenAI",)))

    assert "critical_keys" not in json.loads(settings_path.read_text(encoding="utf-8"))
    assert store.load().settings.critical_keys == ()


def test_a_missing_critical_keys_key_reads_as_none_with_no_warning(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"key_names": ["OpenAI"]}), encoding="utf-8")

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.critical_keys == ()
    assert loaded.warning is None


def test_a_repeated_mark_is_kept_once_and_its_spaces_are_trimmed(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"critical_keys": [" OpenAI ", "OpenAI"]}), encoding="utf-8")

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.critical_keys == ("OpenAI",)
    assert loaded.warning is None


def test_a_malformed_critical_keys_value_warns_and_reads_as_none(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"critical_keys": "OpenAI"}), encoding="utf-8")

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.critical_keys == ()
    assert loaded.warning is not None and "critical_keys" in loaded.warning


def test_a_blank_name_in_the_marks_is_a_malformed_value(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"critical_keys": ["OpenAI", "  "]}), encoding="utf-8")

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.critical_keys == ()
    assert loaded.warning is not None and "critical_keys" in loaded.warning


def test_critical_marks_are_kept_by_other_saves_through_the_store(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(key_names=("OpenAI",), critical_keys=("OpenAI",)))

    store.set_mode("hosted")

    assert store.load().settings.critical_keys == ("OpenAI",), "a later save through the store keeps the marks"
