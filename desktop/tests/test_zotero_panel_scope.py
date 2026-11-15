"""The Zotero tab's project scope: which collection each project searches, set and cleared by the owner. Offscreen Qt.

The panel gets a fake client and a temporary scope file, so no test reaches Zotero, the bridge or the real settings folder.
"""

import json
import threading

from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton

from meridian_desktop.ui.zotero_panel import (
    COLLECTIONS_FAILED_TEXT,
    SCOPE_CLEARED_TEXT,
    SCOPE_NEED_TEXT,
    SCOPE_LOADING_TEXT,
    SCOPE_NO_LIBRARY_TEXT,
    SCOPE_NO_SOURCE_TEXT,
    SCOPE_SAVED_TEXT,
    SCOPE_SEARCH_IN,
    SCOPE_SEARCH_WHOLE,
    ZoteroPanel,
    results_text,
)
from meridian_desktop import zotero_scope
from meridian_desktop.zotero_client import (
    API_NOT_RUNNING,
    API_ON,
    DISABLED_MESSAGE,
    GROUPS_UNAVAILABLE_NOTE,
    LIBRARY_GROUP,
    LIBRARY_USER,
    NOT_RUNNING_MESSAGE,
    ON_MESSAGE,
    ApiCheck,
    ZoteroCollection,
    ZoteroError,
    ZoteroItem,
    ZoteroLibrary,
    ZoteroStatus,
)

ZOTERO_EXE = r"C:\Program Files\Zotero\zotero.exe"
READY = ZoteroStatus(exe=ZOTERO_EXE, process_running=False, api=ApiCheck(API_ON, ON_MESSAGE))
USER = ZoteroLibrary(LIBRARY_USER, "0", "My Library")
GROUP = ZoteroLibrary(LIBRARY_GROUP, "123", "Lab Group")
PAPERS = ZoteroCollection("AAAABBBB", "Papers", "")
NOTES = ZoteroCollection("CCCCDDDD", "Notes", "")
HIT = ZoteroItem(key="ABCD1234", title="Deep", authors="", year="2015", item_type="book")
PROJECTS = (("p1", "Thesis", ""), ("p2", "Grant", ""))


class ScopeFake:
    """Answers the scope reads from the values it was given, and records each call and its thread."""

    def __init__(self, libraries=(USER, GROUP), groups_note="", collections=None, libraries_error=None,
                 collections_error=None):
        self.libraries = list(libraries)
        self.groups_note = groups_note
        self.collections = {} if collections is None else collections
        self.libraries_error = libraries_error
        self.collections_error = collections_error
        self.calls = []
        self.threads = []
        self._lock = threading.Lock()

    def _record(self, name, arg=None):
        with self._lock:
            self.calls.append((name, arg))
            self.threads.append(threading.current_thread())

    def names(self):
        return [name for name, _arg in self.calls]

    def status(self):
        self._record("status")
        return READY

    def probe_api(self):
        self._record("probe_api")
        return ApiCheck(API_ON, ON_MESSAGE)

    def start_zotero(self, exe):
        self._record("start_zotero", exe)
        return "started"

    def search_items(self, query):
        self._record("search_items", query)
        return [HIT]

    def search_collection_items(self, query, library, key):
        self._record("search_collection_items", (query, library, key))
        return [HIT]

    def list_libraries(self):
        self._record("list_libraries")
        if self.libraries_error is not None:
            raise self.libraries_error
        return list(self.libraries), self.groups_note

    def list_collections(self, library):
        self._record("list_collections", library)
        if self.collections_error is not None:
            raise self.collections_error
        return list(self.collections.get(f"{library.library_type}:{library.library_id}", []))


def _scope_path(tmp_path):
    return tmp_path / "zotero_project_scopes.json"


def _panel(qtbot, fake, tmp_path, projects=lambda: PROJECTS, path=None):
    panel = ZoteroPanel(
        client=fake,
        projects=projects,
        scopes_path=_scope_path(tmp_path) if path is None else path,
    )
    qtbot.addWidget(panel)
    return panel


def _shown(qtbot, fake, tmp_path, **options):
    panel = _panel(qtbot, fake, tmp_path, **options)
    panel.show()
    qtbot.waitExposed(panel)
    refresh = _named(panel, QPushButton, "zotero-refresh")
    qtbot.waitUntil(lambda: refresh.isEnabled())
    return panel


def _named(panel, cls, name):
    widget = panel.findChild(cls, name)
    assert widget is not None, name
    return widget


def _text(panel, name):
    return _named(panel, QLabel, name).text()


def _settled(qtbot, panel, projects=True):
    """Wait until the project list, the libraries and the first collection list are in, not still loading."""
    project = _named(panel, QComboBox, "zotero-scope-project")
    library = _named(panel, QComboBox, "zotero-scope-library")
    collection = _named(panel, QComboBox, "zotero-scope-collection")
    qtbot.waitUntil(
        lambda: (project.count() > 0 or not projects)
        and library.count() > 0
        and collection.count() > 0
        and collection.itemText(0) not in (SCOPE_LOADING_TEXT, SCOPE_NO_LIBRARY_TEXT)
    )


def _pick(qtbot, panel, combo_name, index):
    combo = _named(panel, QComboBox, combo_name)
    combo.setCurrentIndex(index)
    _settled(qtbot, panel)


def _select_project(qtbot, panel, project_id):
    """Choose a project by its id. The list is sorted by name, so the default project is not always the one a test means."""
    combo = _named(panel, QComboBox, "zotero-scope-project")
    qtbot.waitUntil(lambda: combo.findData(project_id) >= 0)  # the list arrives on a worker, so wait for the entry
    combo.setCurrentIndex(combo.findData(project_id))
    _settled(qtbot, panel)


def _pick_collection_by_key(qtbot, panel, key):
    combo = _named(panel, QComboBox, "zotero-scope-collection")
    index = combo.findData(key)
    assert index >= 0, key
    combo.setCurrentIndex(index)


def _search(qtbot, panel, query):
    _named(panel, QLineEdit, "zotero-search-input").setText(query)
    button = _named(panel, QPushButton, "zotero-search-button")
    button.click()
    qtbot.waitUntil(lambda: button.isEnabled())


def _status(panel):
    return _text(panel, "zotero-scope-status")


# ----------------------------------------------------------------- loading


def test_the_scope_block_loads_projects_libraries_and_collections_on_first_show(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path)
    _settled(qtbot, panel)

    projects = _named(panel, QComboBox, "zotero-scope-project")
    libraries = _named(panel, QComboBox, "zotero-scope-library")
    collections = _named(panel, QComboBox, "zotero-scope-collection")
    assert [projects.itemText(i) for i in range(projects.count())] == ["Grant", "Thesis"]
    assert [libraries.itemText(i) for i in range(libraries.count())] == ["My Library", "Lab Group"]
    assert collections.itemText(0) == "Papers"
    assert collections.itemData(0) == "AAAABBBB"
    assert "list_libraries" in fake.names()
    assert ("list_collections", USER) in fake.calls


def test_the_scope_reads_run_off_the_ui_thread(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path)
    _settled(qtbot, panel)
    main = threading.main_thread()
    scope_threads = [thread for (name, _arg), thread in zip(fake.calls, fake.threads)
                     if name in ("list_libraries", "list_collections")]
    assert scope_threads and all(thread is not main for thread in scope_threads)


def test_a_group_note_from_zotero_is_shown_when_its_groups_are_not_listed(qtbot, tmp_path):
    fake = ScopeFake(libraries=(USER,), groups_note=GROUPS_UNAVAILABLE_NOTE)
    panel = _shown(qtbot, fake, tmp_path)
    qtbot.waitUntil(lambda: _named(panel, QComboBox, "zotero-scope-library").count() == 1)
    note = _named(panel, QLabel, "zotero-scope-note")
    assert note.text() == GROUPS_UNAVAILABLE_NOTE
    assert not note.isHidden()


def test_a_library_failure_is_said_in_the_scope_status(qtbot, tmp_path):
    fake = ScopeFake(libraries_error=ZoteroError("not_running", NOT_RUNNING_MESSAGE))
    panel = _shown(qtbot, fake, tmp_path)
    qtbot.waitUntil(lambda: _status(panel) == NOT_RUNNING_MESSAGE)
    assert _named(panel, QComboBox, "zotero-scope-library").count() == 0
    assert not _named(panel, QComboBox, "zotero-scope-library").isEnabled()


def test_a_disabled_local_api_is_said_for_the_libraries_too(qtbot, tmp_path):
    fake = ScopeFake(libraries_error=ZoteroError("api_disabled", DISABLED_MESSAGE))
    panel = _shown(qtbot, fake, tmp_path)
    qtbot.waitUntil(lambda: _status(panel) == DISABLED_MESSAGE)


def test_a_collections_failure_is_said_in_the_collection_list_and_the_status(qtbot, tmp_path):
    fake = ScopeFake(collections_error=ZoteroError("http_500", "Zotero answered with an error."))
    panel = _shown(qtbot, fake, tmp_path)
    _settled_failure = _named(panel, QComboBox, "zotero-scope-collection")
    qtbot.waitUntil(lambda: _settled_failure.itemText(0) == COLLECTIONS_FAILED_TEXT)
    assert _status(panel) == "Zotero answered with an error."


def test_with_no_project_source_the_block_says_so_and_saves_nothing(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path, projects=None)
    _settled(qtbot, panel, projects=False)
    assert _status(panel) == SCOPE_NO_SOURCE_TEXT
    _named(panel, QPushButton, "zotero-scope-save").click()
    assert _status(panel) == SCOPE_NEED_TEXT
    assert not _scope_path(tmp_path).exists()


def test_a_projects_failure_reaches_the_scope_status_as_its_own_text(qtbot, tmp_path):
    def unavailable():
        raise ZoteroError("projects_unavailable", "The projects could not be read.")

    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path, projects=unavailable)
    qtbot.waitUntil(lambda: _status(panel) == "The projects could not be read.")


def test_showing_the_block_writes_nothing(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path)
    _settled(qtbot, panel)
    assert not _scope_path(tmp_path).exists()


# ----------------------------------------------------------------- saving and clearing


def test_saving_a_collection_writes_the_binding_and_says_so(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path)
    _select_project(qtbot, panel, "p1")
    _named(panel, QPushButton, "zotero-scope-save").click()

    saved = json.loads(_scope_path(tmp_path).read_text(encoding="utf-8"))["scopes"]["p1"]
    assert saved == {
        "library_type": "user",
        "library_id": "0",
        "library_name": "My Library",
        "collection_key": "AAAABBBB",
        "collection_name": "Papers",
    }
    assert _status(panel) == SCOPE_SAVED_TEXT.format("Thesis", "Papers", "My Library")
    assert _text(panel, "zotero-scope-current") == "Thesis searches the collection 'Papers' in My Library."


def test_a_search_for_a_project_with_a_collection_reads_only_that_collection(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS, NOTES]})
    panel = _shown(qtbot, fake, tmp_path)
    _select_project(qtbot, panel, "p1")
    _pick_collection_by_key(qtbot, panel, "CCCCDDDD")
    _named(panel, QPushButton, "zotero-scope-save").click()

    _search(qtbot, panel, "deep")
    assert ("search_collection_items", ("deep", USER, "CCCCDDDD")) in fake.calls
    assert "search_items" not in fake.names()
    status = _named(panel, QLabel, "zotero-search-status").text()
    assert status == results_text("deep", 1) + SCOPE_SEARCH_IN.format("Notes", "My Library")


def test_a_project_with_no_collection_searches_the_whole_library(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path)
    _settled(qtbot, panel)

    _search(qtbot, panel, "deep")
    assert "search_items" in fake.names()
    assert "search_collection_items" not in fake.names()
    status = _named(panel, QLabel, "zotero-search-status").text()
    assert status == results_text("deep", 1) + SCOPE_SEARCH_WHOLE


def test_with_no_project_list_the_search_is_the_plain_library_search_with_no_note(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path, projects=None)
    _search(qtbot, panel, "deep")
    assert fake.names().count("search_items") == 1
    assert _named(panel, QLabel, "zotero-search-status").text() == results_text("deep", 1)


def test_clearing_the_collection_puts_the_search_back_on_the_whole_library(qtbot, tmp_path):
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path)
    _select_project(qtbot, panel, "p1")
    _named(panel, QPushButton, "zotero-scope-save").click()
    _named(panel, QPushButton, "zotero-scope-clear").click()

    assert json.loads(_scope_path(tmp_path).read_text(encoding="utf-8"))["scopes"] == {}
    assert _status(panel) == SCOPE_CLEARED_TEXT.format("Thesis")
    assert _text(panel, "zotero-scope-current") == "Thesis searches your whole library. No collection is set."


def test_picking_a_project_shows_and_selects_its_saved_collection(qtbot, tmp_path):
    path = _scope_path(tmp_path)
    zotero_scope.save_scope(
        zotero_scope.ProjectScope(
            project_id="p2",
            library_type="group",
            library_id="123",
            library_name="Lab Group",
            collection_key="CCCCDDDD",
            collection_name="Notes",
        ),
        path,
    )
    fake = ScopeFake(collections={"user:0": [PAPERS], "group:123": [NOTES]})
    panel = _shown(qtbot, fake, tmp_path)
    _settled(qtbot, panel)
    _pick(qtbot, panel, "zotero-scope-project", 1)  # the list is sorted, so index 1 is "Thesis"
    _pick(qtbot, panel, "zotero-scope-project", 0)  # index 0 is "Grant", which has the saved group collection

    assert _named(panel, QComboBox, "zotero-scope-library").currentData() == "group:123"
    assert _named(panel, QComboBox, "zotero-scope-collection").currentData() == "CCCCDDDD"
    assert _text(panel, "zotero-scope-current") == "Grant searches the collection 'Notes' in Lab Group."


def test_a_save_is_refused_until_a_collection_is_picked(qtbot, tmp_path):
    fake = ScopeFake(collections={})
    panel = _shown(qtbot, fake, tmp_path)
    _settled(qtbot, panel)
    _named(panel, QPushButton, "zotero-scope-save").click()
    assert _status(panel) == SCOPE_NEED_TEXT
    assert not _scope_path(tmp_path).exists()


def test_an_unreadable_scope_file_blocks_the_save_and_is_left_as_it_was(qtbot, tmp_path):
    path = _scope_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    fake = ScopeFake(collections={"user:0": [PAPERS]})
    panel = _shown(qtbot, fake, tmp_path)
    _settled(qtbot, panel)
    _named(panel, QPushButton, "zotero-scope-save").click()
    assert _status(panel) == zotero_scope.UNREADABLE_TEXT
    assert path.read_text(encoding="utf-8") == "{not json"
