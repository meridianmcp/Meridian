"""Default size and tab bar of the main window. Offscreen Qt, no network.

At the old default (about 506 px wide) the eight tab labels did not fit, so Qt hid some
tabs behind scroll arrows and cut off one label. The window also opened too small for the
Settings panel. These tests pin the fix.
"""

import dataclasses

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QLabel

from _fakes import MemoryKeyring
from meridian_desktop import local_client
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.main_window import DEFAULT_WINDOW_SIZE, STATUS_MESSAGE, MainWindow
from meridian_desktop.ui.projects_tab import default_sources
from meridian_desktop.zotero_client import API_NOT_RUNNING, NOT_RUNNING_MESSAGE, ApiCheck, ZoteroStatus

# The Settings panel's key row and pods table need at least this much room on first show.
MIN_DEFAULT_WIDTH = 900
MIN_DEFAULT_HEIGHT = 600


class _OfflineZotero:
    """A stand-in for the Zotero client: Zotero is not installed and not running. Counts status checks."""

    def __init__(self):
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return ZoteroStatus(exe=None, process_running=None, api=ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE))

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE)

    def search_items(self, query):
        return []

    def start_zotero(self, exe):
        return ""


def _offline_sources():
    """The default sources, with a fake probe and a fake server transport, so no test opens a socket.

    The probe says no server is running, so the server read is never called. If it were called,
    the transport answers an empty list, so nothing reaches the network either way.
    """

    def transport(request, timeout):
        return local_client._Reply(200, b"[]")

    real = default_sources(KeyringSecretStore(backend=MemoryKeyring()), server_transport=transport)
    return dataclasses.replace(real, probe=lambda url: local_client.HEALTH_NOT_RUNNING)


def _shown_window(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path), data_sources=_offline_sources())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    return window


def _clipped_tabs(bar):
    """Labels whose tab sits outside the bar, or is too narrow for its text."""
    clipped = []
    for index in range(bar.count()):
        rect = bar.tabRect(index)
        label = bar.tabText(index)
        inside = 0 <= rect.left() and rect.right() < bar.width()
        if not inside or rect.width() < bar.fontMetrics().horizontalAdvance(label):
            clipped.append(label)
    return clipped


def test_default_window_is_as_large_as_the_screen_allows_and_centred_when_it_fits(qtbot, settings_path):
    # The window asks for up to 95% of the usable width and 90% of the usable height, and it can never be smaller
    # than its content (the tab bar and the Settings panel set minimums). When it fits the screen, it is centred.
    window = _shown_window(qtbot, settings_path)
    usable = QGuiApplication.primaryScreen().availableGeometry()
    assert window.width() >= min(MIN_DEFAULT_WIDTH, int(usable.width() * 0.95)) - 1
    assert window.height() >= min(MIN_DEFAULT_HEIGHT, int(usable.height() * 0.9)) - 1
    if window.width() <= usable.width() and window.height() <= usable.height():
        assert abs(window.geometry().center().x() - usable.center().x()) <= 2
        assert abs(window.geometry().center().y() - usable.center().y()) <= 2


def test_every_tab_label_is_visible_at_the_default_size(qtbot, settings_path):
    window = _shown_window(qtbot, settings_path)
    bar = window.tabs.tabBar()
    assert bar.sizeHint().width() <= bar.width()  # no scroll arrows are needed
    assert _clipped_tabs(bar) == []


def test_window_cannot_shrink_below_the_tab_bar(qtbot, settings_path):
    window = _shown_window(qtbot, settings_path)
    window.resize(200, 200)
    qtbot.wait(50)
    bar = window.tabs.tabBar()
    assert bar.sizeHint().width() <= bar.width()
    assert _clipped_tabs(bar) == []


def test_the_home_status_line_shows_on_home_and_does_not_stick_on_another_tab(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path), data_sources=_offline_sources())
    qtbot.addWidget(window)
    names = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    home, settings = names.index("Home"), names.index("Settings")
    assert window.statusBar().currentMessage() == STATUS_MESSAGE

    window.tabs.setCurrentIndex(settings)
    assert window.statusBar().currentMessage() == "", "Home's line is cleared on another tab"

    window.tabs.setCurrentIndex(home)
    assert window.statusBar().currentMessage() == STATUS_MESSAGE


def test_home_zotero_card_checks_on_boot_and_keeps_the_window_size(qtbot, settings_path):
    fake = _OfflineZotero()
    window = MainWindow(SettingsStore(settings_path), zotero=fake, data_sources=_offline_sources())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    # Showing the window checks Zotero once, on Home, with no tab opened.
    qtbot.waitUntil(lambda: fake.status_calls == 1, timeout=5000)
    card = window.findChild(QFrame, "home-card-zotero")
    assert card is not None
    qtbot.waitUntil(lambda: card.findChild(QLabel, "home-zotero-headline").text() == "Not installed", timeout=5000)
    assert fake.status_calls == 1

    assert window.width() <= DEFAULT_WINDOW_SIZE[0]
    bar = window.tabs.tabBar()
    assert bar.sizeHint().width() <= bar.width()
    assert _clipped_tabs(bar) == []
