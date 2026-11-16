"""The recording helper must hold a delivery before any page can act on it.

A pool job emits on its own worker thread. Qt calls the connections of one signal in the order they were made. A page's
connection is queued, so the emit only posts the delivery to the GUI thread. The recorder is a direct connection, so it
runs on the worker, during the emit. If a page was connected before the recorder, the GUI thread can run that page's
handler while the worker has not yet reached the recorder, and a test that reads the recorder at that moment sees
nothing.

The test below connects a receiver that holds the worker, after the page. With the recorder connected after the page,
the hold falls between the page's post and the recorder, the page acts first, and the recorder is still empty. With the
recorder connected first, as conftest does, the recorder has the delivery before the page is posted, so the check holds.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Slot

from _shared_delivery import recording_deliveries
from meridian_desktop.ui.delivery import Delivered, shared_delivery


class _Page(QObject):
    """Connects to the shared object the way a page does: a handler on the GUI thread that keeps every delivery."""

    def __init__(self) -> None:
        super().__init__()
        self.acted: list[object] = []
        shared_delivery().delivered.connect(self._on_delivered)

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        self.acted.append(item)


class _EmitFromWorker(QRunnable):
    """Emits one Delivered on a pool worker, as a real job does."""

    def __init__(self, value: object) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._value = value

    def run(self) -> None:
        shared_delivery().delivered.emit(Delivered(object(), self._value))


def test_the_recorder_has_a_delivery_before_a_page_acts_on_it(qtbot):
    page = _Page()
    release = threading.Event()

    def held_on_the_worker(item: object) -> None:
        # Stands in for a worker that is slow between the page's post and the recorder, as a GIL or I/O stall would be.
        release.wait(5)

    shared_delivery().delivered.connect(held_on_the_worker, Qt.ConnectionType.DirectConnection)
    job = _EmitFromWorker("answer")
    try:
        with recording_deliveries() as emitted:
            QThreadPool.globalInstance().start(job)
            qtbot.waitUntil(lambda: len(page.acted) == 1)  # the page has acted on the delivery
            assert len(emitted) == 1  # so the recorder must already hold it
    finally:
        release.set()
        QThreadPool.globalInstance().waitForDone(5000)
        shared_delivery().delivered.disconnect(held_on_the_worker)
