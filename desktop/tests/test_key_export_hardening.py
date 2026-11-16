"""Hardening for the key export (meridian_desktop/key_export.py).

A file that appears at the target while the keys are read is not overwritten without the owner's Yes, and a failed write
leaves no temporary file behind. Every value and passphrase here is fake. The keychain is a MemoryKeyring, the settings are
a temp file, the Windows user variables go to a fake registry, and the export files are temp files.
"""

import pytest

from _fakes import FakeUserRegistry, MemoryKeyring
from meridian_desktop import key_export
from meridian_desktop.api_keys import ApiKeys
from meridian_desktop.key_export import (
    KIND_ENCRYPTED,
    KIND_PLAINTEXT,
    PLAINTEXT_CONFIRMATION,
    ExportError,
    TargetExistsError,
    export_keys,
)
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.user_environment import UserEnvironment

PASSPHRASE = "fake-export-pass-0001"
OPENAI_VALUE = "sk-fake-hardening-ALPHA-0001"
OLDER_FILE = b"an older file that must stay as it is"


@pytest.fixture
def folder(tmp_path):
    return tmp_path


def _one_saved_key(folder):
    settings = SettingsStore(folder / "settings.json")
    keys = ApiKeys(settings, KeyringSecretStore(backend=MemoryKeyring()), user_environment=UserEnvironment(FakeUserRegistry()))
    keys.save("OpenAI", OPENAI_VALUE)
    return keys


def _file_appears_while_keys_are_read(monkeypatch, target):
    """Create the target right after the first checks run, as another program could while the keys are read."""
    real_saved_values = key_export._saved_values

    def read_then_create(keys):
        values = real_saved_values(keys)
        target.write_bytes(OLDER_FILE)
        return values

    monkeypatch.setattr(key_export, "_saved_values", read_then_create)


@pytest.mark.parametrize(
    "kind, confirmation, passphrase",
    [(KIND_PLAINTEXT, PLAINTEXT_CONFIRMATION, ""), (KIND_ENCRYPTED, "", PASSPHRASE)],
)
def test_a_file_that_appears_during_the_export_is_not_overwritten(folder, monkeypatch, kind, confirmation, passphrase):
    keys = _one_saved_key(folder)
    target = folder / "out.json"
    _file_appears_while_keys_are_read(monkeypatch, target)

    with pytest.raises(TargetExistsError):
        export_keys(keys, target, kind, passphrase=passphrase, confirmation=confirmation, overwrite=False)

    assert target.read_bytes() == OLDER_FILE
    assert [path.name for path in folder.glob("out.json*")] == ["out.json"]


def test_a_failed_plaintext_write_leaves_no_temporary_file_and_no_target(folder, monkeypatch):
    keys = _one_saved_key(folder)
    target = folder / "out.json"

    def disk_full(temp, destination, *, overwrite):
        raise OSError("disk full")

    monkeypatch.setattr(key_export, "_move_into_place", disk_full)

    with pytest.raises(ExportError):
        export_keys(keys, target, KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)

    assert not target.exists()
    assert not (folder / ("out.json" + key_export.TEMP_SUFFIX)).exists()


def test_an_unexpected_error_during_the_plaintext_write_still_removes_the_temporary_file(folder, monkeypatch):
    keys = _one_saved_key(folder)
    target = folder / "out.json"

    def unexpected(temp, destination, *, overwrite):
        raise RuntimeError("not an OS error")

    monkeypatch.setattr(key_export, "_move_into_place", unexpected)

    with pytest.raises(RuntimeError):
        export_keys(keys, target, KIND_PLAINTEXT, confirmation=PLAINTEXT_CONFIRMATION)

    assert not (folder / ("out.json" + key_export.TEMP_SUFFIX)).exists()
