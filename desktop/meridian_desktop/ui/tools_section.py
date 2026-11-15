"""The MCP tools block on the Settings tab: one folding group per tool group, with a switch for every tool. Qt code, in ui/.

Every tool is on by default. A switch saves the change through the settings store (settings_store.disabled_tools), and
the whole group's switch turns every tool of that group on or off at once. The block re-reads the saved choice on every
refresh, so it shows what the file holds. The choice is applied to Claude Desktop and Codex the next time Meridian Desktop
starts (local_cache.py). Nothing here starts or stops a tool.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QVBoxLayout, QWidget

from meridian_desktop import tool_catalog
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.settings_group import CollapsibleGroup
from meridian_desktop.ui.text import make_selectable

TOOLS_TITLE = "MCP tools"
TOOLS_INTRO = (
    "Every tool is on by default. Switch off a single tool, or a whole group. The choice is saved in the settings file, "
    "and Meridian Desktop applies it to Claude Desktop and Codex the next time it starts. Claude Desktop cannot hide a "
    "single tool, so there a switched-off tool still shows, unless every tool of its server is off."
)
NOT_SAVED_TEXT = "Not saved: the settings file could not be written ({name})."
GROUP_SWITCH_TEXT = "Every {title} tool on"
COUNT_TEXT = "{on} of {total} on"
# Tools in a row of the grid, so a long group reads as a short column of pairs and not one tall list.
TOOL_COLUMNS = 3


def tool_object_name(key: str) -> str:
    return "tool-" + key.replace(tool_catalog.KEY_SEPARATOR, "-")


class ToolsSection(QWidget):
    """The MCP tools block. Build it once; call refresh() to show the saved choice again."""

    def __init__(self, store: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tools-section")
        self._store = store
        self._tool_boxes: dict[str, QCheckBox] = {}
        self._group_boxes: dict[str, QCheckBox] = {}
        self._group_counts: dict[str, QLabel] = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        intro = make_selectable(QLabel(TOOLS_INTRO, self))
        intro.setObjectName("tools-intro")
        intro.setWordWrap(True)
        column.addWidget(intro)
        self._status = make_selectable(QLabel(self))
        self._status.setObjectName("tools-status")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        column.addWidget(self._status)
        for group in tool_catalog.GROUPS:
            column.addWidget(self._build_group(group))
        self.refresh()

    # ----------------------------------------------------------- layout

    def _build_group(self, group: tool_catalog.ToolGroup) -> CollapsibleGroup:
        folding = CollapsibleGroup(f"tools-{group.id}", group.title, self, expanded=False)
        all_box = QCheckBox(GROUP_SWITCH_TEXT.format(title=group.title), folding.body())
        all_box.setObjectName(f"tools-{group.id}-all")
        all_box.toggled.connect(lambda checked, gid=group.id: self._on_group_toggled(gid, checked))
        self._group_boxes[group.id] = all_box
        count = make_selectable(QLabel("", folding.body()))
        count.setObjectName(f"tools-{group.id}-count")
        self._group_counts[group.id] = count
        folding.add_widget(all_box)
        folding.add_widget(count)
        for server in group.servers:
            heading = make_selectable(QLabel(server.title, folding.body()))
            heading.setObjectName(f"tools-server-{server.id}")
            folding.add_widget(heading)
            grid = QGridLayout()
            grid.setContentsMargins(14, 0, 0, 0)
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(4)
            for index, tool in enumerate(server.tools):
                key = tool_catalog.tool_key(group.id, tool)
                box = QCheckBox(tool, folding.body())
                box.setObjectName(tool_object_name(key))
                box.toggled.connect(lambda checked, k=key: self._on_tool_toggled(k, checked))
                self._tool_boxes[key] = box
                grid.addWidget(box, index // TOOL_COLUMNS, index % TOOL_COLUMNS)
            folding.add_layout(grid)
        return folding

    # ----------------------------------------------------------- showing the saved choice

    def refresh(self) -> None:
        """Show the saved choice. Showing it never saves anything."""
        off = set(self._store.load().settings.disabled_tools)
        for key, box in self._tool_boxes.items():
            box.blockSignals(True)  # showing the saved value must not save it again
            box.setChecked(key not in off)
            box.blockSignals(False)
        for group in tool_catalog.GROUPS:
            keys = group.tool_keys
            on = sum(1 for key in keys if key not in off)
            box = self._group_boxes[group.id]
            box.blockSignals(True)
            box.setChecked(on == len(keys))
            box.blockSignals(False)
            self._group_counts[group.id].setText(COUNT_TEXT.format(on=on, total=len(keys)))

    # ----------------------------------------------------------- saving

    def _current_off(self) -> set[str]:
        return set(self._store.load().settings.disabled_tools)

    def _save(self, off: set[str]) -> None:
        try:
            self._store.save(replace(self._store.load().settings, disabled_tools=tool_catalog.normalise(off)))
        except OSError as exc:
            self._status.setText(NOT_SAVED_TEXT.format(name=exc.__class__.__name__))
            self._status.setVisible(True)
        else:
            self._status.setText("")
            self._status.setVisible(False)
        self.refresh()

    def _on_tool_toggled(self, key: str, checked: bool) -> None:
        off = self._current_off()
        if checked:
            off.discard(key)
        else:
            off.add(key)
        self._save(off)

    def _on_group_toggled(self, group_id: str, checked: bool) -> None:
        keys = set(tool_catalog.group_keys(group_id))
        off = self._current_off()
        if checked:
            off -= keys
        else:
            off |= keys
        self._save(off)


__all__ = ["TOOLS_TITLE", "ToolsSection", "tool_object_name"]
