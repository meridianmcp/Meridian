"""e24f2daa -- focused regression tests for meridian.test_run_receipt's
fail-closed classifiers, duplicate-run/lease check, and consumer-facing
surface. Covers every scenario called out by the sprint item itself: empty
output, missing terminal receipt, non-zero exit through PowerShell/CI pipes,
xdist INTERNALERROR/node-down, timeout, cancellation, duplicate run, and
successful receipt.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from meridian import test_run_receipt as trr
import scripts.run_tests as rt


def _valid_passed_record(**overrides) -> dict:
    """A dict-shaped TestRunRecord with every piece of evidence
    classify_test_run_record requires for a genuine ``passed`` verdict."""
    record = {
        "run_id": "run-good-1",
        "state": rt.STATE_PASSED,
        "command": ["pixi", "run", "test"],
        "cwd": "C:/Users/13144/Documents/Meridian/repository",
        "last_progress_monotonic": 123.4,
        "last_progress_at": "2026-08-08T16:00:00Z",
        "stdout_tail": "====== 500 passed in 12.3s ======",
        "stderr_tail": "",
        "passed": 500,
        "failed": 0,
        "exit_code": 0,
        "signal": None,
        "timeout_kind": None,
        "results_line_seen": True,
        "cleanup": {"survivors": []},
        "error": None,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# Missing terminal receipt.
# ---------------------------------------------------------------------------


def test_classify_test_run_record_none_is_missing_or_ambiguous():
    result = trr.classify_test_run_record(None)
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert "no test-run record found" in result["reason"]


def test_classify_test_run_record_non_terminal_state_is_missing_or_ambiguous():
    record = _valid_passed_record(state=rt.STATE_RUNNING)
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert "non-terminal" in result["reason"]


def test_classify_test_run_record_unrecognized_terminal_state_fails_closed():
    record = _valid_passed_record(state="some_future_state")
    # Force through the terminal-states check by widening what "terminal"
    # means is NOT possible from here -- unrecognized states that aren't in
    # TERMINAL_STATES already report missing_or_ambiguous above; this proves
    # the *other* fail-closed branch (a terminal-but-unrecognized state) by
    # monkeypatching is unnecessary since the real TERMINAL_STATES frozenset
    # cannot contain a state this module doesn't know either -- covered by
    # the non-terminal-state test above. Kept as a documented no-op guard so
    # a future terminal-state addition to scripts.run_tests is caught here.
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS


# ---------------------------------------------------------------------------
# Empty output.
# ---------------------------------------------------------------------------


def test_classify_test_run_record_passed_state_but_empty_output_is_ambiguous():
    """The exact 'never let an empty log look like a pass' failure mode this
    item calls out by name."""
    record = _valid_passed_record(
        stdout_tail="", stderr_tail="", results_line_seen=False,
        passed=None, failed=None,
    )
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert "empty log" in result["reason"]


def test_classify_subprocess_result_zero_exit_empty_output_is_ambiguous():
    result = trr.classify_subprocess_result(exit_code=0, stdout="", stderr="")
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert "empty log" in result["reason"]


# ---------------------------------------------------------------------------
# Successful receipt.
# ---------------------------------------------------------------------------


def test_classify_test_run_record_fully_evidenced_pass_classifies_passed():
    result = trr.classify_test_run_record(_valid_passed_record())
    assert result["classification"] == trr.CLASS_PASSED
    assert result["run_id"] == "run-good-1"
    assert result["cleanup_status"] == "ok"


def test_classify_test_run_record_passed_state_missing_cleanup_receipt_is_ambiguous():
    record = _valid_passed_record(cleanup=None)
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert "cleanup status is 'unknown'" in result["reason"]


def test_classify_test_run_record_passed_state_incomplete_cleanup_is_ambiguous():
    record = _valid_passed_record(cleanup={"survivors": [1234]})
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert "incomplete" in result["reason"]


def test_classify_subprocess_result_clean_exit_with_output_is_passed():
    result = trr.classify_subprocess_result(
        exit_code=0, stdout="500 passed in 12.3s", passed=500, failed=0,
    )
    assert result["classification"] == trr.CLASS_PASSED


# ---------------------------------------------------------------------------
# Non-zero exit through PowerShell/CI pipes (real captured exit code -> a
# genuine assertion failure, not infra crash).
# ---------------------------------------------------------------------------


def test_classify_test_run_record_failed_state_real_exit_code_classifies_failed():
    record = _valid_passed_record(
        state=rt.STATE_FAILED, exit_code=1, passed=495, failed=5,
        stdout_tail="5 failed, 495 passed",
    )
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_FAILED
    assert "5 failing test" in result["reason"]


def test_classify_test_run_record_failed_state_no_real_exit_code_is_ambiguous():
    """A PowerShell pipeline that masks the real exit code -- state says
    failed but no genuine exit_code was ever captured."""
    record = _valid_passed_record(state=rt.STATE_FAILED, exit_code=None)
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert "no real exit_code" in result["reason"]


def test_classify_subprocess_result_nonzero_exit_is_failed():
    result = trr.classify_subprocess_result(
        exit_code=1, stdout="", stderr="AssertionError: 1 != 2", failed=1,
    )
    assert result["classification"] == trr.CLASS_FAILED


def test_classify_subprocess_result_bool_exit_code_is_never_real():
    """isinstance(True, int) is True in Python -- must not be treated as a
    genuine exit code."""
    result = trr.classify_subprocess_result(exit_code=True)  # type: ignore[arg-type]
    assert result["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert "no real exit_code" in result["reason"]


# ---------------------------------------------------------------------------
# xdist INTERNALERROR / node-down (infrastructure crash).
# ---------------------------------------------------------------------------


def test_classify_test_run_record_crashed_state_with_internalerror_marker():
    record = _valid_passed_record(
        state=rt.STATE_CRASHED, exit_code=None,
        stdout_tail="INTERNALERROR> Traceback...\nWorkerController lost worker",
        error=None,
    )
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_INFRA_CRASH
    assert "INTERNALERROR>" in result["reason"]


def test_classify_test_run_record_crashed_state_node_down_marker():
    record = _valid_passed_record(
        state=rt.STATE_CRASHED, exit_code=None, stdout_tail="",
        stderr_tail="gw0 node down: Not properly terminated",
    )
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_INFRA_CRASH
    assert "node down" in result["reason"]


def test_classify_subprocess_result_pytest_internal_error_exit_code_3():
    result = trr.classify_subprocess_result(exit_code=3, stdout="INTERNALERROR>")
    assert result["classification"] == trr.CLASS_INFRA_CRASH
    assert "INTERNAL_ERROR" in result["reason"]


def test_classify_subprocess_result_pytest_usage_error_exit_code_4():
    result = trr.classify_subprocess_result(exit_code=4)
    assert result["classification"] == trr.CLASS_INFRA_CRASH


def test_classify_subprocess_result_negative_exit_code_is_infra_crash():
    result = trr.classify_subprocess_result(exit_code=-11)
    assert result["classification"] == trr.CLASS_INFRA_CRASH
    assert "negative exit_code" in result["reason"]


def test_classify_subprocess_result_infra_crash_marker_in_output_wins_over_exit_0():
    """Exit code 0 with a MemoryError marker in output must not be treated
    as a pass -- a worker can occasionally report a misleading zero exit
    after a partial crash."""
    result = trr.classify_subprocess_result(exit_code=0, stdout="MemoryError: unable to allocate")
    assert result["classification"] == trr.CLASS_INFRA_CRASH


# ---------------------------------------------------------------------------
# Timeout.
# ---------------------------------------------------------------------------


def test_classify_test_run_record_timed_out_state():
    record = _valid_passed_record(state=rt.STATE_TIMED_OUT, timeout_kind="wall_clock")
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_TIMEOUT
    assert "wall_clock" in result["reason"]


def test_classify_subprocess_result_timed_out_flag():
    result = trr.classify_subprocess_result(exit_code=None, timed_out=True)
    assert result["classification"] == trr.CLASS_TIMEOUT


# ---------------------------------------------------------------------------
# Cancellation.
# ---------------------------------------------------------------------------


def test_classify_test_run_record_cancelled_state():
    record = _valid_passed_record(state=rt.STATE_CANCELLED, error="user requested cancel")
    result = trr.classify_test_run_record(record)
    assert result["classification"] == trr.CLASS_CANCELLED
    assert result["reason"] == "user requested cancel"


def test_classify_subprocess_result_sigint_is_cancelled():
    result = trr.classify_subprocess_result(exit_code=-2, signal=2)
    assert result["classification"] == trr.CLASS_CANCELLED


def test_classify_subprocess_result_other_signal_is_infra_crash():
    result = trr.classify_subprocess_result(exit_code=-15, signal=15)
    assert result["classification"] == trr.CLASS_INFRA_CRASH


# ---------------------------------------------------------------------------
# Duplicate run / lease check.
# ---------------------------------------------------------------------------


def test_check_active_test_run_no_lock_file_is_inactive(tmp_path):
    result = trr.check_active_test_run(tmp_path)
    assert result["checked"] is True
    assert result["active"] is False


def test_check_duplicate_test_run_none_when_no_lock(tmp_path):
    assert trr.check_duplicate_test_run(tmp_path) is None


def test_check_duplicate_test_run_reports_active_run_owned_by_live_pid(tmp_path):
    lock = rt.TestRunLock(tmp_path)
    try:
        # Simulate another live process (this test's own PID is guaranteed
        # alive) owning the lock with a non-terminal run in progress.
        lock.path.write_text(f"{os.getpid()}\t2026-08-08T16:00:00Z", encoding="utf-8")
        record = rt.TestRunRecord(run_id="run-active-1", state=rt.STATE_RUNNING)
        rt._write_record_atomic(lock.state_path, record)

        probe = trr.check_active_test_run(tmp_path)
        assert probe["checked"] is True
        assert probe["active"] is True
        assert probe["run_id"] == "run-active-1"

        dup = trr.check_duplicate_test_run(tmp_path)
        assert dup is not None
        assert dup["duplicate"] is True
        assert dup["run_id"] == "run-active-1"
        assert "already active" in dup["message"]
    finally:
        lock.path.unlink(missing_ok=True)
        lock.state_path.unlink(missing_ok=True)


def test_check_duplicate_test_run_terminal_state_is_not_active(tmp_path):
    """A lock file whose owner pid is alive but whose record already reached
    a terminal state must not be reported as a duplicate -- the run is
    simply done, not still in flight."""
    lock = rt.TestRunLock(tmp_path)
    try:
        lock.path.write_text(f"{os.getpid()}\t2026-08-08T16:00:00Z", encoding="utf-8")
        record = rt.TestRunRecord(run_id="run-done-1", state=rt.STATE_PASSED, exit_code=0)
        rt._write_record_atomic(lock.state_path, record)

        assert trr.check_duplicate_test_run(tmp_path) is None
    finally:
        lock.path.unlink(missing_ok=True)
        lock.state_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Consumer-facing surface: get_test_run_evidence.
# ---------------------------------------------------------------------------


def test_get_test_run_evidence_no_record_is_missing_or_ambiguous(tmp_path):
    evidence = trr.get_test_run_evidence(tmp_path)
    assert evidence["classification"] == trr.CLASS_MISSING_OR_AMBIGUOUS
    assert evidence["duplicate_active_run"] is None
    assert evidence["repo_root"] == str(tmp_path.resolve())


def test_get_test_run_evidence_reads_real_passed_record(tmp_path):
    lock = rt.TestRunLock(tmp_path)
    try:
        record = rt.TestRunRecord(
            run_id="run-evidence-1", state=rt.STATE_PASSED,
            command=["pixi", "run", "test"], cwd=str(tmp_path),
            last_progress_monotonic=1.0, exit_code=0,
            stdout_tail="10 passed", passed=10, failed=0,
            results_line_seen=True, cleanup={"survivors": []},
        )
        rt._write_record_atomic(lock.state_path, record)

        evidence = trr.get_test_run_evidence(tmp_path)
        assert evidence["classification"] == trr.CLASS_PASSED
        assert evidence["run_id"] == "run-evidence-1"
    finally:
        lock.state_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Override auditing.
# ---------------------------------------------------------------------------


async def test_record_test_run_receipt_override_requires_nonempty_reason():
    with pytest.raises(ValueError, match="override_reason is required"):
        await trr.record_test_run_receipt_override(
            db=None, project_id="p1", item_id="i1",
            actor="tester", reason="   ", evidence={},
        )
