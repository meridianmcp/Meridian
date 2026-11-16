"""Fixes from the plain-file import review.

1. After an import, Replace goes back to off and the owner's ticks are kept, so a second press cannot overwrite a value.
2. The real Yes and No question (ask_plain_import) answers Yes only on a click on Yes, and No on No or Escape.
3. A name whose keychain cannot be read is refused with a fixed sentence when Replace is off, and nothing is saved.

Every value here is fake. The keychain is in memory, the settings are a temp file, the Windows user variables go to a fake
registry, and the keys file is a temp file.
"""

import json

import pytest
from PySide6.QtWidgets import QCheckBox, QMessageBox, QPushButton

from _fakes import FakeUserRegistry, MemoryKeyring
from meridian_desktop import keys_file
from meridian_desktop.api_keys import ApiKeys, slug
from meridian_desktop.key_import import KEYCHAIN_UNREADABLE_TEXT, import_plain_checked
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.key_import_plain_dialog import (
    NO_OBJECT_NAME,
    YES_OBJECT_NAME,
    KeyPlainImportDialog,
    ask_plain_import,
)
from meridian_desktop.user_environment import UserEnvironment

FILE_OPENAI = "sk-fake-fixes-FILE-0001"
OTHER_OPENAI = "sk-fake-fixes-OTHER-0002"
FILE_ACME = "sk-fake-fixes-ACME-0003"


class FailingReadKeyring(MemoryKeyring):
    """A keychain whose reads always fail, as a locked or damaged store would. Writes still work."""

    def get_password(self, service, account):
        raise RuntimeError("keychain read failed")


def _keys(folder, backend=None):
    settings = SettingsStore(folder / "settings.json")
    backend = backend if backend is not None else MemoryKeyring()
    keys = ApiKeys(settings, KeyringSecretStore(backend=backend), user_environment=UserEnvironment(FakeUserRegistry()))
    return keys, backend


def _file(folder, data):
    path = folder / "service.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _dialog(qtbot, path, keys):
    listing = keys_file.read_names(path)
    # The Windows-variable question is declined, so these tests exercise only the keychain import.
    dialog = KeyPlainImportDialog(path, listing, keys, None, confirm_environment=lambda *_: False)
    qtbot.addWidget(dialog)
    return dialog


def _box(dialog, name) -> QCheckBox:
    return dialog.findChild(QCheckBox, f"keys-plain-import-name-{slug(name)}")


def _press_import(dialog) -> None:
    dialog.findChild(QPushButton, "keys-plain-import-go").click()


def _answer_as(monkeypatch, object_name: str | None) -> None:
    """Inject the owner's answer to the modal question: click the named button, or press Escape (the No button).

    The question itself is the real one from ask_plain_import; only the wait for a human is replaced, as the test rules require.
    """

    def answer(self, *args, **kwargs):
        if object_name is None:
            self.escapeButton().click()
        else:
            self.findChild(QPushButton, object_name).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer)


# --------------------------------------------------------------- 1. the second press

def test_a_second_press_with_replace_ticked_earlier_does_not_overwrite_a_value(qtbot, tmp_path):
    keys, _backend = _keys(tmp_path)
    keys.save("OpenAI", OTHER_OPENAI)
    path = _file(tmp_path, {"OpenAI": FILE_OPENAI})
    dialog = _dialog(qtbot, path, keys)

    dialog.findChild(QCheckBox, "keys-plain-import-replace").setChecked(True)
    _press_import(dialog)
    assert keys.read_value("OpenAI") == FILE_OPENAI, "the first press, with Replace ticked, replaces the value"
    assert dialog.findChild(QCheckBox, "keys-plain-import-replace").isChecked() is False, "Replace goes back to off"

    keys.save("OpenAI", OTHER_OPENAI)  # the saved value changes between presses
    _press_import(dialog)

    assert keys.read_value("OpenAI") == OTHER_OPENAI, "a second press without Replace ticked must keep the value"


def test_an_unticked_name_stays_unticked_after_an_import(qtbot, tmp_path):
    keys, _backend = _keys(tmp_path)
    path = _file(tmp_path, {"OpenAI": FILE_OPENAI, "Acme": FILE_ACME})
    dialog = _dialog(qtbot, path, keys)

    _box(dialog, "Acme").setChecked(False)
    _press_import(dialog)

    assert _box(dialog, "OpenAI").isChecked() is True
    assert _box(dialog, "Acme").isChecked() is False, "the owner's untick is not undone by the import"
    assert keys.status("Acme") == "not_set", "the unticked name was not imported"


# --------------------------------------------------------------- 2. the real Yes and No question

def test_clicking_yes_on_the_real_question_returns_true(monkeypatch):
    _answer_as(monkeypatch, YES_OBJECT_NAME)
    assert ask_plain_import(None, "Import keys", "Read this file?") is True


def test_clicking_no_on_the_real_question_returns_false(monkeypatch):
    _answer_as(monkeypatch, NO_OBJECT_NAME)
    assert ask_plain_import(None, "Import keys", "Read this file?") is False


def test_escape_on_the_real_question_returns_false(monkeypatch):
    _answer_as(monkeypatch, None)
    assert ask_plain_import(None, "Import keys", "Read this file?") is False


# --------------------------------------------------------------- 3. an unreadable keychain

def test_a_name_whose_keychain_cannot_be_read_is_refused_when_replace_is_off(tmp_path):
    backend = FailingReadKeyring()
    keys, _ = _keys(tmp_path, backend)
    path = _file(tmp_path, {"OpenAI": FILE_OPENAI})
    listing = keys_file.read_names(path)

    result = import_plain_checked(keys, path, listing.names, ["OpenAI"], replace=False)

    assert result.kept == ()
    assert result.imported == ()
    assert [(refusal.name, refusal.reason) for refusal in result.refused] == [("OpenAI", KEYCHAIN_UNREADABLE_TEXT)]
    assert backend.snapshot() == {}, "nothing is written when the keychain cannot be read for the name"
