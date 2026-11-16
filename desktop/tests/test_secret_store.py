"""Tests for meridian_desktop/secret_store.py. Only in-memory fakes: no OS credential store."""

import subprocess
import sys
from pathlib import Path

import keyring
import pytest

from _fakes import FailingKeyring, MemoryKeyring
from meridian_desktop.secret_store import (
    MAX_SECRET_LENGTH,
    RUNPOD_ACCOUNT,
    RUNPOD_SERVICE,
    KeyringSecretStore,
    SecretStoreError,
)

REPO = Path(__file__).resolve().parents[1]
FAKE_KEY = "test-key-not-real"


@pytest.fixture
def backend():
    return MemoryKeyring()


@pytest.fixture
def store(backend):
    return KeyringSecretStore(backend=backend)


def test_runpod_constants_match_the_core_reference():
    # The core reads keyring:meridian-desktop/runpod-api-key, so these names are fixed.
    assert RUNPOD_SERVICE == "meridian-desktop"
    assert RUNPOD_ACCOUNT == "runpod-api-key"


def test_set_then_get_round_trip(store, backend):
    store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, FAKE_KEY)
    assert store.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) == FAKE_KEY
    assert backend.snapshot() == {(RUNPOD_SERVICE, RUNPOD_ACCOUNT): FAKE_KEY}


def test_get_missing_returns_none(store):
    assert store.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) is None


def test_set_strips_surrounding_whitespace(store):
    store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, f"  \t{FAKE_KEY}\r\n ")
    assert store.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) == FAKE_KEY


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_empty_or_blank_value_is_rejected(store, backend, value):
    with pytest.raises(SecretStoreError, match="empty"):
        store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, value)
    assert backend.snapshot() == {}


def test_value_at_the_limit_is_accepted(store):
    value = "k" * MAX_SECRET_LENGTH
    store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, value)
    assert store.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) == value


def test_value_over_the_limit_is_rejected(store, backend):
    with pytest.raises(SecretStoreError, match="longer than 512"):
        store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, "k" * (MAX_SECRET_LENGTH + 1))
    assert backend.snapshot() == {}


def test_length_is_checked_after_stripping(store):
    padded = " " * 20 + "k" * MAX_SECRET_LENGTH + " " * 20
    store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, padded)
    assert store.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) == "k" * MAX_SECRET_LENGTH


def test_non_string_value_is_rejected(store, backend):
    with pytest.raises(SecretStoreError, match="must be a string"):
        store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, None)  # type: ignore[arg-type]
    assert backend.snapshot() == {}


def test_delete_removes_and_reports_true(store, backend):
    store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, FAKE_KEY)
    assert store.delete_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) is True
    assert backend.snapshot() == {}
    assert store.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) is None


def test_delete_missing_reports_false_without_error(store):
    assert store.delete_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) is False


def test_backend_failure_names_operation_and_class_only(backend):
    store = KeyringSecretStore(backend=FailingKeyring(FAKE_KEY))
    with pytest.raises(SecretStoreError) as info:
        store.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT)
    assert str(info.value) == "keyring read failed (RuntimeError)"


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda s: s.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, FAKE_KEY), "keyring save failed (ValueError)"),
        (lambda s: s.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT), "keyring read failed (RuntimeError)"),
        (lambda s: s.delete_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT), "keyring read failed (RuntimeError)"),
    ],
)
def test_failures_never_carry_the_value(call, expected):
    # FailingKeyring puts the secret in every exception it raises, on purpose.
    store = KeyringSecretStore(backend=FailingKeyring(FAKE_KEY))
    with pytest.raises(SecretStoreError) as info:
        call(store)
    error = info.value
    assert str(error) == expected
    assert FAKE_KEY not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_delete_failure_after_a_present_read_names_the_delete_step():
    class DeleteFails(MemoryKeyring):
        def delete_password(self, service, username):
            raise OSError(f"could not remove {FAKE_KEY}")

    backend = DeleteFails()
    backend.set_password(RUNPOD_SERVICE, RUNPOD_ACCOUNT, FAKE_KEY)
    with pytest.raises(SecretStoreError) as info:
        KeyringSecretStore(backend=backend).delete_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT)
    assert str(info.value) == "keyring delete failed (OSError)"
    assert FAKE_KEY not in repr(info.value)


def test_default_store_uses_the_keyring_module():
    # conftest installs an in-memory backend for the session, so this touches no OS store.
    assert isinstance(keyring.get_keyring(), MemoryKeyring)
    store = KeyringSecretStore()
    store.set_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT, FAKE_KEY)
    try:
        assert keyring.get_password(RUNPOD_SERVICE, RUNPOD_ACCOUNT) == FAKE_KEY
        assert store.get_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) == FAKE_KEY
    finally:
        assert store.delete_secret(RUNPOD_SERVICE, RUNPOD_ACCOUNT) is True


def test_importing_the_module_loads_no_keyring_and_no_qt():
    code = (
        "import sys\n"
        "import meridian_desktop.secret_store\n"
        "assert 'keyring' not in sys.modules, 'keyring imported at module import'\n"
        "assert not any(name.startswith('PySide6') for name in sys.modules), 'Qt imported'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
