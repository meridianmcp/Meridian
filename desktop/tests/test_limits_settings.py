"""Tests for the owner limits in the settings store (settings_store.LIMIT_SPECS): defaults, clamping and the saved file.

No Qt here. Each test uses a settings file in pytest's temp dir (conftest's settings_path fixture), so the real file is never
touched. Enforcement happens on the server and is not part of this file.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace

import pytest

from meridian_desktop.settings_store import (
    LIMIT_KEYS,
    LIMIT_SPECS,
    SETTINGS_UNREADABLE_NO_COPY_TEXT,
    SETTINGS_UNREADABLE_TEXT,
    Settings,
    SettingsStore,
    SettingsUnreadable,
    clamp_limit,
)

SPEC = {spec.key: spec for spec in LIMIT_SPECS}
BAK_SUFFIX = ".unreadable.bak"


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _saved(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _backup(path):
    return path.with_name(path.name + BAK_SUFFIX)


# ----------------------------------------------------------- defaults and ranges


def test_the_six_limits_are_the_owner_facing_keys():
    assert LIMIT_KEYS == (
        "limit_approval_total_mb",
        "limit_chat_total_mb",
        "limit_file_mb",
        "limit_zip_download_mb",
        "limit_writes",
        "limit_claims",
    )


def test_the_defaults_are_the_values_the_owner_sees_first():
    assert {key: SPEC[key].default for key in LIMIT_KEYS} == {
        "limit_approval_total_mb": 20480,
        "limit_chat_total_mb": 500,
        "limit_file_mb": 50,
        "limit_zip_download_mb": 1000,
        "limit_writes": 500,
        "limit_claims": 20,
    }


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_each_default_is_inside_its_range_and_the_range_is_not_empty(key):
    spec = SPEC[key]
    assert spec.minimum < spec.maximum
    assert spec.minimum <= spec.default <= spec.maximum


def test_every_limit_is_a_settings_field_that_starts_at_its_default():
    names = {field.name for field in fields(Settings)}
    settings = Settings()
    for key in LIMIT_KEYS:
        assert key in names
        assert getattr(settings, key) == SPEC[key].default


def test_a_missing_file_gives_the_default_limits_with_no_warning(settings_path):
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    for key in LIMIT_KEYS:
        assert getattr(result.settings, key) == SPEC[key].default


def test_a_file_from_before_the_limits_reads_the_defaults_with_no_warning(settings_path):
    _write(settings_path, json.dumps({"mode": "local", "autostart": True}))
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings.limit_writes == SPEC["limit_writes"].default


def test_saving_the_default_limits_writes_no_limit_key(settings_path):
    SettingsStore(settings_path).save(Settings())
    assert not [key for key in _saved(settings_path) if key.startswith("limit_")]


def test_a_limit_saved_straight_from_settings_is_written_inside_its_range(settings_path):
    SettingsStore(settings_path).save(replace(Settings(), limit_file_mb=99999, limit_claims=-3))
    assert _saved(settings_path)["limit_file_mb"] == SPEC["limit_file_mb"].maximum
    assert _saved(settings_path)["limit_claims"] == SPEC["limit_claims"].minimum
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings.limit_file_mb == SPEC["limit_file_mb"].maximum


def test_a_limit_set_back_to_its_default_is_dropped_from_the_file(settings_path):
    store = SettingsStore(settings_path)
    store.set_limit("limit_claims", 40)
    assert _saved(settings_path)["limit_claims"] == 40
    store.set_limit("limit_claims", SPEC["limit_claims"].default)
    assert "limit_claims" not in _saved(settings_path)


# ----------------------------------------------------------- clamping


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_clamp_limit_moves_a_value_into_the_range_and_leaves_an_inside_value_alone(key):
    spec = SPEC[key]
    assert clamp_limit(key, spec.minimum - 1) == spec.minimum
    assert clamp_limit(key, -(10**9)) == spec.minimum
    assert clamp_limit(key, spec.maximum + 1) == spec.maximum
    assert clamp_limit(key, 10**9) == spec.maximum
    for inside in (spec.minimum, spec.default, spec.maximum):
        assert clamp_limit(key, inside) == inside


def test_clamp_limit_refuses_a_key_that_is_not_an_owner_limit():
    with pytest.raises(ValueError, match="unknown limit"):
        clamp_limit("limit_nope", 5)


@pytest.mark.parametrize("key", LIMIT_KEYS)
def test_set_limit_saves_the_nearer_end_of_the_range_for_each_limit(settings_path, key):
    spec = SPEC[key]
    store = SettingsStore(settings_path)

    above = store.set_limit(key, spec.maximum + 500)
    assert getattr(above, key) == spec.maximum
    assert _saved(settings_path)[key] == spec.maximum

    below = store.set_limit(key, spec.minimum - 500)
    assert getattr(below, key) == spec.minimum
    assert _saved(settings_path)[key] == spec.minimum


def test_a_file_value_outside_the_range_is_clamped_and_the_owner_is_told(settings_path):
    _write(settings_path, json.dumps({"limit_file_mb": 99999, "limit_claims": 0}))
    result = SettingsStore(settings_path).load()
    assert result.settings.limit_file_mb == SPEC["limit_file_mb"].maximum
    assert result.settings.limit_claims == SPEC["limit_claims"].minimum
    assert result.warning is not None
    assert "limit_file_mb must be between 1 and 500; using 500" in result.warning
    assert "limit_claims must be between 1 and 200; using 1" in result.warning


@pytest.mark.parametrize("bad", ["50", 12.5, True, None, [10]])
def test_a_file_value_that_is_not_a_whole_number_reads_as_the_default_with_a_warning(settings_path, bad):
    _write(settings_path, json.dumps({"limit_file_mb": bad}))
    result = SettingsStore(settings_path).load()
    assert result.settings.limit_file_mb == SPEC["limit_file_mb"].default
    assert result.warning == "limit_file_mb must be a whole number; using its default"


# ----------------------------------------------------------- set_limit and persistence


def test_all_six_limits_round_trip_through_the_file(settings_path):
    saved = replace(
        Settings(),
        limit_approval_total_mb=4096,
        limit_approval_enabled=False,
        limit_chat_total_mb=1200,
        limit_file_mb=75,
        limit_zip_download_mb=4000,
        limit_writes=900,
        limit_claims=35,
    )
    SettingsStore(settings_path).save(saved)
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings == saved


def test_set_limit_persists_for_a_new_store(settings_path):
    returned = SettingsStore(settings_path).set_limit("limit_chat_total_mb", 1500)
    assert returned.limit_chat_total_mb == 1500
    assert SettingsStore(settings_path).load().settings.limit_chat_total_mb == 1500


def test_set_limit_keeps_every_other_field_in_the_file(settings_path):
    _write(
        settings_path,
        json.dumps({"mode": "hosted", "db_path": "C:/data/meridian.db", "gpu_hosts": ["gpu-box"]}),
    )
    updated = SettingsStore(settings_path).set_limit("limit_writes", 800)
    assert updated.mode == "hosted"
    assert updated.db_path == "C:/data/meridian.db"
    assert updated.gpu_hosts == ("gpu-box",)
    assert updated.limit_writes == 800
    saved = _saved(settings_path)
    assert saved["mode"] == "hosted"
    assert saved["db_path"] == "C:/data/meridian.db"
    assert saved["gpu_hosts"] == ["gpu-box"]
    assert saved["limit_writes"] == 800


def test_set_limit_refuses_a_key_that_is_not_an_owner_limit_and_writes_nothing(settings_path):
    for key in ("limit_nope", "gpu_hosts", "mode"):
        with pytest.raises(ValueError, match="unknown limit"):
            SettingsStore(settings_path).set_limit(key, 5)
    assert not settings_path.exists()


@pytest.mark.parametrize("value", [True, 12.5, "12", None])
def test_set_limit_refuses_a_value_that_is_not_a_whole_number_and_writes_nothing(settings_path, value):
    with pytest.raises(ValueError, match="must be a whole number"):
        SettingsStore(settings_path).set_limit("limit_file_mb", value)
    assert not settings_path.exists()


@pytest.mark.parametrize("text", ["[1, 2, 3]", "{not json"])
def test_set_limit_refuses_an_unreadable_file_and_keeps_a_copy(settings_path, text):
    _write(settings_path, text)
    with pytest.raises(SettingsUnreadable) as caught:
        SettingsStore(settings_path).set_limit("limit_file_mb", 60)
    assert str(caught.value) == SETTINGS_UNREADABLE_TEXT
    assert settings_path.read_text(encoding="utf-8") == text
    assert _backup(settings_path).read_text(encoding="utf-8") == text


def test_set_limit_says_no_copy_was_kept_when_the_copy_fails(settings_path, monkeypatch):
    _write(settings_path, "[1, 2, 3]")
    monkeypatch.setattr(SettingsStore, "_keep_copy", lambda self: False)
    with pytest.raises(SettingsUnreadable) as caught:
        SettingsStore(settings_path).set_limit("limit_file_mb", 60)
    assert str(caught.value) == SETTINGS_UNREADABLE_NO_COPY_TEXT
    assert settings_path.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_set_limit_keeps_a_copy_of_a_readable_file_that_had_a_bad_value(settings_path):
    text = json.dumps({"limit_file_mb": "big"})
    _write(settings_path, text)
    SettingsStore(settings_path).set_limit("limit_writes", 300)
    assert _backup(settings_path).read_text(encoding="utf-8") == text
    assert _saved(settings_path)["limit_writes"] == 300


def test_the_total_size_note_is_on_by_default_at_twenty_gigabytes(settings_path):
    assert SPEC["limit_approval_total_mb"].default == 20480
    assert SettingsStore(settings_path).load().settings.limit_approval_enabled is True


def test_the_total_size_note_switch_is_written_only_when_turned_off(settings_path):
    store = SettingsStore(settings_path)
    assert store.set_limit_approval_enabled(False).limit_approval_enabled is False
    assert _saved(settings_path)["limit_approval_enabled"] is False
    store.set_limit_approval_enabled(True)
    assert "limit_approval_enabled" not in _saved(settings_path)


def test_the_total_size_note_switch_refuses_a_value_that_is_not_true_or_false(settings_path):
    with pytest.raises(ValueError):
        SettingsStore(settings_path).set_limit_approval_enabled("no")


def test_a_file_with_a_non_boolean_switch_keeps_the_note_on_with_a_warning(settings_path):
    _write(settings_path, json.dumps({"limit_approval_enabled": "no"}))
    result = SettingsStore(settings_path).load()
    assert result.settings.limit_approval_enabled is True
    assert result.warning is not None and "limit_approval_enabled" in result.warning
