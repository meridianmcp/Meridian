"""Tests for the Projects tab's subproject tree: the pure arrangement, then the tab itself.

The arrangement is project_tree.arrange, which needs no Qt. The Qt tests read a real temp database
file through the tab's own reader, with a fake probe, so no test opens a socket or reads the real home.
"""

import sqlite3
from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QToolButton,
)

from meridian_desktop import local_client, project_tree, projects_reader
from meridian_desktop.data_source import DataSources
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.projects_tab import TREE_DEPTH_ROLE, TREE_INDENT_PX, ProjectsTab, _TreeNameDelegate
from meridian_desktop.ui.text import selected_cells_text

WRITE_CONTROLS = (
    QPushButton,
    QToolButton,
    QCheckBox,
    QRadioButton,
    QLineEdit,
    QComboBox,
    QAbstractSpinBox,
)


def _row(project_id, parent=None, created="2026-01-01 09:00:00"):
    return {"id": project_id, "name": project_id, "parent_project_id": parent, "created_at": created}


def _ids(arranged):
    return [row["id"] for row, _depth in arranged]


def _depths(arranged):
    return [depth for _row, depth in arranged]


# ----------------------------------------------------------- arrangement (pure)


def test_parent_with_two_children_nests_both_under_it_in_the_given_order():
    rows = [
        _row("c-1", parent="par"),
        _row("solo"),
        _row("par"),
        _row("c-2", parent="par"),
    ]
    arranged = project_tree.arrange(rows)
    # Roots keep their order (solo, par). Each child follows its parent, in the order the reader gave them.
    assert _ids(arranged) == ["solo", "par", "c-1", "c-2"]
    assert _depths(arranged) == [0, 0, 1, 1]


def test_an_orphan_child_is_a_root_and_is_still_shown():
    rows = [
        _row("orph", parent="gone-project"),
        _row("par"),
    ]
    arranged = project_tree.arrange(rows)
    # The parent is not in the list, so the orphan is a root at depth 0. Nothing is dropped.
    assert _ids(arranged) == ["orph", "par"]
    assert _depths(arranged) == [0, 0]


def test_a_project_with_no_parent_is_a_root_whether_none_or_empty():
    rows = [_row("a", parent=None), _row("b", parent=""), _row("c")]
    arranged = project_tree.arrange(rows)
    assert _ids(arranged) == ["a", "b", "c"]
    assert _depths(arranged) == [0, 0, 0]


def test_a_grandchild_is_one_level_deeper_than_its_child():
    rows = [_row("gc", parent="kid"), _row("kid", parent="top"), _row("top")]
    arranged = project_tree.arrange(rows)
    assert _ids(arranged) == ["top", "kid", "gc"]
    assert _depths(arranged) == [0, 1, 2]


def test_a_project_that_names_itself_as_parent_is_a_root():
    arranged = project_tree.arrange([_row("me", parent="me"), _row("other")])
    assert _ids(arranged) == ["me", "other"]
    assert _depths(arranged) == [0, 0]


def test_a_loop_of_parents_places_every_row_exactly_once():
    rows = [_row("a", parent="b"), _row("b", parent="a"), _row("c", parent="a"), _row("d")]
    arranged = project_tree.arrange(rows)
    # Every row appears once. The root d comes first. The loop of a and b has no root, so its first row
    # in the list (a) becomes one, and b and c are its children.
    assert _ids(arranged) == ["d", "a", "b", "c"]
    assert _depths(arranged) == [0, 0, 1, 1]


def test_an_empty_list_arranges_to_nothing():
    assert project_tree.arrange([]) == []


def test_the_input_rows_are_not_changed():
    rows = [_row("c", parent="p"), _row("p")]
    snapshot = [dict(row) for row in rows]
    project_tree.arrange(rows)
    assert rows == snapshot


# ----------------------------------------------------------- the Projects tab (Qt)

TREE_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    creator_human_id TEXT,
    icon TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority TEXT NOT NULL DEFAULT 'P2',
    parent_project_id TEXT,
    created_at TEXT NOT NULL
)
"""
# A parent with two children, an orphan child whose parent is not in the table, and a project with no parent.
# Inserted in no particular order. The reader lists them newest first.
TREE_ROWS = [
    ("par", "Parent", None, None, "active", "P1", None, "2026-01-01 09:00:00"),
    ("c-1", "Child first", None, None, "active", "P2", "par", "2026-02-01 09:00:00"),
    ("c-2", "Child second", None, None, "parked", "P0", "par", "2026-03-01 09:00:00"),
    ("orph", "Orphan child", None, None, "active", "P2", "gone", "2026-02-15 09:00:00"),
    ("lone", "No parent", None, None, "archived", "P2", None, "2026-01-15 09:00:00"),
]
# The roots keep the reader's order, newest first: Orphan child, No parent, Parent. Each parent's children
# follow it in that same order: Child second, then Child first.
TREE_NAMES = ["Orphan child", "No parent", "Parent", "Child second", "Child first"]
TREE_DEPTHS = [0, 0, 0, 1, 1]


def _tree_db(tmp_path) -> Path:
    path = tmp_path / "tree.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(TREE_SCHEMA)
        conn.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?)", TREE_ROWS)
        conn.commit()
    finally:
        conn.close()
    return path


def _unexpected(*args):
    raise AssertionError("this data source should not be read in this test")


def _tab(qtbot, settings_path, db):
    SettingsStore(settings_path).save(Settings(mode="local", db_path=str(db)))
    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_unexpected,
        read_file=projects_reader.list_projects,
        get_token=lambda: None,
        list_hosted=_unexpected,
    )
    tab = ProjectsTab(SettingsStore(settings_path), sources)
    qtbot.addWidget(tab)
    tab.refresh()
    return tab


def test_the_tab_nests_children_under_their_parent_and_keeps_orphans(qtbot, settings_path, tmp_path):
    tab = _tab(qtbot, settings_path, _tree_db(tmp_path))
    table = tab.findChild(QTableWidget, "projects-table")
    status = tab.findChild(QLabel, "projects-status")
    detail = tab.findChild(QLabel, "projects-detail")

    qtbot.waitUntil(lambda: table.rowCount() == 5, timeout=5000)
    assert status.text() == "Showing 5 project(s) from the database file."
    assert [table.item(row, 0).text() for row in range(5)] == TREE_NAMES
    assert [table.item(row, 0).data(TREE_DEPTH_ROLE) for row in range(5)] == TREE_DEPTHS

    # The name cell holds the plain name, so copy gives the same text as it did before the tree.
    table.selectRow(3)
    assert selected_cells_text(table).split("\t")[0] == "Child second"

    # The orphan is shown at the top level, and its detail still names the missing parent.
    table.selectRow(0)
    qtbot.waitUntil(lambda: "Name: Orphan child" in detail.text(), timeout=5000)
    assert "Parent project: gone" in detail.text()


def test_the_tree_tab_stays_read_only(qtbot, settings_path, tmp_path):
    tab = _tab(qtbot, settings_path, _tree_db(tmp_path))
    table = tab.findChild(QTableWidget, "projects-table")
    qtbot.waitUntil(lambda: table.rowCount() == 5, timeout=5000)

    for widget_type in WRITE_CONTROLS:
        found = tab.findChildren(widget_type)
        # The one text box is the search filter. It hides rows and never edits or sends anything.
        if widget_type is QLineEdit:
            assert [widget.objectName() for widget in found] == ["projects-search"], widget_type.__name__
        else:
            assert found == [], widget_type.__name__
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert isinstance(table.itemDelegateForColumn(0), _TreeNameDelegate)


def test_the_indented_tree_paints_without_error(qtbot, settings_path, tmp_path):
    tab = _tab(qtbot, settings_path, _tree_db(tmp_path))
    table = tab.findChild(QTableWidget, "projects-table")
    qtbot.waitUntil(lambda: table.rowCount() == 5, timeout=5000)
    table.selectRow(4)
    # Painting runs the delegate on every row, the indented children included. An image back means it drew.
    assert not table.grab().isNull()


def test_the_name_column_is_sized_for_the_indent(qtbot, settings_path, tmp_path):
    """Column 0 is sized from the delegate's size hint, so each name must ask for its indent or its text is clipped."""
    tab = _tab(qtbot, settings_path, _tree_db(tmp_path))
    table = tab.findChild(QTableWidget, "projects-table")
    qtbot.waitUntil(lambda: table.rowCount() == 5, timeout=5000)
    delegate = table.itemDelegateForColumn(0)
    option = QStyleOptionViewItem()
    widest = 0
    depths = []
    for row in range(table.rowCount()):
        index = table.model().index(row, 0)
        depth = index.data(TREE_DEPTH_ROLE)
        depths.append(depth)
        base = super(_TreeNameDelegate, delegate).sizeHint(option, index)
        assert delegate.sizeHint(option, index).width() == base.width() + depth * TREE_INDENT_PX
        widest = max(widest, delegate.sizeHint(option, index).width())
    # The fixture has indented children, so the per-row check above is not vacuous.
    assert max(depths) >= 1
    assert table.columnWidth(0) >= widest
