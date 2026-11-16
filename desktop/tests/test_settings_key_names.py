"""Tests for the saved API key names in settings.json (settings_store.Settings.key_names). Pure Python, no Qt.

Only names are stored in the settings file. The keychain holds the values, and these tests check that none is written here.
"""

from __future__ import annotations

import json

from meridian_desktop.settings_store import Settings, SettingsStore


def test_key_names_round_trip_through_the_settings_file(settings_path):
    store = SettingsStore(settings_path)

    store.save(Settings(key_names=("OpenAI", "Anthropic")))

    assert json.loads(settings_path.read_text(encoding="utf-8"))["key_names"] == ["OpenAI", "Anthropic"]
    loaded = SettingsStore(settings_path).load()
    assert loaded.settings.key_names == ("OpenAI", "Anthropic")
    assert loaded.warning is None


def test_a_file_with_no_key_names_is_written_without_the_key(settings_path):
    store = SettingsStore(settings_path)

    store.save(Settings(local_url="http://127.0.0.1:7999"))

    assert "key_names" not in json.loads(settings_path.read_text(encoding="utf-8"))
    assert store.load().settings.key_names == ()


def test_key_names_are_kept_by_other_saves(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(key_names=("OpenAI",)))

    store.set_mode("hosted")

    assert store.load().settings.key_names == ("OpenAI",), "a later save through the store keeps the names"


def test_a_malformed_key_names_value_warns_and_reads_as_none(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"key_names": "OpenAI"}), encoding="utf-8")

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.key_names == ()
    assert loaded.warning is not None and "key_names" in loaded.warning


def test_a_blank_name_in_the_list_is_a_malformed_value(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"key_names": ["OpenAI", "   "]}), encoding="utf-8")

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.key_names == ()
    assert loaded.warning is not None and "key_names" in loaded.warning
