"""The Windows-variable step of the plain-file import: each imported key can also be set as a Windows user variable, after one
confirmation (meridian_desktop/ui/key_import_plain_dialog.py).

Every value here is fake. The keychain is in memory, the registry is a fake, the settings are a temp file, and the keys file
is a temp file.
"""

import json

from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

from _fakes import FakeUserRegistry, MemoryKeyring
from meridian_desktop import keys_file
from meridian_desktop.api_keys import STATUS_SET, ApiKeys
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.key_import_plain_dialog import KeyPlainImportDialog
from meridian_desktop.user_environment import UserEnvironment, UserEnvironmentError

S3_KEY = "fake-s3-key-id-0001"
S3_SECRET = "fake-s3-secret-0002"


def _keys(folder, registry):
    settings = SettingsStore(folder / "settings.json")
    return ApiKeys(settings, KeyringSecretStore(backend=MemoryKeyring()), user_environment=UserEnvironment(registry))


def _file(folder):
    path = folder / "service.json"
    path.write_text(json.dumps({"RUNPOD_S3_KEY": S3_KEY, "RUNPOD_S3_KEY_SECRET": S3_SECRET}), encoding="utf-8")
    return path


def _dialog(qtbot, path, keys, confirm):
    dialog = KeyPlainImportDialog(path, keys_file.read_names(path), keys, None, confirm_environment=confirm)
    qtbot.addWidget(dialog)
    return dialog


def _press(dialog):
    dialog.findChild(QPushButton, "keys-plain-import-go").click()


def _result(dialog):
    return dialog.findChild(QLabel, "keys-plain-import-result").text()


def test_the_windows_variable_box_is_unticked_by_default(qtbot, tmp_path):
    keys = _keys(tmp_path, FakeUserRegistry())
    dialog = _dialog(qtbot, _file(tmp_path), keys, confirm=lambda *_: False)

    assert dialog.findChild(QCheckBox, "keys-plain-import-env").isChecked() is False


def _tick_windows_variables(dialog):
    """The owner ticks the Windows user variables box. The default leaves it unticked, so each test that needs it ticks it."""
    dialog.findChild(QCheckBox, "keys-plain-import-env").setChecked(True)


def test_yes_sets_each_imported_key_as_a_windows_user_variable_after_one_question(qtbot, tmp_path):
    registry = FakeUserRegistry()
    keys = _keys(tmp_path, registry)
    asked = []

    def confirm(parent, title, text):
        asked.append(text)
        return True

    dialog = _dialog(qtbot, _file(tmp_path), keys, confirm)
    _tick_windows_variables(dialog)
    _press(dialog)

    assert registry.writes == [("RUNPOD_S3_KEY", S3_KEY), ("RUNPOD_S3_KEY_SECRET", S3_SECRET)]
    assert len(asked) == 1, "one question covers the whole import"
    assert S3_KEY not in asked[0] and S3_SECRET not in asked[0], "the question never shows a value"
    assert keys.status("RUNPOD_S3_KEY") == STATUS_SET
    text = _result(dialog)
    assert "Set as Windows user variables: RUNPOD_S3_KEY, RUNPOD_S3_KEY_SECRET" in text
    assert "The keys file was not changed." in text
    assert S3_KEY not in text and S3_SECRET not in text


def test_no_sets_no_windows_variable_and_the_keychain_keeps_the_values(qtbot, tmp_path):
    registry = FakeUserRegistry()
    keys = _keys(tmp_path, registry)
    dialog = _dialog(qtbot, _file(tmp_path), keys, confirm=lambda *_: False)
    _tick_windows_variables(dialog)

    _press(dialog)

    assert registry.writes == []
    assert keys.status("RUNPOD_S3_KEY") == STATUS_SET
    assert "No Windows user variable was set." in _result(dialog)


def test_an_unticked_box_asks_nothing_and_sets_nothing(qtbot, tmp_path):
    registry = FakeUserRegistry()
    keys = _keys(tmp_path, registry)
    asked = []
    dialog = _dialog(qtbot, _file(tmp_path), keys, confirm=lambda *args: asked.append(args) or True)
    dialog.findChild(QCheckBox, "keys-plain-import-env").setChecked(False)

    _press(dialog)

    assert asked == []
    assert registry.writes == []
    assert keys.status("RUNPOD_S3_KEY") == STATUS_SET
    assert "The keys file was not changed." not in _result(dialog)


def test_a_failed_windows_write_is_reported_and_the_key_stays_in_the_keychain(qtbot, tmp_path, monkeypatch):
    registry = FakeUserRegistry()
    keys = _keys(tmp_path, registry)
    real_set = keys.set_user_environment

    def fail_for_the_secret(name):
        if name == "RUNPOD_S3_KEY_SECRET":
            raise UserEnvironmentError("user environment write failed")
        return real_set(name)

    monkeypatch.setattr(keys, "set_user_environment", fail_for_the_secret)
    dialog = _dialog(qtbot, _file(tmp_path), keys, confirm=lambda *_: True)
    _tick_windows_variables(dialog)

    _press(dialog)

    assert registry.writes == [("RUNPOD_S3_KEY", S3_KEY)]
    assert keys.status("RUNPOD_S3_KEY_SECRET") == STATUS_SET, "the keychain keeps the key even when its variable fails"
    text = _result(dialog)
    assert (
        "Not set as a Windows user variable, RUNPOD_S3_KEY_SECRET: the Windows user environment could not be written" in text
    )
    assert S3_SECRET not in text
