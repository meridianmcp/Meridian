"""Import keys from a password-protected vault file: the pure import (meridian_desktop/key_import.py) and the Import dialog that
the Keys tab opens from its keys-import-vault button (ui/key_import_dialog.py).

Every value and password here is fake. The vault is a temp file, the keychain is a MemoryKeyring created for each test, the
settings are a temp file, and the Windows user variables go to a FakeUserRegistry, so nothing real is touched. The file picker
is replaced by a function that returns the temp vault path, so no real dialog opens.
"""

import logging

import pytest
from PySide6.QtWidgets import QCheckBox, QFrame, QLabel, QLineEdit, QPushButton

from _fakes import FakeUserRegistry, MemoryKeyring
from meridian_desktop import key_vault
from meridian_desktop.api_keys import STATUS_NOT_SET, STATUS_SET, ApiKeys
from meridian_desktop.key_import import NOT_SAVED_TEXT, import_checked, is_already_saved
from meridian_desktop.key_vault import VaultError
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.key_import_dialog import EMPTY_VAULT_TEXT, KeyImportDialog
from meridian_desktop.ui.keys_section import KeysSection
from meridian_desktop.user_environment import UserEnvironment

PASSWORD = "fake-import-pass-0001"
WRONG_PASSWORD = "fake-wrong-pass-0002"
OPENAI_VALUE = "sk-fake-import-ALPHA-0001"
ACME_VALUE = "sk-fake-import-BRAVO-0002"
OLD_VALUE = "sk-fake-import-OLD-0003"


@pytest.fixture
def folder(tmp_path):
    return tmp_path


@pytest.fixture
def vault_file(folder):
    return folder / "vault" / key_vault.VAULT_FILE_NAME


@pytest.fixture
def backend():
    """A keychain of its own for each test. The session-wide one is shared, so a test never uses it."""
    return MemoryKeyring()


@pytest.fixture
def registry():
    """The fake Windows user environment. An import must leave it empty."""
    return FakeUserRegistry()


def _write_vault(path, entries, password=PASSWORD):
    """A vault file with ``entries`` in it, written the way the critical vault writes one."""
    vault = key_vault.create(path, password)
    for name, value in entries.items():
        vault.put(name, value)
    vault.save()
    vault.lock()
    return path


def _keys(folder, backend, registry):
    settings = SettingsStore(folder / "settings.json")
    return ApiKeys(settings, KeyringSecretStore(backend=backend), user_environment=UserEnvironment(registry))


def _section(qtbot, keys, vault_file):
    section = KeysSection(keys, None, choose_vault=lambda parent: str(vault_file))
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section


def _open_dialog(section):
    section.findChild(QPushButton, "keys-import-vault").click()
    dialog = section.findChild(KeyImportDialog, "keys-import-dialog")
    assert dialog is not None
    return dialog


def _unlock(dialog, password):
    dialog.findChild(QLineEdit, "keys-import-password").setText(password)
    dialog.findChild(QPushButton, "keys-import-unlock").click()


def _text_of(dialog):
    """Every label, button, checkbox and line of the dialog. A value or the password must appear in none of them."""
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    texts += [button.text() for button in dialog.findChildren(QPushButton)]
    texts += [box.text() for box in dialog.findChildren(QCheckBox)]
    texts += [field.text() for field in dialog.findChildren(QLineEdit)]
    return "\n".join(texts)


# ----------------------------------------------------------------- the pure import


def test_ticked_names_are_saved_to_the_keychain(folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    vault = key_vault.unlock(vault_file, PASSWORD)

    result = import_checked(keys, vault, ["OpenAI", "Acme Lab"], replace=False)

    # The vault lists its names sorted, so the import follows that order: "Acme Lab" comes before "OpenAI".
    assert result.imported == ("Acme Lab", "OpenAI")
    assert result.kept == () and result.refused == () and result.skipped == ()
    assert keys.read_value("OpenAI") == OPENAI_VALUE
    assert keys.read_value("Acme Lab") == ACME_VALUE
    assert keys.status("Acme Lab") == STATUS_SET
    assert "Acme Lab" in keys.names()


def test_a_wrong_password_raises_and_writes_nothing(folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE})
    keys = _keys(folder, backend, registry)
    before = backend.snapshot()

    with pytest.raises(VaultError) as caught:
        key_vault.unlock(vault_file, WRONG_PASSWORD)

    assert str(caught.value) == key_vault.WRONG_PASSWORD_TEXT
    assert backend.snapshot() == before, "the keychain is unchanged after a wrong password"
    assert not (folder / "settings.json").exists(), "the settings file is not written after a wrong password"
    assert keys.status("OpenAI") == STATUS_NOT_SET
    assert registry.writes == []


def test_unticked_names_are_skipped(folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    vault = key_vault.unlock(vault_file, PASSWORD)

    result = import_checked(keys, vault, ["OpenAI"], replace=False)

    assert result.imported == ("OpenAI",)
    assert result.skipped == ("Acme Lab",)
    assert keys.status("Acme Lab") == STATUS_NOT_SET
    assert "Acme Lab" not in keys.names()


def test_existing_names_are_kept_unless_replace_is_ticked(folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE})
    keys = _keys(folder, backend, registry)
    keys.save("OpenAI", OLD_VALUE)
    vault = key_vault.unlock(vault_file, PASSWORD)

    kept = import_checked(keys, vault, ["OpenAI"], replace=False)
    assert kept.kept == ("OpenAI",)
    assert kept.imported == ()
    assert keys.read_value("OpenAI") == OLD_VALUE, "a kept name keeps its saved value"

    replaced = import_checked(keys, vault, ["OpenAI"], replace=True)
    assert replaced.imported == ("OpenAI",)
    assert replaced.kept == ()
    assert keys.read_value("OpenAI") == OPENAI_VALUE


def test_a_name_that_cannot_be_a_key_name_is_refused_without_its_value(folder, vault_file, backend, registry):
    _write_vault(vault_file, {"bad/name": ACME_VALUE, "OpenAI": OPENAI_VALUE})
    keys = _keys(folder, backend, registry)
    vault = key_vault.unlock(vault_file, PASSWORD)

    result = import_checked(keys, vault, ["bad/name", "OpenAI"], replace=False)

    assert result.imported == ("OpenAI",)
    assert [refusal.name for refusal in result.refused] == ["bad/name"]
    assert ACME_VALUE not in result.refused[0].reason
    assert all(ACME_VALUE not in saved for saved in backend.snapshot().values()), "the refused value is never saved"


def test_a_second_name_for_the_same_key_is_refused(folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "openai": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    vault = key_vault.unlock(vault_file, PASSWORD)

    result = import_checked(keys, vault, ["OpenAI", "openai"], replace=True)

    assert result.imported == ("OpenAI",)
    assert [refusal.name for refusal in result.refused] == ["openai"]
    assert keys.read_value("OpenAI") == OPENAI_VALUE, "the first name's value is the one kept"


def test_a_keychain_write_that_fails_is_refused_with_a_fixed_sentence(folder, vault_file, backend, registry, monkeypatch):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE})
    keys = _keys(folder, backend, registry)
    vault = key_vault.unlock(vault_file, PASSWORD)

    def refuse(name, value):
        raise OSError(f"write refused while holding {value}")

    monkeypatch.setattr(keys, "save", refuse)

    result = import_checked(keys, vault, ["OpenAI"], replace=False)

    assert result.imported == ()
    assert result.refused[0].reason == NOT_SAVED_TEXT
    assert OPENAI_VALUE not in result.refused[0].reason


def test_import_never_writes_the_windows_user_variable(folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    vault = key_vault.unlock(vault_file, PASSWORD)

    result = import_checked(keys, vault, ["OpenAI", "Acme Lab"], replace=True)

    assert len(result.imported) == 2
    assert registry.writes == [], "the fake registry records no write"
    assert registry.values == {}
    assert registry.announcements == 0


def test_the_password_and_values_never_reach_the_settings_file_or_the_log(folder, vault_file, backend, registry, caplog):
    caplog.set_level(logging.DEBUG)
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    vault = key_vault.unlock(vault_file, PASSWORD)
    import_checked(keys, vault, ["OpenAI", "Acme Lab"], replace=False)
    with pytest.raises(VaultError):
        key_vault.unlock(vault_file, WRONG_PASSWORD)

    settings_text = (folder / "settings.json").read_text(encoding="utf-8")
    assert "Key vault import" in caplog.text, "the import logs its counts"
    for secret in (PASSWORD, WRONG_PASSWORD, OPENAI_VALUE, ACME_VALUE):
        assert secret not in settings_text
        assert secret not in caplog.text


def test_is_already_saved_is_false_for_a_name_that_is_not_valid(folder, backend, registry):
    keys = _keys(folder, backend, registry)
    keys.save("OpenAI", OLD_VALUE)

    assert is_already_saved(keys, "OpenAI") is True
    assert is_already_saved(keys, "Acme Lab") is False
    assert is_already_saved(keys, "bad/name") is False


# ----------------------------------------------------------------- the dialog from the Keys tab


def test_the_import_button_opens_a_locked_dialog_for_the_chosen_file(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE})
    keys = _keys(folder, backend, registry)
    section = _section(qtbot, keys, vault_file)

    dialog = _open_dialog(section)

    password = dialog.findChild(QLineEdit, "keys-import-password")
    assert password.echoMode() == QLineEdit.EchoMode.Password, "the password is never echoed"
    assert dialog.findChild(QPushButton, "keys-import-go").isEnabled() is False
    assert dialog.findChild(QCheckBox, "keys-import-replace").isHidden()
    assert dialog.findChild(QCheckBox, "keys-import-name-openai") is None, "no name is listed before unlocking"
    assert keys.status("OpenAI") == STATUS_NOT_SET, "opening the dialog imports nothing"


def test_a_wrong_password_in_the_dialog_shows_an_error_and_writes_nothing(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE})
    keys = _keys(folder, backend, registry)
    section = _section(qtbot, keys, vault_file)
    before = backend.snapshot()
    dialog = _open_dialog(section)

    _unlock(dialog, WRONG_PASSWORD)

    assert dialog.findChild(QLabel, "keys-import-status").text() == key_vault.WRONG_PASSWORD_TEXT
    assert dialog.findChild(QLineEdit, "keys-import-password").text() == "", "the password field is cleared"
    assert dialog.findChild(QCheckBox, "keys-import-name-openai") is None
    assert backend.snapshot() == before
    assert registry.writes == []


def test_unlocking_lists_every_name_ticked_with_replace_off(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    section = _section(qtbot, keys, vault_file)
    dialog = _open_dialog(section)

    _unlock(dialog, PASSWORD)

    for key in ("openai", "acme-lab"):
        box = dialog.findChild(QCheckBox, f"keys-import-name-{key}")
        assert box is not None and box.isChecked(), f"{key} is ticked by default"
        assert not box.isHidden(), f"{key} is shown once the vault is unlocked"
    replace = dialog.findChild(QCheckBox, "keys-import-replace")
    assert replace.isChecked() is False, "Replace existing keys is off by default"
    assert not replace.isHidden()
    assert dialog.findChild(QPushButton, "keys-import-go").isEnabled()
    assert dialog.findChild(QLineEdit, "keys-import-password").text() == "", "the password is cleared after unlocking"


def test_an_empty_vault_file_says_there_is_nothing_to_import(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {})
    keys = _keys(folder, backend, registry)
    section = _section(qtbot, keys, vault_file)
    dialog = _open_dialog(section)

    _unlock(dialog, PASSWORD)

    assert dialog.findChild(QLabel, "keys-import-status").text() == EMPTY_VAULT_TEXT
    assert dialog.findChild(QPushButton, "keys-import-go").isEnabled() is False


def test_importing_the_ticked_names_saves_them_and_reports_the_count(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    section = _section(qtbot, keys, vault_file)
    dialog = _open_dialog(section)
    _unlock(dialog, PASSWORD)
    dialog.findChild(QCheckBox, "keys-import-name-acme-lab").setChecked(False)
    received = []
    dialog.imported.connect(received.append)

    dialog.findChild(QPushButton, "keys-import-go").click()

    assert keys.read_value("OpenAI") == OPENAI_VALUE
    assert keys.status("Acme Lab") == STATUS_NOT_SET, "the unticked name is not imported"
    assert received == [1]
    result_text = dialog.findChild(QLabel, "keys-import-result").text()
    assert "Imported 1 key" in result_text
    assert OPENAI_VALUE not in result_text
    assert dialog.findChild(QPushButton, "keys-import-go").isEnabled() is False, "the vault is locked after an import"
    message = section.findChild(QLabel, "keys-message").text()
    assert "Imported 1 key" in message
    assert "Windows user variables were not changed" in message
    assert section.findChild(QFrame, "keys-entry-openai") is not None, "the imported key is listed on the Keys tab"


def test_an_existing_name_is_kept_unless_replace_is_ticked_in_the_dialog(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE})
    keys = _keys(folder, backend, registry)
    keys.save("OpenAI", OLD_VALUE)
    section = _section(qtbot, keys, vault_file)
    dialog = _open_dialog(section)
    _unlock(dialog, PASSWORD)
    note = dialog.findChild(QLabel, "keys-import-note-openai")
    assert note is not None and not note.isHidden(), "an existing name carries a note"

    dialog.findChild(QPushButton, "keys-import-go").click()

    assert keys.read_value("OpenAI") == OLD_VALUE
    assert "Kept 1 existing key" in dialog.findChild(QLabel, "keys-import-result").text()

    _unlock(dialog, PASSWORD)
    dialog.findChild(QCheckBox, "keys-import-replace").setChecked(True)
    dialog.findChild(QPushButton, "keys-import-go").click()

    assert keys.read_value("OpenAI") == OPENAI_VALUE


def test_the_dialog_never_shows_a_value_or_the_password(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    section = _section(qtbot, keys, vault_file)
    dialog = _open_dialog(section)
    _unlock(dialog, PASSWORD)

    text = _text_of(dialog)
    assert OPENAI_VALUE not in text and ACME_VALUE not in text
    assert PASSWORD not in text

    dialog.findChild(QPushButton, "keys-import-go").click()
    text = _text_of(dialog)
    assert OPENAI_VALUE not in text and ACME_VALUE not in text and PASSWORD not in text


def test_the_dialog_import_never_writes_the_windows_user_variable(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE, "Acme Lab": ACME_VALUE})
    keys = _keys(folder, backend, registry)
    section = _section(qtbot, keys, vault_file)
    dialog = _open_dialog(section)
    _unlock(dialog, PASSWORD)
    dialog.findChild(QCheckBox, "keys-import-replace").setChecked(True)

    dialog.findChild(QPushButton, "keys-import-go").click()

    assert keys.status("OpenAI") == STATUS_SET and keys.status("Acme Lab") == STATUS_SET
    assert registry.writes == []
    assert registry.values == {}
    assert registry.announcements == 0


def test_closing_the_dialog_without_importing_writes_nothing(qtbot, folder, vault_file, backend, registry):
    _write_vault(vault_file, {"OpenAI": OPENAI_VALUE})
    keys = _keys(folder, backend, registry)
    section = _section(qtbot, keys, vault_file)
    before = backend.snapshot()
    dialog = _open_dialog(section)
    _unlock(dialog, PASSWORD)

    dialog.reject()

    assert backend.snapshot() == before
    assert registry.writes == []
    assert dialog.findChild(QCheckBox, "keys-import-name-openai") is None, "closing hides every value and name"
