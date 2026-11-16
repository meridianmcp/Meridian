"""A titled group on the Settings tab that the owner can fold away. Qt code, so it sits in ui/.

The header is one button with an arrow and the group's title. The body is any widget or layout the tab adds. Folding
a group hides its body and keeps its widgets, their values and their running jobs. Nothing is read or started by
folding or unfolding a group.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget


class CollapsibleGroup(QWidget):
    """One group of the Settings tab: a header button that folds the body beneath it."""

    def __init__(self, name: str, title: str, parent: QWidget | None = None, *, expanded: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName(f"settings-group-{name}")

        self._toggle = QToolButton(self)
        self._toggle.setObjectName(f"settings-group-{name}-toggle")
        self._toggle.setText(title)
        self._toggle.setToolTip(f"Show or hide {title}")
        self._toggle.setCheckable(True)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        font = self._toggle.font()
        font.setBold(True)
        self._toggle.setFont(font)

        self._body = QWidget(self)
        self._body.setObjectName(f"settings-group-{name}-body")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(14, 4, 4, 10)
        self._body_layout.setSpacing(8)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._toggle)
        outer.addWidget(self._body)

        self._toggle.toggled.connect(self.set_expanded)
        self.set_expanded(expanded)

    def body(self) -> QWidget:
        """The widget that holds the group's contents."""
        return self._body

    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        self._body_layout.addWidget(widget, stretch)

    def add_layout(self, layout: QLayout) -> None:
        self._body_layout.addLayout(layout)

    def is_expanded(self) -> bool:
        return not self._body.isHidden()

    def set_expanded(self, expanded: bool) -> None:
        """Show the body when expanded, and hide it when folded. The header arrow shows which way it is."""
        expanded = bool(expanded)
        self._body.setVisible(expanded)
        self._toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        if self._toggle.isChecked() != expanded:
            self._toggle.blockSignals(True)
            self._toggle.setChecked(expanded)
            self._toggle.blockSignals(False)


__all__ = ["CollapsibleGroup"]
