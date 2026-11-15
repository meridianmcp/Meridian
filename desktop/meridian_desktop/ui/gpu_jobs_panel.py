"""The GPUs tab: this computer's GPUs and GPU jobs, the SSH hosts saved in Settings, Slurm and LSF, and RunPod pods.

This is Qt code and sits in ui/, so the boundary check allows it. The panel has one control that does anything, the
Refresh button. Every table is read-only. There is no start, stop, kill, submit or edit control, and nothing is read
until the tab is first shown or Refresh is pressed.

Every read runs on the thread pool, never on the UI thread. The reads come back as parts, each one a separate delivery:
this computer's part (GPUs, processes, Slurm, LSF and RunPod) and one part per saved SSH host. A part is shown as soon as
it arrives, so one slow host never holds back the local tables. Each delivery is tagged with this panel's owner token, so a
result that belongs to another tab is dropped. While the tab is visible, a timer starts a local read on each tick. A saved host is read no more often than every
gpu_jobs.REMOTE_REFRESH_SECONDS (30 s); the local part is read on every tick, 5 s apart.

The panel takes three injected callables. The window passes the real ones, and tests pass fakes:
- hosts_provider() -> sequence of SSH host names, from Settings. It runs on the worker thread.
- runpod_provider() -> the local Meridian server's RunPod pod list, or None when RunPod is not wired. It may raise.
- run: the program runner (gpu_jobs.Runner). Defaults to gpu_jobs.run_command.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import time
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QRunnable, Qt, QThreadPool, QTimer, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import gpu_jobs, resources
from meridian_desktop.gpu_jobs import GpuSnapshot, HostBackoff, Reading, Runner
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import enable_table_copy, make_selectable

REFRESH_MS = 5000
# The job table draws at most this many rows. A longer list says how many there are, so a huge list never rebuilds the
# table on each tick.
JOB_ROW_LIMIT = 200
# A table never grows past this height. A longer list scrolls, so the rest of the page stays in view.
TABLE_MAX_HEIGHT = 220
GPU_COLUMNS = ("Host", "GPU", "Name", "Utilization", "Memory", "Temperature", "Power")
JOB_COLUMNS = ("Source", "Host", "Job", "User", "Name or command", "State", "GPU", "GPU memory")
STATUS_COLUMNS = ("Where", "Part", "Status")
RESOURCE_COLUMNS = ("Target", "Cores", "CPU now", "Memory", "Note")
RESOURCES_HEADING = "CPU, cores and memory"
INTRO_TEXT = (
    "GPUs and GPU jobs on this computer, the SSH hosts saved in Settings, Slurm and LSF, and RunPod pods. "
    "Read-only: nothing is started, stopped or changed."
)
REFRESH_TEXT = "Refresh"
NOT_READ_TEXT = "Press Refresh to read the GPUs. Nothing is read until then."
LOADING_TEXT = "Reading GPUs..."
CHECKED_PREFIX = "Checked at"
NO_GPUS_TEXT = "No GPUs to show yet."
NO_JOBS_TEXT = "No GPU jobs or processes to show."
GPUS_HEADING = "GPUs"
JOBS_HEADING = "GPU jobs and processes"
STATUS_HEADING = "Where each part was read"


@dataclass(frozen=True)
class LocalPart:
    """This computer's part, with the time it was read and the saved hosts it was read with."""

    checked_at: datetime
    hosts: tuple[str, ...]
    reading: Reading


@dataclass(frozen=True)
class RemotePart:
    """One saved host's part."""

    alias: str
    reading: Reading


class _ReadJob(QRunnable):
    """Run one read on the thread pool and deliver its part. A failure becomes the fixed failure part, so no raw
    exception reaches the UI thread."""

    def __init__(self, work: Callable[[], object], deliver: Callable[[object], None], failure: Callable[[], object]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver
        self._failure = failure

    def run(self) -> None:
        try:
            part = self._work()
        except Exception:  # noqa: BLE001 - a worker must never raise into the pool
            part = self._failure()
        try:
            self._deliver(part)
        except RuntimeError:
            pass  # the panel was deleted while the read ran


def _local_resources() -> resources.ResourceRow:
    """This computer's CPU, cores and memory, read now. Looked up at each read, so a test can replace it."""
    return resources.local_row(gpu_jobs.LOCAL_HOST)


def _table(parent: QWidget, name: str, columns: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns), parent)
    table.setObjectName(name)
    table.setHorizontalHeaderLabels(list(columns))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.setMaximumHeight(TABLE_MAX_HEIGHT)
    enable_table_copy(table)
    return table


def _fill(table: QTableWidget, rows: Sequence[Sequence[str]]) -> None:
    table.setRowCount(len(rows))
    for row_index, cells in enumerate(rows):
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row_index, column, item)


def _label(parent: QWidget, name: str, text: str) -> QLabel:
    label = make_selectable(QLabel(text, parent))
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class GpuJobsPanel(QWidget):
    """The GPUs tab. Read on first show and on Refresh, and while visible on a 5-second timer."""

    def __init__(
        self,
        hosts_provider: Callable[[], Sequence[str]],
        runpod_provider: Callable[[], Any] | None = None,
        parent: QWidget | None = None,
        *,
        run: Runner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("gpu-jobs-panel")
        self._hosts_provider = hosts_provider
        self._runpod_provider = runpod_provider
        self._run: Runner = run if run is not None else gpu_jobs.run_command
        # Reads the time the remote hosts are spaced by. Tests pass a clock they move by hand.
        self._clock = clock
        self._remote_started: dict[str, float] = {}
        # Identifies this tab's reads. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        # The backoff reads the same clock as the spacing, so a test that moves the clock moves both.
        self._backoff = HostBackoff(clock=clock)
        # Each kind of read has its own pool, so a slow host's SSH call never waits behind a local read, and the other way.
        # The remote pool is sized to the saved hosts when they are read, and is capped at MAX_HOSTS.
        self._local_pool = QThreadPool(self)
        self._local_pool.setObjectName("gpu-local-pool")
        self._local_pool.setMaxThreadCount(1)
        self._remote_pool = QThreadPool(self)
        self._remote_pool.setObjectName("gpu-remote-pool")
        self._remote_pool.setMaxThreadCount(1)
        # Each host's last Refresh-triggered read. Refresh may retry a host once per REMOTE_REFRESH_SECONDS.
        self._remote_manual: dict[str, float] = {}
        # Set by Refresh, read by the local part that Refresh starts. A timer tick never sets it.
        self._manual_pending = False
        self._local_in_flight = False
        self._remote_in_flight: set[str] = set()
        self._read_once = False
        # The last local part, and the last part for each saved host. The tables are the sum of these.
        self._checked_at: datetime | None = None
        self._hosts: tuple[str, ...] = ()
        self._local: Reading | None = None
        self._remote: dict[str, Reading] = {}
        # What the tables show now. A snapshot that renders to the same rows rebuilds nothing.
        self._rendered: tuple[tuple, ...] | None = None
        shared_delivery().delivered.connect(self._on_delivered)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._on_tick)
        self._build()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(_label(self, "gpu-intro", INTRO_TEXT))

        row = QHBoxLayout()
        self._refresh = QPushButton(REFRESH_TEXT, self)
        self._refresh.setObjectName("gpu-refresh")
        self._refresh.clicked.connect(self.refresh)
        row.addWidget(self._refresh)
        self._status = _label(self, "gpu-status", NOT_READ_TEXT)
        row.addWidget(self._status, 1)
        layout.addLayout(row)

        self._summary = _label(self, "gpu-summary", "")
        layout.addWidget(self._summary)

        layout.addWidget(_label(self, "gpu-resources-heading", RESOURCES_HEADING))
        self._resources = _table(self, "gpu-resources", RESOURCE_COLUMNS)
        layout.addWidget(self._resources)

        layout.addWidget(_label(self, "gpu-gpus-heading", GPUS_HEADING))
        self._gpus = _table(self, "gpu-gpus", GPU_COLUMNS)
        layout.addWidget(self._gpus)
        self._gpus_empty = _label(self, "gpu-gpus-empty", NO_GPUS_TEXT)
        layout.addWidget(self._gpus_empty)

        layout.addWidget(_label(self, "gpu-jobs-heading", JOBS_HEADING))
        self._jobs = _table(self, "gpu-jobs", JOB_COLUMNS)
        layout.addWidget(self._jobs)
        self._jobs_cap = _label(self, "gpu-jobs-cap", "")
        self._jobs_cap.setVisible(False)
        layout.addWidget(self._jobs_cap)
        self._jobs_empty = _label(self, "gpu-jobs-empty", NO_JOBS_TEXT)
        layout.addWidget(self._jobs_empty)

        layout.addWidget(_label(self, "gpu-statuses-heading", STATUS_HEADING))
        self._statuses = _table(self, "gpu-statuses", STATUS_COLUMNS)
        layout.addWidget(self._statuses)

    # ----------------------------------------------------------- reads

    def refresh(self) -> None:
        """A read now, of this computer and of each saved host. Refresh skips the timer's spacing, but a host is read by
        Refresh at most once per REMOTE_REFRESH_SECONDS, and each Refresh clears that host's backoff. It does nothing while
        the local read runs, and the Refresh button is off until it ends."""
        if self._local_in_flight:
            return
        self._manual_pending = True
        self._start_local()

    def _start_local(self) -> None:
        self._local_in_flight = True
        self._refresh.setEnabled(False)
        self._status.setText(LOADING_TEXT)
        # The job holds the providers, the runner, the owner token and the shared delivery object. It never holds this tab.
        owner = self._owner
        delivery = shared_delivery()
        hosts_provider = self._hosts_provider
        runpod_provider = self._runpod_provider
        run = self._run

        def work() -> LocalPart:
            hosts = tuple(hosts_provider())
            reading = gpu_jobs.read_local(run, runpod_provider, _local_resources)
            return LocalPart(datetime.now(timezone.utc), hosts, reading)

        def failure() -> LocalPart:
            return LocalPart(datetime.now(timezone.utc), (), gpu_jobs.failed_local())

        def deliver(part: object) -> None:
            delivery.delivered.emit(Delivered(owner, part))

        self._local_pool.start(_ReadJob(work, deliver, failure))

    def _maybe_start_remote(self, alias: str) -> None:
        """Read one saved host, unless its read is still running or its last read started less than
        REMOTE_REFRESH_SECONDS ago. The local part is read on every tick, so the host is read on the first tick at or
        after that interval, never more often."""
        if alias in self._remote_in_flight:
            return
        now = self._clock()
        started = self._remote_started.get(alias)
        if started is not None and now - started < gpu_jobs.REMOTE_REFRESH_SECONDS:
            return
        self._submit_remote(alias, now)

    def _refresh_host(self, alias: str) -> None:
        """Read one saved host because Refresh was pressed. The timer's spacing does not apply, but Refresh itself may
        retry a host once per REMOTE_REFRESH_SECONDS, and a read clears the host's backoff."""
        if alias in self._remote_in_flight:
            return
        now = self._clock()
        last = self._remote_manual.get(alias)
        if last is not None and now - last < gpu_jobs.REMOTE_REFRESH_SECONDS:
            return
        self._remote_manual[alias] = now
        self._backoff.forget(alias)
        self._submit_remote(alias, now)

    def _submit_remote(self, alias: str, now: float) -> None:
        self._remote_started[alias] = now
        self._remote_in_flight.add(alias)
        owner = self._owner
        delivery = shared_delivery()
        run = self._run
        backoff = self._backoff

        def work() -> RemotePart:
            return RemotePart(alias, gpu_jobs.read_host(run, alias, backoff))

        def failure() -> RemotePart:
            return RemotePart(alias, gpu_jobs.failed_host(alias))

        def deliver(part: object) -> None:
            delivery.delivered.emit(Delivered(owner, part))

        self._remote_pool.start(_ReadJob(work, deliver, failure))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab, or anything that is not one of this tab's parts, is dropped.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        if isinstance(item.value, LocalPart):
            self._on_local(item.value)
        elif isinstance(item.value, RemotePart):
            self._on_remote(item.value)

    def _on_local(self, part: LocalPart) -> None:
        self._local_in_flight = False
        self._refresh.setEnabled(True)
        self._checked_at = part.checked_at
        self._hosts = part.hosts
        self._local = part.reading
        manual = self._manual_pending
        self._manual_pending = False
        # A host removed in Settings drops its reading, its spacing, its Refresh record and its backoff. A host added later
        # starts clean.
        for alias in list(self._remote_started):
            if alias not in part.hosts:
                self._remote.pop(alias, None)
                self._remote_started.pop(alias, None)
                self._remote_manual.pop(alias, None)
                self._backoff.forget(alias)
        self._remote_pool.setMaxThreadCount(max(1, min(len(part.hosts), gpu_jobs.MAX_HOSTS)))
        for alias in part.hosts:
            if manual:
                self._refresh_host(alias)
            else:
                self._maybe_start_remote(alias)
        self._render()

    def _on_remote(self, part: RemotePart) -> None:
        self._remote_in_flight.discard(part.alias)
        # A host removed in Settings while its read ran is not shown.
        if part.alias in self._hosts:
            self._remote[part.alias] = part.reading
            self._render()

    def _render(self) -> None:
        if self._checked_at is None:
            return
        readings = [self._local or Reading()]
        readings.extend(self._remote[alias] for alias in self._hosts if alias in self._remote)
        self._show(gpu_jobs.assemble(self._checked_at, readings))

    def _show(self, snapshot: GpuSnapshot) -> None:
        checked = snapshot.checked_at.astimezone().strftime("%H:%M:%S")
        self._status.setText(f"{CHECKED_PREFIX} {checked} local time.")
        self._summary.setText(gpu_jobs.summary_text(snapshot))
        self._gpus_empty.setVisible(not snapshot.gpus)
        self._jobs_empty.setVisible(not snapshot.jobs)
        gpu_rows = gpu_jobs.gpu_table_rows(snapshot)
        job_rows = gpu_jobs.job_table_rows(snapshot)
        status_rows = gpu_jobs.status_table_rows(snapshot)
        resource_rows = gpu_jobs.resource_table_rows(snapshot)
        total = len(job_rows)
        self._jobs_cap.setVisible(total > JOB_ROW_LIMIT)
        if total > JOB_ROW_LIMIT:
            self._jobs_cap.setText(f"Showing {JOB_ROW_LIMIT} of {total}.")
        shown = job_rows[:JOB_ROW_LIMIT]
        rendered = (tuple(gpu_rows), tuple(shown), tuple(status_rows), tuple(resource_rows))
        if rendered == self._rendered:
            return
        self._rendered = rendered
        _fill(self._gpus, gpu_rows)
        _fill(self._jobs, shown)
        _fill(self._statuses, status_rows)
        _fill(self._resources, resource_rows)

    # ----------------------------------------------------------- timer

    def _on_tick(self) -> None:
        if not self._local_in_flight:
            self._start_local()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._timer.start()
        if not self._read_once:
            self._read_once = True
            # The first read is the tab opening, not a Refresh, so it does not count toward a host's Refresh spacing.
            if not self._local_in_flight:
                self._start_local()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        super().hideEvent(event)


__all__ = [
    "GPU_COLUMNS",
    "JOB_COLUMNS",
    "REFRESH_MS",
    "STATUS_COLUMNS",
    "TABLE_MAX_HEIGHT",
    "GpuJobsPanel",
    "LocalPart",
    "RemotePart",
]
