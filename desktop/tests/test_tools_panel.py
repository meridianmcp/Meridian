"""Tests for meridian_desktop/ui/tools_panel.py: the Tools table, the status line, and Refresh off the UI thread.

The status provider is a fake, so nothing reads a real settings file, runs the core or probes a port.
Qt runs offscreen (conftest). The panel has no start, stop or install control, and these tests check that.
"""

import gc
import threading
import weakref
from datetime import datetime

import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QAbstractItemView, QLabel, QPushButton, QTableWidget, QWidget

from _shared_delivery import recording_deliveries, visible_state
from meridian_desktop.tools_status import (
    ENTRY_IN_PLACE_TEXT,
    SERVER_RUNNING_TEXT,
    SERVER_TOOL,
    THIS_COMPUTER,
    UNCHECKED,
    YES,
    ToolRow,
    ToolsStatus,
)
from meridian_desktop.ui.tools_panel import COLUMNS, REFRESH_TEXT, ToolsPanel

FAKE_TOKEN = "sk_meridian_fake-tools-panel-0008"
CLOCK = datetime(2026, 10, 22, 9, 30, 0)
BY_MOUSE = Qt.TextInteractionFlag.TextSelectableByMouse
BY_KEYBOARD = Qt.TextInteractionFlag.TextSelectableByKeyboard


def _rows() -> tuple[ToolRow, ...]:
    return (
        ToolRow("Codex", "meridian-docs", YES, UNCHECKED, ENTRY_IN_PLACE_TEXT),
        ToolRow(THIS_COMPUTER, SERVER_TOOL, YES, YES, SERVER_RUNNING_TEXT),
    )


def _ready(message: str = "Checked. No tool is started or stopped.") -> ToolsStatus:
    return ToolsStatus(rows=_rows(), message=message, checked_at=CLOCK)


def _panel(qtbot, provider) -> ToolsPanel:
    panel = ToolsPanel(provider)
    qtbot.addWidget(panel)
    return panel


def _status_text(panel: ToolsPanel) -> str:
    return panel.findChild(QLabel, "tools-status").text()


def _table(panel: ToolsPanel) -> QTableWidget:
    return panel.findChild(QTableWidget, "tools-table")


def _cells(table: QTableWidget, row: int) -> list[str]:
    return [table.item(row, column).text() for column in range(table.columnCount())]


def _all_text(panel: ToolsPanel) -> str:
    labels = " ".join(label.text() for label in panel.findChildren(QLabel))
    cells = " ".join(
        _table(panel).item(row, column).text()
        for row in range(_table(panel).rowCount())
        for column in range(_table(panel).columnCount())
    )
    return labels + " " + cells


def test_rows_fill_the_table_and_the_status_line_gives_the_time(qtbot):
    panel = _panel(qtbot, lambda: _ready())
    panel.show()
    qtbot.waitUntil(lambda: "Time checked" in _status_text(panel), timeout=5000)

    table = _table(panel)
    assert table.rowCount() == 2
    assert [table.horizontalHeaderItem(column).text() for column in range(table.columnCount())] == list(COLUMNS)
    assert _cells(table, 0) == list(_rows()[0].cells())
    assert _cells(table, 1) == list(_rows()[1].cells())
    assert _status_text(panel) == "Checked. No tool is started or stopped. Time checked: 2026-10-22 09:30:00."
    assert "Not built yet" not in _all_text(panel)


def test_the_read_runs_off_the_ui_thread(qtbot):
    threads: list[int] = []

    def provider() -> ToolsStatus:
        threads.append(threading.get_ident())
        return _ready()

    panel = _panel(qtbot, provider)
    panel.refresh()
    qtbot.waitUntil(lambda: "Time checked" in _status_text(panel), timeout=5000)
    assert threads, "the provider should have run"
    assert threads[0] != threading.main_thread().ident


def test_refresh_reads_again_when_pressed(qtbot):
    calls: list[int] = []

    def provider() -> ToolsStatus:
        calls.append(1)
        return _ready()

    panel = _panel(qtbot, provider)
    panel.show()
    qtbot.waitUntil(lambda: len(calls) == 1 and "Time checked" in _status_text(panel), timeout=5000)
    button = panel.findChild(QPushButton, "tools-refresh")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(calls) == 2 and button.isEnabled(), timeout=5000)


def test_the_first_show_reads_once_and_a_second_show_does_not(qtbot):
    calls: list[int] = []

    def provider() -> ToolsStatus:
        calls.append(1)
        return _ready()

    panel = _panel(qtbot, provider)
    panel.show()
    qtbot.waitUntil(lambda: "Time checked" in _status_text(panel), timeout=5000)
    panel.hide()
    panel.show()
    qtbot.waitUntil(lambda: "Time checked" in _status_text(panel), timeout=5000)
    assert len(calls) == 1


def test_a_second_refresh_while_reading_does_nothing(qtbot):
    gate = threading.Event()
    calls: list[int] = []

    def provider() -> ToolsStatus:
        calls.append(1)
        gate.wait(5)
        return _ready()

    panel = _panel(qtbot, provider)
    button = panel.findChild(QPushButton, "tools-refresh")
    panel.refresh()
    panel.refresh()
    assert not button.isEnabled()
    assert _status_text(panel).startswith("Checking the tools.")
    gate.set()
    qtbot.waitUntil(lambda: button.isEnabled() and "Time checked" in _status_text(panel), timeout=5000)
    assert len(calls) == 1


def test_the_only_button_is_refresh_and_nothing_starts_stops_or_installs(qtbot):
    panel = _panel(qtbot, lambda: _ready())
    texts = [button.text() for button in panel.findChildren(QPushButton)]
    assert texts == [REFRESH_TEXT]
    for text in texts:
        lowered = text.lower()
        assert not any(word in lowered for word in ("start", "stop", "restart", "install", "apply", "remove"))


def test_the_table_cannot_be_edited_and_copies_as_text(qtbot):
    panel = _panel(qtbot, lambda: _ready())
    table = _table(panel)
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert table.contextMenuPolicy() == Qt.ContextMenuPolicy.ActionsContextMenu


def test_a_failed_read_shows_its_class_name_and_never_its_text(qtbot):
    class ToolsBoom(Exception):
        pass

    def provider() -> ToolsStatus:
        raise ToolsBoom(f"leaks {FAKE_TOKEN}")

    panel = _panel(qtbot, provider)
    panel.refresh()
    qtbot.waitUntil(lambda: "ToolsBoom" in _status_text(panel), timeout=5000)
    assert FAKE_TOKEN not in _all_text(panel)
    assert _table(panel).rowCount() == 0


def test_a_provider_that_gives_no_status_says_so(qtbot):
    panel = _panel(qtbot, lambda: None)
    panel.refresh()
    qtbot.waitUntil(lambda: "gave no result" in _status_text(panel), timeout=5000)
    assert _table(panel).rowCount() == 0


def test_before_any_read_the_panel_says_to_press_refresh(qtbot):
    panel = _panel(qtbot, lambda: _ready())
    assert _status_text(panel) == "Press Refresh to read the tools. No tool is started or stopped."
    assert _table(panel).rowCount() == 0


def test_the_headline_says_what_is_set_up_and_what_is_live_without_calling_clients_live(qtbot):
    panel = _panel(qtbot, lambda: _ready())
    intro = panel.findChild(QLabel, "tools-intro").text()
    assert "set up for each AI client" in intro
    assert "the local Meridian server and the tunnel are live" in intro
    assert "live right now" not in intro
    assert "starts, stops and installs" not in intro
    assert "No tool is started or stopped." in intro


def test_every_label_is_selectable(qtbot):
    panel = _panel(qtbot, lambda: _ready())
    panel.show()
    qtbot.waitUntil(lambda: "Time checked" in _status_text(panel), timeout=5000)
    labels = panel.findChildren(QLabel)
    assert labels
    for label in labels:
        flags = label.textInteractionFlags()
        assert bool(flags & BY_MOUSE) and bool(flags & BY_KEYBOARD), label.objectName() or label.text()


def test_a_read_still_running_when_its_panel_is_deleted_does_not_keep_the_panel(qtbot):
    """A panel deleted while its read still runs is freed on the GUI thread, because the read holds no panel.

    The read emits to the shared delivery object, never to the panel's own signal, so a worker never touches a panel
    that the GUI thread may have deleted. This is a guard: a read that captured the panel would keep it alive here.
    The collector is off, so only reference counting can free the panel.
    """
    entered = threading.Event()
    gate = threading.Event()

    def held_provider() -> ToolsStatus:
        entered.set()
        gate.wait(5)
        return _ready()

    gc.disable()
    try:
        container = QWidget()
        panel = ToolsPanel(held_provider, parent=container)
        panel.refresh()
        assert entered.wait(5)  # the read has started and is held at the gate
        ref = weakref.ref(panel)
        shiboken6.delete(container)  # the panel's widgets are deleted on the GUI thread while the read still runs
        del panel
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
    finally:
        gate.set()
        gc.enable()


def test_a_read_from_another_panel_is_dropped(qtbot):
    """A read that another panel started, and that reaches the shared object, never changes this panel's status.

    Both panels listen on the shared delivery object, so the sibling's status reaches this panel too. The panel keeps no
    generation, because it runs one read at a time, so the owner check is the only guard that applies here. The stale
    generation half of a drop test does not apply to this panel. The panel keeps the status of its own read.
    """
    panel = _panel(qtbot, lambda: _ready("Own read."))
    sibling = _panel(qtbot, lambda: _ready("Sibling read."))
    with recording_deliveries() as emitted:
        panel.refresh()
        qtbot.waitUntil(lambda: "Own read." in _status_text(panel), timeout=5000)
        assert len(emitted) == 1
        shown = visible_state(panel)
        sibling.refresh()
        qtbot.waitUntil(lambda: "Sibling read." in _status_text(sibling), timeout=5000)
        assert len(emitted) == 2
        qtbot.wait(20)  # the sibling's result has reached every panel that listens on the shared object
    assert visible_state(panel) == shown
    assert "Sibling read." not in _status_text(panel)


def test_a_read_that_takes_too_long_stops_holding_refresh_and_says_so(qtbot, monkeypatch):
    """The owner saw the panel stay on "Checking the tools". A read that does not finish after STALL_SECONDS now
    leaves Refresh pressable, says it has stalled, and keeps the read: if the read finishes, its answer still shows."""
    from meridian_desktop.ui import tools_panel

    monkeypatch.setattr(tools_panel, "STALL_SECONDS", 0.05)
    gate = threading.Event()

    def held() -> ToolsStatus:
        gate.wait(5)
        return _ready("Late answer.")

    panel = _panel(qtbot, held)
    button = panel.findChild(QPushButton, "tools-refresh")
    panel.refresh()
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)
    assert "Press Refresh to check again" in _status_text(panel)
    assert "Checking the tools" not in _status_text(panel)

    gate.set()
    qtbot.waitUntil(lambda: "Late answer." in _status_text(panel), timeout=5000)
    assert _table(panel).rowCount() == len(_rows())


def test_a_newer_refresh_replaces_a_stalled_read_and_its_late_answer_is_dropped(qtbot, monkeypatch):
    from meridian_desktop.ui import tools_panel

    monkeypatch.setattr(tools_panel, "STALL_SECONDS", 0.05)
    gate = threading.Event()
    answers = iter(["First, stalled.", "Second, newer."])

    def provider() -> ToolsStatus:
        message = next(answers)
        if message == "First, stalled.":
            gate.wait(5)
        return _ready(message)

    panel = _panel(qtbot, provider)
    button = panel.findChild(QPushButton, "tools-refresh")
    panel.refresh()
    qtbot.waitUntil(lambda: button.isEnabled(), timeout=5000)
    panel.refresh()
    qtbot.waitUntil(lambda: "Second, newer." in _status_text(panel), timeout=5000)

    gate.set()
    qtbot.wait(100)
    assert "Second, newer." in _status_text(panel)
    assert "First, stalled." not in _status_text(panel)

