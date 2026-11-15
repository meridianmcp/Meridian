"""Removing a key also removes the Windows user variable it was set as, but only when that variable holds the key's value.

Sprint item 73bbd554 (KEYS-UI). The keychain is a MemoryKeyring and the Windows user environment is a FakeRegistry, so
nothing touches the real credential store or HKCU. FAKE_VALUE is obviously fake, so the leak checks can search for it.
"""

from __future__ import annotations

import pytest

from _fakes import MemoryKeyring
from meridian_desktop.api_keys import KEY_SERVICE, ApiKeys, account_for
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.user_environment import UserEnvironment, UserEnvironmentError

FAKE_VALUE = "sk-fake-remove-env-0001"
OTHER_VALUE = "sk-fake-remove-env-0002"


class FakeRegistry:
    """The user environment in memory, standing in for HKCU Environment. ``fail_delete`` makes every delete fail."""

    def __init__(self, fail_delete: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.fail_delete = fail_delete
        self.announced = 0

    def write_user_value(self, name: str, value: str) -> None:
        self.values[name] = value

    def read_user_value(self, name: str) -> str | None:
        return self.values.get(name)

    def delete_user_value(self, name: str) -> bool:
        if self.fail_delete:
            raise PermissionError("denied")
        return self.values.pop(name, None) is not None

    def announce_change(self) -> bool:
        self.announced += 1
        return True


@pytest.fixture
def registry():
    return FakeRegistry()


@pytest.fixture
def backend():
    return MemoryKeyring()


def _keys(settings_path, backend, registry):
    return ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend), user_environment=UserEnvironment(registry))


def test_removing_a_key_deletes_the_windows_variable_it_was_set_as(settings_path, backend, registry):
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)
    keys.set_user_environment("OpenAI")
    assert registry.values == {"OPENAI": FAKE_VALUE}

    assert keys.remove("OpenAI") is True

    assert registry.values == {}
    assert backend.snapshot() == {}
    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


def test_a_variable_holding_other_text_is_left_alone_and_the_key_is_still_removed(settings_path, backend, registry):
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)
    registry.write_user_value("OPENAI", OTHER_VALUE)

    keys.remove("OpenAI")

    assert registry.values == {"OPENAI": OTHER_VALUE}
    assert backend.snapshot() == {}


def test_removing_a_key_with_no_windows_variable_still_works(settings_path, backend, registry):
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)

    assert keys.remove("OpenAI") is True
    assert registry.values == {}
    assert backend.snapshot() == {}


def test_a_failed_variable_delete_keeps_the_keychain_value_so_the_owner_can_retry(settings_path, backend):
    registry = FakeRegistry(fail_delete=True)
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)
    keys.set_user_environment("OpenAI")

    with pytest.raises(UserEnvironmentError):
        keys.remove("OpenAI")

    # Nothing is removed: the keychain still holds the key, and the variable is still there.
    assert backend.snapshot() == {(KEY_SERVICE, account_for("OpenAI")): FAKE_VALUE}
    assert registry.values == {"OPENAI": FAKE_VALUE}


def test_reading_an_unset_variable_gives_none(registry):
    assert UserEnvironment(registry).read("OPENAI") is None


def test_a_successful_delete_announces_the_change_once(registry):
    registry.write_user_value("OPENAI", FAKE_VALUE)
    assert UserEnvironment(registry).delete("OPENAI") is True
    assert registry.announced == 1
