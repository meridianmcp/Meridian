"""The Projects tab's search and selection for a backup. The search hides rows whose name does not match, ignoring case.
Select all shown picks only the rows the search shows, select none clears the selection, and the count says how many
listed projects are selected. A backup of the selection writes only the selected projects that are shown.

Pure Qt with fake reads: a temp backups folder, no socket, and no real home folder."""

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QTableWidget

from meridian_desktop import local_client
from meridian_desktop.data_source import DataSources
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.projects_tab import LOADING_TEXT, ProjectsTab, selection_count_text

FAKE_TOKEN = "sk_meridian_fake-selection-0007"
LOCAL_URL = "http://127.0.0.1:7878"
HOSTED_URL = "https://hosted.test"
ROWS = [
    {"id": "p-1", "name": "Alpha study", "created_at": "2026-01-01T00:00:00"},
    {"id": "p-2", "name": "Beta notes", "created_at": "2026-01-02T00:00:00"},
    {"id": "p-3", "name": "alphabet draft", "created_at": "2026-01-03T00:00:00"},
]


@pytest.fixture
def local_folder(tmp_path, monkeypatch) -> Path:
    """A LOCALAPPDATA of this test's own, so the backups folder is this test's and goes away with it."""
    root = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    return root / "meridian-desktop" / "backups"


def _sources(token: str | None = FAKE_TOKEN) -> DataSources:
    """The local server is running and serves ROWS. The hosted list is ROWS too, so a backup has the same projects."""
    return DataSources(
        probe=lambda url: local_client.HEALTH_RUNNING,
        read_server=lambda url: [dict(row) for row in ROWS],
        read_file=lambda path: [],
        get_token=lambda: token,
        list_hosted=lambda url, saved: [dict(row) for row in ROWS],
    )


def _tab(qtbot, settings_path):
    """A Projects tab that has loaded ROWS. Returns the tab and its table."""
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    tab = ProjectsTab(SettingsStore(settings_path), _sources())
    qtbot.addWidget(tab)
    tab.refresh()
    table = tab.findChild(QTableWidget, "projects-table")
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT and table.rowCount() == len(ROWS), timeout=5000)
    return tab, table


def _name(table, row: int) -> str:
    return table.item(row, 0).text()


def _hidden_names(table) -> set[str]:
    return {_name(table, row) for row in range(table.rowCount()) if table.isRowHidden(row)}


def _selected_names(table) -> set[str]:
    rows = {index.row() for index in table.selectionModel().selectedRows()}
    return {_name(table, row) for row in rows}


def _count(tab) -> str:
    return tab.findChild(QLabel, "projects-selection-count").text()


def _type(tab, text: str) -> None:
    tab.findChild(QLineEdit, "projects-search").setText(text)


def test_the_count_text_says_how_many_are_selected_and_how_many_the_search_shows():
    assert selection_count_text(0, 3, 3) == "0 of 3 selected"
    assert selection_count_text(2, 3, 3) == "2 of 3 selected"
    assert selection_count_text(1, 3, 2) == "1 of 3 selected (2 shown)"


def test_the_search_hides_names_that_do_not_match_and_ignores_case(qtbot, settings_path):
    tab, table = _tab(qtbot, settings_path)

    _type(tab, "ALPHA")

    assert _hidden_names(table) == {"Beta notes"}
    # The first shown row was selected on load, and it still matches, so one is selected of the two shown.
    assert _count(tab) == "1 of 3 selected (2 shown)"


def test_clearing_the_search_shows_every_row_again(qtbot, settings_path):
    tab, table = _tab(qtbot, settings_path)
    _type(tab, "beta")
    _type(tab, "")

    assert _hidden_names(table) == set()


def test_select_all_shown_takes_only_the_rows_the_search_shows(qtbot, settings_path):
    tab, table = _tab(qtbot, settings_path)
    _type(tab, "alpha")

    tab.findChild(QPushButton, "projects-select-all").click()

    assert _selected_names(table) == {"Alpha study", "alphabet draft"}
    assert _count(tab) == "2 of 3 selected (2 shown)"


def test_select_none_clears_the_selection_and_turns_off_the_selected_backup(qtbot, settings_path, local_folder):
    tab, table = _tab(qtbot, settings_path)
    tab.findChild(QPushButton, "projects-select-all").click()
    assert _count(tab) == "3 of 3 selected"

    tab.findChild(QPushButton, "projects-select-none").click()

    assert _selected_names(table) == set()
    assert _count(tab) == "0 of 3 selected"
    assert not tab.findChild(QPushButton, "projects-backup-selected").isEnabled()


def test_a_row_picked_by_hand_is_counted(qtbot, settings_path):
    tab, table = _tab(qtbot, settings_path)

    table.clearSelection()
    table.selectRow(2)

    assert _count(tab) == "1 of 3 selected"
    assert tab.findChild(QPushButton, "projects-backup-selected").isEnabled()


def test_a_search_that_hides_a_selected_row_deselects_it(qtbot, settings_path):
    tab, table = _tab(qtbot, settings_path)
    tab.findChild(QPushButton, "projects-select-all").click()

    _type(tab, "alpha")

    assert _selected_names(table) == {"Alpha study", "alphabet draft"}
    assert "Beta notes" not in _selected_names(table)
    assert _count(tab) == "2 of 3 selected (2 shown)"


def test_the_selected_backup_writes_only_the_selected_projects_that_are_shown(qtbot, settings_path, local_folder):
    tab, _table = _tab(qtbot, settings_path)
    status = tab.findChild(QLabel, "projects-backup-status")
    _type(tab, "alpha")
    tab.findChild(QPushButton, "projects-select-all").click()

    tab.findChild(QPushButton, "projects-backup-selected").click()
    qtbot.waitUntil(lambda: status.text().startswith("Backed up"), timeout=5000)

    files = sorted(local_folder.glob("projects-*.json"))
    assert len(files) == 1
    held = [row["id"] for row in json.loads(files[0].read_text(encoding="utf-8"))["projects"]]
    assert sorted(held) == ["p-1", "p-3"]


def test_the_selection_controls_are_absent_without_a_saved_token(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(mode="local", hosted_url=HOSTED_URL, local_url=LOCAL_URL))
    sources = _sources(token=None)
    tab = ProjectsTab(SettingsStore(settings_path), sources)
    qtbot.addWidget(tab)
    tab.refresh()
    status = tab.findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() != LOADING_TEXT, timeout=5000)

    assert tab.findChild(QPushButton, "projects-select-all") is None
    assert tab.findChild(QLabel, "projects-selection-count") is None
    # The search is on the tab whatever the token state, and it is the only text box.
    assert tab.findChild(QLineEdit, "projects-search") is not None
