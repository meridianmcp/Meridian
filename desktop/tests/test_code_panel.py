"""Code tab: search, each state's text, results, copy, and the off-thread search. Offscreen Qt, no network.

The panel gets a fake transport, or the default transport aimed at a loopback port that nothing listens
on. No test contacts another host or reads a real setting. A fake records each call and the thread it ran on,
so a test can show that the Local URL is read and the search runs off the UI thread.
"""

import gc
import json
import socket
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
    QPushButton,
    QTableWidget,
    QTableWidgetSelectionRange,
    QWidget,
)

from meridian_desktop.code_client import (
    BLANK_QUERY_MESSAGE,
    STATE_BLANK_QUERY,
    STATE_ERROR,
    STATE_NOT_INDEXED,
    STATE_NOT_RUNNING,
    STATE_OK,
    STATE_ROUTE_MISSING,
    UNCHECKED_MESSAGE,
    CodeHit,
    Reply,
    SearchResult,
)
from meridian_desktop.local_client import NOT_RUNNING_MESSAGE, LocalClientError
from meridian_desktop.ui import code_panel
from meridian_desktop.ui.code_panel import (
    BLANK_TEXT,
    ERROR_TEXT,
    NOT_INDEXED_TEXT,
    NOT_RUNNING_TEXT,
    NO_MATCHES_TEXT,
    ROUTE_MISSING_TEXT,
    SEARCH_FAILED_TEXT,
    SEARCH_HINT,
    CodePanel,
    location_text,
    results_text,
    score_text,
    snippet_text,
    status_text,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT

from _shared_delivery import emit, recording_deliveries, shows, visible_state

BASE = "http://127.0.0.1:7878"
SNIPPET_ONE = "def search(base_url, query):"
SNIPPET_TWO = "import pytest\n\nfrom _fakes import QuietServer"
HITS_PAYLOAD = {
    "results": [
        {"path": "meridian_desktop/code_client.py", "line": 12, "score": 0.9123, "snippet": SNIPPET_ONE},
        {"path": "tests/test_code_client.py", "line": 3, "score": 0.5, "snippet": SNIPPET_TWO},
    ]
}
ROW_ONE = ("meridian_desktop/code_client.py:12", "0.912", SNIPPET_ONE)
ROW_TWO = ("tests/test_code_client.py:3", "0.500", "import pytest from _fakes import QuietServer")


def _json(payload) -> Reply:
    return Reply(200, json.dumps(payload).encode("utf-8"))


class FakeTransport:
    """Records each call and the thread it ran on. Answers with ``reply``, or raises ``error``.

    With ``gate`` set, the call waits until the test sets the gate, so a test can act while a search runs.
    """

    def __init__(self, reply=None, error=None, gate=None):
        self.reply = reply if reply is not None else _json(HITS_PAYLOAD)
        self.error = error
        self.gate = gate
        self.calls = []
        self.threads = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        self.threads.append(threading.current_thread())
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.reply


class Provider:
    """The Local URL provider. It records each call and the thread it ran on."""

    def __init__(self, url=BASE, error=None):
        self.url = url
        self.error = error
        self.threads = []

    def __call__(self):
        self.threads.append(threading.current_thread())
        if self.error is not None:
            raise self.error
        return self.url


def _closed_port_url() -> str:
    """A loopback URL on a port that nothing listens on right now."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


def _panel(qtbot, provider=None, transport=None):
    """A panel with a fake transport unless a test passes one. No test reaches the default transport by accident."""
    panel = CodePanel(Provider() if provider is None else provider, transport=FakeTransport() if transport is None else transport)
    qtbot.addWidget(panel)
    return panel


def _shown_panel(qtbot, provider=None, transport=None):
    panel = _panel(qtbot, provider, transport)
    panel.show()
    qtbot.waitExposed(panel)
    return panel


def _named(panel, cls, name):
    widget = panel.findChild(cls, name)
    assert widget is not None, name
    return widget


def _status(panel):
    return _named(panel, QLabel, "code-status").text()


def _search(qtbot, panel, query):
    box = _named(panel, QLineEdit, "code-search-input")
    box.setText(query)
    button = _named(panel, QPushButton, "code-search-button")
    button.click()
    qtbot.waitUntil(lambda: button.isEnabled())


def _table(panel):
    return _named(panel, QTableWidget, "code-results")


def _row(table, row):
    return tuple(table.item(row, column).text() for column in range(3))


# ----------------------------------------------------------------- build and not-connected


def test_building_and_showing_the_panel_makes_no_request(qtbot):
    provider = Provider()
    transport = FakeTransport()
    panel = _shown_panel(qtbot, provider, transport)
    qtbot.wait(20)
    assert provider.threads == []
    assert transport.calls == []
    assert _status(panel) == SEARCH_HINT


def test_code_panel_shows_not_connected_without_engine(qtbot):
    # No transport is passed, so the real default transport runs against a loopback port nothing listens on.
    url = _closed_port_url()
    panel = CodePanel(Provider(url=url))
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    _search(qtbot, panel, "search")

    assert _status(panel) == NOT_RUNNING_TEXT.format(url)
    assert f"not running at {url}" in _status(panel)
    assert "python -m meridian.local_runner start" in _status(panel)
    assert _table(panel).rowCount() == 0


def test_not_connected_text_names_the_address_and_says_how_to_start(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport(error=LocalClientError("not_running", NOT_RUNNING_MESSAGE)))
    _search(qtbot, panel, "search")
    text = _status(panel)
    assert BASE in text
    assert "Start Meridian on this computer" in text
    assert "Then press Search again." in text


# ----------------------------------------------------------------- results


def test_results_show_path_line_score_and_snippet(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport())
    _search(qtbot, panel, "search")
    table = _table(panel)
    assert table.rowCount() == 2
    assert _row(table, 0) == ROW_ONE
    assert _row(table, 1) == ROW_TWO
    assert _status(panel) == results_text("search", 2)


def test_each_result_location_is_path_colon_line(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport())
    _search(qtbot, panel, "search")
    assert _table(panel).item(0, 0).text() == "meridian_desktop/code_client.py:12"


def test_results_are_plain_text_so_code_is_never_read_as_html(qtbot):
    payload = {"results": [{"path": "x.html", "line": 1, "score": 1, "snippet": "<b>bold</b> <script>x</script>"}]}
    panel = _shown_panel(qtbot, transport=FakeTransport(reply=_json(payload)))
    _search(qtbot, panel, "bold")
    assert _table(panel).item(0, 2).text() == "<b>bold</b> <script>x</script>"
    for label in panel.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_a_search_that_returns_nothing_says_so_and_leaves_the_table_empty(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport(reply=_json({"results": []})))
    _search(qtbot, panel, "zzz")
    assert _status(panel) == NO_MATCHES_TEXT.format("zzz")
    assert _table(panel).rowCount() == 0


def test_a_second_search_clears_the_first_results(qtbot):
    transport = FakeTransport()
    panel = _shown_panel(qtbot, transport=transport)
    _search(qtbot, panel, "search")
    assert _table(panel).rowCount() == 2
    transport.reply = _json({"results": []})
    _search(qtbot, panel, "nothing")
    assert _table(panel).rowCount() == 0
    assert _status(panel) == NO_MATCHES_TEXT.format("nothing")


# ----------------------------------------------------------------- the other states


def test_route_missing_says_the_server_is_too_old_and_how_to_update(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport(reply=Reply(404, b'{"detail": "Not Found"}')))
    _search(qtbot, panel, "search")
    assert _status(panel) == ROUTE_MISSING_TEXT.format(BASE)
    assert "HTTP 404" in _status(panel)
    assert "Update Meridian" in _status(panel)


def test_not_indexed_says_how_to_build_the_index(qtbot):
    body = b'{"error": {"code": "not_indexed", "message": "no index"}}'
    panel = _shown_panel(qtbot, transport=FakeTransport(reply=Reply(409, body)))
    _search(qtbot, panel, "search")
    assert _status(panel) == NOT_INDEXED_TEXT
    assert "meridian-codeindex" in _status(panel)
    assert "--db-path" in _status(panel)
    assert _table(panel).rowCount() == 0


def test_an_unexpected_server_error_shows_the_status_and_no_body(qtbot):
    body = b'{"error": {"code": "boom"}, "secret": "leak-me"}'
    panel = _shown_panel(qtbot, transport=FakeTransport(reply=Reply(500, body)))
    _search(qtbot, panel, "search")
    assert "HTTP 500" in _status(panel)
    assert "leak-me" not in _status(panel)


def test_a_non_loopback_local_url_is_refused_and_nothing_is_sent(qtbot):
    transport = FakeTransport()
    panel = _shown_panel(qtbot, Provider(url="http://example.com:7878"), transport)
    _search(qtbot, panel, "search")
    assert "must be 127.0.0.1, localhost or ::1" in _status(panel)
    assert transport.calls == []


def test_an_unexpected_transport_failure_shows_fixed_text_only(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport(error=RuntimeError("secret detail inside the failure")))
    _search(qtbot, panel, "search")
    assert "secret" not in _status(panel)
    assert ERROR_TEXT.format(UNCHECKED_MESSAGE) == _status(panel)


def test_a_failing_local_url_provider_shows_the_class_name_only(qtbot):
    panel = _shown_panel(qtbot, Provider(error=RuntimeError("secret provider detail")), FakeTransport())
    _search(qtbot, panel, "search")
    assert _status(panel) == SEARCH_FAILED_TEXT.format("RuntimeError")
    assert "secret" not in _status(panel)


# ----------------------------------------------------------------- blank, enter and the thread


def test_a_blank_search_sends_nothing_and_says_to_type_a_term(qtbot):
    transport = FakeTransport()
    provider = Provider()
    panel = _shown_panel(qtbot, provider, transport)
    _search_no_wait(panel, "   ")
    assert _status(panel) == BLANK_TEXT == BLANK_QUERY_MESSAGE
    assert transport.calls == []
    assert provider.threads == []


def _search_no_wait(panel, query):
    _named(panel, QLineEdit, "code-search-input").setText(query)
    _named(panel, QPushButton, "code-search-button").click()


def test_enter_in_the_search_box_searches_off_the_ui_thread(qtbot):
    transport = FakeTransport()
    provider = Provider()
    panel = _shown_panel(qtbot, provider, transport)
    box = _named(panel, QLineEdit, "code-search-input")
    box.setText("search")
    qtbot.keyClick(box, Qt.Key.Key_Return)

    qtbot.waitUntil(lambda: _table(panel).rowCount() == 2)
    assert len(transport.calls) == 1
    assert transport.threads[0] is not threading.main_thread()
    assert provider.threads[0] is not threading.main_thread()


def test_the_local_url_is_read_once_per_search(qtbot):
    provider = Provider()
    panel = _shown_panel(qtbot, provider, FakeTransport())
    _search(qtbot, panel, "search")
    assert len(provider.threads) == 1


def test_a_search_in_flight_blocks_a_second_one_and_disables_the_button(qtbot):
    gate = threading.Event()
    transport = FakeTransport(gate=gate)
    panel = _shown_panel(qtbot, transport=transport)
    button = _named(panel, QPushButton, "code-search-button")
    box = _named(panel, QLineEdit, "code-search-input")
    box.setText("search")
    button.click()
    assert not button.isEnabled()
    assert _status(panel) == code_panel.SEARCHING_TEXT

    qtbot.keyClick(box, Qt.Key.Key_Return)
    gate.set()
    qtbot.waitUntil(lambda: button.isEnabled())
    assert len(transport.calls) == 1
    assert _table(panel).rowCount() == 2


# ----------------------------------------------------------------- copy and read-only


def test_results_can_be_copied_as_tab_separated_text(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport())
    _search(qtbot, panel, "search")
    table = _table(panel)
    table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 2), True)
    copy = next(action for action in table.actions() if action.text() == "Copy")
    QGuiApplication.clipboard().setText("before")
    copy.trigger()
    expected = "\n".join(["\t".join(ROW_ONE), "\t".join(ROW_TWO)])
    assert QGuiApplication.clipboard().text() == expected


def test_results_are_read_only(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport())
    _search(qtbot, panel, "search")
    table = _table(panel)
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert not (table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable)


def test_every_label_is_plain_text_and_selectable(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport())
    _search(qtbot, panel, "search")
    labels = [label for label in panel.findChildren(QLabel) if label.text()]
    assert len(labels) >= 2  # the intro and the status line
    for label in labels:
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert (label.textInteractionFlags() & SELECTABLE_TEXT) == SELECTABLE_TEXT


def test_the_search_input_and_button_have_their_names(qtbot):
    panel = _shown_panel(qtbot, transport=FakeTransport())
    assert _named(panel, QLineEdit, "code-search-input").placeholderText()
    assert _named(panel, QPushButton, "code-search-button").text() == "Search"


# ----------------------------------------------------------------- pure text helpers


OUTCOMES = [
    SearchResult(STATE_OK, hits=(CodeHit("a.py", 1, 0.5, "x"),)),
    SearchResult(STATE_OK),
    SearchResult(STATE_NOT_RUNNING),
    SearchResult(STATE_ROUTE_MISSING),
    SearchResult(STATE_NOT_INDEXED),
    SearchResult(STATE_BLANK_QUERY, detail=BLANK_QUERY_MESSAGE),
    SearchResult(STATE_ERROR, detail=UNCHECKED_MESSAGE),
    SearchResult(STATE_ERROR),
]


def test_no_state_ever_says_the_forbidden_phrase():
    for outcome in OUTCOMES:
        text = status_text(outcome, "q", BASE)
        assert "not built yet" not in text.lower(), text


def test_the_panel_and_client_sources_never_say_the_forbidden_phrase():
    root = Path(code_panel.__file__).resolve().parents[1]
    for path in (root / "ui" / "code_panel.py", root / "code_client.py"):
        assert "not built yet" not in path.read_text(encoding="utf-8").lower(), path.name


def test_results_text_names_the_query_and_the_count():
    assert results_text("search", 2) == '2 result(s) for "search".'
    assert results_text("zzz", 0) == NO_MATCHES_TEXT.format("zzz")


def test_results_text_says_when_the_list_is_cut_at_the_limit():
    assert "Showing the first 25" in results_text("search", 25)
    assert "Showing the first" not in results_text("search", 24)


def test_location_text_drops_the_line_when_there_is_none():
    assert location_text(CodeHit("a.py", 7, None, "")) == "a.py:7"
    assert location_text(CodeHit("a.py", 0, None, "")) == "a.py"


def test_score_text_is_blank_without_a_score():
    assert score_text(None) == ""
    assert score_text(0.12345) == "0.123"


def test_snippet_text_collapses_to_one_line():
    assert snippet_text("def f():\n    return 1\n") == "def f(): return 1"


# ----------------------------------------------------------------- a panel deleted while its search runs


def test_a_search_still_running_when_its_panel_is_deleted_touches_no_widget(qtbot, monkeypatch):
    """A panel deleted while its search still runs is freed on the GUI thread, and its late answer touches no widget.

    The search job holds the provider, the transport, the owner token and the shared delivery object, never the
    panel. So the panel is freed here on the GUI thread, and the late answer is dropped without the panel's update
    step running. That step is replaced by a recorder, so the test can show that it never ran. The collector is off,
    so only reference counting can free the panel.
    """
    gate = threading.Event()
    transport = FakeTransport(gate=gate)
    applied = []
    # _finish is the update step. It is looked up with raising=False, so the test does not depend
    # on the panel having that method.
    monkeypatch.setattr(CodePanel, "_finish", lambda self, answer: applied.append(answer), raising=False)
    gc.disable()
    try:
        container = QWidget()
        panel = CodePanel(Provider(), container, transport=transport)
        _search_no_wait(panel, "search")
        qtbot.waitUntil(lambda: len(transport.calls) == 1, timeout=5000)  # the search has started, held at the gate
        ref = weakref.ref(panel)
        shiboken6.delete(container)  # the panel's widgets are deleted on the GUI thread while the search still runs
        del panel
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late answer reaches the event loop, and nothing raises
        assert applied == []
    finally:
        gate.set()
        gc.enable()


# ----------------------------------------------------------------- a result for another owner, or a superseded search


def _one_hit(path):
    return {"results": [{"path": path, "line": 4, "score": 0.5, "snippet": "def read():"}]}


def test_a_result_from_another_owner_or_from_a_superseded_search_is_dropped(qtbot):
    """A sibling panel's answer, and an answer from a search that this panel's newer search superseded, show nothing.

    Two panels share one transport. The panel's first search emits its Delivered. The sibling's first search then emits
    one at the same generation, and it reaches the panel too, so the panel drops it by owner alone. The panel searches
    again, and its first Delivered is replayed: it carries the panel's own owner with a superseded generation, so it is
    dropped by generation alone. The panel's second result is what the user sees throughout, and it is the control that
    the panel's own current Delivered is accepted.
    """
    transport = FakeTransport(reply=_json(_one_hit("panel_first.py")))
    panel = _panel(qtbot, transport=transport)
    sibling = _panel(qtbot, transport=transport)
    with recording_deliveries() as emitted:
        _search(qtbot, panel, "first")
        assert len(emitted) == 1
        own_first = emitted[0]
        shown_first = visible_state(panel)
        transport.reply = _json(_one_hit("sibling_first.py"))
        _search(qtbot, sibling, "first")
        assert shows(visible_state(sibling), "sibling_first.py")
        assert visible_state(panel) == shown_first
        transport.reply = _json(_one_hit("panel_second.py"))
        _search(qtbot, panel, "second")
        shown_second = visible_state(panel)
        assert shows(shown_second, "panel_second.py")
        emit(own_first)  # this panel's answer from the first search, which the second search superseded
        qtbot.wait(20)
    assert visible_state(panel) == shown_second
    assert not shows(visible_state(panel), "panel_first.py")
