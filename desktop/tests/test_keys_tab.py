"""Tests for the Keys tab (meridian_desktop/ui/keys_tab.py): it is a top-level tab before Settings and holds the Keys section.

Offscreen Qt from conftest. The keychain is a MemoryKeyring, the RunPod client is a fake that counts its probes and never
sends a request, and the data sources are fakes, so nothing touches the network, the real credential store or a dialog.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QScrollArea, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import local_client, projects_reader
from meridian_desktop.data_source import DataSources
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.keys_section import KeysSection
from meridian_desktop.ui.keys_tab import KeysTab
from meridian_desktop.ui.main_window import TAB_NAMES, MainWindow
from meridian_desktop.ui.runpod_panel import RunPodPanel
from meridian_desktop.ui.settings_tab import SettingsTab


def _unexpected(*args):
    raise AssertionError("no data source should be read in this test")


def _sources():
    return DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_unexpected,
        read_file=projects_reader.list_projects,
        get_token=lambda: None,
        list_hosted=_unexpected,
    )


class CountingClient:
    """A RunPod client that counts its probes and reports the local server as not running. No request is sent."""

    def __init__(self) -> None:
        self.probes = 0

    def probe_health(self, url, timeout):
        self.probes += 1
        return local_client.HEALTH_NOT_RUNNING


def _window(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path), secret_store=KeyringSecretStore(backend=MemoryKeyring()),
                        data_sources=_sources())
    qtbot.addWidget(window)
    return window


def _tab_names(window) -> list[str]:
    return [window.tabs.tabText(index) for index in range(window.tabs.count())]


def test_keys_is_a_top_level_tab_just_before_settings(qtbot, settings_path):
    window = _window(qtbot, settings_path)

    assert TAB_NAMES[-2:] == ("Keys", "Settings")
    names = _tab_names(window)
    assert names.index("Keys") == names.index("Settings") - 1, "Keys sits just before Settings"
    assert isinstance(window.tabs.widget(names.index("Keys")), KeysTab)


def test_the_keys_tab_holds_the_keys_section_and_the_runpod_card(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    page = window.tabs.widget(_tab_names(window).index("Keys"))

    assert page.objectName() == "keys-tab"
    assert isinstance(page, QScrollArea), "a long list of names scrolls inside the tab"
    section = page.findChild(KeysSection, "keys-section")
    assert section is not None
    assert section.objectName() == "keys-section"
    card = page.findChild(QWidget, "runpod-entry-card")
    assert card is not None and section.isAncestorOf(card), "the RunPod card is placed by the Keys section"


def test_the_window_holds_exactly_one_keys_section_and_settings_holds_none(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    settings = window.tabs.widget(_tab_names(window).index("Settings"))

    assert len(window.findChildren(KeysSection)) == 1, "the keys are mounted once, not copied into Settings"
    assert settings.findChild(KeysSection, "keys-section") is None
    assert settings.findChild(RunPodPanel) is None
    assert settings.findChild(SettingsTab, "settings-tab") is not None


def test_the_keys_tab_probes_runpod_once_on_its_first_show(qtbot, settings_path):
    client = CountingClient()
    tab = KeysTab(SettingsStore(settings_path), KeyringSecretStore(backend=MemoryKeyring()), client=client)
    qtbot.addWidget(tab)
    assert client.probes == 0, "building the tab probes nothing"

    tab.show()
    qtbot.waitUntil(lambda: client.probes == 1, timeout=5000)
    panel = tab.findChild(RunPodPanel, "runpod-panel")
    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)
    status = tab.findChild(QLabel, "runpod-server-status")
    assert status.text().startswith("Local Meridian server: not running"), "the card shows the probe's answer"

    tab.hide()
    tab.show()
    qtbot.wait(50)
    assert client.probes == 1, "showing the tab again does not probe again"
