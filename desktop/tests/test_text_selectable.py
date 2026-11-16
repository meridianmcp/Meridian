"""Read-only text in the desktop UI can be selected and copied. Offscreen Qt, no network.

Labels get TextSelectableByMouse and TextSelectableByKeyboard. The read-only Logs panes already
select by default. The read-only tables allow cell selection and copy the selected cells on
Ctrl+C (and from the right-click Copy action) as tab-separated text.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
)

from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.text import enable_table_copy, selected_cells_text

BY_MOUSE = Qt.TextInteractionFlag.TextSelectableByMouse
BY_KEYBOARD = Qt.TextInteractionFlag.TextSelectableByKeyboard
TABLE_NAMES = ("projects-table", "runpod-pods", "tools-table", "search-results", "zotero-results", "tunnelpanel-slots")


def _window(qtbot, settings_path):
    window = MainWindow(SettingsStore(settings_path))
    qtbot.addWidget(window)
    return window


def _selectable(widget) -> bool:
    flags = widget.textInteractionFlags()
    return bool(flags & BY_MOUSE) and bool(flags & BY_KEYBOARD)


def test_every_label_with_text_is_selectable(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    labels = [label for label in window.findChildren(QLabel) if label.text()]
    assert len(labels) >= 10  # the walk reaches the real labels, not an empty tree
    missing = [label.objectName() or label.text() for label in labels if not _selectable(label)]
    assert missing == []


def test_read_only_plain_text_panes_are_selectable(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    # The one Logs view lives in Settings, Diagnostics. Its four log panes, the recent tunnel log on the Tunnel tab, and the
    # held-lines box in the Zotero tab, are the only read-only plain-text panes in the window. Each one must be selectable.
    panes = [pane for pane in window.findChildren(QPlainTextEdit) if pane.isReadOnly()]
    assert sorted(pane.objectName() for pane in panes) == [
        "logs-control-text",
        "logs-desktop-text",
        "logs-runner-text",
        "logs-tunnel-text",
        "tunnelpanel-log",
        "zotero-held-lines",
    ]
    for pane in panes:
        assert _selectable(pane), pane.objectName()


def test_read_only_tables_allow_selection_and_have_a_copy_action(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    for name in TABLE_NAMES:
        table = window.findChild(QTableWidget, name)
        assert table is not None, name
        assert table.selectionMode() != QAbstractItemView.SelectionMode.NoSelection, name
        copies = [action for action in table.actions() if action.text() == "Copy"]
        assert len(copies) == 1, name
        assert copies[0].shortcut() == QKeySequence(QKeySequence.StandardKey.Copy), name
        assert table.contextMenuPolicy() == Qt.ContextMenuPolicy.ActionsContextMenu, name


def _filled_table(qtbot) -> QTableWidget:
    table = QTableWidget(2, 3)
    qtbot.addWidget(table)
    for row in range(2):
        for column in range(3):
            table.setItem(row, column, QTableWidgetItem(f"r{row}c{column}"))
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    enable_table_copy(table)
    return table


def test_selected_cells_copy_as_tab_separated_lines(qtbot):
    table = _filled_table(qtbot)
    for column in range(3):
        table.item(1, column).setSelected(True)
    assert selected_cells_text(table) == "r1c0\tr1c1\tr1c2"

    table.clearSelection()
    assert selected_cells_text(table) == ""


def test_copy_action_writes_the_selection_and_ignores_an_empty_one(qtbot):
    table = _filled_table(qtbot)
    copy = next(action for action in table.actions() if action.text() == "Copy")
    clipboard = QGuiApplication.clipboard()

    clipboard.setText("before")
    copy.trigger()  # nothing selected, so the clipboard keeps its text
    assert clipboard.text() == "before"

    table.item(0, 1).setSelected(True)
    table.item(0, 2).setSelected(True)
    copy.trigger()
    assert clipboard.text() == "r0c1\tr0c2"


def test_ctrl_c_copies_the_selection(qtbot):
    table = _filled_table(qtbot)
    table.show()
    table.activateWindow()
    assert QTest.qWaitForWindowActive(table, 2000)  # the shortcut fires only in the active window
    table.setFocus()
    table.item(1, 0).setSelected(True)
    table.item(1, 1).setSelected(True)
    QGuiApplication.clipboard().setText("before")

    qtbot.keyClick(table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == "r1c0\tr1c1"
