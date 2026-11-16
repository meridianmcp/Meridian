"""The mouse wheel over the tab bar does not change the tab (meridian_desktop/ui/tab_wheel.py). pytest-qt.

The wheel event is sent straight to the tab bar, the way a real wheel turn reaches it. The test checks the current tab
before and after, and that the filter consumes the event.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QTabWidget, QWidget

from meridian_desktop.ui.tab_wheel import TabBarWheelBlocker


def _tabs(qtbot) -> QTabWidget:
    tabs = QTabWidget()
    qtbot.addWidget(tabs)
    for name in ("Home", "Projects", "Keys"):
        tabs.addTab(QWidget(), name)
    tabs.setCurrentIndex(0)
    return tabs


def _wheel(angle_y: int) -> QWheelEvent:
    """One wheel turn, as the tab bar receives it. A negative angle is a turn down."""
    return QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, angle_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_a_wheel_turn_over_the_tab_bar_does_not_move_to_another_tab(qtbot):
    tabs = _tabs(qtbot)
    tabs.tabBar().installEventFilter(TabBarWheelBlocker(tabs))

    QApplication.sendEvent(tabs.tabBar(), _wheel(-120))
    QApplication.sendEvent(tabs.tabBar(), _wheel(-120))
    QApplication.sendEvent(tabs.tabBar(), _wheel(120))

    assert tabs.currentIndex() == 0, "the wheel scrolls the page, not the tabs"


def test_the_filter_consumes_wheel_events_and_passes_other_events_through(qtbot):
    tabs = _tabs(qtbot)
    blocker = TabBarWheelBlocker(tabs)

    assert blocker.eventFilter(tabs.tabBar(), _wheel(-120)) is True
    assert blocker.eventFilter(tabs.tabBar(), QEvent(QEvent.Type.Enter)) is False
