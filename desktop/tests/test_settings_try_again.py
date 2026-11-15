"""The Try again button on the Settings tab, beside Test connection (meridian_desktop/ui/settings_tab.py), with pytest-qt.

Try again runs the same action as Test connection. The tab connects each button to that action when the tab is built,
so the recorder test puts its stand-in on the class before the tab is built, then clicks the button. The list call is
injected, so nothing reaches the network or the real keychain. Offscreen Qt, from conftest.
"""

from __future__ import annotations

import threading

from PySide6.QtWidgets import QLabel, QPushButton

from _fakes import MemoryKeyring
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import DEFAULT_HOSTED_URL, SettingsStore
from meridian_desktop.ui.settings_tab import TEST_CONNECTED_TEXT, TEST_RUNNING_TEXT, SettingsTab

TRY_AGAIN = "settings-try-again"
TEST_CONNECTION = "settings-test-connection"
RESULT = "settings-test-result"


def _unexpected_list(url, token):
    raise AssertionError("the hosted list must not be called in this test")


def _tab(qtbot, settings_path, list_hosted=_unexpected_list):
    tab = SettingsTab(
        SettingsStore(settings_path),
        secrets=KeyringSecretStore(backend=MemoryKeyring()),
        list_hosted=list_hosted,
    )
    qtbot.addWidget(tab)
    return tab


def test_try_again_button_is_built_beside_test_connection(qtbot, settings_path):
    tab = _tab(qtbot, settings_path)

    button = tab.findChild(QPushButton, TRY_AGAIN)
    assert button is not None
    assert button.text() == "Try again"
    assert button.isEnabled()
    assert button.isVisibleTo(tab)
    # Same group body as Test connection, so the two sit in one row.
    assert button.parentWidget() is tab.findChild(QPushButton, TEST_CONNECTION).parentWidget()


def test_clicking_try_again_calls_the_test_action(qtbot, settings_path, monkeypatch):
    calls: list[object] = []

    def recorder(self):
        calls.append(self)

    monkeypatch.setattr(SettingsTab, "_on_test_clicked", recorder)
    tab = _tab(qtbot, settings_path)

    tab.findChild(QPushButton, TRY_AGAIN).click()

    assert len(calls) == 1
    assert calls[0] is tab


def test_try_again_runs_the_real_test_and_is_disabled_while_it_runs(qtbot, settings_path):
    gate = threading.Event()

    def slow(url, token):
        gate.wait(5)
        return []

    tab = _tab(qtbot, settings_path, list_hosted=slow)
    try_again = tab.findChild(QPushButton, TRY_AGAIN)
    test_button = tab.findChild(QPushButton, TEST_CONNECTION)
    result = tab.findChild(QLabel, RESULT)

    try_again.click()

    # While the test runs, both buttons are disabled, as Test connection alone was before.
    assert not try_again.isEnabled()
    assert not test_button.isEnabled()
    assert result.text() == TEST_RUNNING_TEXT
    gate.set()
    qtbot.waitUntil(lambda: try_again.isEnabled() and test_button.isEnabled(), timeout=5000)
    assert result.text() == TEST_CONNECTED_TEXT.format(url=DEFAULT_HOSTED_URL, count=0)
