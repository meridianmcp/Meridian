"""Tests for 2cebf4ae -- durable observable test-run lifecycle with truthful
crash, hang, exit-code, and cleanup evidence.

Coverage:
  1. The Windows PID-liveness fix: os.kill(pid, 0) on Windows raised
     OSError: [WinError 87] for a dead pid instead of ProcessLookupError,
     which used to crash TestRunLock.acquire()'s stale-lock reclaim instead
     of correctly reclaiming it. _win32_pid_is_running is tested directly
     via an injectable fake kernel32 probe (never touches a real
     ctypes.WinDLL, so this is exercisable on any platform) plus one
     real-OS regression check guarded to actual Windows.
  2. TestRunLock: stale-lock reclaim (now works reliably), live-duplicate
     rejection, and the explicit --supersede takeover path.
  3. TestRunTracker: state-transition persistence, progress/tail tracking,
     results-summary detection, and JSON round-trip.
  4. _run_pytest_observed: the stalled -> timed_out(stalled) escalation,
     wall-clock timeout, the post-results-hang classification (the exact
     motivating bug: results printed, process never exits), honest exit
     code preservation across passed/failed/crashed, and cleanup receipts
     for orphaned children -- all driven by an injected fake clock/sleep/
     Popen so no test needs a real sleep or a real subprocess.
  5. meridian.orphan_reaper's new public tree-kill/collect/survivor
     wrappers.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

import scripts.run_tests as rt
from meridian import orphan_reaper


# ---------------------------------------------------------------------------
# 1. Windows PID-liveness fix
# ---------------------------------------------------------------------------


class _FakeWin32Probe:
    def __init__(self, *, handle=None, last_error=0):
        self._handle = handle
        self._last_error = last_error
        self.opened_pid = None
        self.closed_handle = None

    def open_process(self, pid):
        self.opened_pid = pid
        return self._handle

    def close_handle(self, handle):
        self.closed_handle = handle
        return True

    def get_last_error(self):
        return self._last_error


def test_win32_pid_is_running_dead_pid_via_error_invalid_parameter():
    """The actual regression: OpenProcess returns NULL with
    ERROR_INVALID_PARAMETER (87) for a pid that does not exist -- must be
    treated as dead (False), never raise."""
    probe = _FakeWin32Probe(handle=None, last_error=87)
    assert rt._win32_pid_is_running(999999, probe_loader=lambda: probe) is False
    assert probe.opened_pid == 999999


def test_win32_pid_is_running_alive_pid_via_handle():
    probe = _FakeWin32Probe(handle=1234, last_error=0)
    assert rt._win32_pid_is_running(42, probe_loader=lambda: probe) is True
    assert probe.closed_handle == 1234


def test_win32_pid_is_running_access_denied_treated_as_alive():
    """ERROR_ACCESS_DENIED (5) means the process exists but we lack rights
    to query it -- alive, not dead."""
    probe = _FakeWin32Probe(handle=None, last_error=5)
    assert rt._win32_pid_is_running(4, probe_loader=lambda: probe) is True


def test_win32_pid_is_running_unknown_error_fails_safe_alive():
    probe = _FakeWin32Probe(handle=None, last_error=999)
    assert rt._win32_pid_is_running(4, probe_loader=lambda: probe) is True


def test_win32_pid_is_running_no_probe_available_fails_safe_alive():
    assert rt._win32_pid_is_running(4, probe_loader=lambda: None) is True


def test_win32_pid_is_running_probe_raises_fails_safe_alive():
    def _raising_loader():
        raise RuntimeError("boom")

    assert rt._win32_pid_is_running(4, probe_loader=_raising_loader) is True


@pytest.mark.skipif(sys.platform != "win32", reason="real OpenProcess probe is Windows-only")
def test_pid_is_running_dead_pid_real_os():
    """Real-OS regression check for the actual reported bug: a definitely
    nonexistent pid must resolve to False, not raise WinError 87."""
    assert rt._pid_is_running(999_999_999) is False


def test_pid_is_running_self_pid_is_alive():
    import os

    assert rt._pid_is_running(os.getpid()) is True


def test_pid_is_running_non_positive_pid_is_dead():
    assert rt._pid_is_running(0) is False
    assert rt._pid_is_running(-5) is False


def test_pid_is_running_posix_path_dead(monkeypatch):
    """Forces the no-psutil, non-Windows branch deterministically."""
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(rt.sys, "platform", "linux")

    def _fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(rt.os, "kill", _fake_kill)
    assert rt._pid_is_running(4321) is False


def test_pid_is_running_posix_path_permission_denied_is_alive(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(rt.sys, "platform", "linux")

    def _fake_kill(pid, sig):
        raise PermissionError()

    monkeypatch.setattr(rt.os, "kill", _fake_kill)
    assert rt._pid_is_running(4321) is True


# ---------------------------------------------------------------------------
# 2. TestRunLock: stale reclaim, live-duplicate rejection, supersede
# ---------------------------------------------------------------------------


def test_lock_rejects_a_live_pid(tmp_path):
    """Pre-existing behavior must survive unchanged."""
    lock = rt.TestRunLock(tmp_path)
    assert lock.acquire()
    second = rt.TestRunLock(tmp_path)
    assert not second.acquire()
    assert second.owner_pid == lock.owner_pid
    lock.release()


def test_lock_reclaims_a_stale_lock_from_a_dead_pid(tmp_path, monkeypatch):
    """The actual bug this item fixes: a lock file left behind by a
    force-killed run (dead owner pid) must be reliably reclaimed, not
    crash acquisition. Simulated here by controlling _pid_is_running
    directly so the test is platform-independent."""
    dead_pid = 918273

    def _fake_pid_is_running(pid):
        return pid != dead_pid

    monkeypatch.setattr(rt, "_pid_is_running", _fake_pid_is_running)

    lock = rt.TestRunLock(tmp_path)
    lock.path.write_text(f"{dead_pid}\t1234.0\t/some/old/cwd\n", encoding="utf-8")

    assert lock.acquire() is True
    assert lock.reclaimed_stale_pid == dead_pid
    lock.release()


def test_lock_owner_pid_is_confirmed_alive_reflects_real_liveness(tmp_path, monkeypatch):
    lock = rt.TestRunLock(tmp_path)
    lock.owner_pid = 55
    monkeypatch.setattr(rt, "_pid_is_running", lambda pid: pid == 55)
    assert lock.owner_pid_is_confirmed_alive() is True
    lock.owner_pid = 56
    assert lock.owner_pid_is_confirmed_alive() is False


def test_lock_reclaim_after_supersede_forces_deletion(tmp_path):
    lock = rt.TestRunLock(tmp_path)
    lock.path.write_text("123\t1.0\t/x\n", encoding="utf-8")
    assert lock.reclaim_after_supersede() is True
    assert lock.acquired is True
    lock.release()


def test_print_duplicate_report_with_existing_record(tmp_path, capsys):
    record = rt.TestRunRecord(run_id="r1", state=rt.STATE_RUNNING, started_at="t0")
    lock = rt.TestRunLock(tmp_path)
    lock.owner_pid = 77
    rt._print_duplicate_report(record, lock)
    captured = capsys.readouterr()
    assert "r1" in captured.err
    assert "running" in captured.err
    assert "77" in captured.err
    assert "--supersede" in captured.err


def test_print_duplicate_report_without_existing_record(tmp_path, capsys):
    lock = rt.TestRunLock(tmp_path)
    lock.owner_pid = 88
    rt._print_duplicate_report(None, lock)
    captured = capsys.readouterr()
    assert "no run-state record found" in captured.err
    assert "88" in captured.err


# ---------------------------------------------------------------------------
# 3. TestRunTracker
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_tracker_state_transitions_persist_atomically(tmp_path):
    clock = _FakeClock()
    state_path = tmp_path / "run.state.json"
    tracker = rt.TestRunTracker(state_path, run_id="run-1", clock=clock)

    tracker.mark_queued()
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["state"] == rt.STATE_QUEUED
    assert on_disk["run_id"] == "run-1"

    tracker.mark_starting(command=["tests/"], cwd=str(tmp_path), owner_session="sess-1")
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["state"] == rt.STATE_STARTING
    assert on_disk["command"] == ["tests/"]
    assert on_disk["owner_session"] == "sess-1"
    assert on_disk["started_at"] is not None

    tracker.mark_collecting()
    assert json.loads(state_path.read_text(encoding="utf-8"))["state"] == rt.STATE_COLLECTING

    tracker.mark_running(pid=4242, worker_count=4)
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["state"] == rt.STATE_RUNNING
    assert on_disk["pid"] == 4242
    assert on_disk["worker_count"] == 4

    tracker.mark_terminal(rt.STATE_PASSED, exit_code=0)
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["state"] == rt.STATE_PASSED
    assert on_disk["exit_code"] == 0
    assert on_disk["terminal_at"] is not None


def test_tracker_mark_terminal_rejects_non_terminal_state(tmp_path):
    tracker = rt.TestRunTracker(tmp_path / "run.state.json")
    with pytest.raises(ValueError):
        tracker.mark_terminal(rt.STATE_RUNNING)


def test_tracker_note_output_updates_progress_and_bounds_tail(tmp_path):
    clock = _FakeClock()
    tracker = rt.TestRunTracker(tmp_path / "run.state.json", clock=clock)
    tracker.mark_running(pid=1, worker_count=1)
    before = tracker.record.last_progress_monotonic

    clock.advance(5)
    tracker.note_output("stdout", "collecting tests...\n")
    assert tracker.record.last_progress_monotonic == before + 5

    huge = "x" * (rt._TAIL_CHARS + 500)
    tracker.note_output("stdout", huge)
    assert len(tracker.record.stdout_tail) == rt._TAIL_CHARS


def test_tracker_detects_results_summary_line_and_parses_counts(tmp_path):
    clock = _FakeClock()
    tracker = rt.TestRunTracker(tmp_path / "run.state.json", clock=clock)
    tracker.mark_running(pid=1, worker_count=1)

    tracker.note_output("stdout", "collecting 12 items\n")
    assert tracker.record.results_line_seen is False

    tracker.note_output("stdout", "===== 5 passed, 1 failed in 2.34s =====\n")
    assert tracker.record.results_line_seen is True
    assert tracker.record.passed == 5
    assert tracker.record.failed == 1
    assert tracker.record.results_seen_monotonic == clock.now


def test_tracker_load_record_roundtrip(tmp_path):
    state_path = tmp_path / "run.state.json"
    tracker = rt.TestRunTracker(state_path, run_id="abc")
    tracker.mark_running(pid=99, worker_count=2)

    loaded = rt.TestRunTracker.load_record(state_path)
    assert loaded is not None
    assert loaded.run_id == "abc"
    assert loaded.pid == 99
    assert loaded.state == rt.STATE_RUNNING


def test_tracker_load_record_missing_file_returns_none(tmp_path):
    assert rt.TestRunTracker.load_record(tmp_path / "nope.json") is None


def test_tracker_load_record_corrupt_json_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert rt.TestRunTracker.load_record(p) is None


# ---------------------------------------------------------------------------
# 4. _run_pytest_observed -- stalled/timed_out/post-results-hang/crash,
#    all driven by a fake clock/sleep/Popen, no real waiting.
# ---------------------------------------------------------------------------


class _FakeProc:
    """Never exits on its own -- used for the stall/wall-timeout paths."""

    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class _FakeExitingProc:
    """Exits with a fixed code after N poll() calls."""

    def __init__(self, pid, code, calls_before_exit=2):
        self.pid = pid
        self.returncode = None
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self._calls = 0
        self._code = code
        self._calls_before_exit = calls_before_exit

    def poll(self):
        self._calls += 1
        if self._calls >= self._calls_before_exit:
            self.returncode = self._code
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture(autouse=True)
def _stub_cleanup_and_integrations(monkeypatch):
    """Every _run_pytest_observed test drives its own fake process tree --
    never touch real psutil/meridian integrations or sleep for real."""
    monkeypatch.setattr(rt, "_kill_process_tree", lambda pid: True)
    monkeypatch.setattr(rt, "_report_survivors", lambda pids: [])
    monkeypatch.setattr(rt, "_process_tree_pids", lambda pid: [pid] if pid else [])
    monkeypatch.setattr(rt, "_maybe_heartbeat_owner_session", lambda: None)
    monkeypatch.setattr(rt, "_maybe_register_process_lease", lambda pid, run_id: (lambda: None))


def _make_tracker(tmp_path, clock):
    tracker = rt.TestRunTracker(tmp_path / "run.state.json", run_id="run-x", clock=clock)
    tracker.mark_starting(command=["tests/"], cwd=str(tmp_path))
    return tracker


def test_stalled_escalates_to_timed_out_stalled(tmp_path):
    clock = _FakeClock()
    tracker = _make_tracker(tmp_path, clock)
    proc = _FakeProc()

    def _sleep(secs):
        clock.advance(secs)

    code = rt._run_pytest_observed(
        ["tests/"], tracker,
        wall_timeout=100_000, stall_timeout=5, post_results_grace=100_000,
        max_workers=4, poll_interval=1.0,
        popen_factory=lambda cmd, **kw: proc, sleep_fn=_sleep, clock=clock,
    )

    assert code == 124
    assert tracker.record.state == rt.STATE_TIMED_OUT
    assert tracker.record.timeout_kind == "stalled"
    assert tracker.record.cleanup["attempted"] is True
    assert tracker.record.cleanup["method"] == "process_tree_kill"


def test_wall_clock_timeout_fires_even_with_fresh_progress(tmp_path):
    """A wall-clock timeout must fire regardless of recent progress -- it is
    a SEPARATE budget from the stall/no-progress timeout."""
    clock = _FakeClock()
    tracker = _make_tracker(tmp_path, clock)
    proc = _FakeProc()

    calls = {"n": 0}

    def _sleep(secs):
        clock.advance(secs)
        calls["n"] += 1
        # Keep "faking" fresh progress every tick so the stall timeout
        # would never fire on its own -- only the wall-clock budget should.
        tracker.note_output("stdout", f"still going {calls['n']}\n")

    code = rt._run_pytest_observed(
        ["tests/"], tracker,
        wall_timeout=5, stall_timeout=100_000, post_results_grace=100_000,
        max_workers=4, poll_interval=1.0,
        popen_factory=lambda cmd, **kw: proc, sleep_fn=_sleep, clock=clock,
    )

    assert code == 124
    assert tracker.record.state == rt.STATE_TIMED_OUT
    assert tracker.record.timeout_kind == "wall_clock"


def test_post_results_hang_uses_parsed_counts_for_terminal_state(tmp_path):
    """The exact motivating bug: pytest prints its results summary and then
    hangs on exit. Must be classified from the parsed counts, never left
    as an empty/ambiguous result."""
    clock = _FakeClock()
    tracker = _make_tracker(tmp_path, clock)
    proc = _FakeProc()

    # Pre-seed the "results already printed" signal the way a real reader
    # thread would via note_output -- deterministic instead of racing a
    # background thread in the test.
    tracker.record.results_line_seen = True
    tracker.record.passed = 5
    tracker.record.failed = 0

    def _sleep(secs):
        clock.advance(secs)
        if tracker.record.results_seen_monotonic is None:
            tracker.record.results_seen_monotonic = clock.now

    code = rt._run_pytest_observed(
        ["tests/"], tracker,
        wall_timeout=100_000, stall_timeout=100_000, post_results_grace=2,
        max_workers=4, poll_interval=1.0,
        popen_factory=lambda cmd, **kw: proc, sleep_fn=_sleep, clock=clock,
    )

    assert tracker.record.state == rt.STATE_PASSED
    assert tracker.record.timeout_kind == "post_results_hang"
    assert code == 0


def test_post_results_hang_classifies_failed_from_parsed_counts(tmp_path):
    clock = _FakeClock()
    tracker = _make_tracker(tmp_path, clock)
    proc = _FakeProc()
    tracker.record.results_line_seen = True
    tracker.record.passed = 4
    tracker.record.failed = 2

    def _sleep(secs):
        clock.advance(secs)
        if tracker.record.results_seen_monotonic is None:
            tracker.record.results_seen_monotonic = clock.now

    code = rt._run_pytest_observed(
        ["tests/"], tracker,
        wall_timeout=100_000, stall_timeout=100_000, post_results_grace=2,
        max_workers=4, poll_interval=1.0,
        popen_factory=lambda cmd, **kw: proc, sleep_fn=_sleep, clock=clock,
    )

    assert tracker.record.state == rt.STATE_FAILED
    assert tracker.record.timeout_kind == "post_results_hang"
    assert code == 1


@pytest.mark.parametrize(
    "returncode,expected_state,expected_exit_code",
    [
        (0, rt.STATE_PASSED, 0),
        (1, rt.STATE_FAILED, 1),
        (3, rt.STATE_CRASHED, 3),  # pytest INTERNALERROR
        (4, rt.STATE_CRASHED, 4),  # pytest USAGE_ERROR
        (2, rt.STATE_CANCELLED, None),
    ],
)
def test_natural_exit_classification_preserves_real_exit_code(
    tmp_path, returncode, expected_state, expected_exit_code
):
    clock = _FakeClock()
    tracker = _make_tracker(tmp_path, clock)
    proc = _FakeExitingProc(pid=777, code=returncode)

    def _sleep(secs):
        clock.advance(secs)

    code = rt._run_pytest_observed(
        ["tests/"], tracker,
        wall_timeout=100_000, stall_timeout=100_000, post_results_grace=100_000,
        max_workers=4, poll_interval=1.0,
        popen_factory=lambda cmd, **kw: proc, sleep_fn=_sleep, clock=clock,
    )

    assert tracker.record.state == expected_state
    assert tracker.record.exit_code == returncode
    if expected_state == rt.STATE_CANCELLED:
        assert code == 130
    else:
        assert code == expected_exit_code


def test_natural_exit_negative_returncode_is_crashed_with_signal(tmp_path):
    """A negative returncode on POSIX means the child was killed by a
    signal -- must be reported as CRASHED with the signal number, exit_code
    left honestly None (never fabricated)."""
    clock = _FakeClock()
    tracker = _make_tracker(tmp_path, clock)
    proc = _FakeExitingProc(pid=888, code=-9)

    def _sleep(secs):
        clock.advance(secs)

    code = rt._run_pytest_observed(
        ["tests/"], tracker,
        wall_timeout=100_000, stall_timeout=100_000, post_results_grace=100_000,
        max_workers=4, poll_interval=1.0,
        popen_factory=lambda cmd, **kw: proc, sleep_fn=_sleep, clock=clock,
    )

    assert tracker.record.state == rt.STATE_CRASHED
    assert tracker.record.signal == 9
    assert tracker.record.exit_code is None
    assert code == 1


def test_spawn_failure_is_crashed_never_raises(tmp_path):
    clock = _FakeClock()
    tracker = _make_tracker(tmp_path, clock)

    def _boom(cmd, **kw):
        raise OSError("no such executable")

    code = rt._run_pytest_observed(
        ["tests/"], tracker,
        wall_timeout=10, stall_timeout=10, post_results_grace=10,
        max_workers=4, popen_factory=_boom, sleep_fn=lambda s: None, clock=clock,
    )
    assert code == 1
    assert tracker.record.state == rt.STATE_CRASHED
    assert "no such executable" in tracker.record.error


def test_cleanup_receipt_reports_survivors_without_touching_unrelated_pids(tmp_path, monkeypatch):
    """Cleanup receipt must report which OWNED pids survived a kill attempt
    -- and _report_survivors/_kill_process_tree must only ever be called
    with pids drawn from this run's own recorded process_tree, never a
    broader/unrelated set."""
    clock = _FakeClock()
    tracker = _make_tracker(tmp_path, clock)
    proc = _FakeProc()

    seen_kill_pids = []
    seen_survivor_calls = []

    monkeypatch.setattr(rt, "_process_tree_pids", lambda pid: [pid, pid + 1, pid + 2])
    monkeypatch.setattr(rt, "_kill_process_tree", lambda pid: (seen_kill_pids.append(pid), True)[1])
    monkeypatch.setattr(
        rt, "_report_survivors",
        lambda pids: (seen_survivor_calls.append(list(pids)), [pids[0]])[1],
    )

    def _sleep(secs):
        clock.advance(secs)

    code = rt._run_pytest_observed(
        ["tests/"], tracker,
        wall_timeout=100_000, stall_timeout=2, post_results_grace=100_000,
        max_workers=4, poll_interval=1.0,
        popen_factory=lambda cmd, **kw: proc, sleep_fn=_sleep, clock=clock,
    )

    assert code == 124
    assert tracker.record.state == rt.STATE_TIMED_OUT
    assert seen_kill_pids == [proc.pid]
    # Every survivor check was scoped to this run's own recorded tree.
    for call in seen_survivor_calls:
        assert set(call).issubset({proc.pid, proc.pid + 1, proc.pid + 2})
    assert tracker.record.cleanup["survivors"] == [proc.pid]


# ---------------------------------------------------------------------------
# 5. orphan_reaper public wrappers
# ---------------------------------------------------------------------------


def test_kill_process_tree_wrapper_delegates(monkeypatch):
    called = {}

    def _fake(pid):
        called["pid"] = pid
        return True

    monkeypatch.setattr(orphan_reaper, "_psutil_kill_tree", _fake)
    assert orphan_reaper.kill_process_tree(4242) is True
    assert called["pid"] == 4242


def test_collect_process_tree_wrapper_delegates(monkeypatch):
    monkeypatch.setattr(orphan_reaper, "_psutil_collect_tree", lambda pid: [pid, pid + 1])
    assert orphan_reaper.collect_process_tree(10) == [10, 11]


def test_report_tree_survivors_only_checks_given_pids(monkeypatch):
    class _FakePsutil:
        @staticmethod
        def pid_exists(pid):
            return pid in (1, 3)

    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    assert orphan_reaper.report_tree_survivors([1, 2, 3, 4]) == [1, 3]


def test_report_tree_survivors_no_psutil_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert orphan_reaper.report_tree_survivors([1, 2, 3]) == []
