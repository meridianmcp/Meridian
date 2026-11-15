"""Zotero tab: status view, the Start Zotero confirmation, library search and copy. Offscreen Qt, no network.

The panel gets a fake client, so no test reaches Zotero, the process list or the real network.
The fake records each call and the thread it ran on, so a test can prove that checks, searches
and the start action run off the UI thread.
"""

import gc
import threading
import weakref
from pathlib import Path

import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetSelectionRange,
    QWidget,
)

from meridian_desktop.ui.text import SELECTABLE_TEXT

from _shared_delivery import emit, recording_deliveries, visible_state
from meridian_desktop.ui.zotero_panel import (
    API_NOT_READY_TEXT,
    API_READY_TEXT,
    API_TEXT,
    BLANK_TEXT,
    CHECK_FAILED_TEXT,
    INSTALLED_TEXT,
    NO_RESULTS_TEXT,
    NOT_CHECKED_TEXT,
    NOT_INSTALLED_TEXT,
    PROCESS_RUNNING_TEXT,
    PROCESS_STOPPED_TEXT,
    START_CANCELLED_TEXT,
    START_HINT_NOT_INSTALLED,
    START_HINT_RUNNING,
    BRIDGE_CHECKING_TEXT,
    STATUS,
    ZoteroPanel,
    ask_start,
    results_text,
    start_dialog,
)
from meridian_desktop.zotero_client import (
    API_DISABLED,
    API_NOT_RUNNING,
    API_ON,
    DISABLED_MESSAGE,
    NOT_RUNNING_MESSAGE,
    ON_MESSAGE,
    ApiCheck,
    ZoteroError,
    ZoteroItem,
    ZoteroStatus,
    command_line,
)

ZOTERO_EXE = r"C:\Program Files\Zotero\zotero.exe"
READY = ZoteroStatus(exe=ZOTERO_EXE, process_running=False, api=ApiCheck(API_ON, ON_MESSAGE))
NOT_READY = ZoteroStatus(exe=ZOTERO_EXE, process_running=False, api=ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE))
ITEMS = [
    ZoteroItem(
        key="ABCD1234",
        title="Deep Learning for maps",
        authors="LeCun, Yann; Bengio, Yoshua",
        year="2015",
        item_type="journalArticle",
    ),
    ZoteroItem(key="WXYZ9876", title="Corporate report", authors="", year="", item_type="book"),
]


class FakeZotero:
    """Records each call and the thread it ran on. Answers from the values it was given."""

    def __init__(self, status=READY, items=None, search_error=None, start_error=None, ready=True):
        self.status_value = status
        self.items = list(ITEMS if items is None else items)
        self.search_error = search_error
        self.start_error = start_error
        self.ready = ready
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
        return self.status_value

    def probe_api(self):
        self._record("probe_api")
        if self.ready:
            return ApiCheck(API_ON, ON_MESSAGE)
        return ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE)

    def search_items(self, query):
        self._record("search_items", query)
        if self.search_error is not None:
            raise self.search_error
        return list(self.items)

    def start_zotero(self, exe):
        self._record("start_zotero", exe)
        if self.start_error is not None:
            raise self.start_error
        return f"Zotero was started with: {command_line(exe)}"


def _panel(qtbot, fake, confirm=None):
    panel = ZoteroPanel(client=fake, confirm=confirm)
    qtbot.addWidget(panel)
    return panel


def _shown_panel(qtbot, fake, confirm=None):
    panel = _panel(qtbot, fake, confirm)
    panel.show()
    qtbot.waitExposed(panel)
    _wait_idle_refresh(qtbot, panel)
    return panel


def _named(panel, cls, name):
    widget = panel.findChild(cls, name)
    assert widget is not None, name
    return widget


def _text(panel, name):
    return _named(panel, QLabel, name).text()


def _wait_idle_refresh(qtbot, panel):
    refresh = _named(panel, QPushButton, "zotero-refresh")
    qtbot.waitUntil(lambda: refresh.isEnabled())


def _all_label_texts(panel):
    return [label.text() for label in panel.findChildren(QLabel)]


def _search(qtbot, panel, query):
    panel_input = _named(panel, QLineEdit, "zotero-search-input")
    panel_input.setText(query)
    button = _named(panel, QPushButton, "zotero-search-button")
    button.click()
    qtbot.waitUntil(lambda: button.isEnabled())


# ----------------------------------------------------------------- build and show


def test_panel_makes_no_request_when_it_is_built(qtbot):
    fake = FakeZotero()
    _panel(qtbot, fake)
    assert fake.calls == []


def test_first_show_checks_zotero_once_off_the_ui_thread(qtbot):
    fake = FakeZotero()
    panel = _shown_panel(qtbot, fake)
    assert fake.names() == ["status"]
    assert fake.threads[0] is not threading.main_thread()

    panel.hide()
    panel.show()
    qtbot.wait(20)
    assert fake.names() == ["status"]


def test_a_status_check_still_running_when_its_panel_is_deleted_does_not_keep_the_panel(qtbot, monkeypatch):
    """A panel deleted while its status check still runs is freed on the GUI thread, and no widget of it is touched.

    The status job holds the client's bound method, the owner token and the shared delivery object, never the panel. The
    collector is off, so only reference counting can free the panel here.
    """
    entered = threading.Event()
    gate = threading.Event()
    touched = []
    # The status handler is the update step. The test only records whether it ran, and never reads the panel.
    monkeypatch.setattr(ZoteroPanel, "_on_status_ready", lambda self, payload: touched.append(payload))

    class HeldAtTheGate(FakeZotero):
        def status(self):
            entered.set()
            gate.wait(5)
            return super().status()

    gc.disable()
    try:
        container = QWidget()
        panel = ZoteroPanel(client=HeldAtTheGate(), parent=container)
        _named(panel, QPushButton, "zotero-refresh").click()
        assert entered.wait(5)  # the status check has started and is held at the gate
        ref = weakref.ref(panel)
        shiboken6.delete(container)  # the panel's widgets are deleted on the GUI thread while the check still runs
        del panel
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
        assert touched == []  # the deleted panel's status handler never runs, so its widgets are never touched
    finally:
        gate.set()
        gc.enable()


def test_refresh_button_checks_again(qtbot):
    fake = FakeZotero()
    panel = _shown_panel(qtbot, fake)
    _named(panel, QPushButton, "zotero-refresh").click()
    qtbot.waitUntil(lambda: fake.names().count("status") == 2)
    _wait_idle_refresh(qtbot, panel)


def test_installed_running_and_api_on_are_shown_in_plain_words(qtbot):
    panel = _shown_panel(qtbot, FakeZotero(status=ZoteroStatus(ZOTERO_EXE, True, ApiCheck(API_ON, ON_MESSAGE))))
    assert _text(panel, "zotero-installed") == INSTALLED_TEXT.format(ZOTERO_EXE)
    assert _text(panel, "zotero-process") == PROCESS_RUNNING_TEXT
    assert _text(panel, "zotero-api") == API_TEXT[API_ON]
    assert _text(panel, "zotero-api-detail") == ON_MESSAGE


def test_stopped_process_is_shown_and_start_is_offered(qtbot):
    panel = _shown_panel(qtbot, FakeZotero(status=READY))
    assert _text(panel, "zotero-process") == PROCESS_STOPPED_TEXT
    assert _named(panel, QPushButton, "zotero-start").isEnabled()


def test_api_disabled_says_where_to_turn_it_on(qtbot):
    status = ZoteroStatus(ZOTERO_EXE, True, ApiCheck(API_DISABLED, DISABLED_MESSAGE))
    panel = _shown_panel(qtbot, FakeZotero(status=status))
    assert _text(panel, "zotero-api") == API_TEXT[API_DISABLED]
    detail = _text(panel, "zotero-api-detail")
    assert "Allow other applications on this computer to communicate with Zotero" in detail
    assert "Advanced" in detail


def test_running_zotero_disables_start_and_says_why(qtbot):
    status = ZoteroStatus(ZOTERO_EXE, True, ApiCheck(API_ON, ON_MESSAGE))
    panel = _shown_panel(qtbot, FakeZotero(status=status))
    assert not _named(panel, QPushButton, "zotero-start").isEnabled()
    assert _text(panel, "zotero-start-hint") == START_HINT_RUNNING


def test_missing_install_disables_start_and_says_to_install(qtbot):
    status = ZoteroStatus(None, None, ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE))
    panel = _shown_panel(qtbot, FakeZotero(status=status))
    assert _text(panel, "zotero-installed") == NOT_INSTALLED_TEXT
    assert _text(panel, "zotero-start-hint") == START_HINT_NOT_INSTALLED
    assert not _named(panel, QPushButton, "zotero-start").isEnabled()


def test_unexpected_status_failure_shows_the_class_name_only(qtbot):
    class Broken(FakeZotero):
        def status(self):
            raise RuntimeError("secret detail inside the failure")

    panel = _shown_panel(qtbot, Broken())
    assert "secret detail" not in _text(panel, "zotero-api-detail")
    assert CHECK_FAILED_TEXT.format("RuntimeError") in _text(panel, "zotero-api-detail")
    assert not _named(panel, QPushButton, "zotero-start").isEnabled()


# ----------------------------------------------------------------- start (confirmation)


def test_start_not_called_when_confirmation_declined(qtbot):
    fake = FakeZotero()
    asked = []

    def decline(parent, command, folder):
        asked.append((command, folder))
        return False

    panel = _shown_panel(qtbot, fake, confirm=decline)
    _named(panel, QPushButton, "zotero-start").click()

    assert asked == [(command_line(ZOTERO_EXE), str(Path(ZOTERO_EXE).parent))]
    assert "start_zotero" not in fake.names()
    assert "probe_api" not in fake.names()
    assert _text(panel, "zotero-start-result") == START_CANCELLED_TEXT


def test_start_runs_after_yes_off_the_ui_thread_and_shows_api_ready(qtbot):
    fake = FakeZotero()
    panel = _shown_panel(qtbot, fake, confirm=lambda parent, command, folder: True)
    _named(panel, QPushButton, "zotero-start").click()

    qtbot.waitUntil(lambda: API_READY_TEXT in _text(panel, "zotero-start-result"))
    assert ("start_zotero", ZOTERO_EXE) in fake.calls
    start_thread = fake.threads[fake.names().index("start_zotero")]
    assert start_thread is not threading.main_thread()
    assert "Zotero was started with" in _text(panel, "zotero-start-result")


def test_start_reports_api_not_ready_yet_when_zotero_is_still_opening(qtbot):
    fake = FakeZotero(ready=False)
    panel = _shown_panel(qtbot, fake, confirm=lambda parent, command, folder: True)
    _named(panel, QPushButton, "zotero-start").click()
    qtbot.waitUntil(lambda: API_NOT_READY_TEXT in _text(panel, "zotero-start-result"))


def test_start_failure_shows_the_fixed_message_only(qtbot):
    fake = FakeZotero(start_error=ZoteroError("start_failed", "Zotero could not be started (OSError)."))
    panel = _shown_panel(qtbot, fake, confirm=lambda parent, command, folder: True)
    _named(panel, QPushButton, "zotero-start").click()
    qtbot.waitUntil(lambda: "Zotero could not be started (OSError)." in _text(panel, "zotero-start-result"))
    assert API_READY_TEXT not in _text(panel, "zotero-start-result")


def test_default_dialog_has_cancel_as_its_default_and_escape_button(qtbot):
    box, start, cancel = start_dialog(None, command_line(ZOTERO_EXE), str(Path(ZOTERO_EXE).parent))
    qtbot.addWidget(box)
    assert box.defaultButton() is cancel
    assert box.escapeButton() is cancel
    assert box.clickedButton() is None
    assert start is not cancel


def test_dialog_names_the_exact_command_and_is_plain_text_and_selectable(qtbot):
    box, _start, _cancel = start_dialog(None, command_line(ZOTERO_EXE), str(Path(ZOTERO_EXE).parent))
    qtbot.addWidget(box)
    assert command_line(ZOTERO_EXE) in box.informativeText()
    assert str(Path(ZOTERO_EXE).parent) in box.informativeText()
    for label in box.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert (label.textInteractionFlags() & SELECTABLE_TEXT) == SELECTABLE_TEXT


def test_closing_the_dialog_without_a_choice_is_a_no(qtbot, monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    assert ask_start(None, command_line(ZOTERO_EXE), str(Path(ZOTERO_EXE).parent)) is False


# ----------------------------------------------------------------- search


def test_blank_search_sends_nothing_and_says_to_type_a_term(qtbot):
    fake = FakeZotero()
    panel = _shown_panel(qtbot, fake)
    _named(panel, QLineEdit, "zotero-search-input").setText("   ")
    _named(panel, QPushButton, "zotero-search-button").click()
    assert "search_items" not in fake.names()
    assert _text(panel, "zotero-search-status") == BLANK_TEXT


def test_enter_in_the_search_box_searches_off_the_ui_thread(qtbot):
    fake = FakeZotero()
    panel = _shown_panel(qtbot, fake)
    box = _named(panel, QLineEdit, "zotero-search-input")
    box.setText("deep learning")
    qtbot.keyClick(box, Qt.Key.Key_Return)

    qtbot.waitUntil(lambda: _named(panel, QTableWidget, "zotero-results").rowCount() == 2)
    assert ("search_items", "deep learning") in fake.calls
    assert fake.threads[fake.names().index("search_items")] is not threading.main_thread()


def test_results_show_title_authors_year_and_key_in_a_table(qtbot):
    panel = _shown_panel(qtbot, FakeZotero())
    _search(qtbot, panel, "deep learning")
    table = _named(panel, QTableWidget, "zotero-results")
    assert table.rowCount() == 2
    first = [table.item(0, column).text() for column in range(4)]
    assert first == ["Deep Learning for maps", "LeCun, Yann; Bengio, Yoshua", "2015", "ABCD1234"]
    assert table.item(1, 2).text() == ""
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert not (table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable)
    assert _text(panel, "zotero-search-status") == results_text("deep learning", 2)


def test_results_are_copyable_as_tab_separated_text(qtbot):
    panel = _shown_panel(qtbot, FakeZotero())
    _search(qtbot, panel, "deep learning")
    table = _named(panel, QTableWidget, "zotero-results")
    table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 0, 3), True)
    copy = next(action for action in table.actions() if action.text() == "Copy")
    QGuiApplication.clipboard().setText("before")
    copy.trigger()
    assert QGuiApplication.clipboard().text() == "Deep Learning for maps\tLeCun, Yann; Bengio, Yoshua\t2015\tABCD1234"


def test_no_results_names_the_query(qtbot):
    panel = _shown_panel(qtbot, FakeZotero(items=[]))
    _search(qtbot, panel, "nothing")
    assert _text(panel, "zotero-search-status") == NO_RESULTS_TEXT.format("nothing")
    assert _named(panel, QTableWidget, "zotero-results").rowCount() == 0


def test_search_error_shows_the_returned_message_with_no_traceback(qtbot):
    fake = FakeZotero(search_error=ZoteroError("api_disabled", DISABLED_MESSAGE))
    panel = _shown_panel(qtbot, fake)
    _search(qtbot, panel, "x")
    status = _text(panel, "zotero-search-status")
    assert status == DISABLED_MESSAGE
    assert "Traceback" not in status
    assert _named(panel, QTableWidget, "zotero-results").rowCount() == 0


def test_unexpected_search_error_shows_the_class_name_not_the_detail(qtbot):
    fake = FakeZotero(search_error=RuntimeError("private detail"))
    panel = _shown_panel(qtbot, fake)
    _search(qtbot, panel, "x")
    status = _text(panel, "zotero-search-status")
    assert status == CHECK_FAILED_TEXT.format("RuntimeError")
    assert "private detail" not in status


def test_results_text_says_when_the_list_is_cut_at_the_limit():
    assert results_text("q", 3) == '3 result(s) for "q".'
    assert "Showing the first 25" in results_text("q", 25)
    assert results_text("q", 0) == NO_RESULTS_TEXT.format("q")


# ----------------------------------------------------------------- text rules


def test_every_label_is_selectable_and_plain_text(qtbot):
    panel = _shown_panel(qtbot, FakeZotero())
    _search(qtbot, panel, "deep learning")
    labels = panel.findChildren(QLabel)
    assert labels
    for label in labels:
        assert (label.textInteractionFlags() & SELECTABLE_TEXT) == SELECTABLE_TEXT, label.objectName()
        assert label.textFormat() == Qt.TextFormat.PlainText, label.objectName()


def test_no_label_says_not_built_yet(qtbot):
    panel = _shown_panel(qtbot, FakeZotero())
    for text in _all_label_texts(panel):
        assert "Not built yet" not in text


def test_text_before_any_check_says_not_checked(qtbot):
    panel = _panel(qtbot, FakeZotero())
    assert _text(panel, "zotero-installed") == NOT_CHECKED_TEXT


def test_a_search_still_running_when_its_panel_is_deleted_does_not_keep_the_panel(qtbot, monkeypatch):
    """A panel deleted while its search still runs is freed on the GUI thread, and no widget of it is touched.

    The search job holds the client's bound method, the owner token and the shared delivery object, never the panel. The
    update step is looked up with raising=False, so the test does not depend on the panel having it. The collector is
    off, so only reference counting can free the panel here.
    """
    entered = threading.Event()
    gate = threading.Event()
    touched = []
    monkeypatch.setattr(ZoteroPanel, "_on_search_ready", lambda self, payload: touched.append(payload), raising=False)

    class HeldAtTheGate(FakeZotero):
        def search_items(self, query):
            entered.set()
            gate.wait(5)
            return super().search_items(query)

    gc.disable()
    try:
        container = QWidget()
        panel = ZoteroPanel(client=HeldAtTheGate(), parent=container)
        _named(panel, QLineEdit, "zotero-search-input").setText("deep")
        _named(panel, QPushButton, "zotero-search-button").click()
        assert entered.wait(5)  # the search has started and is held at the gate
        ref = weakref.ref(panel)
        shiboken6.delete(container)  # the panel's widgets are deleted on the GUI thread while the search still runs
        del panel
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
        assert touched == []  # the deleted panel's search handler never runs, so its widgets are never touched
    finally:
        gate.set()
        gc.enable()


# ----------------------------------------------------------- a result for another owner, or a superseded status check


def _status_deliveries(emitted):
    """The status checks' deliveries only. Each refresh also runs a bridge check, which this test does not follow."""
    return [item for item in emitted if item.value.kind == STATUS]


def _wait_bridge_result(qtbot, panel):
    """The bridge check runs after each refresh. Its result can land after the refresh button is enabled, so wait for it."""
    state = _named(panel, QLabel, "zotero-bridge-state")
    qtbot.waitUntil(lambda: state.text() != BRIDGE_CHECKING_TEXT, timeout=5000)


def test_a_result_from_another_owner_or_from_a_superseded_status_check_is_dropped(qtbot):
    """A sibling panel's answer, and an answer from a status check that this panel's newer check superseded, show nothing.

    Two panels each read their own status. The panel's first check emits its Delivered. The sibling's first check then
    emits one at the same generation, and it reaches the panel too, so the panel drops it by owner alone. The panel checks
    again, and its first Delivered is replayed: it carries the panel's own owner with a superseded generation of the same
    kind, so it is dropped by generation alone. The panel's second result is what the user sees throughout, and it is the
    control that the panel's own current Delivered is accepted.
    """
    fake = FakeZotero(status=READY)
    panel = _panel(qtbot, fake)
    sibling = _panel(qtbot, FakeZotero(status=NOT_READY))
    with recording_deliveries() as emitted:
        _named(panel, QPushButton, "zotero-refresh").click()
        _wait_idle_refresh(qtbot, panel)
        _wait_bridge_result(qtbot, panel)
        assert len(_status_deliveries(emitted)) == 1
        own_first = _status_deliveries(emitted)[0]
        shown_first = visible_state(panel)
        _named(sibling, QPushButton, "zotero-refresh").click()
        _wait_idle_refresh(qtbot, sibling)
        _wait_bridge_result(qtbot, sibling)
        assert len(_status_deliveries(emitted)) == 2
        assert visible_state(panel) == shown_first
        fake.status_value = NOT_READY
        _named(panel, QPushButton, "zotero-refresh").click()
        _wait_idle_refresh(qtbot, panel)
        _wait_bridge_result(qtbot, panel)
        shown_second = visible_state(panel)
        assert shown_second != shown_first
        emit(own_first)  # this panel's answer from the first check, which the second check superseded
        qtbot.wait(20)
    assert visible_state(panel) == shown_second
