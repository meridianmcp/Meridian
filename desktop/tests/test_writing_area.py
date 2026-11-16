"""Tests for the Writing area in Settings (meridian_desktop/ui/writing_area.py), with pytest-qt.

The area reads nothing and sends nothing, so these tests need no socket and no fake server. The window test builds
the real MainWindow with fake data sources, the same way test_smoke does, so nothing touches the network or the disk.
Offscreen Qt, from conftest.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import local_client, projects_reader
from meridian_desktop.data_source import DataSources
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.main_window import TAB_NAMES, MainWindow
from meridian_desktop.ui.text import SELECTABLE_TEXT
from meridian_desktop.ui.writing_area import DOCX_LINE, SLIDES_LINE, WRITING_SECTION, WritingArea


def _unexpected(*_args, **_kwargs):
    raise AssertionError("the Writing area must not reach the network or the data sources")


def _sources():
    return DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_unexpected,
        read_file=projects_reader.list_projects,
        get_token=lambda: None,
        list_hosted=_unexpected,
    )


def test_docx_and_slides_each_say_not_connected_with_the_missing_route(qtbot):
    area = WritingArea()
    qtbot.addWidget(area)
    docx = area.findChild(QLabel, "writing-docx")
    slides = area.findChild(QLabel, "writing-slides")
    assert docx is not None and slides is not None
    assert docx.text() == DOCX_LINE
    assert slides.text() == SLIDES_LINE
    assert docx.text().startswith("DOCX: Not connected.")
    assert "no DOCX route yet" in docx.text()
    assert slides.text().startswith("Slides: Not connected.")
    assert "no Slides route yet" in slides.text()


def test_writing_labels_are_plain_text_and_selectable(qtbot):
    area = WritingArea()
    qtbot.addWidget(area)
    labels = area.findChildren(QLabel)
    assert len(labels) == 3
    for label in labels:
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.textInteractionFlags() & SELECTABLE_TEXT == SELECTABLE_TEXT, label.objectName()


def test_settings_page_holds_the_writing_area_and_no_tab_of_its_own(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path), secret_store=KeyringSecretStore(backend=MemoryKeyring()),
                        data_sources=_sources())
    qtbot.addWidget(window)
    # The Writing area is a section of Settings, never a main tab of its own.
    assert TAB_NAMES == ("Home", "Projects", "Sessions", "GPUs", "Search", "Tunnel", "Zotero", "LaTeX", "Keys", "Settings")
    assert window.tabs.count() == 10
    names = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    page = window.tabs.widget(names.index("Settings"))
    body = page.findChild(QWidget, "settings-page")
    area = body.findChild(WritingArea, "writing-area")
    assert area is not None
    assert WRITING_SECTION in [label.text() for label in body.findChildren(QLabel)]
    assert area.findChild(QLabel, "writing-docx") is not None
    assert area.findChild(QLabel, "writing-slides") is not None
