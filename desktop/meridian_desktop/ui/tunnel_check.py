"""The tunnel check job for the top bar. Qt code, so it sits in ui/.

The main window runs one read-only tunnel status call on the thread pool each time Hosted mode is chosen, and
this job carries it there. The Home tunnel card runs its own read through the same status call. The Tunnel panel
uses TunnelReadJob, which runs the same status call and then reads the tail of the tunnel log. Nothing here starts,
stops or restarts the tunnel.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QRunnable

from meridian_desktop.log_tail import LogTail
from meridian_desktop.slot_ports import SlotPortResult
from meridian_desktop.tunnel_status import TunnelStatus


class TunnelCheckJob(QRunnable):
    """Run one tunnel check on the thread pool and deliver (status, failure_name).

    The job holds only the work and the deliver callable its caller builds. The caller's deliver emits to the shared
    delivery object, never to a window, so the worker never holds one (see ui/main_window.py). A check that raises is
    reduced to its class name here, so a raw exception never reaches the UI.
    """

    def __init__(self, work: Callable[[], TunnelStatus], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        status: TunnelStatus | None = None
        failure: str | None = None
        try:
            status = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        try:
            self._deliver((status, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


class TunnelReadJob(QRunnable):
    """Run the Tunnel panel's read on the thread pool: one tunnel status check, then one read of the tunnel log's tail.

    Both reads are read-only. The status read is reduced to its class name when it raises, as TunnelCheckJob does. The
    log read is guarded too, and a read that raises is delivered as None, which the panel shows as not read. The
    deliver callable receives (status, failure_name, tail, ports). ``ports`` is the port check's list of SlotPortResult,
    or None when no ``ports_work`` was given or the check raised. It emits to the shared delivery object, never to a panel.
    """

    def __init__(
        self,
        status_work: Callable[[], TunnelStatus],
        log_work: Callable[[], LogTail],
        deliver: Callable[[tuple], None],
        ports_work: Callable[[], Sequence[SlotPortResult]] | None = None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._status_work = status_work
        self._log_work = log_work
        self._deliver = deliver
        self._ports_work = ports_work

    def run(self) -> None:
        status: TunnelStatus | None = None
        failure: str | None = None
        try:
            status = self._status_work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        tail: LogTail | None = None
        try:
            tail = self._log_work()
        except Exception:  # noqa: BLE001 - a log that cannot be read is shown as not read, never as a crash
            tail = None
        ports: list[SlotPortResult] | None = None
        if self._ports_work is not None:
            try:
                ports = list(self._ports_work())
            except Exception:  # noqa: BLE001 - a port check that raises is shown as not checked, never as a crash
                ports = None
        try:
            self._deliver((status, failure, tail, ports))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown
