"""A vault file whose nonce or salt has the wrong length is refused as a vault error, not a crash (meridian_desktop/key_vault.py).

The password and the value are fake, and the vault is a temp file.
"""

import base64
import json

import pytest

from meridian_desktop import key_vault
from meridian_desktop.key_vault import VaultError

PASSWORD = "fake-damage-pass-0001"
VALUE = "sk-fake-damage-ALPHA-0001"


def _vault_file(tmp_path):
    path = tmp_path / key_vault.VAULT_FILE_NAME
    vault = key_vault.create(path, PASSWORD)
    vault.put("OpenAI", VALUE)
    vault.save()
    vault.lock()
    return path


def _replace_field(path, field, raw):
    document = json.loads(path.read_text(encoding="utf-8"))
    document[field] = base64.b64encode(raw).decode("ascii")
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("length", [0, 4, 200])
def test_a_damaged_nonce_is_refused_as_a_vault_error(tmp_path, length):
    path = _vault_file(tmp_path)
    _replace_field(path, "nonce", b"\x00" * length)

    with pytest.raises(VaultError):
        key_vault.unlock(path, PASSWORD)


@pytest.mark.parametrize("length", [0, 8, 64])
def test_a_damaged_salt_is_refused_as_a_vault_error(tmp_path, length):
    path = _vault_file(tmp_path)
    _replace_field(path, "salt", b"\x00" * length)

    with pytest.raises(VaultError):
        key_vault.unlock(path, PASSWORD)


def test_an_undamaged_vault_still_unlocks_with_the_right_password(tmp_path):
    path = _vault_file(tmp_path)

    vault = key_vault.unlock(path, PASSWORD)

    assert vault.get("OpenAI") == VALUE
