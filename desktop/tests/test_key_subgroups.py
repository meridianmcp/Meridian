"""Key subgroups: keys that only work as a set (for example an access key ID with its secret) sit under one subgroup heading.

Sprint item for owner decision b ("subgroups for paired keys"). The keychain is a MemoryKeyring, the Windows user environment
is a FakeUserRegistry and the settings file is in pytest's temp folder, so nothing touches the real credential store, HKCU or
the real settings. FAKE_VALUE is obviously fake, so the leak checks can search for it.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QWidget

from _fakes import FakeUserRegistry, MemoryKeyring
from meridian_desktop import api_keys, local_client
from meridian_desktop.api_keys import ApiKeys, KeyNameError, clean_subgroup
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.keys_section import KeysSection
from meridian_desktop.ui.keys_tab import KeysTab
from meridian_desktop.user_environment import UserEnvironment

FAKE_VALUE = "sk-fake-subgroups-0001"
ACCESS = "AWS Access ID"
SECRET = "AWS Secret"
AWS = "AWS keys"


@pytest.fixture
def keys(settings_path):
    backend = MemoryKeyring()
    return ApiKeys(
        SettingsStore(settings_path),
        KeyringSecretStore(backend=backend),
        user_environment=UserEnvironment(FakeUserRegistry()),
    )


def _settings_json(settings_path) -> dict:
    return json.loads(settings_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------- the settings file


def test_a_subgroup_object_loads_as_name_and_label_pairs(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"key_subgroups": {ACCESS: " AWS keys "}}), encoding="utf-8")
    loaded = SettingsStore(settings_path).load()
    assert loaded.settings.key_subgroups == ((ACCESS, AWS),)


def test_a_subgroup_file_of_the_wrong_shape_is_warned_about_and_ignored(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"key_subgroups": [ACCESS, AWS]}), encoding="utf-8")
    loaded = SettingsStore(settings_path).load()
    assert loaded.settings.key_subgroups == ()
    assert loaded.warning is not None and "key_subgroups" in loaded.warning


# --------------------------------------------------------------------- defaults


def test_no_key_starts_in_a_subgroup_because_none_is_seeded(keys, settings_path):
    keys.save("OpenAI", FAKE_VALUE)
    keys.save("Anthropic", FAKE_VALUE)
    assert api_keys.DEFAULT_SUBGROUP_NAMES == {}
    assert keys.subgroup_of("OpenAI") == ""
    assert keys.subgroup_of("Anthropic") == ""
    assert "key_subgroups" not in _settings_json(settings_path)


def test_a_seeded_default_subgroup_applies_until_the_owner_sets_one(keys, monkeypatch):
    monkeypatch.setitem(api_keys.DEFAULT_SUBGROUP_NAMES, ACCESS, "Pair")
    keys.save(ACCESS, FAKE_VALUE)
    assert keys.subgroup_of(ACCESS) == "Pair"

    keys.set_subgroup(ACCESS, "Access pair")
    assert keys.subgroup_of(ACCESS) == "Access pair"

    keys.set_subgroup(ACCESS, "")
    assert keys.subgroup_of(ACCESS) == "Pair"


# --------------------------------------------------------------------- set and clear


def test_the_owner_sets_a_subgroup_and_it_is_stored_by_name(keys, settings_path):
    keys.save(ACCESS, FAKE_VALUE)
    assert keys.set_subgroup(ACCESS, AWS) == AWS
    assert keys.subgroup_of(ACCESS) == AWS
    assert _settings_json(settings_path)["key_subgroups"] == {ACCESS: AWS}


def test_changing_a_subgroup_replaces_the_label_and_keeps_one_entry(keys, settings_path):
    keys.save(ACCESS, FAKE_VALUE)
    keys.set_subgroup(ACCESS, AWS)
    keys.set_subgroup(ACCESS.lower(), "Cloud pair")
    assert keys.subgroup_of(ACCESS) == "Cloud pair"
    assert _settings_json(settings_path)["key_subgroups"] == {ACCESS: "Cloud pair"}


def test_an_empty_label_clears_the_subgroup_and_leaves_no_key_in_settings(keys, settings_path):
    keys.save(ACCESS, FAKE_VALUE)
    keys.set_subgroup(ACCESS, AWS)
    assert keys.set_subgroup(ACCESS, "") == ""
    assert keys.subgroup_of(ACCESS) == ""
    assert "key_subgroups" not in _settings_json(settings_path)


def test_a_blank_label_clears_the_subgroup_too(keys):
    keys.save(ACCESS, FAKE_VALUE)
    keys.set_subgroup(ACCESS, AWS)
    assert keys.set_subgroup(ACCESS, "   ") == ""
    assert keys.subgroup_of(ACCESS) == ""


def test_setting_a_subgroup_leaves_the_group_and_the_other_keys_alone(keys):
    keys.save(ACCESS, FAKE_VALUE)
    keys.save("Neon", FAKE_VALUE)
    keys.set_subgroup(ACCESS, AWS)
    assert keys.group_of("Neon") == "Hosting"
    assert keys.subgroup_of("Neon") == ""
    assert keys.group_of(ACCESS) == api_keys.FALLBACK_GROUP


# --------------------------------------------------------------------- validation


def test_a_subgroup_label_has_its_spaces_collapsed():
    assert clean_subgroup("  AWS   keys ") == AWS


@pytest.mark.parametrize("label", ["x" * 41, "y" * 80])
def test_a_subgroup_label_longer_than_40_characters_is_refused_and_changes_nothing(keys, label):
    keys.save(ACCESS, FAKE_VALUE)
    keys.set_subgroup(ACCESS, AWS)
    with pytest.raises(KeyNameError):
        keys.set_subgroup(ACCESS, label)
    assert keys.subgroup_of(ACCESS) == AWS


def test_a_subgroup_label_of_exactly_40_characters_is_kept(keys):
    keys.save(ACCESS, FAKE_VALUE)
    assert keys.set_subgroup(ACCESS, "x" * 40) == "x" * 40


def test_a_subgroup_for_a_name_that_is_not_listed_is_refused(keys, settings_path):
    with pytest.raises(KeyNameError):
        keys.set_subgroup("NotAKey", AWS)
    with pytest.raises(KeyNameError):
        keys.subgroup_of("NotAKey")
    assert not settings_path.exists()


# --------------------------------------------------------------------- values and removal


def test_a_subgroup_label_never_holds_the_value(keys, settings_path):
    keys.save(ACCESS, FAKE_VALUE)
    keys.set_subgroup(ACCESS, AWS)
    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


def test_removing_a_custom_key_drops_its_subgroup_label_and_keeps_the_others(keys, settings_path):
    keys.save(ACCESS, FAKE_VALUE)
    keys.save(SECRET, FAKE_VALUE)
    keys.set_subgroup(ACCESS, AWS)
    keys.set_subgroup(SECRET, AWS)

    keys.remove(SECRET)

    assert _settings_json(settings_path)["key_subgroups"] == {ACCESS: AWS}
    assert keys.subgroup_of(ACCESS) == AWS


def test_removing_the_last_key_with_a_subgroup_leaves_no_subgroup_key(keys, settings_path):
    keys.save(SECRET, FAKE_VALUE)
    keys.set_subgroup(SECRET, AWS)

    keys.remove(SECRET)

    assert "key_subgroups" not in _settings_json(settings_path)


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


def _set_subgroup_in_ui(tab, key, label):
    tab.findChild(QLineEdit, f"keys-entry-{key}-subgroup-edit").setText(label)
    tab.findChild(QPushButton, f"keys-entry-{key}-subgroup-save").click()


def _headings(tab) -> list[QLabel]:
    return [label for label in tab.findChildren(QLabel) if label.objectName().startswith("keys-subgroup-")]


def _list_order(tab) -> list[str]:
    """The object names of the widgets in the keys list, top to bottom, in the order the layout holds them."""
    layout = tab.findChild(KeysSection)._entry_layout  # noqa: SLF001 - the order the list shows its widgets in
    widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
    return [widget.objectName() for widget in widgets if widget is not None]


def test_a_card_shows_its_subgroup_line_and_the_owner_can_set_it(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, ACCESS)
    assert tab.findChild(QLabel, "keys-entry-aws-access-id-subgroup").text() == "Subgroup: none"

    _set_subgroup_in_ui(tab, "aws-access-id", AWS)

    assert tab.findChild(QLabel, "keys-entry-aws-access-id-subgroup").text() == "Subgroup: AWS keys"
    assert FAKE_VALUE.encode() not in settings_path.read_bytes()


def test_two_cards_sharing_a_subgroup_show_one_heading_above_them(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, ACCESS)
    _add(tab, SECRET)
    _add(tab, "Neon")
    _set_subgroup_in_ui(tab, "aws-access-id", AWS)
    _set_subgroup_in_ui(tab, "aws-secret", AWS)

    headings = _headings(tab)
    assert [heading.objectName() for heading in headings] == ["keys-subgroup-aws-keys"]
    assert headings[0].text() == AWS
    assert not headings[0].isHidden()
    assert _list_order(tab) == [
        "keys-entry-runpod",
        "keys-subgroup-aws-keys",
        "keys-entry-aws-access-id",
        "keys-entry-aws-secret",
        "keys-entry-neon",
    ]


def test_clearing_the_last_card_of_a_subgroup_removes_its_heading(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, ACCESS)
    _set_subgroup_in_ui(tab, "aws-access-id", AWS)

    _set_subgroup_in_ui(tab, "aws-access-id", "")

    assert tab.findChild(QLabel, "keys-subgroup-aws-keys").isHidden()
    assert "keys-subgroup-aws-keys" not in _list_order(tab)
    assert tab.findChild(QLabel, "keys-entry-aws-access-id-subgroup").text() == "Subgroup: none"


def test_a_subgroup_label_longer_than_40_characters_is_refused_on_the_card(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, ACCESS)

    _set_subgroup_in_ui(tab, "aws-access-id", "x" * 41)

    assert "subgroup name can be at most 40 characters" in tab.findChild(QLabel, "keys-message").text()
    assert tab.findChild(QLabel, "keys-entry-aws-access-id-subgroup").text() == "Subgroup: none"
    assert "key_subgroups" not in _settings_json(settings_path)


def test_search_finds_a_key_by_its_subgroup_label(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, ACCESS)
    _add(tab, "OpenAI")
    _set_subgroup_in_ui(tab, "aws-access-id", AWS)

    tab.findChild(QLineEdit, "keys-search").setText("aws keys")

    assert not tab.findChild(QWidget, "keys-entry-aws-access-id").isHidden()
    assert tab.findChild(QWidget, "keys-entry-openai").isHidden()
    assert not tab.findChild(QLabel, "keys-subgroup-aws-keys").isHidden()


def test_a_subgroup_heading_hides_with_its_cards_when_the_search_misses_them(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, ACCESS)
    _set_subgroup_in_ui(tab, "aws-access-id", AWS)

    tab.findChild(QLineEdit, "keys-search").setText("openai")

    assert tab.findChild(QWidget, "keys-entry-aws-access-id").isHidden()
    assert tab.findChild(QLabel, "keys-subgroup-aws-keys").isHidden()


def test_search_does_not_match_the_subgroup_line_shown_on_a_card(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path)
    _add(tab, "OpenAI")

    tab.findChild(QLineEdit, "keys-search").setText("subgroup")

    assert tab.findChild(QWidget, "keys-entry-openai").isHidden()
