"""The Settings page scrolls as one page: its column is as tall as its content, so no part of it hides behind a second scrollbar.

The main window puts the Settings tab inside the page's own scroll area. The tab's column is a scroll area too. Before this
fix, that inner scroll area's size hint was capped at 24 text lines (336 px), so the Client setup result line and the Diagnostics
group were cut off. The page is built the same way here, at 1280 px wide. Offscreen Qt, from conftest.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from _fakes import MemoryKeyring
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.settings_tab import SettingsTab

PAGE_WIDTH = 1280
PAGE_HEIGHT = 900


def _page(qtbot, settings_path):
    """A page the way the main window builds it: an outer scroll area around the Settings tab."""
    outer = QScrollArea()
    outer.setWidgetResizable(True)
    container = QWidget()
    column = QVBoxLayout(container)
    tab = SettingsTab(SettingsStore(settings_path), secrets=KeyringSecretStore(backend=MemoryKeyring()))
    column.addWidget(tab)
    column.addStretch(1)
    outer.setWidget(container)
    qtbot.addWidget(outer)
    outer.resize(PAGE_WIDTH, PAGE_HEIGHT)
    outer.show()
    qtbot.waitExposed(outer)
    return outer, tab


def test_settings_column_shows_all_of_its_content_without_an_inner_scrollbar(qtbot, settings_path):
    _outer, tab = _page(qtbot, settings_path)
    inner = tab.findChild(QScrollArea, "settings-scroll")
    assert inner is not None
    content = inner.widget()
    qtbot.wait(100)
    assert inner.viewport().height() >= content.height(), "the Settings column is cut off behind its own scrollbar"


def test_client_setup_result_line_sits_inside_the_visible_column(qtbot, settings_path):
    _outer, tab = _page(qtbot, settings_path)
    inner = tab.findChild(QScrollArea, "settings-scroll")
    result = tab.findChild(QLabel, "client-setup-result")
    assert result is not None
    result.setText("Written: a path. Backup saved at: another path. Restart Codex to load the change.")
    result.setVisible(True)
    qtbot.wait(100)
    top_left = result.mapTo(inner.viewport(), QPoint(0, 0))
    assert top_left.y() + result.height() <= inner.viewport().height(), "the result line is cut off below the column"
