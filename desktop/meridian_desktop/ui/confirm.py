"""A plain Yes or No question for the owner. Qt code, so it sits in ui/.

No is the default button, and Escape means No. Only a click on Yes returns True, so an owner who presses Enter
without reading the question changes nothing. The text is shown as plain text.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

YES_TEXT = "Yes"
NO_TEXT = "No"


def ask_yes_no(parent: QWidget, title: str, text: str) -> bool:
    """A modal Yes or No. No is the default and Escape, and only a click on Yes returns True."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    yes = box.addButton(YES_TEXT, QMessageBox.ButtonRole.YesRole)
    no = box.addButton(NO_TEXT, QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    box.exec()
    return box.clickedButton() is yes


__all__ = ["NO_TEXT", "YES_TEXT", "ask_yes_no"]
