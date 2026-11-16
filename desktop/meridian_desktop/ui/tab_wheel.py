"""The tab bar ignores the mouse wheel, so the wheel scrolls the page and never moves between tabs. Qt code.

Qt moves through the tabs when the wheel turns over the tab bar, and the owner did not want that: the wheel is for scrolling
up and down. TabBarWheelBlocker is an event filter for the tab bar. It takes every wheel event and stops it there, so the
tab bar does nothing with it. A wheel over the page below the tab bar is a different widget, so it still scrolls the page.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject


class TabBarWheelBlocker(QObject):
    """Install on a QTabBar with installEventFilter. A wheel event on the bar is consumed; every other event passes through."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - the Qt name
        return event.type() == QEvent.Type.Wheel


__all__ = ["TabBarWheelBlocker"]
