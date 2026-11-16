"""Key groups: each API key shows a group label. A key starts in its default group, and the owner can change it.

Sprint item 0c14d2d6 (dynamic groups with editable defaults, decision 4e636fdf). The keychain is a MemoryKeyring and the
settings file is in pytest's temp folder, so nothing touches the real credential store. FAKE_VALUE is obviously fake, so the
leak checks can search for it.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import local_client
from meridian_desktop.api_keys import FALLBACK_GROUP, ApiKeys, KeyNameError, clean_group
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.keys_tab import KeysTab
from meridian_desktop.user_environment import UserEnvironment

FAKE_VALUE = "sk-fake-groups-0001"


class FakeRegistry:
    """The user environment in memory, so no test writes a real Windows variable."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def write_user_value(self, name: str, value: str) -> None:
        self.values[name] = value

    def read_user_value(self, name: str) -> str | None:
        return self.values.get(name)

    def delete_user_value(self, name: str) -> bool:
        return self.values.pop(name, None) is not None

    def announce_change(self) -> bool:
        return True


@pytest.fixture
def keys(settings_path):
    backend = MemoryKeyring()
    return ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend), user_environment=UserEnvironment(FakeRegistry()))


def _settings_json(settings_path) -> dict:
    return json.loads(settings_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- defaults and changes


def test_a_known_key_starts_in_its_default_group(keys):
    keys.save("OpenAI", FAKE_VALUE)
    keys.save("Neon", FAKE_VALUE)
    assert keys.group_of("OpenAI") == "AI"
    assert keys.group_of("Neon") == "Hosting"


def test_a_key_with_no_default_starts_in_the_fallback_group(keys):
    keys.save("Mystery", FAKE_VALUE)
    assert keys.group_of("Mystery") == FALLBACK_GROUP


def test_the_owner_can_change_a_default_group(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)

    assert keys.set_group("OpenAI", "Models") == "Models"

    assert keys.group_of("OpenAI") == "Models"
    assert _settings_json(settings_path)["key_groups"] == {"OpenAI": "Models"}


def test_changing_a_group_again_replaces_the_label_and_keeps_one_entry(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)
    keys.set_group("OpenAI", "Models")
    keys.set_group("openai", "Vendors")

    assert keys.group_of("OpenAI") == "Vendors"
    assert _settings_json(settings_path)["key_groups"] == {"OpenAI": "Vendors"}


def test_a_group_label_never_holds_the_value(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)
    keys.set_group("OpenAI", "Models")
    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


# --------------------------------------------------------------------- label checks


def test_a_label_has_its_spaces_collapsed():
    assert clean_group("  Cloud   hosting ") == "Cloud hosting"


@pytest.mark.parametrize("label", ["", "   ", "x" * 41])
def test_an_empty_or_too_long_label_is_refused(keys, label):
    keys.save("OpenAI", FAKE_VALUE)
    with pytest.raises(KeyNameError):
        keys.set_group("OpenAI", label)
    assert keys.group_of("OpenAI") == "AI"


def test_a_group_for_a_name_that_is_not_listed_is_refused(keys):
    with pytest.raises(KeyNameError):
        keys.set_group("NotAKey", "Models")


# --------------------------------------------------------------------- removal and the settings file


def test_removing_a_custom_key_also_drops_its_group_label(keys, settings_path):
    keys.save("Mystery", FAKE_VALUE)
    keys.set_group("Mystery", "Misc")

    keys.remove("Mystery")

    assert "key_groups" not in _settings_json(settings_path)


def test_no_group_label_means_no_key_in_the_settings_file(settings_path):
    store = SettingsStore(settings_path)
    store.save(Settings())
    assert "key_groups" not in _settings_json(settings_path)


def test_a_group_file_of_the_wrong_shape_is_warned_about_and_ignored(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"key_groups": ["OpenAI", "AI"]}), encoding="utf-8")
    loaded = SettingsStore(settings_path).load()
    assert loaded.settings.key_groups == ()
    assert loaded.warning is not None and "key_groups" in loaded.warning


# --------------------------------------------------------------------- the Keys tab


def _keys_tab(qtbot, settings_path):
    class FakeClient:
        def probe_health(self, url, timeout):
            return local_client.HEALTH_NOT_RUNNING

    secrets = KeyringSecretStore(backend=MemoryKeyring())
    tab = KeysTab(SettingsStore(settings_path), secrets, client=FakeClient(), confirm=lambda *_: True)
    qtbot.addWidget(tab)
    return tab


def _add(tab, name):
    tab.findChild(QLineEdit, "keys-new-name").setText(name)
    tab.findChild(QLineEdit, "keys-new-value").setText(FAKE_VALUE)
    tab.findChild(QPushButton, "keys-add").click()


def test_a_card_shows_its_default_group_and_the_owner_can_change_it(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, "OpenAI")
    assert tab.findChild(QLabel, "keys-entry-openai-group").text() == "Group: AI"

    tab.findChild(QLineEdit, "keys-entry-openai-group-edit").setText("Vendors")
    tab.findChild(QPushButton, "keys-entry-openai-group-save").click()

    assert tab.findChild(QLabel, "keys-entry-openai-group").text() == "Group: Vendors"
    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


def test_search_finds_a_key_by_its_group_label(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, "Neon")
    _add(tab, "OpenAI")

    tab.findChild(QLineEdit, "keys-search").setText("hosting")

    assert not tab.findChild(QWidget, "keys-entry-neon").isHidden()
    assert tab.findChild(QWidget, "keys-entry-openai").isHidden()
