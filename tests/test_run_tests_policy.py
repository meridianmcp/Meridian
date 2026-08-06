"""Unit tests for the adaptive test-runner policy."""

from scripts.run_tests import TestRunLock, build_run_args, parse_collected_count


def test_parse_collected_count_variants():
    assert parse_collected_count("37 tests collected in 1.2s") == 37
    assert parse_collected_count("collected 4 items") == 4
    assert parse_collected_count("collection failed") is None


def test_small_selection_is_serial_and_instrumented():
    args = build_run_args(["tests/test_one.py", "-q"], 40)
    assert "-p" in args and "no:xdist" in args
    assert "--durations=20" in args
    assert "--timeout=60" in args
    assert "-n" not in args


def test_large_selection_uses_worksteal_with_cap():
    args = build_run_args(["tests/", "-q"], 100, max_workers=6)
    assert args[-5:] == ["-n", "auto", "--dist=worksteal", "--maxprocesses", "6"]
    assert "--durations=20" in args


def test_lock_rejects_a_live_pid(tmp_path, monkeypatch):
    lock = TestRunLock(tmp_path)
    assert lock.acquire()
    second = TestRunLock(tmp_path)
    assert not second.acquire()
    lock.release()
