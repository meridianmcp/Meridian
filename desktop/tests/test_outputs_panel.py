"""Tests for the Outputs tab (meridian_desktop/ui/outputs_panel.py), with pytest-qt. Offscreen Qt, no network.

The panel is built with a fake loopback fetch (outputs_client.fetch), so each search goes through the real
client and its typed states, with no socket opened. The fake records each call and the thread it ran on,
so a test can prove that a search runs off the UI thread and that the panel sends nothing when it is built.
"""

import gc
import json
import threading
import weakref

import pytest
import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetSelectionRange,
    QWidget,
)

from meridian_desktop import local_client, outputs_client as oc
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.ui.outputs_panel import (
    CHECK_FAILED_TEXT,
    INDEX_COMPLETE_TEXT,
    INDEX_DEGRADED_TEXT,
    INDEX_NOT_CHECKED_TEXT,
    INDEX_NOT_REPORTED_TEXT,
    RESULT_COLUMNS,
    SEARCH_HINT,
    OutputsPanel,
    hit_row,
    index_text,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT

from _shared_delivery import emit, recording_deliveries, shows, visible_state

LOCAL = "http://127.0.0.1:8471"


class FakeFetch:
    """Stands in for the loopback fetch. Records each call and its thread. Optionally blocks until released."""

    def __init__(self, reply=None, error=None, release=None):
        self.reply = reply
        self.error = error
        self.release = release
        self.calls = []
        self.threads = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        self.threads.append(threading.current_thread())
        if self.release is not None:
            self.release.wait(5)
        if self.error is not None:
            raise self.error
        return self.reply


def ok(payload):
    return oc.Reply(200, json.dumps(payload).encode("utf-8"))


def _hit(**overrides):
    hit = {
        "path": "C:/outputs/figures/fit.csv",
        "score": 0.912,
        "kind": "csv",
        "is_archival": False,
        "canonical_path": None,
        "generating_script": "make_table.py",
        "csv_columns": ["x", "y", "err"],
        "json_keys": None,
        "size": 2048,
        "mtime": "2026-10-18T10:00:00Z",
    }
    hit.update(overrides)
    return hit


def _fake(monkeypatch, reply=None, error=None, release=None):
    fake = FakeFetch(reply=reply, error=error, release=release)
    monkeypatch.setattr(oc, "fetch", fake)
    return fake


def _panel(qtbot, provider=None):
    panel = OutputsPanel(provider or (lambda: LOCAL))
    qtbot.addWidget(panel)
    return panel


def _named(panel, cls, name):
    widget = panel.findChild(cls, name)
    assert widget is not None, name
    return widget


def _text(panel, name):
    return _named(panel, QLabel, name).text()


def _table(panel):
    return _named(panel, QTableWidget, "outputs-results")


def _search(qtbot, panel, text, enter=False):
    box = _named(panel, QLineEdit, "outputs-search-input")
    box.setText(text)
    if enter:
        qtbot.keyClick(box, Qt.Key.Key_Return)
    else:
        _named(panel, QPushButton, "outputs-search-button").click()
    button = _named(panel, QPushButton, "outputs-search-button")
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)


def _row(panel, row):
    table = _table(panel)
    return [table.item(row, column).text() for column in range(table.columnCount())]


# ----------------------------------------------------------------- no request until asked


def test_building_and_showing_the_panel_makes_no_request(qtbot, monkeypatch):
    fake = _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    panel.show()
    qtbot.waitExposed(panel)
    assert fake.calls == []
    assert _text(panel, "outputs-status") == SEARCH_HINT
    assert _text(panel, "outputs-index") == INDEX_NOT_CHECKED_TEXT


# ----------------------------------------------------------------- search runs off the UI thread


def test_enter_searches_off_the_ui_thread_with_the_address_from_the_provider(qtbot, monkeypatch):
    fake = _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    provided = []

    def provider():
        provided.append(threading.current_thread())
        return LOCAL

    panel = _panel(qtbot, provider)
    _search(qtbot, panel, "fit", enter=True)

    qtbot.waitUntil(lambda: _table(panel).rowCount() == 1, timeout=5000)
    assert fake.calls == [(LOCAL, "/desktop/outputs/search?q=fit&limit=25", oc.SEARCH_TIMEOUT)]
    assert fake.threads[0] is not threading.main_thread()
    assert provided and provided[0] is not threading.main_thread()


def test_a_second_search_is_not_sent_while_one_is_running(qtbot, monkeypatch):
    release = threading.Event()
    fake = _fake(monkeypatch, reply=ok({"hits": []}), release=release)
    panel = _panel(qtbot)
    _named(panel, QLineEdit, "outputs-search-input").setText("fit")
    _named(panel, QPushButton, "outputs-search-button").click()
    qtbot.keyClick(_named(panel, QLineEdit, "outputs-search-input"), Qt.Key.Key_Return)
    release.set()

    button = _named(panel, QPushButton, "outputs-search-button")
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)
    assert len(fake.calls) == 1


# ----------------------------------------------------------------- results and provenance


def test_hits_show_each_provenance_field_in_its_column(qtbot, monkeypatch):
    archival = _hit(
        path="C:/outputs/archive/fit_old.csv",
        score=None,
        is_archival=True,
        canonical_path="C:/outputs/figures/fit.csv",
        csv_columns=None,
        json_keys=["alpha", "beta"],
        size=None,
        mtime="",
        kind="",
        generating_script="",
    )
    _fake(monkeypatch, reply=ok({"hits": [_hit(), archival]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")

    table = _table(panel)
    headers = [table.horizontalHeaderItem(column).text() for column in range(table.columnCount())]
    assert headers == list(RESULT_COLUMNS)
    assert _row(panel, 0) == [
        "C:/outputs/figures/fit.csv", "0.912", "csv", "no", "none", "make_table.py", "x, y, err", "2048",
        "2026-10-18T10:00:00Z",
    ]
    assert _row(panel, 1) == [
        "C:/outputs/archive/fit_old.csv", "not reported", "not reported", "yes", "C:/outputs/figures/fit.csv",
        "not reported", "alpha, beta", "not reported", "not reported",
    ]
    assert _named(panel, QLabel, "outputs-status").text() == oc.OUTPUTS_RESULTS_MESSAGE.format(2, "fit")


def test_the_results_table_is_read_only(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    table = _table(panel)
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert not (table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable)


def test_results_are_copyable_as_tab_separated_text(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    table = _table(panel)
    table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 0, 2), True)
    copy = next(action for action in table.actions() if action.text() == "Copy")
    assert copy.shortcut() == QKeySequence(QKeySequence.StandardKey.Copy)
    QGuiApplication.clipboard().setText("before")
    copy.trigger()
    assert QGuiApplication.clipboard().text() == "C:/outputs/figures/fit.csv\t0.912\tcsv"


def test_copying_several_rows_gives_one_tab_separated_line_per_row(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": [_hit(), _hit(path="C:/outputs/b.csv", score=None)]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    table = _table(panel)
    table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 1), True)
    copy = next(action for action in table.actions() if action.text() == "Copy")
    QGuiApplication.clipboard().setText("before")
    copy.trigger()
    assert QGuiApplication.clipboard().text() == (
        "C:/outputs/figures/fit.csv\t0.912\nC:/outputs/b.csv\tnot reported"
    )


def test_copy_with_nothing_selected_keeps_the_clipboard(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    table = _table(panel)
    copy = next(action for action in table.actions() if action.text() == "Copy")
    QGuiApplication.clipboard().setText("before")
    copy.trigger()
    assert QGuiApplication.clipboard().text() == "before"


# ----------------------------------------------------------------- each state, in plain words


def test_results_state_names_the_count_and_the_query(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    assert _text(panel, "outputs-status") == oc.OUTPUTS_RESULTS_MESSAGE.format(1, "fit")
    assert _table(panel).rowCount() == 1


def test_empty_state_says_no_outputs_match(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": []}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "nothing")
    assert _text(panel, "outputs-status") == oc.OUTPUTS_EMPTY_MESSAGE.format("nothing")
    assert _table(panel).rowCount() == 0


def test_not_running_state_says_not_connected_and_clears_the_table(qtbot, monkeypatch):
    _fake(monkeypatch, error=LocalClientError("not_running", local_client.NOT_RUNNING_MESSAGE))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    status = _text(panel, "outputs-status")
    assert status == oc.NOT_RUNNING_MESSAGE
    assert status.startswith("Not connected")
    assert _table(panel).rowCount() == 0
    assert _text(panel, "outputs-index") == INDEX_NOT_CHECKED_TEXT


def test_missing_route_state_says_the_server_needs_updating(qtbot, monkeypatch):
    _fake(monkeypatch, reply=oc.Reply(404, b'{"error": {"code": "not_found"}}'))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    assert _text(panel, "outputs-status") == oc.OUTPUTS_MISSING_ROUTE_MESSAGE
    assert _table(panel).rowCount() == 0


def test_error_state_names_the_http_status_and_nothing_else(qtbot, monkeypatch):
    _fake(monkeypatch, reply=oc.Reply(500, b"TRACEBACK-SECRET-DETAIL"))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    status = _text(panel, "outputs-status")
    assert status == oc.HTTP_STATUS_MESSAGE.format(500)
    assert "SECRET" not in status


def test_a_failure_in_the_provider_shows_only_the_class_name(qtbot, monkeypatch):
    fake = _fake(monkeypatch, reply=ok({"hits": []}))

    def provider():
        raise RuntimeError("settings read failed at C:/private/settings.json")

    panel = _panel(qtbot, provider)
    _search(qtbot, panel, "fit")
    status = _text(panel, "outputs-status")
    assert status == CHECK_FAILED_TEXT.format("RuntimeError")
    assert "private" not in status
    assert fake.calls == []


def test_a_blank_search_sends_nothing_and_says_to_type_a_term(qtbot, monkeypatch):
    fake = _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "   ")
    assert fake.calls == []
    assert _text(panel, "outputs-status") == oc.BLANK_QUERY_MESSAGE


def test_an_over_long_search_sends_nothing(qtbot, monkeypatch):
    fake = _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "x" * (oc.MAX_QUERY_CHARS + 1))
    assert fake.calls == []
    assert _text(panel, "outputs-status") == oc.QUERY_TOO_LONG_MESSAGE


def test_a_blank_search_after_results_clears_the_table(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    assert _table(panel).rowCount() == 1
    _search(qtbot, panel, "")
    assert _table(panel).rowCount() == 0
    assert _text(panel, "outputs-status") == oc.BLANK_QUERY_MESSAGE


# ----------------------------------------------------------------- index state beside the results


def test_a_degraded_index_is_shown_beside_the_results_with_its_reasons(qtbot, monkeypatch):
    payload = {
        "hits": [_hit()],
        "convergence": {"partial_index": True, "pending_count": 4, "degraded": True, "walk_errors": ["x"]},
    }
    _fake(monkeypatch, reply=ok(payload))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    index = _text(panel, "outputs-index")
    assert index.startswith(INDEX_DEGRADED_TEXT)
    assert "the last rebuild ran out of time" in index
    assert "4 file(s) are still queued" in index
    assert "1 folder(s) could not be read" in index


def test_a_complete_index_says_complete(qtbot, monkeypatch):
    payload = {"hits": [_hit()], "convergence": {"degraded": False, "partial_index": False, "pending_count": 0,
                                                 "last_rebuilt_at": "2026-10-18T10:00:00Z"}}
    _fake(monkeypatch, reply=ok(payload))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    assert _text(panel, "outputs-index") == f"{INDEX_COMPLETE_TEXT} Last rebuilt: 2026-10-18T10:00:00Z."


def test_an_index_the_server_did_not_report_is_said_so(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    assert _text(panel, "outputs-index") == INDEX_NOT_REPORTED_TEXT


def test_an_empty_result_from_a_degraded_index_is_not_called_a_confirmed_miss(qtbot, monkeypatch):
    payload = {"hits": [], "convergence": {"partial_index": True, "pending_count": 9}}
    _fake(monkeypatch, reply=ok(payload))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    assert _text(panel, "outputs-status") == oc.OUTPUTS_EMPTY_DEGRADED_MESSAGE.format("fit")
    assert _text(panel, "outputs-index").startswith(INDEX_DEGRADED_TEXT)


@pytest.mark.parametrize(
    "index, state, expected",
    [
        (None, oc.RESULTS, INDEX_NOT_REPORTED_TEXT),
        (None, oc.EMPTY, INDEX_NOT_REPORTED_TEXT),
        (None, oc.NOT_RUNNING, INDEX_NOT_CHECKED_TEXT),
        (None, oc.ERROR, INDEX_NOT_CHECKED_TEXT),
        (None, oc.INVALID, INDEX_NOT_CHECKED_TEXT),
        (oc.IndexState(False, False, 0, False, 0, ""), oc.RESULTS, INDEX_COMPLETE_TEXT),
    ],
)
def test_index_line_for_each_state(index, state, expected):
    assert index_text(index, state) == expected


def test_index_line_lists_every_degraded_reason():
    index = oc.IndexState(
        degraded=True, partial=True, pending_count=2, db_write_failed=True, walk_error_count=3, last_rebuilt_at=""
    )
    text = index_text(index)
    assert text == (
        "Index state: incomplete, so results may be missing. Reasons: the last rebuild ran out of time; "
        "2 file(s) are still queued; 3 folder(s) could not be read; some index rows could not be saved."
    )


def test_index_line_says_degraded_even_when_no_reason_was_named():
    text = index_text(oc.IndexState(True, False, 0, False, 0, ""))
    assert "the server reports the index as degraded" in text


# ----------------------------------------------------------------- read-only, no other controls, plain text


def test_outputs_panel_is_read_only(qtbot):
    panel = _panel(qtbot)
    assert [button.objectName() for button in panel.findChildren(QPushButton)] == ["outputs-search-button"]
    assert [button.text() for button in panel.findChildren(QPushButton)] == ["Search"]


def test_every_label_is_selectable_and_plain_text_and_none_says_not_built_yet(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"hits": [_hit()]}))
    panel = _panel(qtbot)
    _search(qtbot, panel, "fit")
    labels = panel.findChildren(QLabel)
    assert labels
    for label in labels:
        assert (label.textInteractionFlags() & SELECTABLE_TEXT) == SELECTABLE_TEXT, label.objectName()
        assert label.textFormat() == Qt.TextFormat.PlainText, label.objectName()
        assert "Not built yet" not in label.text()


def test_hit_row_marks_missing_provenance_as_not_reported_and_a_missing_canonical_as_none():
    hit = oc.OutputHit(
        path="C:/a.csv", score=None, kind="", is_archival=False, canonical_path="", generating_script="",
        csv_columns=(), json_keys=(), size=None, modified="",
    )
    assert hit_row(hit) == (
        "C:/a.csv", "not reported", "not reported", "no", "none", "not reported", "not reported",
        "not reported", "not reported",
    )


# ----------------------------------------------------------------- a panel deleted while its search runs


def test_a_search_still_running_when_its_panel_is_deleted_touches_no_widget(qtbot, monkeypatch):
    """A panel deleted while its search still runs is freed on the GUI thread, and its late answer touches no widget.

    The search job holds the provider, the query, the owner token and the shared delivery object, never the panel.
    So the panel is freed here on the GUI thread, and the late answer is dropped without the panel's update step
    running. That step is replaced by a recorder, so the test can show that it never ran. The collector is off, so
    only reference counting can free the panel.
    """
    gate = threading.Event()
    fake = _fake(monkeypatch, reply=ok({"hits": [_hit()]}), release=gate)
    applied = []
    # _finish is the update step. It is looked up with raising=False, so the test does not depend
    # on the panel having that method.
    monkeypatch.setattr(OutputsPanel, "_finish", lambda self, answer: applied.append(answer), raising=False)
    gc.disable()
    try:
        container = QWidget()
        panel = OutputsPanel(lambda: LOCAL, container)
        _named(panel, QLineEdit, "outputs-search-input").setText("fit")
        _named(panel, QPushButton, "outputs-search-button").click()
        qtbot.waitUntil(lambda: len(fake.calls) == 1, timeout=5000)  # the search has started, held at the gate
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


def test_a_result_from_another_owner_or_from_a_superseded_search_is_dropped(qtbot, monkeypatch):
    """A sibling panel's answer, and an answer from a search that this panel's newer search superseded, show nothing.

    Two panels share one fetch. The panel's first search emits its Delivered. The sibling's first search then emits one
    at the same generation, and it reaches the panel too, so the panel drops it by owner alone. The panel searches
    again, and its first Delivered is replayed: it carries the panel's own owner with a superseded generation, so it is
    dropped by generation alone. The panel's second result is what the user sees throughout, and it is the control that
    the panel's own current Delivered is accepted.
    """
    fake = _fake(monkeypatch, reply=ok({"hits": [_hit(path="C:/outputs/panel_first.csv")]}))
    panel = _panel(qtbot)
    sibling = _panel(qtbot)
    with recording_deliveries() as emitted:
        _search(qtbot, panel, "first")
        assert len(emitted) == 1
        own_first = emitted[0]
        shown_first = visible_state(panel)
        fake.reply = ok({"hits": [_hit(path="C:/outputs/sibling_first.csv")]})
        _search(qtbot, sibling, "first")
        assert shows(visible_state(sibling), "sibling_first.csv")
        assert visible_state(panel) == shown_first
        fake.reply = ok({"hits": [_hit(path="C:/outputs/panel_second.csv")]})
        _search(qtbot, panel, "second")
        shown_second = visible_state(panel)
        assert shows(shown_second, "panel_second.csv")
        emit(own_first)  # this panel's answer from the first search, which the second search superseded
        qtbot.wait(20)
    assert visible_state(panel) == shown_second
    assert not shows(visible_state(panel), "panel_first.csv")
