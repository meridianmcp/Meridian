import ast
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from meridian_desktop import settings_store, token_store
from meridian_desktop.settings_store import (
    DEFAULT_LOCAL_URL,
    Settings,
    SettingsStore,
    core_path_problem,
    default_settings_path,
)

REPO = Path(__file__).resolve().parents[1]

# What a missing file gives: the defaults, with no mode the owner chose.
NO_FILE_SETTINGS = replace(Settings(), mode_explicit=False)
# What a corrupt or unreadable file gives: the same, but with the tunnel switch off (fail closed, X1).
FAILED_READ_SETTINGS = replace(NO_FILE_SETTINGS, tunnel_control_enabled=False)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_missing_file_gives_defaults_without_warning(settings_path):
    result = SettingsStore(settings_path).load()
    assert result.settings == NO_FILE_SETTINGS
    assert result.settings.mode_explicit is False
    assert result.settings.mode == "local"
    assert result.warning is None


def test_round_trip(settings_path):
    saved = Settings(
        mode="hosted",
        hosted_url="https://example.test",
        local_url=DEFAULT_LOCAL_URL,
        engine_url="http://127.0.0.1:9999",
    )
    SettingsStore(settings_path).save(saved)
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings == saved


def test_set_mode_round_trip(settings_path):
    store = SettingsStore(settings_path)
    store.set_mode("hosted")
    assert SettingsStore(settings_path).load().settings.mode == "hosted"
    store.set_mode("local")
    assert SettingsStore(settings_path).load().settings.mode == "local"


def test_corrupt_file_returns_defaults_and_warning(settings_path):
    _write(settings_path, "{not json")
    result = SettingsStore(settings_path).load()
    assert result.settings == FAILED_READ_SETTINGS
    assert result.warning is not None
    assert "corrupt" in result.warning


def test_json_that_is_not_an_object_returns_defaults_and_warning(settings_path):
    _write(settings_path, "[1, 2, 3]")
    result = SettingsStore(settings_path).load()
    assert result.settings == FAILED_READ_SETTINGS
    assert result.warning is not None
    assert "JSON object" in result.warning


def test_unreadable_file_returns_defaults_and_warning(tmp_path):
    # A directory at the settings path cannot be read as a file.
    path = tmp_path / "settings.json"
    path.mkdir()
    result = SettingsStore(path).load()
    assert result.settings == FAILED_READ_SETTINGS
    assert result.warning is not None
    assert "unreadable" in result.warning


@pytest.mark.parametrize("text", ["{not json", "[1, 2, 3]"])
def test_a_corrupt_settings_file_turns_the_tunnel_switch_off_and_says_so(settings_path, text):
    _write(settings_path, text)
    loaded = SettingsStore(settings_path).load()
    assert loaded.settings.tunnel_control_enabled is False
    assert loaded.warning is not None and "tunnel controls off" in loaded.warning


def test_an_unreadable_settings_file_turns_the_tunnel_switch_off_and_says_so(tmp_path):
    path = tmp_path / "settings.json"
    path.mkdir()
    loaded = SettingsStore(path).load()
    assert loaded.settings.tunnel_control_enabled is False
    assert loaded.warning is not None and "tunnel controls off" in loaded.warning


def test_a_missing_settings_file_keeps_the_tunnel_switch_on(settings_path):
    loaded = SettingsStore(settings_path).load()
    assert loaded.settings.tunnel_control_enabled is True
    assert loaded.warning is None


def test_invalid_mode_in_file_falls_back_to_local(settings_path):
    _write(settings_path, '{"mode": "cloud", "hosted_url": "https://example.test"}')
    result = SettingsStore(settings_path).load()
    assert result.settings.mode == "local"
    assert result.settings.hosted_url == "https://example.test"
    assert result.warning is not None
    assert "mode" in result.warning


def test_non_string_url_falls_back_to_default(settings_path):
    _write(settings_path, '{"engine_url": 8471}')
    result = SettingsStore(settings_path).load()
    assert result.settings.engine_url == Settings().engine_url
    assert result.warning is not None


def test_invalid_mode_is_rejected_and_nothing_is_written(settings_path):
    store = SettingsStore(settings_path)
    for bad in ("cloud", "", "HOSTED", None, 1):
        with pytest.raises(ValueError):
            store.set_mode(bad)
    assert not settings_path.exists()


def test_atomic_write_leaves_no_temp_file(settings_path):
    store = SettingsStore(settings_path)
    store.set_mode("hosted")
    store.set_mode("local")
    assert sorted(os.listdir(settings_path.parent)) == ["settings.json"]


def test_failed_replace_keeps_old_file_and_removes_temp(settings_path, monkeypatch):
    store = SettingsStore(settings_path)
    store.set_mode("hosted")
    before = settings_path.read_text(encoding="utf-8")

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(settings_store.os, "replace", failing_replace)
    with pytest.raises(OSError):
        store.set_mode("local")
    assert settings_path.read_text(encoding="utf-8") == before
    assert sorted(os.listdir(settings_path.parent)) == ["settings.json"]


def test_default_path_uses_localappdata(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "local")}
    assert default_settings_path(env=env) == tmp_path / "local" / "meridian-desktop" / "settings.json"


def test_default_path_falls_back_to_home_when_localappdata_unset(tmp_path):
    assert default_settings_path(env={}, home=tmp_path) == tmp_path / ".meridian-desktop" / "settings.json"


def test_settings_store_imports_no_qt():
    tree = ast.parse((REPO / "meridian_desktop" / "settings_store.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    qt_roots = {"PySide6", "PySide2", "PyQt5", "PyQt6", "PyQt4", "shiboken6", "shiboken2"}
    assert [name for name in imported if name.split(".")[0] in qt_roots] == []


SAVED_KEYS = {"mode", "hosted_url", "local_url", "engine_url", "db_path"}


def test_db_path_round_trip_and_blank_is_none(settings_path):
    store = SettingsStore(settings_path)
    assert store.load().settings.db_path is None

    db = str(settings_path.parent / "meridian.db")
    store.save(Settings(db_path=db))
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings.db_path == db

    # An unset db_path is saved as null and reads back as None with no warning.
    store.save(Settings())
    result = SettingsStore(settings_path).load()
    assert result.settings.db_path is None
    assert result.warning is None

    # A blank or non-string value loads as None with a warning, and never raises.
    for bad in ("", "   ", 42, True, ["a"], {"path": "x"}):
        _write(settings_path, json.dumps({"db_path": bad}))
        result = SettingsStore(settings_path).load()
        assert result.settings.db_path is None, bad
        assert result.warning is not None and "db_path" in result.warning, bad


def test_file_written_before_db_path_loads_without_warning(settings_path):
    _write(
        settings_path,
        json.dumps(
            {
                "mode": "hosted",
                "hosted_url": "https://example.test",
                "local_url": DEFAULT_LOCAL_URL,
                "engine_url": "http://127.0.0.1:9999",
            }
        ),
    )
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings.mode == "hosted"
    assert result.settings.db_path is None


def test_saved_file_holds_only_the_five_keys(settings_path):
    SettingsStore(settings_path).save(Settings(db_path=str(settings_path.parent / "meridian.db")))
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert set(saved) == SAVED_KEYS


def test_token_like_keys_are_neither_kept_nor_written(settings_path):
    _write(
        settings_path,
        json.dumps({"mode": "hosted", "hosted_token": "fake-token-value-123", "api_key": "fake-key-value-456"}),
    )
    SettingsStore(settings_path).set_mode("local")
    text = settings_path.read_text(encoding="utf-8")
    assert "fake-token-value-123" not in text
    assert "fake-key-value-456" not in text
    assert set(json.loads(text)) == SAVED_KEYS


# ----------------------------------------------------------- core checkout and core Python


def _core_checkout(tmp_path: Path) -> Path:
    """A folder with the meridian package in it, as a Meridian core checkout has."""
    checkout = tmp_path / "core"
    (checkout / "meridian").mkdir(parents=True)
    (checkout / "meridian" / "__init__.py").write_text("", encoding="utf-8")
    return checkout


def test_core_settings_default_to_not_set_without_a_warning(settings_path):
    result = SettingsStore(settings_path).load()
    assert result.settings.core_checkout == ""
    assert result.settings.core_python == ""
    assert result.warning is None


def test_core_settings_round_trip(settings_path, tmp_path):
    saved = Settings(core_checkout=str(tmp_path / "core"), core_python=str(tmp_path / "python.exe"))
    SettingsStore(settings_path).save(saved)
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings == saved


def test_unset_core_settings_are_not_written(settings_path):
    SettingsStore(settings_path).save(Settings())
    assert set(json.loads(settings_path.read_text(encoding="utf-8"))) == SAVED_KEYS


def test_set_core_settings_are_written_and_only_the_set_one_when_one_is_set(settings_path, tmp_path):
    SettingsStore(settings_path).save(Settings(core_python=str(tmp_path / "python.exe")))
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["core_python"] == str(tmp_path / "python.exe")
    assert "core_checkout" not in saved


def test_non_text_core_values_warn_and_are_not_set(settings_path):
    _write(settings_path, json.dumps({"core_checkout": 5, "core_python": ["a"]}))
    result = SettingsStore(settings_path).load()
    assert result.settings.core_checkout == ""
    assert result.settings.core_python == ""
    assert result.warning is not None
    assert "core_checkout" in result.warning and "core_python" in result.warning


def test_blank_core_value_loads_as_not_set_without_a_warning(settings_path):
    _write(settings_path, json.dumps({"core_checkout": "   "}))
    result = SettingsStore(settings_path).load()
    assert result.settings.core_checkout == ""
    assert result.warning is None


def test_core_path_problem_refuses_a_checkout_folder_that_does_not_exist(tmp_path):
    message = core_path_problem("core_checkout", str(tmp_path / "gone"))
    assert message is not None
    assert message.startswith("Not saved. ")
    assert "does not exist" in message


def test_core_path_problem_refuses_a_folder_without_the_meridian_package(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    message = core_path_problem("core_checkout", str(folder))
    assert message is not None and "not a Meridian core checkout" in message


def test_core_path_problem_accepts_a_real_checkout_and_an_empty_value(tmp_path):
    assert core_path_problem("core_checkout", str(_core_checkout(tmp_path))) is None
    assert core_path_problem("core_checkout", "") is None
    assert core_path_problem("core_python", "") is None


def test_core_path_problem_refuses_a_python_that_does_not_exist_or_is_a_folder(tmp_path):
    missing = core_path_problem("core_python", str(tmp_path / "nope" / "python.exe"))
    assert missing is not None and "does not exist" in missing
    folder = core_path_problem("core_python", str(tmp_path))
    assert folder is not None and "does not exist" in folder


def test_core_path_problem_rejects_an_unknown_key(tmp_path):
    with pytest.raises(ValueError):
        core_path_problem("core_token", str(tmp_path))


def test_set_core_path_saves_the_value_and_keeps_every_other_field(settings_path, tmp_path):
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    SettingsStore(settings_path).save(Settings(mode="hosted", local_url="http://127.0.0.1:9999"))

    updated = SettingsStore(settings_path).set_core_path("core_python", str(python))

    loaded = SettingsStore(settings_path).load().settings
    assert loaded.core_python == str(python)
    assert loaded.mode == "hosted" and loaded.local_url == "http://127.0.0.1:9999"
    assert updated == loaded


def test_a_refused_core_path_raises_plain_words_and_writes_nothing(settings_path, tmp_path):
    store = SettingsStore(settings_path)
    with pytest.raises(ValueError) as refused:
        store.set_core_path("core_checkout", str(tmp_path / "gone"))
    assert str(refused.value).startswith("Not saved. ")
    assert not settings_path.exists()


def test_clearing_a_core_path_removes_its_key(settings_path, tmp_path):
    store = SettingsStore(settings_path)
    store.set_core_path("core_checkout", str(_core_checkout(tmp_path)))
    store.set_core_path("core_checkout", "")
    assert store.load().settings.core_checkout == ""
    assert "core_checkout" not in json.loads(settings_path.read_text(encoding="utf-8"))


# ----------------------------------------------------------- update check


def test_check_updates_defaults_to_on_without_a_warning(settings_path):
    assert Settings().check_updates is True
    result = SettingsStore(settings_path).load()
    assert result.settings.check_updates is True
    assert result.warning is None


def test_check_updates_round_trips_off_and_back_on(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(check_updates=False))
    assert json.loads(settings_path.read_text(encoding="utf-8"))["check_updates"] is False
    result = SettingsStore(settings_path).load()
    assert result.warning is None
    assert result.settings == Settings(check_updates=False)
    # A later mode change keeps the setting as it was.
    store.set_mode("hosted")
    assert SettingsStore(settings_path).load().settings.check_updates is False
    # On is the default, so turning it back on removes the key again.
    store.save(Settings(check_updates=True))
    assert "check_updates" not in json.loads(settings_path.read_text(encoding="utf-8"))
    assert SettingsStore(settings_path).load().settings == Settings(check_updates=True)


def test_check_updates_that_is_not_true_or_false_warns_and_stays_on(settings_path):
    _write(settings_path, json.dumps({"check_updates": "no"}))
    result = SettingsStore(settings_path).load()
    assert result.settings.check_updates is True
    assert result.warning is not None and "check_updates" in result.warning


# ----------------------------------------------------------- the runner control switches (contract rule 10, amendments A to C)

CONTROL_KEYS = ("tunnel_control_enabled", "zotero_autostart", "local_server_autostart")
# The defaults. tunnel_control_enabled is on by owner decision on 2026-10-23 (contract amendment F). The two
# launch-time switches are off until the owner confirms them.
CONTROL_DEFAULTS = {"tunnel_control_enabled": True, "zotero_autostart": False, "local_server_autostart": False}
CONFIRMED_KEYS = ("confirmed_core_python", "confirmed_core_checkout", "confirmed_zotero_exe")


def test_runner_control_switches_default_to_their_defaults_and_are_not_written(settings_path):
    store = SettingsStore(settings_path)
    loaded = store.load()

    assert loaded.warning is None
    for key in CONTROL_KEYS:
        assert getattr(loaded.settings, key) is CONTROL_DEFAULTS[key], key
    store.save(loaded.settings)
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert not any(key in saved for key in CONTROL_KEYS)


def test_the_tunnel_switch_defaults_to_on_and_is_written_only_when_turned_off(settings_path):
    store = SettingsStore(settings_path)
    assert store.load().settings.tunnel_control_enabled is True

    store.save(Settings(tunnel_control_enabled=False))
    assert json.loads(settings_path.read_text(encoding="utf-8"))["tunnel_control_enabled"] is False
    assert SettingsStore(settings_path).load().settings.tunnel_control_enabled is False

    store.save(Settings(tunnel_control_enabled=True))
    assert "tunnel_control_enabled" not in json.loads(settings_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", CONTROL_KEYS)
def test_each_runner_switch_round_trips_when_changed_and_leaves_the_others_at_their_defaults(settings_path, key):
    store = SettingsStore(settings_path)
    changed = not CONTROL_DEFAULTS[key]
    store.save(Settings(**{key: changed}))

    loaded = store.load()
    assert loaded.warning is None
    assert getattr(loaded.settings, key) is changed
    for other in CONTROL_KEYS:
        if other != key:
            assert getattr(loaded.settings, other) is CONTROL_DEFAULTS[other], other
    assert json.loads(settings_path.read_text(encoding="utf-8"))[key] is changed


def test_a_launch_switch_turned_back_off_removes_its_key(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(zotero_autostart=True, tunnel_control_enabled=True))
    store.save(Settings(zotero_autostart=False, tunnel_control_enabled=True))

    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "zotero_autostart" not in saved
    assert "tunnel_control_enabled" not in saved


@pytest.mark.parametrize("key", CONTROL_KEYS)
def test_an_absent_runner_switch_gives_its_default_without_a_warning(settings_path, key):
    _write(settings_path, "{}")

    loaded = SettingsStore(settings_path).load()

    assert loaded.warning is None
    assert getattr(loaded.settings, key) is CONTROL_DEFAULTS[key]


MALFORMED_SWITCH_VALUES = ["yes", "true", 1, 0, None, []]


@pytest.mark.parametrize("value", MALFORMED_SWITCH_VALUES)
def test_a_malformed_tunnel_switch_is_read_as_false_and_never_as_its_default(settings_path, value):
    # The default of this switch is true. A value that is not a real true or false must not give that default (X1).
    _write(settings_path, json.dumps({"tunnel_control_enabled": value}))

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.tunnel_control_enabled is False
    assert "tunnel_control_enabled must be true or false; using false" in loaded.warning


@pytest.mark.parametrize("key", ["zotero_autostart", "local_server_autostart"])
@pytest.mark.parametrize("value", MALFORMED_SWITCH_VALUES)
def test_a_malformed_launch_switch_is_read_as_false_with_a_warning(settings_path, key, value):
    _write(settings_path, json.dumps({key: value}))

    loaded = SettingsStore(settings_path).load()

    assert getattr(loaded.settings, key) is False
    assert f"{key} must be true or false; using false" in loaded.warning


def test_a_saved_switch_keeps_every_other_field(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings(local_url="http://127.0.0.1:7999", zotero_autostart=True))

    loaded = store.load().settings
    assert loaded.local_url == "http://127.0.0.1:7999"
    assert loaded.zotero_autostart is True
    assert loaded.local_server_autostart is False


# ----------------------------------------------------------- the confirmed values (amendments A and B)


def test_confirmed_values_round_trip_exactly_and_are_not_written_when_unset(settings_path, tmp_path):
    exe = str(tmp_path / "Zotero" / "zotero.exe")
    confirmed = dict(
        confirmed_core_python=str(tmp_path / "python.exe"),
        confirmed_core_checkout=str(tmp_path / "core"),
        confirmed_zotero_exe=exe,
    )
    store = SettingsStore(settings_path)
    store.save(Settings(**confirmed))

    loaded = SettingsStore(settings_path).load()
    assert loaded.warning is None
    for key, value in confirmed.items():
        assert getattr(loaded.settings, key) == value, key

    store.save(Settings())
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert not any(key in saved for key in CONFIRMED_KEYS)


def test_a_confirmed_value_that_is_not_text_warns_and_is_empty(settings_path):
    _write(settings_path, json.dumps({"confirmed_zotero_exe": 7}))

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.confirmed_zotero_exe == ""
    assert "confirmed_zotero_exe must be a path" in loaded.warning


def test_saving_a_core_path_turns_both_launch_switches_off_and_keeps_the_other_fields(settings_path, tmp_path):
    store = SettingsStore(settings_path)
    store.save(
        Settings(
            zotero_autostart=True,
            local_server_autostart=True,
            tunnel_control_enabled=False,
            local_url="http://127.0.0.1:7999",
            confirmed_core_python=str(tmp_path / "old-python.exe"),
            confirmed_zotero_exe=str(tmp_path / "zotero.exe"),
        )
    )
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")

    updated = store.set_core_path("core_python", str(python))

    assert updated.zotero_autostart is False
    assert updated.local_server_autostart is False
    loaded = SettingsStore(settings_path).load().settings
    assert loaded.zotero_autostart is False
    assert loaded.local_server_autostart is False
    assert loaded.core_python == str(python)
    assert loaded.tunnel_control_enabled is False  # the tunnel switch is not a launch switch, so it is kept
    assert loaded.local_url == "http://127.0.0.1:7999"
    assert loaded.confirmed_zotero_exe == str(tmp_path / "zotero.exe")


def test_a_refused_core_path_leaves_the_launch_switches_as_they_were(settings_path, tmp_path):
    store = SettingsStore(settings_path)
    store.save(Settings(zotero_autostart=True, local_server_autostart=True))

    with pytest.raises(ValueError):
        store.set_core_path("core_checkout", str(tmp_path / "gone"))

    loaded = SettingsStore(settings_path).load().settings
    assert loaded.zotero_autostart is True
    assert loaded.local_server_autostart is True


# ----------------------------------------------------------- the launch mode (owner decision, 2026-10-23)

FAKE_TOKEN = "sk_meridian_FAKE-launch-mode-0001"


class FakeSecrets:
    """A fake secret store. It holds one token, or none, and counts the reads. It is never the real keychain."""

    def __init__(self, token=None):
        self._token = token
        self.reads = 0

    def get_secret(self, service, account):
        self.reads += 1
        return self._token

    def set_secret(self, service, account, value):
        raise AssertionError("the launch mode never writes a token")

    def delete_secret(self, service, account):
        raise AssertionError("the launch mode never removes a token")


def _launch_mode(settings_path, secrets):
    store = SettingsStore(settings_path)
    mode = store.resolve_launch_mode(lambda: token_store.has_token(store=secrets))
    return store, mode


def test_no_mode_in_the_file_and_a_saved_hosted_token_starts_in_hosted(settings_path):
    _write(settings_path, json.dumps({"hosted_url": "https://usemeridian.us"}))

    store, mode = _launch_mode(settings_path, FakeSecrets(FAKE_TOKEN))

    assert mode == "hosted"
    assert store.load().settings.mode == "hosted"
    assert store.load().warning is None


def test_no_mode_in_the_file_and_no_token_starts_in_local(settings_path):
    _write(settings_path, json.dumps({"hosted_url": "https://usemeridian.us"}))

    store, mode = _launch_mode(settings_path, FakeSecrets(None))

    assert mode == "local"
    assert store.load().settings.mode == "local"


def test_a_missing_settings_file_starts_in_hosted_only_when_a_token_is_saved(settings_path):
    assert _launch_mode(settings_path, FakeSecrets(FAKE_TOKEN))[1] == "hosted"
    assert _launch_mode(settings_path, FakeSecrets(None))[1] == "local"


@pytest.mark.parametrize("mode, expected", [("local", "local"), ("hosted", "hosted"), ("tunnel", "hosted")])
@pytest.mark.parametrize("token", [FAKE_TOKEN, None])
def test_an_explicit_mode_in_the_file_always_wins_and_the_keychain_is_not_read(settings_path, mode, expected, token):
    # An older saved "tunnel" is Hosted now, so it is explicit Hosted and wins over the launch mode.
    _write(settings_path, json.dumps({"mode": mode}))
    secrets = FakeSecrets(token)

    store, resolved = _launch_mode(settings_path, secrets)

    assert resolved == expected
    assert store.load().settings.mode == expected
    assert secrets.reads == 0


def test_the_launch_mode_is_never_written_to_the_file(settings_path):
    _write(settings_path, json.dumps({"hosted_url": "https://usemeridian.us"}))
    store, _mode = _launch_mode(settings_path, FakeSecrets(FAKE_TOKEN))

    store.set_core_path("core_checkout", "")
    store.save(store.load().settings)

    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "mode" not in saved


def test_choosing_a_mode_makes_it_explicit_and_it_then_wins_over_a_saved_token(settings_path):
    _write(settings_path, json.dumps({"hosted_url": "https://usemeridian.us"}))
    store, mode = _launch_mode(settings_path, FakeSecrets(FAKE_TOKEN))
    assert mode == "hosted"

    store.set_mode("local")

    assert SettingsStore(settings_path).resolve_launch_mode(lambda: True) == "local"


def test_an_invalid_saved_mode_warns_and_follows_the_launch_mode(settings_path):
    _write(settings_path, json.dumps({"mode": "cloud"}))

    store, mode = _launch_mode(settings_path, FakeSecrets(FAKE_TOKEN))

    assert mode == "hosted"
    loaded = store.load()
    assert loaded.settings.mode == "hosted"
    assert "mode must be one of" in loaded.warning


def test_without_a_resolved_launch_mode_a_store_still_reads_local(settings_path):
    _write(settings_path, json.dumps({"hosted_url": "https://usemeridian.us"}))

    assert SettingsStore(settings_path).load().settings.mode == "local"
