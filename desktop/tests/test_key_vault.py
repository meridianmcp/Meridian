"""The keys vault (meridian_desktop/key_vault.py): create, unlock, save, lock, and the failures that must not leak anything.

These run the real scrypt and AES-GCM on a temp folder. No test reads the owner's real folder, starts a process or opens a
socket. The password and the values are fake."""

import json

import pytest

from meridian_desktop import key_vault
from meridian_desktop.key_vault import VaultError

PASSWORD = "fake-vault-password-0001"
OTHER = "fake-vault-password-0002"
VALUE_A = "FAKE-VALUE-ALPHA-0001"
VALUE_B = "FAKE-VALUE-BRAVO-0002"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "keys-vault.json"


def test_a_new_vault_opens_empty_and_holds_what_is_saved(path):
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE_A)
    vault.save()
    vault.lock()

    reopened = key_vault.unlock(path, PASSWORD)

    assert reopened.names() == ["OpenAI"]
    assert reopened.get("OpenAI") == VALUE_A


def test_the_file_holds_no_plaintext_name_or_value(path):
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE_A)
    vault.save()

    text = path.read_text(encoding="utf-8")

    assert VALUE_A not in text
    assert "OpenAI" not in text
    document = json.loads(text)
    assert set(document) == {"version", "kdf", "salt", "nonce", "ciphertext"}


def test_a_wrong_password_opens_nothing_and_says_so_without_the_password(path):
    key_vault.create(path, PASSWORD).save()

    with pytest.raises(VaultError) as raised:
        key_vault.unlock(path, OTHER)

    assert raised.value.args[0] == key_vault.WRONG_PASSWORD_TEXT
    assert OTHER not in str(raised.value) and PASSWORD not in str(raised.value)


def test_a_changed_ciphertext_fails_like_a_wrong_password(path):
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE_A)
    vault.save()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["ciphertext"] = document["ciphertext"][:-4] + ("AAAA" if not document["ciphertext"].endswith("AAAA") else "BBBB")
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(VaultError) as raised:
        key_vault.unlock(path, PASSWORD)

    assert raised.value.args[0] == key_vault.WRONG_PASSWORD_TEXT


def test_a_changed_header_fails_because_the_header_is_authenticated(path):
    key_vault.create(path, PASSWORD).save()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["kdf"]["n"] = 2**14
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(VaultError) as raised:
        key_vault.unlock(path, PASSWORD)

    assert raised.value.args[0] == key_vault.DAMAGED_TEXT


@pytest.mark.parametrize("content", ["not json", "[]", json.dumps({"version": 99})])
def test_a_damaged_file_is_named_as_damaged_and_never_opened(path, content):
    path.write_text(content, encoding="utf-8")

    with pytest.raises(VaultError) as raised:
        key_vault.unlock(path, PASSWORD)

    assert raised.value.args[0] == key_vault.DAMAGED_TEXT


def test_a_missing_vault_is_not_created_by_unlock(path):
    with pytest.raises(VaultError) as raised:
        key_vault.unlock(path, PASSWORD)

    assert raised.value.args[0] == key_vault.NOT_FOUND_TEXT
    assert not path.exists()


def test_create_never_replaces_an_existing_vault(path):
    key_vault.create(path, PASSWORD).save()
    before = path.read_text(encoding="utf-8")

    with pytest.raises(VaultError) as raised:
        key_vault.create(path, OTHER)

    assert raised.value.args[0] == key_vault.ALREADY_EXISTS_TEXT
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("password", ["", "short", "x" * (key_vault.MIN_PASSWORD_LENGTH - 1)])
def test_a_short_password_is_refused_before_anything_is_written(path, password):
    with pytest.raises(VaultError) as raised:
        key_vault.create(path, password)

    assert raised.value.args[0] == key_vault.SHORT_PASSWORD_TEXT
    assert not path.exists()


def test_each_save_uses_a_fresh_nonce(path):
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE_A)
    vault.save()
    first = json.loads(path.read_text(encoding="utf-8"))["nonce"]
    vault.put("OpenAI", VALUE_B)
    vault.save()
    second = json.loads(path.read_text(encoding="utf-8"))["nonce"]

    assert first != second


def test_a_removed_name_is_gone_after_save_and_reopen(path):
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE_A)
    vault.put("Anthropic", VALUE_B)
    vault.save()
    vault.remove("OpenAI")
    vault.save()

    assert key_vault.unlock(path, PASSWORD).names() == ["Anthropic"]


def test_lock_forgets_every_value_in_memory(path):
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE_A)
    vault.lock()

    assert not vault.is_open
    assert vault.names() == [] and vault.get("OpenAI") is None
    with pytest.raises(VaultError):
        vault.put("OpenAI", VALUE_B)


def test_a_save_that_cannot_be_written_says_so_and_leaves_the_old_file(path, monkeypatch):
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE_A)
    vault.save()
    before = path.read_text(encoding="utf-8")

    def fail_replace(src, dst):
        raise OSError("the disk went away")

    monkeypatch.setattr(key_vault.os, "replace", fail_replace)
    vault.put("OpenAI", VALUE_B)
    with pytest.raises(VaultError) as raised:
        vault.save()

    assert raised.value.args[0] == key_vault.SAVE_FAILED_TEXT
    assert path.read_text(encoding="utf-8") == before
    assert "disk went away" not in str(raised.value)
