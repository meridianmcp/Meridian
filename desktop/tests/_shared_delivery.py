"""Helpers for the shared-delivery tests: the Delivered objects a pool job emits to shared_delivery().

A pool job emits each finished result as a Delivered to the one shared delivery object. A page keeps a Delivered only
when its owner token is its own and its generation (or job id) is current. Every Delivered reaches every page that is
connected to the shared object, so two pages of one kind see each other's answers.

The drop tests use two instances of a page. A sibling's answer carries another owner, and it is at the same generation
as this page's own answer, so the owner check alone must reject it. Replaying this page's earlier answer after a newer
read carries its own owner and a superseded generation, so the generation check alone must reject it. Nothing here
reads a private attribute of a page.
"""

from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget, QWidget

from meridian_desktop.ui.delivery import Delivery, shared_delivery

# Every delivery the running test has seen, in arrival order. start_recording resets it for each test.
_recorded: list[object] = []


def start_recording(delivery: Delivery) -> None:
    """Connect the recorder to a test's shared delivery object. conftest calls this before the test makes any page.

    Qt calls the connections of one signal in the order they were made. A page's connection is queued, so an emit only
    posts the delivery to the GUI thread. The recorder is a direct connection, so it runs on the emitting thread, during
    the emit. The recorder must therefore be the first connection: connected after a page, it can still be unrecorded
    while the GUI thread has already let the page act on the delivery.
    """
    global _recorded
    _recorded = []
    delivery.delivered.connect(_record, Qt.ConnectionType.DirectConnection)


def _record(item: object) -> None:
    _recorded.append(item)


class _Window:
    """A live view of the deliveries that arrived while one recording_deliveries block was open."""

    def __init__(self) -> None:
        self._start = len(_recorded)
        self._stop: int | None = None

    def _items(self) -> list[object]:
        return _recorded[self._start : self._stop]

    def __len__(self) -> int:
        return len(self._items())

    def __getitem__(self, index):
        return self._items()[index]

    def __iter__(self):
        return iter(self._items())


@contextmanager
def recording_deliveries():
    """Yields a view that gains each object delivered to the shared delivery object while the block runs.

    The recorder is connected before any page of the test exists, so each delivery is recorded before a page can act
    on it. A test can therefore read the view as soon as the page has shown a result.
    """
    window = _Window()
    try:
        yield window
    finally:
        window._stop = len(_recorded)


def emit(item: object) -> None:
    """Emit an object to the shared delivery object, as a pool job does."""
    shared_delivery().delivered.emit(item)


def visible_state(root: QWidget) -> tuple:
    """What a user sees under root: each label, button, line edit, plain-text box and table cell, in widget order."""
    state: list[tuple] = []
    for widget in root.findChildren(QWidget):
        if isinstance(widget, (QLabel, QPushButton)):
            state.append((widget.objectName(), widget.text(), widget.isEnabled()))
        elif isinstance(widget, QLineEdit):
            state.append((widget.objectName(), widget.text()))
        elif isinstance(widget, QPlainTextEdit):
            state.append((widget.objectName(), widget.toPlainText()))
        elif isinstance(widget, QTableWidget):
            cells = tuple(
                "" if widget.item(row, column) is None else widget.item(row, column).text()
                for row in range(widget.rowCount())
                for column in range(widget.columnCount())
            )
            state.append((widget.objectName(), widget.rowCount(), cells))
    return tuple(state)


def shows(state: tuple, text: str) -> bool:
    """True when text appears in any part of a visible_state result."""
    return any(text in str(part) for part in state)
