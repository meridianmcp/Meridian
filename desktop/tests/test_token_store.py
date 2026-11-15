"""Tests for meridian_desktop/token_store.py. Only in-memory fakes: no OS credential store.

FAKE_TOKEN is an obviously fake value. It is the same text in every test, so a leak
check can search for it.
"""

import json
import subprocess
import sys
from pathlib import Path

import keyring
import pytest

from _fakes import FailingKeyring, MemoryKeyring
from meridian_desktop.secret_store import KeyringSecretStore, SecretStoreError
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.token_store import (
    HOSTED_TOKEN_ACCOUNT,
    TOKEN_SERVICE,
    TokenStoreError,
    delete_token,
    get_token,
    has_token,
    set_token,
)

REPO = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "sk_meridian_fake-test-value-0001"
OTHER_ACCOUNT = "google-refresh-token"
OTHER_FAKE = "fake-refresh-value-0002"


@pytest.fixture
def backend():
    return MemoryKeyring()


@pytest.fixture
def store(backend):
    return KeyringSecretStore(backend=backend)


def test_get_token_returns_none_when_nothing_is_stored(store):
    assert get_token(store=store) is None


def test_set_get_delete_round_trip(store, backend):
    set_token(FAKE_TOKEN, store=store)
    assert get_token(store=store) == FAKE_TOKEN
    assert backend.snapshot() == {(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT): FAKE_TOKEN}

    assert delete_token(store=store) is True
    assert get_token(store=store) is None
    assert backend.snapshot() == {}


def test_delete_when_nothing_is_stored_reports_false(store):
    assert delete_token(store=store) is False


def test_accounts_are_kept_apart_in_one_store(store, backend):
    set_token(FAKE_TOKEN, store=store)
    set_token(OTHER_FAKE, OTHER_ACCOUNT, store=store)
    assert get_token(store=store) == FAKE_TOKEN
    assert get_token(OTHER_ACCOUNT, store=store) == OTHER_FAKE

    assert delete_token(OTHER_ACCOUNT, store=store) is True
    assert get_token(store=store) == FAKE_TOKEN
    assert get_token(OTHER_ACCOUNT, store=store) is None
    assert backend.snapshot() == {(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT): FAKE_TOKEN}


@pytest.mark.parametrize("account", ["", "Hosted", "../escape", "a b", "x" * 65, "-leading"])
def test_invalid_account_is_refused_before_any_backend_call(store, backend, account):
    with pytest.raises(TokenStoreError, match="account must be"):
        get_token(account, store=store)
    with pytest.raises(TokenStoreError, match="account must be"):
        set_token(FAKE_TOKEN, account, store=store)
    with pytest.raises(TokenStoreError, match="account must be"):
        delete_token(account, store=store)
    assert backend.snapshot() == {}


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_empty_token_is_refused_and_nothing_is_stored(store, backend, value):
    with pytest.raises(TokenStoreError, match="secret is empty"):
        set_token(value, store=store)
    assert backend.snapshot() == {}


def test_get_token_reads_a_backend_failure_as_none():
    store = KeyringSecretStore(backend=FailingKeyring(FAKE_TOKEN))
    assert get_token(store=store) is None


def test_set_token_failure_raises_typed_error_without_token_text():
    # FailingKeyring puts the value in the exception it raises, on purpose.
    store = KeyringSecretStore(backend=FailingKeyring(FAKE_TOKEN))
    with pytest.raises(TokenStoreError) as info:
        set_token(FAKE_TOKEN, store=store)
    error = info.value
    assert str(error) == "token could not be saved: keyring save failed (ValueError)"
    assert FAKE_TOKEN not in str(error)
    assert FAKE_TOKEN not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert isinstance(error, SecretStoreError)


def test_delete_failure_on_read_raises_typed_error_without_token_text():
    store = KeyringSecretStore(backend=FailingKeyring(FAKE_TOKEN))
    with pytest.raises(TokenStoreError) as info:
        delete_token(store=store)
    error = info.value
    assert str(error) == "token could not be removed: keyring read failed (RuntimeError)"
    assert FAKE_TOKEN not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_delete_failure_on_remove_raises_typed_error_without_token_text():
    class DeleteFails(MemoryKeyring):
        def delete_password(self, service, username):
            raise OSError(f"could not remove {FAKE_TOKEN}")

    backend = DeleteFails()
    backend.set_password(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT, FAKE_TOKEN)
    with pytest.raises(TokenStoreError) as info:
        delete_token(store=KeyringSecretStore(backend=backend))
    assert str(info.value) == "token could not be removed: keyring delete failed (OSError)"
    assert FAKE_TOKEN not in repr(info.value)


def test_token_never_written_to_settings_file(settings_path):
    backend = MemoryKeyring()
    secrets = KeyringSecretStore(backend=backend)
    settings = SettingsStore(settings_path)
    settings.set_mode("hosted")  # the app writes settings.json this way

    set_token(FAKE_TOKEN, store=secrets)
    settings.set_mode("local")  # a later save must not pick the token up either

    assert get_token(store=secrets) == FAKE_TOKEN  # stored, but in the keychain only
    assert settings_path.exists(), "the settings file should exist, or the scan proves nothing"

    directory = settings_path.parent
    files = [path for path in directory.rglob("*") if path.is_file()]
    assert files
    needles = (FAKE_TOKEN.encode("utf-8"), FAKE_TOKEN.encode("utf-16-le"))
    for path in files:
        data = path.read_bytes()
        for needle in needles:
            assert needle not in data, f"token text found in {path.name}"

    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert set(saved) == {"mode", "hosted_url", "local_url", "engine_url", "db_path"}


def test_default_store_uses_the_keyring_module():
    # conftest installs an in-memory backend for the session, so this touches no OS store.
    assert isinstance(keyring.get_keyring(), MemoryKeyring)
    set_token(FAKE_TOKEN)
    try:
        assert keyring.get_password(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT) == FAKE_TOKEN
        assert get_token() == FAKE_TOKEN
    finally:
        assert delete_token() is True
    assert get_token() is None


def test_importing_the_module_loads_no_keyring_and_no_qt():
    code = (
        "import sys\n"
        "import meridian_desktop.token_store\n"
        "assert 'keyring' not in sys.modules, 'keyring imported at module import'\n"
        "assert not any(name.startswith('PySide6') for name in sys.modules), 'Qt imported'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_has_token_says_only_whether_a_token_is_saved(store):
    assert has_token(store=store) is False
    set_token(FAKE_TOKEN, store=store)

    assert has_token(store=store) is True
    assert delete_token(store=store) is True
    assert has_token(store=store) is False


def test_has_token_returns_a_bool_and_never_the_value(store):
    set_token(FAKE_TOKEN, store=store)

    answer = has_token(store=store)

    assert answer is True
    assert not isinstance(answer, str)
    assert FAKE_TOKEN not in repr(answer)


def test_has_token_reads_a_backend_failure_as_no_token_and_never_shows_its_text():
    store = KeyringSecretStore(backend=FailingKeyring(FAKE_TOKEN))

    answer = has_token(store=store)

    assert answer is False
    assert FAKE_TOKEN not in repr(answer)
