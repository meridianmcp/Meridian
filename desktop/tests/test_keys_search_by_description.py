"""The keys search also matches a known service's description, and never a value. Offscreen Qt, from conftest.

Sprint item 73bbd554 (KEYS-UI). The keychain is a MemoryKeyring, so nothing touches the real credential store. FAKE_VALUE
is an obviously fake value, so the test can show that searching for it finds no card.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLineEdit, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.keys_tab import KeysTab

FAKE_VALUE = "sk-fake-keys-search-0001"


class FakeClient:
    """A RunPod client that never answers, so no request is sent."""

    def probe_health(self, url, timeout):
        from meridian_desktop import local_client

        return local_client.HEALTH_NOT_RUNNING


def _keys_tab(qtbot, settings_path, secrets):
    tab = KeysTab(SettingsStore(settings_path), secrets, client=FakeClient(), confirm=lambda *_: True)
    qtbot.addWidget(tab)
    return tab


def _add(tab, name, value=FAKE_VALUE):
    tab.findChild(QLineEdit, "keys-new-name").setText(name)
    tab.findChild(QLineEdit, "keys-new-value").setText(value)
    tab.findChild(QPushButton, "keys-add").click()


def _search(tab, text):
    tab.findChild(QLineEdit, "keys-search").setText(text)


def _shown(tab, key):
    # isHidden reads the explicit hidden flag, so it works without the tab being on screen.
    return not tab.findChild(QLineEdit, "keys-search").parent().findChild(object, f"keys-entry-{key}").isHidden()


def test_a_search_for_words_in_a_services_description_shows_that_card_only(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))
    _add(tab, "OpenAI")
    _add(tab, "Anthropic")

    # "claude models" is in Anthropic's description only. Neither name contains it.
    _search(tab, "claude models")
    assert _shown(tab, "anthropic") is True
    assert _shown(tab, "openai") is False


def test_a_search_for_a_name_still_matches_by_name(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))
    _add(tab, "OpenAI")
    _add(tab, "Anthropic")

    _search(tab, "openai")
    assert _shown(tab, "openai") is True
    assert _shown(tab, "anthropic") is False


def test_a_search_for_a_value_finds_no_card(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))
    _add(tab, "OpenAI")

    _search(tab, FAKE_VALUE)
    assert _shown(tab, "openai") is False
