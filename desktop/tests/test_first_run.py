"""Tests for the first-run dialog (meridian_desktop/ui/first_run.py), the autostart key in settings_store,
and the rule that --smoke never shows the dialog. Offscreen Qt, no network.

The start-with-Windows entry goes to a FakeRegistry, and every settings file is in pytest's temp dir.
Nothing here touches the real registry, the real %LOCALAPPDATA% or the real settings file.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from meridian_desktop import install as inst
from meridian_desktop import updater
from meridian_desktop.settings_store import DEFAULT_LOCAL_URL, Settings, SettingsStore
from meridian_desktop.ui import app as ui_app
from meridian_desktop.ui import first_run as fr

BY_MOUSE = Qt.TextInteractionFlag.TextSelectableByMouse
BY_KEYBOARD = Qt.TextInteractionFlag.TextSelectableByKeyboard


@pytest.fixture(autouse=True)
def no_registry_writes(monkeypatch):
    monkeypatch.setenv(inst.NO_REGISTRY_ENV, "1")


class FakeRegistry:
    """In-memory stand-in for HKEY_CURRENT_USER: key -> {value name: string}."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    def write_strings(self, key: str, values: dict[str, str]) -> None:
        self.keys.setdefault(key, {}).update(values)

    def delete_value(self, key: str, name: str) -> bool:
        values = self.keys.get(key, {})
        if name not in values:
            return False
        del values[name]
        return True

    def delete_key(self, key: str) -> bool:
        return self.keys.pop(key, None) is not None

    def run_value(self) -> str | None:
        return self.keys.get(inst.RUN_KEY, {}).get(inst.RUN_VALUE)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "USERPROFILE": str(tmp_path / "profile"),
        inst.NO_REGISTRY_ENV: "1",
    }


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "local" / "meridian-desktop" / "settings.json")


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _selectable(widget) -> bool:
    flags = widget.textInteractionFlags()
    return bool(flags & BY_MOUSE) and bool(flags & BY_KEYBOARD)


# ----------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    "address",
    ["http://127.0.0.1:7878", "https://example.test", "http://localhost", "http://[::1]:7878", " http://127.0.0.1:7100 "],
)
def test_valid_local_addresses_are_accepted(address):
    assert fr.validate_local_url(address) is None


@pytest.mark.parametrize(
    "address",
    ["", "   ", "127.0.0.1:7878", "ftp://127.0.0.1", "http://", "http://127.0.0.1:99999", "http://127.0.0.1:0"],
)
def test_bad_local_addresses_get_a_plain_message(address):
    message = fr.validate_local_url(address)
    assert message is not None
    assert "http" in message or "port" in message


def test_choices_keep_every_other_setting():
    current = Settings(hosted_url="https://example.test", engine_url="http://127.0.0.1:9999", db_path="C:/data/meridian.db")
    chosen = fr.settings_from_choices(current, mode="hosted", local_url=" http://127.0.0.1:7000 ", autostart=True)
    assert chosen == replace(current, mode="hosted", local_url="http://127.0.0.1:7000", autostart=True)


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="mode must be one of"):
        fr.settings_from_choices(Settings(), mode="cloud", local_url=DEFAULT_LOCAL_URL, autostart=False)


def test_first_run_is_needed_only_without_a_settings_file_and_outside_smoke(tmp_path):
    store = _store(tmp_path)
    assert fr.needs_first_run(store, smoke=False) is True
    assert fr.needs_first_run(store, smoke=True) is False
    store.save(Settings())
    assert fr.needs_first_run(store, smoke=False) is False


# ----------------------------------------------------------------- autostart in settings_store


def test_autostart_defaults_to_off_and_is_not_written_while_off(tmp_path):
    store = _store(tmp_path)
    assert store.load().settings.autostart is False
    store.save(Settings())
    assert "autostart" not in json.loads(store.path.read_text(encoding="utf-8"))


def test_autostart_round_trips_when_on(tmp_path):
    store = _store(tmp_path)
    store.save(Settings(autostart=True))
    result = SettingsStore(store.path).load()
    assert result.warning is None
    assert result.settings.autostart is True
    assert json.loads(store.path.read_text(encoding="utf-8"))["autostart"] is True


@pytest.mark.parametrize("bad", ["yes", 1, None, "false"])
def test_a_non_boolean_autostart_loads_as_off_with_a_warning(tmp_path, bad):
    store = _store(tmp_path)
    _write_json(store.path, {"autostart": bad})
    result = store.load()
    assert result.settings.autostart is False
    assert result.warning is not None and "autostart" in result.warning


# ----------------------------------------------------------------- saving the choice


def test_a_saved_choice_writes_the_run_entry_for_the_installed_exe(tmp_path):
    env = _env(tmp_path)
    # The installed copy is the version that current.json names, under versions\ (the layout install.py writes).
    installed = inst.install_dir(env) / "versions" / "0.2.0" / inst.EXE_NAME
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"MZ-fake")
    updater.write_current(inst.install_dir(env), "0.2.0")
    registry = FakeRegistry()
    chosen = Settings(autostart=True, local_url="http://127.0.0.1:7100")

    outcome = fr.save_first_run(_store(tmp_path), chosen, env=env, registry=registry)

    assert outcome.notice is None
    assert outcome.settings == chosen
    assert _store(tmp_path).load().settings == chosen
    assert registry.run_value() == f'"{installed}"'


def test_autostart_chosen_before_install_is_kept_and_explained(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.frozen", False, raising=False)
    registry = FakeRegistry()

    outcome = fr.save_first_run(_store(tmp_path), Settings(autostart=True), env=_env(tmp_path), registry=registry)

    assert _store(tmp_path).load().settings.autostart is True
    assert outcome.notice is not None and "install.cmd" in outcome.notice
    assert registry.run_value() is None


def test_autostart_off_writes_no_run_entry_and_no_notice(tmp_path):
    registry = FakeRegistry()
    outcome = fr.save_first_run(_store(tmp_path), Settings(), env=_env(tmp_path), registry=registry)
    assert outcome.notice is None
    assert inst.RUN_KEY not in registry.keys
    assert _store(tmp_path).load().settings == Settings()


def test_a_failed_save_is_reported_and_nothing_is_written(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def refuse(settings):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(store, "save", refuse)
    registry = FakeRegistry()

    outcome = fr.save_first_run(store, Settings(autostart=True), env=_env(tmp_path), registry=registry)

    assert outcome.notice is not None and "could not save your choices" in outcome.notice
    assert registry.keys == {}


# ----------------------------------------------------------------- the dialog


@pytest.fixture
def dialog(qtbot):
    widget = fr.FirstRunDialog(Settings())
    qtbot.addWidget(widget)
    return widget


def test_defaults_are_local_with_autostart_off(dialog):
    assert dialog.local_button.isChecked()
    assert not dialog.hosted_button.isChecked()
    assert dialog.address_field.text() == DEFAULT_LOCAL_URL
    assert not dialog.autostart_box.isChecked()
    assert dialog.settings() == Settings()


def test_hosted_keeps_the_saved_local_address_and_disables_the_field(qtbot):
    widget = fr.FirstRunDialog(Settings(local_url="http://127.0.0.1:7100"))
    qtbot.addWidget(widget)
    widget.hosted_button.setChecked(True)

    assert not widget.address_field.isEnabled()
    chosen = widget.settings()
    assert chosen.mode == "hosted"
    assert chosen.local_url == "http://127.0.0.1:7100"


def test_the_address_field_follows_the_mode_both_ways(dialog):
    dialog.hosted_button.setChecked(True)
    assert not dialog.address_field.isEnabled()
    dialog.local_button.setChecked(True)
    assert dialog.address_field.isEnabled()
    assert dialog.settings().mode == "local"


def test_save_refuses_a_bad_local_address_and_says_why(dialog):
    dialog.address_field.setText("ftp://127.0.0.1")
    dialog.accept()

    assert dialog.saved is False
    assert "http://" in dialog.error_label.text()
    assert dialog.error_label.isHidden() is False


def test_save_keeps_a_good_local_address_and_the_autostart_choice(dialog):
    dialog.address_field.setText(" http://127.0.0.1:7100 ")
    dialog.autostart_box.setChecked(True)
    dialog.accept()

    assert dialog.saved is True
    assert dialog.settings() == replace(Settings(), local_url="http://127.0.0.1:7100", autostart=True)


def test_every_label_in_the_dialog_can_be_selected(dialog):
    labels = dialog.findChildren(QLabel)
    assert len(labels) >= 5
    assert [label.text() for label in labels if not _selectable(label)] == []


# ----------------------------------------------------------------- the whole first run


def test_closing_the_dialog_without_save_keeps_the_defaults_and_autostart_off(tmp_path, monkeypatch, qtbot):
    def close_without_save(self):
        self.autostart_box.setChecked(True)
        return 0

    monkeypatch.setattr(fr.FirstRunDialog, "exec", close_without_save)
    registry = FakeRegistry()

    outcome = fr.run_first_run(_store(tmp_path), env=_env(tmp_path), registry=registry)

    # Nothing was chosen, so the mode is not explicit: a later launch may still pick hosted when a token is saved.
    assert outcome.settings == replace(Settings(), mode_explicit=False)
    assert _store(tmp_path).load().settings == replace(Settings(), mode_explicit=False)
    assert registry.keys == {}
    assert fr.needs_first_run(_store(tmp_path), smoke=False) is False


def test_the_saved_dialog_answers_are_what_gets_stored(tmp_path, monkeypatch, qtbot):
    def save_hosted(self):
        self.hosted_button.setChecked(True)
        self.autostart_box.setChecked(True)
        self.accept()
        return 1

    monkeypatch.setattr(fr.FirstRunDialog, "exec", save_hosted)

    outcome = fr.run_first_run(_store(tmp_path), env=_env(tmp_path), registry=FakeRegistry())

    stored = _store(tmp_path).load().settings
    assert stored.mode == "hosted"
    assert stored.autostart is True
    # Meridian is not installed in this test, so the notice says what to do next.
    assert outcome.notice is not None and "install.cmd" in outcome.notice


def test_smoke_run_never_shows_the_first_run_dialog(tmp_path, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("the first-run dialog must not run under --smoke")

    monkeypatch.setattr(ui_app, "run_first_run", must_not_run)
    path = tmp_path / "settings" / "settings.json"

    assert ui_app.main(["--smoke"], settings_path=path) == 0
    assert not path.exists()


def test_a_real_start_without_settings_asks_first(tmp_path, monkeypatch):
    class Asked(Exception):
        pass

    def ask(*args, **kwargs):
        raise Asked()

    monkeypatch.setattr(ui_app, "run_first_run", ask)

    # The dialog runs before the window is built, so the event loop is never reached.
    with pytest.raises(Asked):
        ui_app.main([], settings_path=tmp_path / "settings" / "settings.json")


def test_a_mode_chosen_in_the_first_run_dialog_is_explicit_so_a_later_launch_keeps_it(tmp_path):
    chosen = fr.settings_from_choices(
        replace(Settings(), mode_explicit=False), mode="local", local_url=DEFAULT_LOCAL_URL, autostart=False,
    )
    assert chosen.mode_explicit is True

    store = _store(tmp_path)
    store.save(chosen)

    # A saved token must not move a file whose owner chose local at the first run.
    assert SettingsStore(store.path).resolve_launch_mode(lambda: True) == "local"
