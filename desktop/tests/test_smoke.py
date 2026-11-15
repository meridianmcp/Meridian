"""Smoke tests for the window and its tabs. Offscreen Qt, no network.

The Projects tab reads through fake data sources here, so no test probes a server, opens
the real home directory or reads the keychain. The one real database is a temp file.
"""

import dataclasses
import gc
import json
import socket
import sqlite3
import threading
import weakref
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QToolButton,
    QWidget,
)

from _fakes import MemoryKeyring
from meridian_desktop import __main__ as entry
from meridian_desktop import local_client, projects_reader, settings_store
from meridian_desktop.data_source import (
    NO_LOCAL_DATA_MESSAGE,
    NOT_SIGNED_IN_MESSAGE,
    DataSources,
)
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.token_store import HOSTED_TOKEN_ACCOUNT, TOKEN_SERVICE
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck, ZoteroStatus
from meridian_desktop.ui.home_tab import HomeTab
from meridian_desktop.ui.keys_section import KeysSection
from meridian_desktop.ui.latex_panel import LatexPanel
from meridian_desktop.ui.logs_page import LogsPage
from meridian_desktop.ui.main_window import LATEX_SECTION, STATUS_MESSAGE, TAB_NAMES, MainWindow
from meridian_desktop.ui.projects_tab import TREE_DEPTH_ROLE, ProjectsTab, default_sources
from meridian_desktop.ui.runpod_panel import RunPodPanel
from meridian_desktop.ui.search_tab import SearchTab
from meridian_desktop.ui.tools_panel import ToolsPanel
from meridian_desktop.ui.zotero_panel import ZoteroPanel
from meridian_desktop.ui.settings_tab import (
    DB_SAVED_TEXT,
    LOCAL_URL_SAVED_TEXT,
    NOT_CHOSEN_TEXT,
    NOT_WRITTEN_TEXT,
    SettingsTab,
    mode_text,
)
EXPECTED_TABS = ("Home", "Projects", "Sessions", "GPUs", "Search", "Tunnel", "Zotero", "LaTeX", "Keys", "Settings")
STATUS = STATUS_MESSAGE

FAKE_TOKEN = "sk_meridian_fake-projects-tab"

# The core projects table. executor_config and agent_instructions are present so a test can
# prove the Projects tab never shows them. Inserted oldest first, so the expected order differs.
PROJECT_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    creator_human_id TEXT,
    icon TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT NOT NULL DEFAULT 'P2',
    parent_project_id TEXT,
    created_at TEXT NOT NULL,
    executor_config TEXT,
    agent_instructions TEXT
)
"""
PROJECT_ROWS = [
    ("p-old", "Oldest", "human-1", "book", "active", "P2", None,
     "2026-01-05 09:00:00", "EXEC-SECRET-OLD", "AGENT-NOTE-OLD"),
    ("p-new", "Newest", "human-1", "rocket", "parked", "P0", "p-old",
     "2026-03-02 17:30:00", "EXEC-SECRET-NEW", "AGENT-NOTE-NEW"),
    ("p-mid", "Middle", None, None, "archived", "P1", None,
     "2026-02-14 12:00:00", "EXEC-SECRET-MID", "AGENT-NOTE-MID"),
]
WRITE_CONTROLS = (
    QPushButton,
    QToolButton,
    QCheckBox,
    QRadioButton,
    QLineEdit,
    QComboBox,
    QAbstractSpinBox,
)


def _unexpected(*args):
    raise AssertionError("this data source should not be read in this test")


def _sources(*, probe_state=local_client.HEALTH_NOT_RUNNING, token=None, read_file=None, list_hosted=None):
    """Fake data sources. The probe answers from probe_state. Nothing here touches the network or the disk."""
    return DataSources(
        probe=lambda url: probe_state,
        read_server=_unexpected,
        read_file=read_file or projects_reader.list_projects,
        get_token=lambda: token,
        list_hosted=list_hosted or _unexpected,
    )


class _QuietZotero:
    """The window's Zotero client. It answers that Zotero is not running, and never runs tasklist.exe or calls the API.

    The Home card and the Zotero panel call status and probe_api on the pool, so a window built with the real client
    would run a process and a socket that no test controls. Launching and searching are never reached by these tests.
    """

    def status(self):
        return ZoteroStatus(
            exe=None,
            process_running=False,
            api=ApiCheck(API_NOT_RUNNING, "Zotero is not answering on its local API."),
        )

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, "Zotero is not answering on its local API.")

    start_zotero = _unexpected
    search_items = _unexpected


def _window(qtbot, settings_path, sources=None):
    window = MainWindow(
        SettingsStore(settings_path),
        data_sources=sources if sources is not None else _sources(),
        zotero=_QuietZotero(),
    )
    qtbot.addWidget(window)
    return window


def _shown_window(qtbot, settings_path, sources=None):
    """A shown window with the Projects tab in front. Home is the first tab, and its checks are tested in test_home_tab."""
    window = _window(qtbot, settings_path, sources)
    window.show()
    qtbot.waitExposed(window)
    window.tabs.setCurrentWidget(_page(window, "Projects"))
    return window


def _projects_db(tmp_path) -> Path:
    path = tmp_path / "meridian.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(PROJECT_SCHEMA)
        conn.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?,?)", PROJECT_ROWS)
        conn.commit()
    finally:
        conn.close()
    return path


def _empty_db(tmp_path) -> Path:
    """A file at a database path. A fake reader stands in for it, so its bytes do not matter."""
    path = tmp_path / "meridian.db"
    path.write_bytes(b"")
    return path


def _all_text(widget):
    """Every string a user can read on this widget: labels, table headers and table cells."""
    chunks = [label.text() for label in widget.findChildren(QLabel)]
    for table in widget.findChildren(QTableWidget):
        for column in range(table.columnCount()):
            header = table.horizontalHeaderItem(column)
            if header is not None:
                chunks.append(header.text())
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None:
                    chunks.append(item.text())
    return "\n".join(chunks)


def _tab_names(window):
    return tuple(window.tabs.tabText(i) for i in range(window.tabs.count()))


def _page(window, name):
    return window.tabs.widget(_tab_names(window).index(name))


def _label_texts(widget):
    return [label.text() for label in widget.findChildren(QLabel)]


def test_tab_names_and_order(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert _tab_names(window) == EXPECTED_TABS
    assert TAB_NAMES == EXPECTED_TABS
    assert entry.TAB_NAMES == EXPECTED_TABS


def test_no_tab_is_a_placeholder_and_no_old_tab_is_left(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    for name in EXPECTED_TABS:
        texts = _label_texts(_page(window, name))
        assert not any("Not built yet" in text for text in texts), name
        assert not _page(window, name).objectName().startswith("placeholder-"), name


def test_home_search_and_settings_are_built_pages(qtbot, settings_path):
    window = _shown_window(qtbot, settings_path)
    assert _tab_names(window) == EXPECTED_TABS

    assert isinstance(_page(window, "Home"), HomeTab)
    assert _page(window, "Home").objectName() == "home-tab"
    assert isinstance(_page(window, "Search"), SearchTab)
    assert _page(window, "Search").objectName() == "search-tab"
    assert _page(window, "Projects").objectName() == "projects-tab"


def test_settings_page_holds_diagnostics_and_no_zotero_latex_or_keys(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    page = _page(window, "Settings")
    assert page.objectName() == "settings-scroll"
    body = page.findChild(QWidget, "settings-page")
    assert body is not None
    # The keys, Zotero and LaTeX are tabs of their own, so none of them is on the Settings page.
    assert body.findChild(RunPodPanel) is None
    assert body.findChild(KeysSection, "keys-section") is None
    assert body.findChild(ZoteroPanel, "zotero-panel") is None
    assert body.findChild(LatexPanel) is None
    assert LATEX_SECTION not in _label_texts(body)
    # Diagnostics: the full tools table and the logs (Logs moved here from its own tab).
    assert body.findChild(ToolsPanel, "tools-panel") is not None
    assert body.findChild(LogsPage, "logs-page") is not None
    # Zotero and LaTeX each have their own tab, holding their panel.
    assert _page(window, "Zotero").findChild(ZoteroPanel, "zotero-panel") is not None
    assert _page(window, "LaTeX").findChild(LatexPanel) is not None
    assert LATEX_SECTION in _label_texts(_page(window, "LaTeX"))


def test_settings_page_is_scrollable(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    page = _page(window, "Settings")
    assert page.widgetResizable()
    assert not any("Not built yet" in text for text in _label_texts(page))


def test_mode_selector_offers_hosted_and_local(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    combo = window.mode_selector
    assert [combo.itemData(i) for i in range(combo.count())] == ["hosted", "local"]
    assert [combo.itemText(i) for i in range(combo.count())] == ["Hosted", "Local"]


def test_mode_selector_writes_to_temp_settings_path(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    combo = window.mode_selector
    assert combo.currentData() == "local"
    assert not settings_path.exists()

    combo.setCurrentIndex(combo.findData("hosted"))

    assert settings_path.is_file()
    assert SettingsStore(settings_path).load().settings.mode == "hosted"


def test_mode_selector_reads_saved_mode(qtbot, settings_path):
    SettingsStore(settings_path).set_mode("hosted")
    window = _window(qtbot, settings_path)
    assert window.mode_selector.currentData() == "hosted"


def test_failed_mode_save_reverts_selector(qtbot, settings_path, monkeypatch):
    SettingsStore(settings_path).set_mode("hosted")
    before = settings_path.read_bytes()
    window = _window(qtbot, settings_path)
    window.show()
    qtbot.waitExposed(window)
    combo = window.mode_selector
    assert combo.currentData() == "hosted"

    attempts = []
    real_save = SettingsStore.save

    def counting_save(self, settings):
        attempts.append(settings.mode)
        return real_save(self, settings)

    def failing_replace(src, dst):
        # The last step of the atomic save. Failing here leaves settings.json as it was.
        raise OSError("simulated disk failure")

    monkeypatch.setattr(SettingsStore, "save", counting_save)
    monkeypatch.setattr(settings_store.os, "replace", failing_replace)

    combo.setCurrentIndex(combo.findData("local"))

    # One attempt for the user's change. The revert must not trigger a second save.
    assert attempts == ["local"]
    assert combo.currentData() == "hosted"
    assert "could not save mode" in window.settings_warning.text()
    assert window.settings_warning.isVisible()
    assert settings_path.read_bytes() == before
    assert sorted(p.name for p in settings_path.parent.iterdir()) == ["settings.json"]


def test_status_bar_text(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    assert window.statusBar().currentMessage() == STATUS


def test_smoke_flag_returns_zero_and_prints_ok(settings_path, capsys):
    code = entry.main(["--smoke"], settings_path=settings_path)
    assert code == 0
    assert "smoke ok" in capsys.readouterr().out


# ----------------------------------------------------------- Projects tab (read-only)


def test_projects_tab_is_built_not_a_placeholder(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    page = _page(window, "Projects")
    assert page.objectName() == "projects-tab"
    assert not any("Not built yet" in text for text in _label_texts(page))


def test_projects_tab_lists_rows_and_has_no_write_controls(qtbot, settings_path, tmp_path):
    db = _projects_db(tmp_path)
    SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
    window = _shown_window(qtbot, settings_path)
    page = _page(window, "Projects")
    table = page.findChild(QTableWidget, "projects-table")
    detail = page.findChild(QLabel, "projects-detail")

    qtbot.waitUntil(lambda: table.rowCount() == 3, timeout=5000)
    headers = tuple(table.horizontalHeaderItem(column).text() for column in range(table.columnCount()))
    assert headers == ("Name", "Status", "Priority", "Created at", "Last backup")
    # Newest is a subproject of Oldest, so it nests under Oldest. The roots keep the reader's order,
    # newest first (Middle, then Oldest), and Newest follows its parent Oldest.
    assert [table.item(row, 0).text() for row in range(3)] == ["Middle", "Oldest", "Newest"]
    assert [table.item(row, 0).data(TREE_DEPTH_ROLE) for row in range(3)] == [0, 0, 1]
    assert [table.item(2, column).text() for column in range(1, 4)] == ["parked", "P0", "2026-03-02 17:30:00"]

    # The first row is selected on load, so its detail shows.
    qtbot.waitUntil(lambda: "Name: Middle" in detail.text(), timeout=5000)
    assert "ID: p-mid" in detail.text()
    # An empty field reads as (none).
    assert "Parent project: (none)" in detail.text()
    # Selecting the subproject changes the detail, and it names its parent.
    table.selectRow(2)
    qtbot.waitUntil(lambda: "Name: Newest" in detail.text(), timeout=5000)
    assert "ID: p-new" in detail.text()
    assert "Icon: rocket" in detail.text()
    assert "Parent project: p-old" in detail.text()

    # No write control on the tab, and the table cannot be edited. The one text box is the search filter: it hides
    # rows and never edits or sends anything.
    for widget_type in WRITE_CONTROLS:
        found = page.findChildren(widget_type)
        if widget_type is QLineEdit:
            assert [widget.objectName() for widget in found] == ["projects-search"], widget_type.__name__
        else:
            assert found == [], widget_type.__name__
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers

    # The fields the desktop never reads do not appear anywhere on the tab.
    text = _all_text(page) + "\n" + detail.text()
    assert "EXEC-SECRET" not in text
    assert "AGENT-NOTE" not in text
    assert "executor_config" not in text
    assert "agent_instructions" not in text


def _raise_os_error(path):
    raise OSError(f"cannot open {path}")


@pytest.mark.parametrize("case", ["no_local_data", "not_signed_in", "read_failed", "no_rows"])
def test_each_resolver_state_shows_its_message(qtbot, settings_path, tmp_path, case):
    db = _empty_db(tmp_path)
    if case == "no_local_data":
        SettingsStore(settings_path).save(Settings(mode="local"))
        sources, expected = _sources(), NO_LOCAL_DATA_MESSAGE
    elif case == "not_signed_in":
        SettingsStore(settings_path).save(Settings(mode="hosted"))
        sources, expected = _sources(token=None), NOT_SIGNED_IN_MESSAGE
    elif case == "read_failed":
        SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
        sources, expected = _sources(read_file=_raise_os_error), "The database file could not be read (OSError)."
    else:
        SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
        sources, expected = _sources(read_file=lambda path: []), "No projects found in the database file."
    window = _shown_window(qtbot, settings_path, sources)
    page = _page(window, "Projects")
    status = page.findChild(QLabel, "projects-status")

    qtbot.waitUntil(lambda: status.text() == expected, timeout=5000)
    assert page.findChild(QTableWidget, "projects-table").rowCount() == 0
    # The message never names the database path.
    assert str(db) not in status.text()


def test_hosted_rows_show_without_the_token_or_hidden_fields(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(mode="hosted"))
    hosted_rows = [
        {
            "id": "h-1",
            "name": "Hosted one",
            "status": "active",
            "priority": "P1",
            "created_at": "2026-05-01 10:00:00",
            "executor_config": "EXEC-SECRET-HOSTED",
            "agent_instructions": "AGENT-NOTE-HOSTED",
        }
    ]
    received = []

    def list_hosted(url, token):
        received.append((url, token))
        return hosted_rows

    window = _shown_window(qtbot, settings_path, _sources(token=FAKE_TOKEN, list_hosted=list_hosted))
    page = _page(window, "Projects")
    table = page.findChild(QTableWidget, "projects-table")
    detail = page.findChild(QLabel, "projects-detail")
    status = page.findChild(QLabel, "projects-status")

    qtbot.waitUntil(lambda: table.rowCount() == 1, timeout=5000)
    assert status.text() == "Showing 1 project(s) from hosted Meridian."
    # The token goes to the hosted client only. It never reaches the tab.
    assert received == [(settings_store.DEFAULT_HOSTED_URL, FAKE_TOKEN)]
    text = _all_text(page) + "\n" + detail.text() + "\n" + status.text()
    assert FAKE_TOKEN not in text
    assert "EXEC-SECRET" not in text
    assert "AGENT-NOTE" not in text


def test_mode_change_reloads_the_projects_tab(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(mode="local"))
    window = _shown_window(qtbot, settings_path, _sources(token=None))
    status = _page(window, "Projects").findChild(QLabel, "projects-status")
    qtbot.waitUntil(lambda: status.text() == NO_LOCAL_DATA_MESSAGE, timeout=5000)

    combo = window.mode_selector
    combo.setCurrentIndex(combo.findData("hosted"))

    qtbot.waitUntil(lambda: status.text() == NOT_SIGNED_IN_MESSAGE, timeout=5000)


def test_a_closed_window_is_freed_by_reference_counting(qtbot, settings_path):
    """A closed window is freed when its last reference goes, not later by the cyclic collector.

    The collector runs on whichever thread allocates. A window left for it could be freed on a pool
    worker, which would delete its widgets off the GUI thread. The collector is off for this test,
    so only reference counting can free the window here.
    """
    gc.disable()
    try:
        window = _shown_window(qtbot, settings_path, _sources(token=None))
        ref = weakref.ref(window)
        window.close()
        del window
        assert ref() is None
    finally:
        gc.enable()


def test_a_load_that_finishes_after_its_tab_is_deleted_touches_no_tab(qtbot, settings_path):
    """The worker finishes a load after its tab is gone. It emits to the shared delivery object only,
    so nothing raises and the deleted tab is never touched."""
    SettingsStore(settings_path).save(Settings(mode="local"))
    entered = threading.Event()
    release = threading.Event()

    def transport(request, timeout):
        entered.set()
        release.wait(5)
        return local_client._Reply(200, json.dumps(SERVER_PROJECT_ROWS).encode("utf-8"))

    window = _shown_window(qtbot, settings_path, _server_sources(transport))
    assert entered.wait(5)
    window.close()
    shiboken6.delete(window)  # the tab's widgets are gone on the GUI thread while the worker still reads
    del window
    release.set()
    assert QThreadPool.globalInstance().waitForDone(5000)
    qtbot.wait(50)  # let any delivered result reach the event loop


def test_a_shown_window_with_fake_sources_opens_no_socket(qtbot, settings_path, monkeypatch):
    """Every read a shown window starts goes through its own sources, so no socket opens, not even to 127.0.0.1:7878.

    The Tools read once used the default probe, a real loopback GET with a 10 second timeout. A test's pool drain has
    5 seconds, so that socket could outlast it. Any connect here is recorded and refused at once, so this test is fast.
    """
    SettingsStore(settings_path).save(Settings(mode="local"))
    opened = []

    def record(self, address):
        opened.append(address)
        raise OSError("a test opened a socket")

    monkeypatch.setattr(socket.socket, "connect", record)
    window = _shown_window(qtbot, settings_path, _sources(probe_state=local_client.HEALTH_NOT_RUNNING))
    assert QThreadPool.globalInstance().waitForDone(5000)
    qtbot.wait(50)
    assert opened == []
    window.close()


# The running server's GET /projects rows. Newest first, as the server orders them, and each row
# carries the hidden fields, so a test can prove the Projects tab drops them.
SERVER_PROJECT_ROWS = [
    {
        "id": "s-new", "name": "Served new", "creator_human_id": "human-2", "icon": "rocket",
        "status": "parked", "priority": "P0", "parent_project_id": None,
        "created_at": "2026-04-20 16:45:00",
        "executor_config": "EXEC-SECRET-SERVER", "agent_instructions": "AGENT-NOTE-SERVER",
    },
    {
        "id": "s-old", "name": "Served old", "creator_human_id": None, "icon": None,
        "status": "active", "priority": "P2", "parent_project_id": "s-new",
        "created_at": "2026-04-01 08:00:00",
        "executor_config": "EXEC-SECRET-SERVER", "agent_instructions": "AGENT-NOTE-SERVER",
    },
]


def _server_sources(transport, probe_state=local_client.HEALTH_RUNNING):
    """The real default sources. The probe answers probe_state, and the server read goes through transport."""
    real = default_sources(KeyringSecretStore(backend=MemoryKeyring()), server_transport=transport)
    return dataclasses.replace(real, probe=lambda url: probe_state)


def test_projects_tab_shows_rows_from_the_running_server(qtbot, settings_path):
    """A running server gives the rows. The fake transport answers the one GET /projects, so no socket opens."""
    SettingsStore(settings_path).save(Settings(mode="local"))
    calls = []

    def transport(request, timeout):
        calls.append((request.get_method(), request.full_url, timeout))
        return local_client._Reply(200, json.dumps(SERVER_PROJECT_ROWS).encode("utf-8"))

    window = _shown_window(qtbot, settings_path, _server_sources(transport))
    page = _page(window, "Projects")
    table = page.findChild(QTableWidget, "projects-table")
    detail = page.findChild(QLabel, "projects-detail")
    status = page.findChild(QLabel, "projects-status")

    qtbot.waitUntil(lambda: table.rowCount() == 2, timeout=5000)
    assert status.text() == "Showing 2 project(s) from the local Meridian server."
    assert [table.item(row, 0).text() for row in range(2)] == ["Served new", "Served old"]
    qtbot.waitUntil(lambda: "Name: Served new" in detail.text(), timeout=5000)
    # The one request is a GET to the loopback server, with the 10 second timeout.
    assert calls[0] == ("GET", "http://127.0.0.1:7878/projects", 10.0)
    # Only the listed fields reach the tab. The hidden ones never do.
    text = _all_text(page) + "\n" + detail.text() + "\n" + status.text()
    assert "EXEC-SECRET" not in text
    assert "AGENT-NOTE" not in text
    assert "executor_config" not in text


def test_projects_tab_refuses_a_non_list_server_reply_and_does_not_fall_back(qtbot, settings_path, tmp_path):
    """A reply that is not a JSON list is read_failed. The database file is not read in its place."""
    db = _projects_db(tmp_path)
    SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))

    def transport(request, timeout):
        return local_client._Reply(200, b'{"detail": "SERVER-BODY-SENTINEL"}')

    window = _shown_window(qtbot, settings_path, _server_sources(transport))
    page = _page(window, "Projects")
    table = page.findChild(QTableWidget, "projects-table")
    status = page.findChild(QLabel, "projects-status")

    expected = "The local Meridian server could not be read (LocalClientError)."
    qtbot.waitUntil(lambda: status.text() == expected, timeout=5000)
    # The file has three rows, and none of them is shown. The body and the path never reach the tab.
    assert table.rowCount() == 0
    assert "SERVER-BODY-SENTINEL" not in _all_text(page)
    assert str(db) not in status.text()


def test_projects_tab_refuses_a_non_loopback_server_url_before_any_request(qtbot, settings_path):
    """A non-loopback local URL is refused by the client before it sends anything. The fake transport sees no call."""
    SettingsStore(settings_path).save(Settings(mode="local", local_url="http://evil.example:7878"))
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return local_client._Reply(200, b"[]")

    window = _shown_window(qtbot, settings_path, _server_sources(transport))
    status = _page(window, "Projects").findChild(QLabel, "projects-status")

    expected = "The local Meridian server could not be read (LocalClientError)."
    qtbot.waitUntil(lambda: status.text() == expected, timeout=5000)
    assert calls == []
    assert "evil.example" not in status.text()


def test_projects_tab_falls_back_to_the_file_when_the_server_is_down(qtbot, settings_path, tmp_path):
    """With the server down and a database file chosen, the tab shows the file's rows. The server is not read."""
    db = _projects_db(tmp_path)
    SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return local_client._Reply(200, b"[]")

    sources = _server_sources(transport, probe_state=local_client.HEALTH_NOT_RUNNING)
    window = _shown_window(qtbot, settings_path, sources)
    page = _page(window, "Projects")
    table = page.findChild(QTableWidget, "projects-table")
    status = page.findChild(QLabel, "projects-status")

    qtbot.waitUntil(lambda: table.rowCount() == 3, timeout=5000)
    assert status.text() == "Showing 3 project(s) from the database file."
    assert [table.item(row, 0).text() for row in range(3)] == ["Middle", "Oldest", "Newest"]
    assert calls == []


def test_a_load_still_running_when_its_projects_tab_is_deleted_does_not_keep_the_tab(qtbot, settings_path, monkeypatch):
    """A Projects tab deleted while its load still runs is freed on the GUI thread, and no widget of it is touched.

    The load holds the settings store, the sources, an owner token and the shared delivery object, never the tab. The
    defect this guards is the one fixed in 687b33a, where the load job held the tab's own bound method. Run on the tree
    before 687b33a, this test fails at the reference check below. It passes on 687b33a, on a178e29, on the tree before
    cec4743 and on the current tree. The collector is off, so only reference counting can free the tab here. The update
    step is looked up with raising=False, so the test does not depend on whether the tab has that method.
    """
    entered = threading.Event()
    gate = threading.Event()
    touched = []
    monkeypatch.setattr(ProjectsTab, "_show", lambda self, outcome: touched.append(outcome), raising=False)

    def held_probe(url):
        entered.set()
        gate.wait(5)
        return local_client.HEALTH_NOT_RUNNING

    SettingsStore(settings_path).save(Settings(mode="local"))
    sources = dataclasses.replace(_sources(), probe=held_probe)

    gc.disable()
    try:
        container = QWidget()
        tab = ProjectsTab(SettingsStore(settings_path), sources, parent=container)
        tab.refresh()
        assert entered.wait(5)  # the load has started and is held at the probe
        ref = weakref.ref(tab)
        shiboken6.delete(container)  # the tab's widgets are deleted on the GUI thread while the load still runs
        del tab
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
        assert touched == []  # the deleted tab's update step never runs, so its widgets are never touched
    finally:
        gate.set()
        gc.enable()


def _project_row(name):
    return dict(SERVER_PROJECT_ROWS[0], name=name)


def _first_name(table):
    item = table.item(0, 0) if table.rowCount() == 1 else None
    return "" if item is None else item.text()


def test_projects_tab_drops_a_result_from_another_owner_or_from_a_superseded_load(qtbot, settings_path, tmp_path):
    """A Projects tab keeps only a load of its own that no newer load has superseded.

    Two tabs read the same file. The tab's first load emits its Delivered. The sibling's first load then emits one at the
    same generation, and it reaches the tab too, so the tab drops it by owner alone. The tab loads again, and its first
    Delivered is replayed: it carries the tab's own owner with a superseded generation, so it is dropped by generation
    alone. The tab's second rows are what the user sees throughout, and they are the control that the tab's own current
    Delivered is accepted. The tab listens on the shared delivery object, so a Delivered emitted there reaches it. On
    a178e29 and on the tree before cec4743, the shared object exists but the Projects tab does not use it yet, so this
    test fails there at its first check: the first load's Delivered never reaches the shared object.
    """
    # Imported here, not at the top of the module, because the helper needs ui/delivery.py, which the older trees do not
    # have. The module, and the lifetime test above, must still import on those trees.
    from _shared_delivery import emit, recording_deliveries, visible_state

    db = tmp_path / "meridian.db"
    db.write_bytes(b"")  # the resolver reads the file branch only for a file that exists; the rows come from the reader
    SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
    rows = {"current": [_project_row("Own first")]}
    tab = ProjectsTab(SettingsStore(settings_path), _sources(read_file=lambda path: rows["current"]))
    qtbot.addWidget(tab)
    sibling = ProjectsTab(SettingsStore(settings_path), _sources(read_file=lambda path: rows["current"]))
    qtbot.addWidget(sibling)
    table = tab.findChild(QTableWidget, "projects-table")
    sibling_table = sibling.findChild(QTableWidget, "projects-table")
    with recording_deliveries() as emitted:
        tab.refresh()
        qtbot.waitUntil(lambda: _first_name(table) == "Own first", timeout=5000)
        assert len(emitted) == 1
        own_first = emitted[0]
        shown_first = visible_state(tab)
        rows["current"] = [_project_row("Sibling first")]
        sibling.refresh()
        qtbot.waitUntil(lambda: _first_name(sibling_table) == "Sibling first", timeout=5000)
        assert len(emitted) == 2
        assert visible_state(tab) == shown_first
        rows["current"] = [_project_row("Own second")]
        tab.refresh()
        qtbot.waitUntil(lambda: _first_name(table) == "Own second", timeout=5000)
        shown_second = visible_state(tab)
        assert shown_second != shown_first
        emit(own_first)  # this tab's answer from the first load, which the second load superseded
        qtbot.wait(20)
    assert visible_state(tab) == shown_second


def test_hosted_reader_gets_the_stored_token_and_never_shows_it(qtbot, settings_path):
    """The hosted read takes the token from the store and passes it to the hosted reader. The token never appears on screen.
    The reader is a fake here, so the test makes no network call."""
    calls = []

    def list_hosted(url, token):
        calls.append(token)
        return []

    SettingsStore(settings_path).save(Settings(mode="hosted"))
    window = _shown_window(qtbot, settings_path, _sources(token=FAKE_TOKEN, list_hosted=list_hosted))
    status = _page(window, "Projects").findChild(QLabel, "projects-status")

    qtbot.waitUntil(lambda: len(calls) > 0, timeout=5000)
    assert calls == [FAKE_TOKEN]
    assert FAKE_TOKEN not in status.text()
    assert FAKE_TOKEN not in _all_text(window)


# ----------------------------------------------------------- Settings tab (connection settings)

LOOPBACK_OK = "http://localhost:7999"


def _settings_tab(window):
    """The connection section, which sits inside the Settings page above the RunPod rows."""
    tab = _page(window, "Settings").findChild(SettingsTab, "settings-tab")
    assert tab is not None
    return tab


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:7878",  # not http
        "ftp://127.0.0.1:7878",  # not http
        "http://example.com:7878",  # not loopback
        "http://10.0.0.5:7878",  # a private address, not loopback
        "http://127.0.0.1.example.com:7878",  # a lookalike of a loopback address
        "http://someone@127.0.0.1:7878",  # a user name in the address
        "",  # empty
    ],
)
def test_settings_tab_rejects_non_loopback_local_url(qtbot, settings_path, url):
    SettingsStore(settings_path).save(Settings(mode="local"))
    before = settings_path.read_bytes()
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)
    tab.findChild(QLineEdit, "settings-local-url").setText(url)

    tab.findChild(QPushButton, "settings-save-local-url").click()

    message = tab.findChild(QLabel, "settings-local-url-message")
    assert message.text().startswith("Not saved.")
    assert not message.isHidden()
    # Nothing was written: the file bytes are unchanged.
    assert settings_path.read_bytes() == before


def test_settings_tab_saves_loopback_local_url_and_shows_saved_values(qtbot, settings_path, tmp_path):
    db = _empty_db(tmp_path)
    SettingsStore(settings_path).save(Settings(mode="local", local_url=LOOPBACK_OK, db_path=str(db)))
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)
    field = tab.findChild(QLineEdit, "settings-local-url")
    assert field.text() == LOOPBACK_OK
    assert tab.findChild(QLabel, "settings-db-path").text() == str(db)

    field.setText("  http://127.0.0.1:7999  ")
    tab.findChild(QPushButton, "settings-save-local-url").click()

    saved = SettingsStore(settings_path).load().settings
    assert saved.local_url == "http://127.0.0.1:7999"
    # Only the local URL changed. The mode and the database file are kept.
    assert saved.mode == "local"
    assert saved.db_path == str(db)
    assert tab.findChild(QLabel, "settings-local-url-message").text() == LOCAL_URL_SAVED_TEXT


def test_settings_tab_local_url_write_failure_is_shown(qtbot, settings_path, monkeypatch):
    SettingsStore(settings_path).save(Settings(mode="local"))
    before = settings_path.read_bytes()

    def failing_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(settings_store.os, "replace", failing_replace)
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)
    tab.findChild(QLineEdit, "settings-local-url").setText(LOOPBACK_OK)

    tab.findChild(QPushButton, "settings-save-local-url").click()

    assert tab.findChild(QLabel, "settings-local-url-message").text() == f"{NOT_WRITTEN_TEXT}."
    assert settings_path.read_bytes() == before
    assert sorted(p.name for p in settings_path.parent.iterdir()) == ["settings.json"]


def test_settings_tab_db_path_chosen_by_dialog_and_shown_back(qtbot, settings_path, tmp_path, monkeypatch):
    db = _empty_db(tmp_path)
    asked = []

    def fake_dialog(parent, caption, directory, file_filter, *args, **kwargs):
        asked.append((caption, file_filter))
        return str(db), "SQLite database (*.db *.sqlite *.sqlite3)"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", fake_dialog)
    SettingsStore(settings_path).save(Settings(mode="local"))
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)
    label = tab.findChild(QLabel, "settings-db-path")
    assert label.text() == NOT_CHOSEN_TEXT

    tab.findChild(QPushButton, "settings-choose-db").click()

    assert len(asked) == 1
    assert SettingsStore(settings_path).load().settings.db_path == str(db)
    assert label.text() == str(db)
    assert tab.findChild(QLabel, "settings-db-message").text() == DB_SAVED_TEXT


def test_settings_tab_cancelled_dialog_changes_nothing(qtbot, settings_path, tmp_path, monkeypatch):
    previous = _empty_db(tmp_path)
    start_dirs = []

    def cancelled(parent, caption, directory, file_filter, *args, **kwargs):
        start_dirs.append(directory)
        return "", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", cancelled)
    SettingsStore(settings_path).save(Settings(mode="local", db_path=str(previous)))
    before = settings_path.read_bytes()
    window = _window(qtbot, settings_path)
    tab = _settings_tab(window)

    tab.findChild(QPushButton, "settings-choose-db").click()

    # The dialog opens in the folder of the file already chosen, and a cancel leaves everything as it was.
    assert start_dirs == [str(previous.parent)]
    assert settings_path.read_bytes() == before
    assert tab.findChild(QLabel, "settings-db-path").text() == str(previous)
    assert tab.findChild(QLabel, "settings-db-message").isHidden()


def test_settings_tab_never_shows_or_takes_the_hosted_token(qtbot, settings_path):
    backend = MemoryKeyring()
    secrets = KeyringSecretStore(backend=backend)
    secrets.set_secret(TOKEN_SERVICE, HOSTED_TOKEN_ACCOUNT, FAKE_TOKEN)
    SettingsStore(settings_path).save(Settings(mode="hosted"))
    window = MainWindow(SettingsStore(settings_path), secret_store=secrets, data_sources=_sources())
    qtbot.addWidget(window)
    tab = _settings_tab(window)

    # Sign-in is by the browser: the tab has no token field and offers a sign-in button. It never shows the stored
    # token, and the tab reports only "set".
    names = {field.objectName() for field in tab.findChildren(QLineEdit)}
    assert {"settings-local-url", "settings-hosted-url"} <= names
    assert "settings-token-input" not in names
    assert tab.findChild(QPushButton, "settings-sign-in") is not None
    assert FAKE_TOKEN not in _all_text(window)
    assert FAKE_TOKEN not in tab.findChild(QLabel, "settings-mode").text()


def test_settings_tab_and_top_bar_show_the_same_saved_mode(qtbot, settings_path, monkeypatch):
    SettingsStore(settings_path).save(Settings(mode="hosted"))
    window = _shown_window(qtbot, settings_path)
    tab = _settings_tab(window)
    mode_label = tab.findChild(QLabel, "settings-mode")
    combo = window.mode_selector
    assert combo.currentData() == "hosted"
    assert mode_label.text() == mode_text("hosted")

    combo.setCurrentIndex(combo.findData("local"))
    assert SettingsStore(settings_path).load().settings.mode == "local"
    assert combo.currentData() == "local"
    assert mode_label.text() == mode_text("local")

    # A failed save reverts the selector, and the tab still shows the mode that is saved.
    def failing_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(settings_store.os, "replace", failing_replace)
    combo.setCurrentIndex(combo.findData("hosted"))
    assert combo.currentData() == "local"
    assert mode_label.text() == mode_text("local")
