"""The Critical vault block (ui/key_vault_box.py) and the compact Keys cards (ui/keys_section.py).

A fake keychain holds the values, a temp folder holds the vault and settings, and nothing else is touched: no real keychain,
no socket, no process. Every value and password is fake."""

import json

import pytest
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QPushButton

from meridian_desktop import key_vault
from meridian_desktop.api_keys import ApiKeys
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui import key_vault_box as box_module
from meridian_desktop.ui.key_vault_box import KeyVaultBox
from meridian_desktop.ui.keys_section import KeysSection

PASSWORD = "fake-vault-password-0101"
VALUE = "FAKE-VALUE-CHARLIE-0003"


class MemoryBackend:
    """A keychain stand-in: the three calls the secret store makes, held in a dict."""

    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


@pytest.fixture
def folder(tmp_path):
    return tmp_path


def _box(qtbot, folder, *, critical=("OpenAI",), keychain=None):
    values = keychain if keychain is not None else {"OpenAI": VALUE}
    box = KeyVaultBox(folder / "keys-vault.json", lambda: list(critical), lambda name: values.get(name))
    qtbot.addWidget(box)
    return box


def _status(box):
    return box.findChild(QLabel, "keyvault-status").text()


def _button(box, name):
    return box.findChild(QPushButton, name)


def _field(box, name):
    return box.findChild(QLineEdit, name)


def _create(box, password=PASSWORD, confirm=PASSWORD):
    _field_set(box, "keyvault-password", password)
    _field_set(box, "keyvault-password-confirm", confirm)
    _button(box, "keyvault-create").click()


def _field_set(box, name, text):
    field = box.findChild(QLineEdit, name)
    field.setText(text)


# ----------------------------------------------------------------- the vault states


def test_with_no_vault_the_block_offers_create_and_no_unlock(qtbot, folder):
    box = _box(qtbot, folder)

    assert _status(box) == box_module.NO_VAULT_TEXT
    assert not _button(box, "keyvault-create").isHidden()
    assert _button(box, "keyvault-unlock").isHidden()
    assert not (folder / "keys-vault.json").exists()


def test_two_different_passwords_create_nothing(qtbot, folder):
    box = _box(qtbot, folder)

    _create(box, confirm=PASSWORD + "-typo")

    assert _status(box) == box_module.MISMATCH_TEXT
    assert not (folder / "keys-vault.json").exists()


def test_creating_the_vault_opens_it_and_offers_to_copy_each_critical_key(qtbot, folder):
    box = _box(qtbot, folder)

    _create(box)

    assert _status(box) == box_module.CREATED_TEXT
    assert not _button(box, "keyvault-lock").isHidden()
    assert _field(box, "keyvault-password").isHidden(), "the password is not kept on screen once the vault is open"
    assert not _button(box, "keyvault-copy-openai").isHidden()
    assert (folder / "keys-vault.json").is_file()


def test_copying_a_key_puts_its_value_in_the_encrypted_file_and_keeps_the_keychain_copy(qtbot, folder):
    keychain = {"OpenAI": VALUE}
    box = _box(qtbot, folder, keychain=keychain)
    _create(box)

    _button(box, "keyvault-copy-openai").click()

    assert _status(box) == box_module.COPIED_TEXT.format(name="OpenAI")
    assert keychain == {"OpenAI": VALUE}, "the keychain copy is unchanged"
    text = (folder / "keys-vault.json").read_text(encoding="utf-8")
    assert VALUE not in text and "OpenAI" not in text
    assert key_vault.unlock(folder / "keys-vault.json", PASSWORD).get("OpenAI") == VALUE


def test_reveal_shows_a_value_only_until_it_is_hidden_again(qtbot, folder):
    box = _box(qtbot, folder)
    _create(box)
    _button(box, "keyvault-copy-openai").click()

    reveal = _button(box, "keyvault-reveal-openai")
    reveal.click()
    assert _field(box, "keyvault-value-openai").text() == VALUE
    assert not _field(box, "keyvault-value-openai").isHidden()

    reveal.click()
    assert _field(box, "keyvault-value-openai").text() == ""
    assert _field(box, "keyvault-value-openai").isHidden()


def test_lock_hides_every_value_and_offers_the_password_again(qtbot, folder):
    box = _box(qtbot, folder)
    _create(box)
    _button(box, "keyvault-copy-openai").click()
    _button(box, "keyvault-reveal-openai").click()

    _button(box, "keyvault-lock").click()

    assert _status(box) == box_module.LOCKED_NOW_TEXT
    assert box.findChild(QLineEdit, "keyvault-value-openai") is None
    assert box.findChild(QPushButton, "keyvault-reveal-openai") is None
    assert not _field(box, "keyvault-password").isHidden()
    assert not _button(box, "keyvault-unlock").isHidden()


def test_a_wrong_password_opens_nothing_and_a_right_one_opens_the_vault(qtbot, folder):
    box = _box(qtbot, folder)
    _create(box)
    _button(box, "keyvault-lock").click()

    _field_set(box, "keyvault-password", PASSWORD + "-wrong")
    _button(box, "keyvault-unlock").click()
    assert _status(box) == key_vault.WRONG_PASSWORD_TEXT
    assert _button(box, "keyvault-lock").isHidden()
    assert PASSWORD not in _status(box) and PASSWORD + "-wrong" not in _status(box)

    _field_set(box, "keyvault-password", PASSWORD)
    _button(box, "keyvault-unlock").click()
    assert _status(box) == box_module.UNLOCKED_TEXT
    assert not _button(box, "keyvault-lock").isHidden()


def test_a_key_with_no_keychain_value_is_not_copied(qtbot, folder):
    box = _box(qtbot, folder, keychain={})
    _create(box)

    _button(box, "keyvault-copy-openai").click()

    assert _status(box) == box_module.NO_KEYCHAIN_TEXT.format(name="OpenAI")
    assert key_vault.unlock(folder / "keys-vault.json", PASSWORD).names() == []


def test_a_keychain_that_raises_is_shown_as_no_value_and_leaks_nothing(qtbot, folder):
    def raising(name):
        raise OSError("keychain detail the block must not show")

    box = KeyVaultBox(folder / "keys-vault.json", lambda: ["OpenAI"], raising)
    qtbot.addWidget(box)
    _create(box)

    _button(box, "keyvault-copy-openai").click()

    assert _status(box) == box_module.NO_KEYCHAIN_TEXT.format(name="OpenAI")
    assert "keychain detail" not in _status(box)


def test_the_block_has_no_text_box_for_a_value_until_one_is_revealed(qtbot, folder):
    box = _box(qtbot, folder)
    _create(box)
    _button(box, "keyvault-copy-openai").click()

    value = _field(box, "keyvault-value-openai")
    assert value.text() == "" and value.isHidden() and value.isReadOnly()


# ----------------------------------------------------------------- the compact Keys cards


def _keys(folder):
    backend = MemoryBackend()
    settings = SettingsStore(folder / "settings.json")
    keys = ApiKeys(settings, KeyringSecretStore(backend=backend))
    return keys, backend


def test_each_key_is_one_line_and_its_details_open_with_the_details_button(qtbot, folder):
    keys, _backend = _keys(folder)
    keys.save("OpenAI", VALUE)
    keys.set_critical("OpenAI", True)
    section = KeysSection(keys)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)

    details = section.findChild(QFrame, "keys-entry-openai-details")
    toggle = section.findChild(QPushButton, "keys-entry-openai-details-toggle")
    assert details.isHidden(), "the details are hidden until the owner asks"
    assert toggle.text() == "Details"
    assert not section.findChild(QLabel, "keys-entry-openai-badge").isHidden(), "the Critical badge stays on the one line"

    toggle.click()

    assert not details.isHidden()
    assert toggle.text() == "Hide"
    assert section.findChild(QLineEdit, "keys-entry-openai-value") is not None


# ----------------------------------------------------------------- a failed copy keeps what the vault held


def _failing_write(*_args, **_kwargs):
    raise OSError("the vault file could not be written")


def test_a_failed_copy_over_an_existing_vault_value_puts_the_earlier_value_back(qtbot, folder, monkeypatch):
    keychain = {"OpenAI": VALUE}
    box = _box(qtbot, folder, keychain=keychain)
    _create(box)
    _button(box, "keyvault-copy-openai").click()
    _button(box, "keyvault-lock").click()
    _field_set(box, "keyvault-password", PASSWORD)
    _button(box, "keyvault-unlock").click()
    keychain["OpenAI"] = "FAKE-VALUE-DELTA-0004"
    monkeypatch.setattr(key_vault, "_write", _failing_write)

    box._on_copy("OpenAI")

    assert _status(box) == key_vault.SAVE_FAILED_TEXT
    _button(box, "keyvault-reveal-openai").click()
    assert _field(box, "keyvault-value-openai").text() == VALUE, "the value the vault held before the copy is kept"


def test_a_failed_copy_of_a_new_name_leaves_no_value_in_memory(qtbot, folder, monkeypatch):
    box = _box(qtbot, folder)
    _create(box)
    monkeypatch.setattr(key_vault, "_write", _failing_write)

    _button(box, "keyvault-copy-openai").click()

    assert _status(box) == key_vault.SAVE_FAILED_TEXT
    assert box.findChild(QPushButton, "keyvault-reveal-openai") is None, "the name is not in the vault"
    assert not _button(box, "keyvault-copy-openai").isHidden(), "the copy can be tried again"
