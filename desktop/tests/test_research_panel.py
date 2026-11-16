"""Tests for the Research tab (meridian_desktop/ui/research_panel.py), with pytest-qt. Offscreen Qt, no network.

The panel is built with a fake loopback fetch (outputs_client.fetch), so each read goes through the real
client and its typed states, with no socket opened. The fake records each call and the thread it ran on.
"""

import gc
import json
import threading
import weakref

import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetSelectionRange,
    QWidget,
)

from meridian_desktop import local_client, outputs_client as oc
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.ui.research_panel import (
    CHECK_FAILED_TEXT,
    CHECKING_TEXT,
    NOT_CHECKED_TEXT,
    ResearchPanel,
    run_row,
)
from meridian_desktop.ui.text import SELECTABLE_TEXT

from _shared_delivery import emit, recording_deliveries, shows, visible_state

LOCAL = "http://127.0.0.1:8471"


class FakeFetch:
    """Stands in for the loopback fetch. Records each call and its thread, and returns or raises as given.

    With ``release`` set, the call waits until the test sets it, so a test can act while a read runs.
    """

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


def _run(**overrides):
    run = {
        "id": "run-1",
        "status": "active",
        "provider": "provider-a",
        "started_at": "2026-10-18T09:00:00Z",
        "updated_at": "2026-10-18T09:05:00Z",
        "completed_at": None,
    }
    run.update(overrides)
    return run


def _fake(monkeypatch, reply=None, error=None, release=None):
    fake = FakeFetch(reply=reply, error=error, release=release)
    monkeypatch.setattr(oc, "fetch", fake)
    return fake


def _panel(qtbot, provider=None):
    panel = ResearchPanel(provider or (lambda: LOCAL))
    qtbot.addWidget(panel)
    return panel


def _named(panel, cls, name):
    widget = panel.findChild(cls, name)
    assert widget is not None, name
    return widget


def _text(panel, name):
    return _named(panel, QLabel, name).text()


def _table(panel):
    return _named(panel, QTableWidget, "research-runs")


def _show_and_wait(qtbot, panel):
    panel.show()
    qtbot.waitExposed(panel)
    refresh = _named(panel, QPushButton, "research-refresh")
    qtbot.waitUntil(lambda: refresh.isEnabled() and _text(panel, "research-status") != CHECKING_TEXT, timeout=5000)
    return panel


def _row(panel, row):
    table = _table(panel)
    return [table.item(row, column).text() for column in range(table.columnCount())]


# ----------------------------------------------------------------- no request until shown, off the UI thread


def test_building_the_panel_makes_no_request_and_showing_it_reads_once_off_the_ui_thread(qtbot, monkeypatch):
    fake = _fake(monkeypatch, reply=ok({"runs": [_run()], "count": 1}))
    panel = _panel(qtbot)
    assert fake.calls == []
    assert _text(panel, "research-status") == NOT_CHECKED_TEXT

    _show_and_wait(qtbot, panel)

    assert fake.calls == [(LOCAL, "/desktop/research/runs?limit=50", oc.RUNS_TIMEOUT)]
    assert fake.threads[0] is not threading.main_thread()


def test_refresh_reads_again(qtbot, monkeypatch):
    fake = _fake(monkeypatch, reply=ok({"runs": [_run()], "count": 1}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    refresh = _named(panel, QPushButton, "research-refresh")
    refresh.click()
    qtbot.waitUntil(lambda: len(fake.calls) == 2, timeout=5000)
    qtbot.waitUntil(lambda: refresh.isEnabled(), timeout=5000)
    assert len(fake.calls) == 2


def test_the_address_comes_from_the_provider_on_the_worker_thread(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"runs": []}))
    provided = []

    def provider():
        provided.append(threading.current_thread())
        return LOCAL

    _show_and_wait(qtbot, _panel(qtbot, provider))
    assert provided and provided[0] is not threading.main_thread()


# ----------------------------------------------------------------- runs, state and timestamps


def test_runs_show_state_and_timestamps_in_their_columns(qtbot, monkeypatch):
    runs = [
        _run(),
        _run(id="run-2", status="completed", completed_at="2026-10-18T10:00:00Z", provider="provider-b"),
    ]
    _fake(monkeypatch, reply=ok({"runs": runs, "count": 2}))
    panel = _show_and_wait(qtbot, _panel(qtbot))

    table = _table(panel)
    headers = [table.horizontalHeaderItem(column).text() for column in range(table.columnCount())]
    assert headers == ["Run", "Provider", "State", "Started", "Updated", "Completed"]
    assert _row(panel, 0) == [
        "run-1", "provider-a", "active", "2026-10-18T09:00:00Z", "2026-10-18T09:05:00Z", "not reported",
    ]
    assert _row(panel, 1) == [
        "run-2", "provider-b", "completed", "2026-10-18T09:00:00Z", "2026-10-18T09:05:00Z", "2026-10-18T10:00:00Z",
    ]
    assert _text(panel, "research-status") == oc.RUNS_RESULTS_MESSAGE.format(2)


def test_a_run_without_a_provider_says_not_reported(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"runs": [_run(provider=None)]}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    assert _row(panel, 0)[1] == "not reported"


def test_run_row_marks_each_missing_field_as_not_reported():
    run = oc.ResearchRun("r", "", "", "", "", "")
    assert run_row(run) == ("r", "not reported", "not reported", "not reported", "not reported", "not reported")


def test_the_runs_table_is_read_only(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"runs": [_run()]}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    table = _table(panel)
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert not (table.item(0, 0).flags() & Qt.ItemFlag.ItemIsEditable)


def test_runs_are_copyable_as_tab_separated_text(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"runs": [_run()]}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    table = _table(panel)
    table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 0, 2), True)
    copy = next(action for action in table.actions() if action.text() == "Copy")
    assert copy.shortcut() == QKeySequence(QKeySequence.StandardKey.Copy)
    QGuiApplication.clipboard().setText("before")
    copy.trigger()
    assert QGuiApplication.clipboard().text() == "run-1\tprovider-a\tactive"


# ----------------------------------------------------------------- each state, in plain words


def test_empty_state_says_there_are_no_runs_yet(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"runs": [], "count": 0}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    assert _text(panel, "research-status") == oc.RUNS_EMPTY_MESSAGE
    assert _table(panel).rowCount() == 0


def test_not_running_state_says_not_connected_and_what_to_do(qtbot, monkeypatch):
    _fake(monkeypatch, error=LocalClientError("not_running", local_client.NOT_RUNNING_MESSAGE))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    status = _text(panel, "research-status")
    assert status == oc.NOT_RUNNING_MESSAGE
    assert status.startswith("Not connected")
    assert "Start Meridian" in status
    assert _table(panel).rowCount() == 0


def test_missing_route_state_says_the_server_needs_updating(qtbot, monkeypatch):
    _fake(monkeypatch, reply=oc.Reply(404, b'{"error": {"code": "not_found"}}'))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    assert _text(panel, "research-status") == oc.RUNS_MISSING_ROUTE_MESSAGE
    assert _table(panel).rowCount() == 0


def test_error_state_names_the_http_status_and_hides_the_body(qtbot, monkeypatch):
    _fake(monkeypatch, reply=oc.Reply(500, b"BODY-SECRET-TEXT"))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    status = _text(panel, "research-status")
    assert status == oc.HTTP_STATUS_MESSAGE.format(500)
    assert "SECRET" not in status


def test_a_response_without_a_runs_list_is_an_error_not_an_empty_list(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"count": 0}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    assert _text(panel, "research-status") == oc.UNREADABLE_MESSAGE


def test_an_unexpected_failure_in_the_provider_shows_only_the_class_name(qtbot, monkeypatch):
    fake = _fake(monkeypatch, reply=ok({"runs": []}))

    def provider():
        raise RuntimeError("settings read failed at C:/private/settings.json")

    panel = _show_and_wait(qtbot, _panel(qtbot, provider))
    status = _text(panel, "research-status")
    assert status == CHECK_FAILED_TEXT.format("RuntimeError")
    assert "private" not in status
    assert fake.calls == []


# ----------------------------------------------------------------- no submit control


def test_research_panel_has_no_submit_control(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"runs": [_run()]}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    buttons = panel.findChildren(QPushButton)
    assert [button.objectName() for button in buttons] == ["research-refresh"]
    assert [button.text() for button in buttons] == ["Refresh"]


def test_has_no_submit_control_no_action_or_menu_item_other_than_copy(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"runs": [_run()]}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    table = _table(panel)
    assert [action.text() for action in table.actions()] == ["Copy"]
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers


# ----------------------------------------------------------------- text rules


def test_every_label_is_selectable_and_plain_text_and_none_says_not_built_yet(qtbot, monkeypatch):
    _fake(monkeypatch, reply=ok({"runs": [_run()]}))
    panel = _show_and_wait(qtbot, _panel(qtbot))
    labels = panel.findChildren(QLabel)
    assert labels
    for label in labels:
        assert (label.textInteractionFlags() & SELECTABLE_TEXT) == SELECTABLE_TEXT, label.objectName()
        assert label.textFormat() == Qt.TextFormat.PlainText, label.objectName()
        assert "Not built yet" not in label.text()


def test_before_any_check_the_status_says_not_checked_yet(qtbot):
    panel = _panel(qtbot)
    assert _text(panel, "research-status") == NOT_CHECKED_TEXT


# ----------------------------------------------------------------- a panel deleted while its read runs


def test_a_read_still_running_when_its_panel_is_deleted_touches_no_widget(qtbot, monkeypatch):
    """A panel deleted while its read still runs is freed on the GUI thread, and its late answer touches no widget.

    The read job holds the provider, the owner token and the shared delivery object, never the panel. So the panel is
    freed here on the GUI thread, and the late answer is dropped without the panel's update step running. That step
    is replaced by a recorder, so the test can show that it never ran. The collector is off, so only reference
    counting can free the panel.
    """
    gate = threading.Event()
    fake = _fake(monkeypatch, reply=ok({"runs": [_run()], "count": 1}), release=gate)
    applied = []
    # _finish is the update step. It is looked up with raising=False, so the test does not depend
    # on the panel having that method.
    monkeypatch.setattr(ResearchPanel, "_finish", lambda self, answer: applied.append(answer), raising=False)
    gc.disable()
    try:
        container = QWidget()
        panel = ResearchPanel(lambda: LOCAL, container)
        _named(panel, QPushButton, "research-refresh").click()
        qtbot.waitUntil(lambda: len(fake.calls) == 1, timeout=5000)  # the read has started, held at the gate
        ref = weakref.ref(panel)
        shiboken6.delete(container)  # the panel's widgets are deleted on the GUI thread while the read still runs
        del panel
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late answer reaches the event loop, and nothing raises
        assert applied == []
    finally:
        gate.set()
        gc.enable()


# ----------------------------------------------------------------- a result for another owner, or a superseded read


def _read_and_wait(qtbot, panel):
    refresh = _named(panel, QPushButton, "research-refresh")
    refresh.click()
    qtbot.waitUntil(lambda: refresh.isEnabled(), timeout=5000)


def test_a_result_from_another_owner_or_from_a_superseded_read_is_dropped(qtbot, monkeypatch):
    """A sibling panel's answer, and an answer from a read that this panel's newer read superseded, show nothing.

    Two panels share one fetch. The panel's first read emits its Delivered. The sibling's first read then emits one at
    the same generation, and it reaches the panel too, so the panel drops it by owner alone. The panel reads again, and
    its first Delivered is replayed: it carries the panel's own owner with a superseded generation, so it is dropped by
    generation alone. The panel's second result is what the user sees throughout, and it is the control that the
    panel's own current Delivered is accepted.
    """
    fake = _fake(monkeypatch, reply=ok({"runs": [_run(id="panel-first")], "count": 1}))
    panel = _panel(qtbot)
    sibling = _panel(qtbot)
    with recording_deliveries() as emitted:
        _read_and_wait(qtbot, panel)
        assert len(emitted) == 1
        own_first = emitted[0]
        shown_first = visible_state(panel)
        fake.reply = ok({"runs": [_run(id="sibling-first")], "count": 1})
        _read_and_wait(qtbot, sibling)
        assert shows(visible_state(sibling), "sibling-first")
        assert visible_state(panel) == shown_first
        fake.reply = ok({"runs": [_run(id="panel-second")], "count": 1})
        _read_and_wait(qtbot, panel)
        shown_second = visible_state(panel)
        assert shows(shown_second, "panel-second")
        emit(own_first)  # this panel's answer from the first read, which the second read superseded
        qtbot.wait(20)
    assert visible_state(panel) == shown_second
    assert not shows(visible_state(panel), "panel-first")
