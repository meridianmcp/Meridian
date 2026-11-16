"""Tests for the MCP tools block on the Settings tab (meridian_desktop/ui/tools_section.py), with pytest-qt.

Offscreen Qt (conftest). Every switch saves to a settings file in pytest's temp dir, so nothing touches the real one.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QCheckBox, QLabel, QToolButton

from meridian_desktop import tool_catalog
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.settings_tab import SettingsTab
from meridian_desktop.ui.tools_section import NOT_SAVED_TEXT, TOOLS_TITLE, ToolsSection, tool_object_name

FIND_SYMBOL = "code:find_symbol"
STEP_OVER = "debug:step_over"


def _saved(path) -> list[str]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("disabled_tools", [])


def _tool_box(section: ToolsSection, key: str) -> QCheckBox:
    box = section.findChild(QCheckBox, tool_object_name(key))
    assert box is not None, key
    return box


def _group_box(section: ToolsSection, group_id: str) -> QCheckBox:
    box = section.findChild(QCheckBox, f"tools-{group_id}-all")
    assert box is not None, group_id
    return box


def _count_text(section: ToolsSection, group_id: str) -> str:
    return section.findChild(QLabel, f"tools-{group_id}-count").text()


@pytest.fixture
def path(tmp_path):
    return tmp_path / "settings" / "settings.json"


@pytest.fixture
def section(qtbot, path):
    widget = ToolsSection(SettingsStore(path))
    qtbot.addWidget(widget)
    return widget


def test_every_tool_is_on_by_default_and_showing_the_choice_saves_nothing(section, path):
    for key in tool_catalog.all_tool_keys():
        assert _tool_box(section, key).isChecked(), key
    for group in tool_catalog.GROUPS:
        assert _group_box(section, group.id).isChecked(), group.id
        total = len(group.tool_keys)
        assert _count_text(section, group.id) == f"{total} of {total} on"
    assert not path.exists()


def test_every_group_and_every_tool_has_a_switch(section):
    assert len(section.findChildren(QCheckBox)) == len(tool_catalog.all_tool_keys()) + len(tool_catalog.GROUPS)


def test_switching_one_tool_off_saves_only_that_tool(section, path):
    _tool_box(section, FIND_SYMBOL).setChecked(False)
    assert _saved(path) == [FIND_SYMBOL]
    assert not _tool_box(section, FIND_SYMBOL).isChecked()
    assert _group_box(section, "code").isChecked() is False
    total = len(tool_catalog.group_keys("code"))
    assert _count_text(section, "code") == f"{total - 1} of {total} on"


def test_switching_the_tool_back_on_removes_it_from_the_file(section, path):
    _tool_box(section, FIND_SYMBOL).setChecked(False)
    _tool_box(section, FIND_SYMBOL).setChecked(True)
    assert _saved(path) == []
    assert _tool_box(section, FIND_SYMBOL).isChecked()


def test_switching_a_group_off_saves_every_tool_in_it(section, path):
    _group_box(section, "debug").setChecked(False)
    assert _saved(path) == sorted(tool_catalog.group_keys("debug"))
    for key in tool_catalog.group_keys("debug"):
        assert not _tool_box(section, key).isChecked()
    assert _count_text(section, "debug") == f"0 of {len(tool_catalog.group_keys('debug'))} on"
    # The other groups are untouched.
    assert _tool_box(section, FIND_SYMBOL).isChecked()


def test_switching_a_group_on_turns_every_tool_back_on(section, path):
    _group_box(section, "debug").setChecked(False)
    _group_box(section, "debug").setChecked(True)
    assert _saved(path) == []
    for key in tool_catalog.group_keys("debug"):
        assert _tool_box(section, key).isChecked()


def test_a_group_switch_keeps_a_single_tool_already_off_in_another_group(section, path):
    _tool_box(section, STEP_OVER).setChecked(False)
    _group_box(section, "code").setChecked(False)
    _group_box(section, "code").setChecked(True)
    assert _saved(path) == [STEP_OVER]


def test_a_saved_choice_shows_after_a_refresh(section, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"disabled_tools": [FIND_SYMBOL]}), encoding="utf-8")
    section.refresh()
    assert not _tool_box(section, FIND_SYMBOL).isChecked()
    assert _tool_box(section, STEP_OVER).isChecked()


def test_showing_the_choice_does_not_save_it_again(section, path, monkeypatch):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"disabled_tools": [FIND_SYMBOL]}), encoding="utf-8")
    store = section._store
    writes = []
    monkeypatch.setattr(store, "save", lambda settings: writes.append(settings))
    section.refresh()
    assert writes == []


def test_a_write_that_fails_says_so_and_shows_the_saved_choice(section, path, monkeypatch):
    store = section._store

    def refuse(settings):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", refuse)
    _tool_box(section, FIND_SYMBOL).setChecked(False)
    status = section.findChild(QLabel, "tools-status")
    assert status.text() == NOT_SAVED_TEXT.format(name="OSError")
    # The switch shows the saved choice again, which is still on, so the screen does not lie about the file.
    assert _tool_box(section, FIND_SYMBOL).isChecked()
    assert not path.exists()


def test_the_settings_tab_holds_the_tools_group_after_the_client_setup_group(qtbot, path):
    tab = SettingsTab(SettingsStore(path))
    qtbot.addWidget(tab)
    group = tab.findChild(QToolButton, "settings-group-tools-toggle")
    assert group is not None
    assert group.text() == TOOLS_TITLE
    assert tab.findChild(ToolsSection, "tools-section") is not None
