"""An open vault's repr never shows its derived key or its stored values (meridian_desktop/key_vault.py).

The password, the stored value and the derived key are fake or come from the vault itself. The vault is a temp file.
"""

from meridian_desktop import key_vault

PASSWORD = "fake-repr-pass-0001"
VALUE = "sk-fake-repr-ALPHA-0001"


def test_the_repr_of_an_open_vault_shows_neither_the_key_nor_a_stored_value(tmp_path):
    path = tmp_path / key_vault.VAULT_FILE_NAME
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE)

    text = repr(vault)

    assert VALUE not in text
    assert vault._key is not None
    assert vault._key.hex() not in text
    assert "_entries" not in text
    assert "_key" not in text
    assert key_vault.VAULT_FILE_NAME in text, "the file name stays visible: it is not a secret"


def test_the_repr_of_a_locked_vault_shows_no_key_either(tmp_path):
    path = tmp_path / key_vault.VAULT_FILE_NAME
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE)
    vault.save()
    vault.lock()

    assert VALUE not in repr(vault)
