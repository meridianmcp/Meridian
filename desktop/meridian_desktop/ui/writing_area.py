"""Writing area in Settings: DOCX and Slides, each shown as Not connected with its reason. Qt code, so it sits in ui/.

The area makes no request and reads nothing. The local Meridian server has no DOCX route and no Slides route yet,
so each line says so in plain words, and no document or deck is made up. The LaTeX engine check stays its own panel
(on the LaTeX tab), and the note in the area says where to find it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from meridian_desktop.ui.text import make_selectable

WRITING_SECTION = "Writing (DOCX and Slides on this computer)"
WRITING_INTRO = "The LaTeX engine check is on the LaTeX tab."
DOCX_LINE = (
    "DOCX: Not connected. The local Meridian server has no DOCX route yet, "
    "so this desktop cannot read or write DOCX files."
)
SLIDES_LINE = (
    "Slides: Not connected. The local Meridian server has no Slides route yet, "
    "so this desktop cannot read slide decks."
)


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class WritingArea(QWidget):
    """The DOCX and Slides lines, each Not connected. Nothing is read and nothing is sent."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("writing-area")
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "writing-intro", WRITING_INTRO))
        layout.addWidget(_label(self, "writing-docx", DOCX_LINE))
        layout.addWidget(_label(self, "writing-slides", SLIDES_LINE))


__all__ = ["DOCX_LINE", "SLIDES_LINE", "WRITING_INTRO", "WRITING_SECTION", "WritingArea"]
