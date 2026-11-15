"""Tests for the GPUs tab (meridian_desktop/ui/gpu_jobs_panel.py), with pytest-qt. Offscreen Qt, no network, no process.

The runner, the host list and the RunPod list are fakes. Each fake records its calls and the thread it ran on, so a test
can prove a read runs off the UI thread, nothing runs before the tab is shown, and a result for another tab is dropped.
"""

import socket
import subprocess
import threading
from datetime import datetime, timezone

import pytest
from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QToolButton,
)

from _shared_delivery import emit, recording_deliveries
from meridian_desktop import gpu_jobs
from meridian_desktop.ui import gpu_jobs_panel
from meridian_desktop.ui.gpu_jobs_panel import LocalPart
from meridian_desktop.gpu_jobs import (
    Reading,
    BJOBS_ARGV,
    NVIDIA_APPS_ARGV,
    NVIDIA_GPU_ARGV,
    SQUEUE_ARGV,
    CommandResult,
    GpuSnapshot,
)
from meridian_desktop.ui.delivery import Delivered
from meridian_desktop.ui.gpu_jobs_panel import (
    CHECKED_PREFIX,
    GPU_COLUMNS,
    JOB_COLUMNS,
    LOADING_TEXT,
    NOT_READ_TEXT,
    REFRESH_MS,
    STATUS_COLUMNS,
    GpuJobsPanel,
)

NOW = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
REAL_GPU_LINE = "0, NVIDIA GeForce RTX 3080, GPU-85b6c4db-6eba-ae6e-4a6f-b1c6ebff448d, 79, 1734, 20480, 59, 272.21, 320.00\n"
REAL_APPS_LINE = "4, [Insufficient Permissions], [N/A], GPU-85b6c4db-6eba-ae6e-4a6f-b1c6ebff448d\n"
RUNPOD = {"ok": True, "pods": [{"id": "pod-1", "name": "trainer", "status": "RUNNING", "gpu": {"id": "A100", "count": 1}}]}


class RecordingRunner:
    """A fake runner. ``gate`` is an optional Event the runner waits on, so a test can hold a read in progress."""

    def __init__(self, answers=None, gate=None, error=None):
        self.answers = answers if answers is not None else {
            NVIDIA_GPU_ARGV: CommandResult(0, REAL_GPU_LINE),
            NVIDIA_APPS_ARGV: CommandResult(0, REAL_APPS_LINE),
            SQUEUE_ARGV: CommandResult(None, missing=True),
            BJOBS_ARGV: CommandResult(None, missing=True),
        }
        self._gate = gate
        self._error = error
        self.calls = 0
        self.threads = []
        self._lock = threading.Lock()

    def __call__(self, argv, timeout):
        with self._lock:
            self.threads.append(threading.get_ident())
            if tuple(argv) in (NVIDIA_GPU_ARGV,):
                self.calls += 1
        if self._gate is not None:
            self._gate.wait(5)
        if self._error is not None:
            raise self._error
        return self.answers.get(tuple(argv), CommandResult(None, missing=True))


def make_panel(qtbot, *, run=None, runpod=lambda: RUNPOD, hosts=lambda: ()):
    panel = GpuJobsPanel(hosts, runpod, run=run if run is not None else RecordingRunner())
    qtbot.addWidget(panel)
    return panel


def table(panel, name):
    return panel.findChild(QTableWidget, name)


def label(panel, name):
    return panel.findChild(QLabel, name)


def button(panel):
    return panel.findChild(QPushButton, "gpu-refresh")


def wait_checked(qtbot, panel):
    qtbot.waitUntil(lambda: label(panel, "gpu-status").text().startswith(CHECKED_PREFIX), timeout=5000)


# ----------------------------------------------------------------- when reads happen


def test_nothing_is_read_when_the_tab_is_built(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    assert run.calls == 0
    assert run.threads == []
    assert label(panel, "gpu-status").text() == NOT_READ_TEXT
    assert table(panel, "gpu-gpus").rowCount() == 0


def test_showing_the_tab_reads_once_and_fills_the_tables(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    assert run.calls == 1
    assert table(panel, "gpu-gpus").rowCount() == 1
    assert table(panel, "gpu-gpus").item(0, 2).text() == "NVIDIA GeForce RTX 3080"
    assert label(panel, "gpu-summary").text() == "1 GPU on 1 host. 1 busy. 1.7 of 20.0 GB GPU memory in use."
    jobs = table(panel, "gpu-jobs")
    assert jobs.rowCount() == 2
    assert jobs.item(0, 0).text() == "GPU process"
    assert jobs.item(1, 0).text() == "RunPod"
    statuses = table(panel, "gpu-statuses")
    assert statuses.rowCount() >= 4
    assert label(panel, "gpu-gpus-empty").isHidden()
    assert label(panel, "gpu-jobs-empty").isHidden()


def test_the_read_runs_off_the_ui_thread(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    assert run.threads
    assert all(thread != threading.get_ident() for thread in run.threads)


def test_the_saved_hosts_are_read_on_the_worker_thread_too(qtbot):
    seen = []

    def hosts():
        seen.append(threading.get_ident())
        return ()

    panel = make_panel(qtbot, hosts=hosts)
    panel.show()
    wait_checked(qtbot, panel)
    assert seen and all(thread != threading.get_ident() for thread in seen)


def test_refresh_reads_again_and_the_button_is_off_while_a_read_runs(qtbot):
    gate = threading.Event()
    run = RecordingRunner(gate=gate)
    panel = make_panel(qtbot, run=run)
    panel.show()
    qtbot.waitUntil(lambda: run.calls == 1, timeout=5000)
    assert button(panel).isEnabled() is False
    assert label(panel, "gpu-status").text() == LOADING_TEXT
    # A press while a read runs starts nothing.
    button(panel).click()
    assert run.calls == 1
    gate.set()
    qtbot.waitUntil(lambda: button(panel).isEnabled(), timeout=5000)
    wait_checked(qtbot, panel)
    button(panel).click()
    qtbot.waitUntil(lambda: run.calls == 2, timeout=5000)
    wait_checked(qtbot, panel)


def test_a_timer_reads_only_while_the_tab_is_visible(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    timer = panel.findChild(QTimer)
    assert timer is not None
    assert timer.interval() == REFRESH_MS
    assert timer.isActive() is False
    panel.show()
    assert timer.isActive() is True
    wait_checked(qtbot, panel)
    panel.hide()
    assert timer.isActive() is False


def test_a_tick_on_an_idle_tab_starts_a_read(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    assert run.calls == 1
    panel.findChild(QTimer).timeout.emit()
    qtbot.waitUntil(lambda: run.calls == 2, timeout=5000)
    wait_checked(qtbot, panel)


def test_refresh_while_a_read_runs_starts_nothing(qtbot):
    gate = threading.Event()
    run = RecordingRunner(gate=gate)
    panel = make_panel(qtbot, run=run)
    panel.show()
    qtbot.waitUntil(lambda: run.calls == 1, timeout=5000)
    panel.refresh()
    assert run.calls == 1
    gate.set()
    wait_checked(qtbot, panel)


def test_a_tick_while_a_read_runs_starts_nothing(qtbot):
    gate = threading.Event()
    run = RecordingRunner(gate=gate)
    panel = make_panel(qtbot, run=run)
    panel.show()
    qtbot.waitUntil(lambda: run.calls == 1, timeout=5000)
    panel.findChild(QTimer).timeout.emit()
    assert run.calls == 1
    gate.set()
    wait_checked(qtbot, panel)


def test_a_result_for_another_tab_is_dropped(qtbot):
    panel = make_panel(qtbot)
    other = gpu_jobs.GpuSnapshot(checked_at=NOW, statuses=(gpu_jobs.SourceStatus("x", "GPUs", "ok", "from elsewhere"),))
    with recording_deliveries() as emitted:
        emit(Delivered(object(), other))
    assert emitted  # the delivery did go out, to every page
    assert table(panel, "gpu-statuses").rowCount() == 0
    assert label(panel, "gpu-status").text() == NOT_READ_TEXT


def test_a_failed_read_says_so_plainly_and_the_button_comes_back(qtbot):
    secret = "SECRET-RUNNER-DETAIL"
    run = RecordingRunner(error=RuntimeError(secret))
    panel = make_panel(qtbot, run=run)
    panel.show()
    qtbot.waitUntil(lambda: button(panel).isEnabled(), timeout=5000)
    statuses = table(panel, "gpu-statuses")
    assert statuses.rowCount() == 1
    assert statuses.item(0, 2).text() == "The GPU read failed. Press Refresh to try again."
    visible = " ".join(label(panel, name).text() for name in ("gpu-status", "gpu-summary"))
    assert secret not in visible
    assert secret not in statuses.item(0, 2).text()


def test_an_empty_read_shows_the_empty_lines_and_no_rows(qtbot):
    run = RecordingRunner(
        answers={
            NVIDIA_GPU_ARGV: CommandResult(0, ""),
            NVIDIA_APPS_ARGV: CommandResult(0, ""),
            SQUEUE_ARGV: CommandResult(None, missing=True),
            BJOBS_ARGV: CommandResult(None, missing=True),
        }
    )
    panel = make_panel(qtbot, run=run, runpod=None)
    panel.show()
    wait_checked(qtbot, panel)
    assert table(panel, "gpu-gpus").rowCount() == 0
    assert not label(panel, "gpu-gpus-empty").isHidden()
    assert not label(panel, "gpu-jobs-empty").isHidden()
    assert label(panel, "gpu-summary").text() == "No GPUs were read."


def test_runpod_not_wired_is_shown_as_not_connected(qtbot):
    panel = make_panel(qtbot, runpod=None)
    panel.show()
    wait_checked(qtbot, panel)
    statuses = table(panel, "gpu-statuses")
    texts = [statuses.item(row, 2).text() for row in range(statuses.rowCount())]
    assert gpu_jobs.RUNPOD_NOT_CONNECTED_TEXT in texts


def test_a_runpod_provider_that_raises_is_not_connected(qtbot):
    def unreachable():
        raise OSError("connection refused")

    panel = make_panel(qtbot, runpod=unreachable)
    panel.show()
    wait_checked(qtbot, panel)
    statuses = table(panel, "gpu-statuses")
    texts = [statuses.item(row, 2).text() for row in range(statuses.rowCount())]
    assert gpu_jobs.RUNPOD_NOT_CONNECTED_TEXT in texts
    assert all("refused" not in text for text in texts)


def panel_owner(qtbot, panel):
    """The owner token this panel sends with its reads, taken from one read it has started."""
    with recording_deliveries() as emitted:
        panel.show()
        wait_checked(qtbot, panel)
    return emitted[0].owner


def test_a_non_delivery_object_is_ignored(qtbot):
    panel = make_panel(qtbot)
    owner = panel_owner(qtbot, panel)
    emit("not a delivery")
    emit(Delivered(owner, "not a snapshot"))
    assert label(panel, "gpu-status").text().startswith(CHECKED_PREFIX)
    assert table(panel, "gpu-statuses").rowCount() > 0


def test_a_local_part_for_this_tab_fills_the_tables_from_its_own_rows(qtbot):
    panel = make_panel(qtbot)
    owner = panel_owner(qtbot, panel)
    reading = Reading(
        gpus=gpu_jobs.parse_gpus(REAL_GPU_LINE, "gpu-box")[0],
        statuses=(gpu_jobs.SourceStatus("gpu-box", "GPUs", "ok", "1 GPU read."),),
    )
    emit(Delivered(owner, LocalPart(checked_at=NOW, hosts=(), reading=reading)))
    assert table(panel, "gpu-gpus").item(0, 0).text() == "gpu-box"
    assert label(panel, "gpu-status").text().startswith(CHECKED_PREFIX)


# ----------------------------------------------------------------- read-only and no sockets


def test_the_tab_has_one_control_and_no_write_controls(qtbot):
    panel = make_panel(qtbot)
    buttons = panel.findChildren(QPushButton)
    assert [widget.objectName() for widget in buttons] == ["gpu-refresh"]
    for widget_type in (QToolButton, QCheckBox, QRadioButton, QLineEdit, QComboBox, QAbstractSpinBox):
        assert panel.findChildren(widget_type) == [], widget_type.__name__


def test_every_table_is_read_only_and_its_cells_are_not_editable(qtbot):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    for name, columns in (("gpu-gpus", GPU_COLUMNS), ("gpu-jobs", JOB_COLUMNS), ("gpu-statuses", STATUS_COLUMNS)):
        widget = table(panel, name)
        assert widget.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers, name
        assert widget.columnCount() == len(columns), name
        for row in range(widget.rowCount()):
            for column in range(widget.columnCount()):
                item = widget.item(row, column)
                assert item is not None, (name, row, column)
                assert not (item.flags() & Qt.ItemFlag.ItemIsEditable), (name, row, column)


class FakeClock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowRemoteRunner(RecordingRunner):
    """The local reads run as usual. The ssh call to the host waits on ``gate`` and records that it started."""

    def __init__(self, gate):
        super().__init__()
        self.gate = gate
        self.ssh_started = threading.Event()
        self.ssh_calls = 0

    def __call__(self, argv, timeout):
        if argv[0] == "ssh":
            with self._lock:
                self.ssh_calls += 1
            self.ssh_started.set()
            self.gate.wait(10)
            return CommandResult(returncode=255, stderr="Could not resolve hostname gpu-box")
        return super().__call__(argv, timeout)


def test_the_local_tables_show_while_a_slow_host_is_still_being_read(qtbot):
    gate = threading.Event()
    run = SlowRemoteRunner(gate)
    panel = GpuJobsPanel(lambda: ("gpu-box",), None, run=run)
    qtbot.addWidget(panel)
    try:
        panel.show()
        qtbot.waitUntil(lambda: table(panel, "gpu-gpus").rowCount() == 1, timeout=5000)
        assert run.ssh_started.wait(5)
        # The local part is on screen, and the host has not reported yet.
        statuses = table(panel, "gpu-statuses")
        texts = [statuses.item(row, 0).text() for row in range(statuses.rowCount())]
        assert "gpu-box" not in texts
    finally:
        gate.set()
    qtbot.waitUntil(
        lambda: any(
            table(panel, "gpu-statuses").item(row, 0).text() == "gpu-box"
            for row in range(table(panel, "gpu-statuses").rowCount())
        ),
        timeout=5000,
    )


def test_a_remote_host_is_read_no_more_often_than_every_thirty_seconds(qtbot):
    clock = FakeClock()
    gate = threading.Event()
    gate.set()
    run = SlowRemoteRunner(gate)
    panel = GpuJobsPanel(lambda: ("gpu-box",), None, run=run, clock=clock)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: run.ssh_calls == 1, timeout=5000)
    # Five local ticks, five seconds apart, do not read the host again.
    for _ in range(5):
        clock.advance(gpu_jobs.REMOTE_REFRESH_SECONDS / 6)
        panel.findChild(QTimer).timeout.emit()
        qtbot.waitUntil(lambda: button(panel).isEnabled(), timeout=5000)
    qtbot.wait(50)
    assert run.ssh_calls == 1
    # The first tick at or after thirty seconds reads the host again.
    clock.advance(gpu_jobs.REMOTE_REFRESH_SECONDS / 6)
    panel.findChild(QTimer).timeout.emit()
    qtbot.waitUntil(lambda: run.ssh_calls == 2, timeout=5000)
    assert gpu_jobs.REMOTE_REFRESH_SECONDS == 30.0


def _apps_text(count: int) -> str:
    return "".join(
        f"{1000 + index}, python.exe, 10, GPU-85b6c4db-6eba-ae6e-4a6f-b1c6ebff448d\n" for index in range(count)
    )


def test_the_job_table_shows_at_most_two_hundred_rows_and_says_how_many_there_are(qtbot):
    run = RecordingRunner(
        answers={
            NVIDIA_GPU_ARGV: CommandResult(0, REAL_GPU_LINE),
            NVIDIA_APPS_ARGV: CommandResult(0, _apps_text(250)),
            SQUEUE_ARGV: CommandResult(None, missing=True),
            BJOBS_ARGV: CommandResult(None, missing=True),
        }
    )
    panel = make_panel(qtbot, run=run, runpod=None)
    panel.show()
    wait_checked(qtbot, panel)
    assert table(panel, "gpu-jobs").rowCount() == 200
    cap = label(panel, "gpu-jobs-cap")
    assert cap.isHidden() is False
    assert cap.text() == "Showing 200 of 250."


def test_an_unchanged_snapshot_does_not_rebuild_the_tables(qtbot, monkeypatch):
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run, runpod=None)
    rebuilds = []
    original = gpu_jobs_panel._fill

    def counting(widget, rows):
        rebuilds.append(widget.objectName())
        original(widget, rows)

    monkeypatch.setattr(gpu_jobs_panel, "_fill", counting)
    panel.show()
    wait_checked(qtbot, panel)
    first = len(rebuilds)
    assert first > 0
    # The same read again, with nothing changed, rebuilds no table.
    panel.findChild(QTimer).timeout.emit()
    qtbot.waitUntil(lambda: run.calls == 2, timeout=5000)
    qtbot.waitUntil(lambda: button(panel).isEnabled(), timeout=5000)
    assert len(rebuilds) == first
    # A changed read rebuilds again.
    run.answers[NVIDIA_GPU_ARGV] = CommandResult(0, REAL_GPU_LINE.replace("79, 1734", "80, 1800"))
    panel.findChild(QTimer).timeout.emit()
    qtbot.waitUntil(lambda: len(rebuilds) > first, timeout=5000)


def test_the_tab_opens_no_socket_and_starts_no_process_with_fakes(qtbot, monkeypatch):
    # The production code catches errors on purpose, so a raised exception alone could pass silently. Every attempt is
    # recorded as a hit, and the test fails on any hit, whether or not the code swallowed the error.
    hits = []

    def recording(name):
        def refuse(*args, **kwargs):
            hits.append(name)
            raise AssertionError("the GPUs tab must not reach the network or start a process in this test")

        return refuse

    monkeypatch.setattr(socket, "socket", recording("socket"))
    monkeypatch.setattr(socket, "create_connection", recording("create_connection"))
    monkeypatch.setattr(subprocess, "run", recording("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", recording("subprocess.Popen"))
    run = RecordingRunner()
    panel = make_panel(qtbot, run=run)
    panel.show()
    wait_checked(qtbot, panel)
    assert run.calls == 1
    assert hits == []


# ----------------------------------------------------------------- round 3: Refresh, removal, own pool, panel clock


DENIED_RESULT = CommandResult(returncode=255, stderr="user@gpu-box: Permission denied (publickey).")


class HostRunner(RecordingRunner):
    """Local reads as usual. Each ssh call is counted, and answers ``ssh_answer`` once ``gate`` opens, when there is one."""

    def __init__(self, ssh_answer=None, gate=None):
        super().__init__()
        self.ssh_answer = ssh_answer if ssh_answer is not None else CommandResult(returncode=255, stderr="Connection timed out")
        self.gate = gate
        self.ssh_calls = 0

    def __call__(self, argv, timeout):
        if argv[0] == "ssh":
            with self._lock:
                self.ssh_calls += 1
            if self.gate is not None:
                self.gate.wait(10)
            return self.ssh_answer
        return super().__call__(argv, timeout)


def _idle(qtbot, panel):
    qtbot.waitUntil(lambda: button(panel).isEnabled(), timeout=5000)


def _press_refresh(qtbot, panel):
    _idle(qtbot, panel)
    button(panel).click()


def _wait_reported(qtbot, panel, word):
    """Wait until a host's read has been shown. A Refresh does nothing while that host's read is still running."""
    qtbot.waitUntil(lambda: any(word in text for text in _status_texts(panel)), timeout=5000)


def _status_texts(panel):
    statuses = table(panel, "gpu-statuses")
    return [statuses.item(row, 2).text() for row in range(statuses.rowCount())]


def test_refresh_reads_remote_hosts_and_bypasses_the_thirty_second_spacing(qtbot):
    clock = FakeClock()
    run = HostRunner()
    panel = GpuJobsPanel(lambda: ("gpu-box",), None, run=run, clock=clock)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: run.ssh_calls == 1, timeout=5000)
    _wait_reported(qtbot, panel, "Timed out")
    # Five seconds later, inside the thirty-second spacing, Refresh still reads the host.
    clock.advance(5)
    _press_refresh(qtbot, panel)
    qtbot.waitUntil(lambda: run.ssh_calls == 2, timeout=5000)


def test_refresh_retries_a_host_at_most_once_per_thirty_seconds(qtbot):
    clock = FakeClock()
    run = HostRunner()
    panel = GpuJobsPanel(lambda: ("gpu-box",), None, run=run, clock=clock)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: run.ssh_calls == 1, timeout=5000)
    _wait_reported(qtbot, panel, "Timed out")
    clock.advance(5)
    _press_refresh(qtbot, panel)
    qtbot.waitUntil(lambda: run.ssh_calls == 2, timeout=5000)
    _wait_reported(qtbot, panel, "Timed out")
    clock.advance(5)
    _press_refresh(qtbot, panel)
    qtbot.wait(50)
    assert run.ssh_calls == 2  # the second Refresh, five seconds after the first, does not read the host again
    clock.advance(30)
    _press_refresh(qtbot, panel)
    qtbot.waitUntil(lambda: run.ssh_calls == 3, timeout=5000)


def test_refresh_clears_a_host_backoff_and_reads_it_again(qtbot):
    clock = FakeClock()
    run = HostRunner(ssh_answer=DENIED_RESULT)
    panel = GpuJobsPanel(lambda: ("gpu-box",), None, run=run, clock=clock)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: run.ssh_calls == 1, timeout=5000)
    _wait_reported(qtbot, panel, "Auth failed")
    clock.advance(5)
    _press_refresh(qtbot, panel)
    qtbot.waitUntil(lambda: run.ssh_calls == 2, timeout=5000)
    assert not any(text.startswith("Skipped for") for text in _status_texts(panel))


def test_removing_a_host_drops_its_backoff_so_a_re_added_host_starts_clean(qtbot):
    clock = FakeClock()
    run = HostRunner(ssh_answer=DENIED_RESULT)
    hosts = ["gpu-box"]
    panel = GpuJobsPanel(lambda: tuple(hosts), None, run=run, clock=clock)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: run.ssh_calls == 1, timeout=5000)
    # Removed in Settings. The next local read drops the host.
    hosts.clear()
    clock.advance(5)
    panel.findChild(QTimer).timeout.emit()
    _idle(qtbot, panel)
    # Re-added. Thirty-one seconds after the failed login the spacing allows a read, and the backoff is gone with the host.
    hosts.append("gpu-box")
    clock.advance(26)
    panel.findChild(QTimer).timeout.emit()
    qtbot.waitUntil(lambda: run.ssh_calls == 2, timeout=5000)
    assert not any(text.startswith("Skipped for") for text in _status_texts(panel))


def test_four_blocked_hosts_do_not_hold_back_the_local_tables(qtbot):
    """The local pool has two threads. Four remote hosts are blocked in SSH, and a second local read, started while all
    four are blocked, still delivers. If remote reads shared the local pool, four blocked reads would fill its two threads,
    the second local read would queue behind them, and the Refresh button would stay off."""
    gate = threading.Event()
    run = HostRunner(gate=gate)
    hosts = ("h0", "h1", "h2", "h3")
    panel = GpuJobsPanel(lambda: hosts, None, run=run)
    qtbot.addWidget(panel)
    try:
        panel.show()
        qtbot.waitUntil(lambda: run.ssh_calls == 4, timeout=5000)
        panel.findChild(QThreadPool, "gpu-local-pool").setMaxThreadCount(2)
        qtbot.waitUntil(lambda: table(panel, "gpu-gpus").rowCount() == 1, timeout=5000)
        # All four hosts are blocked now. A second local read must run and deliver.
        # Refresh asks for each blocked host again. The in-flight guard must refuse, so no host is called twice.
        _press_refresh(qtbot, panel)
        qtbot.waitUntil(lambda: run.calls == 2, timeout=5000)
        qtbot.waitUntil(lambda: button(panel).isEnabled(), timeout=5000)
    finally:
        gate.set()
    qtbot.waitUntil(lambda: sum(1 for text in _status_texts(panel) if "timed out" in text.lower()) >= 4, timeout=5000)
    # Released, and given a moment: the runner was called exactly once per host, four calls in all.
    qtbot.wait(200)
    assert run.ssh_calls == 4


def test_remote_reads_have_their_own_pool_sized_to_the_saved_hosts(qtbot):
    run = HostRunner()
    panel = GpuJobsPanel(lambda: ("h0", "h1", "h2"), None, run=run)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: run.ssh_calls == 3, timeout=5000)
    remote = panel.findChild(QThreadPool, "gpu-remote-pool")
    local = panel.findChild(QThreadPool, "gpu-local-pool")
    assert remote is not None and local is not None and remote is not local
    assert remote.maxThreadCount() == 3
    assert local.maxThreadCount() == 1


def test_the_remote_pool_is_capped_at_eight_threads(qtbot):
    hosts = tuple(f"h{index}" for index in range(gpu_jobs.MAX_HOSTS))
    run = HostRunner()
    panel = GpuJobsPanel(lambda: hosts, None, run=run)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: run.ssh_calls == gpu_jobs.MAX_HOSTS, timeout=5000)
    assert panel.findChild(QThreadPool, "gpu-remote-pool").maxThreadCount() == gpu_jobs.MAX_HOSTS


def test_the_panel_backoff_uses_the_panel_clock(qtbot):
    clock = FakeClock()
    run = HostRunner(ssh_answer=DENIED_RESULT)
    panel = GpuJobsPanel(lambda: ("gpu-box",), None, run=run, clock=clock)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: run.ssh_calls == 1, timeout=5000)
    # Thirty-one seconds on the panel's clock: the spacing allows a read, and the one-minute backoff still holds.
    clock.advance(31)
    panel.findChild(QTimer).timeout.emit()
    _idle(qtbot, panel)
    qtbot.wait(50)
    assert run.ssh_calls == 1
    assert any(text == "Skipped for 1 min after a failed login." for text in _status_texts(panel))
    # Sixty-one seconds on the panel's clock: the backoff has run out, so the host is read again.
    clock.advance(30)
    panel.findChild(QTimer).timeout.emit()
    qtbot.waitUntil(lambda: run.ssh_calls == 2, timeout=5000)


def test_three_hosts_are_read_at_the_same_time_on_the_panel_path(qtbot):
    # The barrier releases only when all three SSH calls are waiting together. Read one after another, the first call would
    # wait until the barrier times out, and no host would report "Timed out".
    barrier = threading.Barrier(3, timeout=5)

    class BarrierHosts(RecordingRunner):
        def __call__(self, argv, timeout):
            if argv[0] == "ssh":
                barrier.wait()
                return CommandResult(returncode=255, stderr="Connection timed out")
            return super().__call__(argv, timeout)

    panel = GpuJobsPanel(lambda: ("h0", "h1", "h2"), None, run=BarrierHosts())
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitUntil(lambda: sum(1 for text in _status_texts(panel) if "Timed out" in text) == 3, timeout=10000)
