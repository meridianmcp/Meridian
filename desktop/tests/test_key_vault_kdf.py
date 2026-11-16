"""The vault's scrypt cost (meridian_desktop/key_vault.py): new files use n = 2**17, files written at n = 2**15 still unlock and keep
their cost on save, and any other recorded cost is refused as damaged before a key is derived.

These run the real scrypt on a temp folder. The password and the values are fake. A new vault takes about a second to derive,
so each test makes only the vaults it needs."""

import hashlib
import json

import pytest

from meridian_desktop import key_vault
from meridian_desktop.key_vault import VaultError

PASSWORD = "fake-kdf-password-0001"
OTHER = "fake-kdf-password-0002"
VALUE = "FAKE-VALUE-KDF-0001"
VALUE_TWO = "FAKE-VALUE-KDF-0002"
NEW_N = 2**17
OLD_N = 2**15


def _document(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_document(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")


def test_a_new_vault_records_the_new_cost(tmp_path):
    path = tmp_path / key_vault.VAULT_FILE_NAME

    key_vault.create(path, PASSWORD)

    assert _document(path)["kdf"] == {"name": "scrypt", "n": 131072, "r": 8, "p": 1}


def test_unlock_derives_the_key_at_the_cost_the_new_file_records(tmp_path, monkeypatch):
    path = tmp_path / key_vault.VAULT_FILE_NAME
    key_vault.create(path, PASSWORD)
    real_scrypt = hashlib.scrypt
    costs = []

    def record_cost(*args, **kwargs):
        costs.append(kwargs["n"])
        return real_scrypt(*args, **kwargs)

    monkeypatch.setattr(key_vault.hashlib, "scrypt", record_cost)

    key_vault.unlock(path, PASSWORD)

    assert costs == [NEW_N]


def test_an_old_vault_written_at_the_old_cost_unlocks_and_keeps_its_cost_on_save(tmp_path, monkeypatch):
    path = tmp_path / key_vault.VAULT_FILE_NAME
    with monkeypatch.context() as patch:
        patch.setattr(key_vault, "SCRYPT_N", OLD_N)
        vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE)
    vault.save()
    vault.lock()
    assert _document(path)["kdf"] == {"name": "scrypt", "n": 32768, "r": 8, "p": 1}

    reopened = key_vault.unlock(path, PASSWORD)

    assert reopened.names() == ["OpenAI"]
    assert reopened.get("OpenAI") == VALUE

    reopened.put("Anthropic", VALUE_TWO)
    reopened.save()

    assert _document(path)["kdf"] == {"name": "scrypt", "n": 32768, "r": 8, "p": 1}
    assert key_vault.unlock(path, PASSWORD).names() == ["Anthropic", "OpenAI"]


@pytest.mark.parametrize("n", [2**14, 65536, 2**20, 0, -OLD_N])
def test_an_unknown_cost_is_refused_as_damaged_without_deriving_a_key(tmp_path, monkeypatch, n):
    path = tmp_path / key_vault.VAULT_FILE_NAME
    key_vault.create(path, PASSWORD)
    document = _document(path)
    document["kdf"]["n"] = n
    _write_document(path, document)
    derivations = []

    def refuse_derivation(*args, **kwargs):
        derivations.append(kwargs.get("n"))
        raise AssertionError("a key was derived from a cost the vault does not know")

    monkeypatch.setattr(key_vault.hashlib, "scrypt", refuse_derivation)

    with pytest.raises(VaultError) as raised:
        key_vault.unlock(path, PASSWORD)

    assert raised.value.args[0] == key_vault.DAMAGED_TEXT
    assert derivations == []


@pytest.mark.parametrize(
    "change",
    [
        {"n": float(OLD_N)},
        {"n": str(OLD_N)},
        {"n": True},
        {"r": 16},
        {"p": 2},
        {"name": "pbkdf2"},
        {"extra": 1},
    ],
    ids=["float-n", "string-n", "bool-n", "r-16", "p-2", "other-name", "extra-field"],
)
def test_a_kdf_block_outside_the_two_known_ones_is_refused_as_damaged(tmp_path, monkeypatch, change):
    path = tmp_path / key_vault.VAULT_FILE_NAME
    key_vault.create(path, PASSWORD)
    document = _document(path)
    document["kdf"].update(change)
    _write_document(path, document)
    derivations = []

    def refuse_derivation(*args, **kwargs):
        derivations.append(kwargs.get("n"))
        raise AssertionError("a key was derived from a kdf block the vault does not know")

    monkeypatch.setattr(key_vault.hashlib, "scrypt", refuse_derivation)

    with pytest.raises(VaultError) as raised:
        key_vault.unlock(path, PASSWORD)

    assert raised.value.args[0] == key_vault.DAMAGED_TEXT
    assert derivations == []


def test_a_wrong_password_on_a_new_vault_raises_the_wrong_password_text(tmp_path):
    path = tmp_path / key_vault.VAULT_FILE_NAME
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE)
    vault.save()
    vault.lock()

    with pytest.raises(VaultError) as raised:
        key_vault.unlock(path, OTHER)

    assert raised.value.args[0] == key_vault.WRONG_PASSWORD_TEXT
    assert OTHER not in str(raised.value) and PASSWORD not in str(raised.value)
