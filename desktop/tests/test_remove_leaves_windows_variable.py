"""Removing a key deletes only its keychain copy. The Windows user variable it was set as is never touched.

Sprint item 73bbd554 (KEYS-UI). A variable is written by set_user_environment, and the owner removes it in Windows. The keychain
is a MemoryKeyring and the Windows user environment is a FakeRegistry, so nothing touches the real credential store or HKCU.
FAKE_VALUE is obviously fake, so the leak checks can search for it.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QPushButton

from _fakes import MemoryKeyring
from meridian_desktop.api_keys import KEY_SERVICE, ApiKeys, account_for
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.keys_section import KeysSection
from meridian_desktop.user_environment import UserEnvironment

FAKE_VALUE = "sk-fake-remove-env-0001"
OTHER_VALUE = "sk-fake-remove-env-0002"


class FakeRegistry:
    """The user environment in memory, standing in for HKCU Environment. Each delete is recorded.

    ``fail_delete`` makes every delete fail, so a test can show that removing a key never reaches the delete step.
    """

    def __init__(self, fail_delete: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.deletes: list[str] = []
        self.fail_delete = fail_delete
        self.announced = 0

    def write_user_value(self, name: str, value: str) -> None:
        self.values[name] = value

    def read_user_value(self, name: str) -> str | None:
        return self.values.get(name)

    def delete_user_value(self, name: str) -> bool:
        self.deletes.append(name)
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


def test_removing_a_key_leaves_the_windows_variable_it_was_set_as_untouched(settings_path, backend, registry):
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)
    keys.set_user_environment("OpenAI")
    assert registry.values == {"OPENAI": FAKE_VALUE}

    assert keys.remove("OpenAI") is True

    assert backend.snapshot() == {}, "the keychain copy is removed"
    assert registry.values == {"OPENAI": FAKE_VALUE}, "the Windows user variable is left as it was"
    assert registry.deletes == []
    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


def test_a_variable_holding_other_text_is_left_alone_and_the_key_is_still_removed(settings_path, backend, registry):
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)
    registry.write_user_value("OPENAI", OTHER_VALUE)

    keys.remove("OpenAI")

    assert registry.values == {"OPENAI": OTHER_VALUE}
    assert registry.deletes == []
    assert backend.snapshot() == {}


def test_removing_a_key_with_no_windows_variable_still_works(settings_path, backend, registry):
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)

    assert keys.remove("OpenAI") is True
    assert registry.values == {}
    assert registry.deletes == []
    assert backend.snapshot() == {}


def test_removing_never_reaches_the_variable_delete_so_a_failing_delete_cannot_block_it(settings_path, backend):
    registry = FakeRegistry(fail_delete=True)
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)
    keys.set_user_environment("OpenAI")

    assert keys.remove("OpenAI") is True

    assert backend.snapshot() == {}
    assert registry.values == {"OPENAI": FAKE_VALUE}
    assert registry.deletes == []


def test_the_remove_question_does_not_say_the_windows_variable_is_deleted(qtbot, settings_path, backend, registry):
    keys = _keys(settings_path, backend, registry)
    keys.save("OpenAI", FAKE_VALUE)
    keys.set_user_environment("OpenAI")
    asked: list[str] = []

    def confirm(parent, title, text):
        asked.append(text)
        return True

    section = KeysSection(keys, confirm=confirm)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)

    section.findChild(QPushButton, "keys-entry-openai-remove").click()

    assert len(asked) == 1
    assert "variable is deleted" not in asked[0], "the question must not say the Windows user variable is deleted"
    assert "is not changed" in asked[0], "the question says the Windows user variable is left alone"
    assert FAKE_VALUE not in asked[0]
    assert registry.values == {"OPENAI": FAKE_VALUE}
    assert registry.deletes == []
    assert backend.snapshot() == {}


# The writer's delete step is still tested here, although no removal calls it now.


def test_reading_an_unset_variable_gives_none(registry):
    assert UserEnvironment(registry).read("OPENAI") is None


def test_a_successful_delete_announces_the_change_once(registry):
    registry.write_user_value("OPENAI", FAKE_VALUE)
    assert UserEnvironment(registry).delete("OPENAI") is True
    assert registry.announced == 1
