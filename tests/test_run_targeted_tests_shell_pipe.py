"""Targeted tests for sprint item 9e7b01cd -- ``pixi run test`` determinism.

``tests/test_2cebf4ae_run_lifecycle.py`` already covers ``TestRunLock``,
``TestRunTracker``, and ``_run_pytest_observed`` at the unit level in
exhaustive detail (stall/timeout/crash classification, Windows PID
liveness, the ``--supersede`` truthful-cleanup fix, psutil-absent
fallbacks). What it does NOT cover -- and what this file adds -- is
``main()`` itself, end to end:

1. Duplicate-run rejection with no ``--supersede``: another live run must
   cause ``main()`` to return exit code 2 WITHOUT ever attempting test
   collection or spawning a pytest child (two overlapping full runs must
   never interleave).
2. The ``MERIDIAN_ALLOW_CONCURRENT_TESTS=1`` escape hatch actually bypasses
   the lock and leaves another run's lock file untouched.
3. Truthful exit-code propagation all the way out of ``main()`` (not just
   out of the inner ``_run_pytest_observed`` helper) for both a passing and
   a failing child.
4. The concrete regression this item's hardening fixes: ``TestRunLock``
   swallows a transient ``OSError`` (e.g. Windows ERROR_SHARING_VIOLATION /
   PermissionError from an antivirus scan or another process briefly
   holding the lock file open) during acquire-reclaim and release, and
   ``main()``'s own ``finally`` block never lets a cleanup exception mask
   an already-computed result. Plain Python semantics: a ``finally`` block
   that raises DISCARDS the ``try`` block's ``return`` value and propagates
   the new exception instead -- so before this fix, a correctly-computed
   "N passed"/"N failed" exit code could be silently replaced by an
   unrelated crash whenever cleanup hit a transient filesystem error. That
   is exactly the "lost final exit code" failure mode named in this item's
   title.

All tests here drive ``scripts.run_tests.main()`` with a fully mocked
pytest child (no real subprocess, no real sleep) so they stay fast and
deterministic, mirroring the injection pattern already established in
``test_2cebf4ae_run_lifecycle.py``.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import scripts.run_tests as rt


# ---------------------------------------------------------------------------
# Shared fakes / helpers
# ---------------------------------------------------------------------------


class _FakeExitingProc:
    """A pytest child that exits with a fixed code on its first poll() --
    avoids any real ``time.sleep`` inside ``_run_pytest_observed``'s poll
    loop, which does not accept an injected clock/sleep_fn from ``main()``."""

    def __init__(self, pid: int, code: int, calls_before_exit: int = 1) -> None:
        self.pid = pid
        self.returncode: "int | None" = None
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
def _stub_integrations(monkeypatch):
    """Every test in this file drives its own fake pytest child -- never
    touch real psutil/meridian process-registry/heartbeat integrations."""
    monkeypatch.delenv("MERIDIAN_ALLOW_CONCURRENT_TESTS", raising=False)
    monkeypatch.setattr(rt, "_kill_process_tree", lambda pid: True)
    monkeypatch.setattr(rt, "_report_survivors", lambda pids: [])
    monkeypatch.setattr(rt, "_process_tree_pids", lambda pid: [pid] if pid else [])
    monkeypatch.setattr(rt, "_maybe_heartbeat_owner_session", lambda: None)
    monkeypatch.setattr(rt, "_maybe_register_process_lease", lambda pid, run_id: (lambda: None))


def _run_main_with_fake_pytest(monkeypatch, argv, *, exit_code, collected=1):
    """Drive ``rt.main(argv)`` with collection and the pytest child both
    mocked -- fast and deterministic, exercises main()'s own return-value
    plumbing (argument parsing -> lock handling -> collect_count ->
    build_run_args -> _run_pytest_observed -> finally-block cleanup)."""
    monkeypatch.setattr(rt, "collect_count", lambda pytest_args: (collected, 0))
    fake_proc = _FakeExitingProc(pid=999_999, code=exit_code)
    monkeypatch.setattr(rt.subprocess, "Popen", lambda cmd, **kw: fake_proc)
    return rt.main(argv)


# ---------------------------------------------------------------------------
# 1. Duplicate-run prevention at the main() level (not yet covered anywhere
#    else -- test_2cebf4ae_run_lifecycle.py only exercises the --supersede
#    refusal path through main(), never the plain "reject and never touch
#    the child" path).
# ---------------------------------------------------------------------------


def test_main_rejects_duplicate_run_without_supersede_and_never_touches_pytest(tmp_path, monkeypatch, capsys):
    lock = rt.TestRunLock(tmp_path)
    lock.path.write_text("424242\t1234.0\t/some/other/cwd\n", encoding="utf-8")
    existing = rt.TestRunRecord(run_id="other-run", state=rt.STATE_RUNNING, started_at="t0")
    rt._write_record_atomic(lock.state_path, existing)

    monkeypatch.setattr(rt, "TestRunLock", lambda repo_root: lock)
    monkeypatch.setattr(rt, "_pid_is_running", lambda pid: True)  # the other run's owner is alive
    # main()'s own tracker.mark_queued() would otherwise overwrite the
    # "existing" record at the shared state_path before the duplicate-check
    # branch reads it back -- neutralize it so this test observes the
    # OTHER run's record undisturbed (same isolation technique used by
    # test_main_supersede_refuses_and_preserves_receipt_when_kill_not_confirmed
    # in test_2cebf4ae_run_lifecycle.py).
    monkeypatch.setattr(rt.TestRunTracker, "mark_queued", lambda self: None)

    spawned = {"popen": False, "collect": False}

    def _boom_collect(pytest_args):
        spawned["collect"] = True
        raise AssertionError("must not attempt collection when a duplicate run is rejected")

    def _boom_popen(cmd, **kw):
        spawned["popen"] = True
        raise AssertionError("must not spawn pytest when a duplicate run is rejected")

    monkeypatch.setattr(rt, "collect_count", _boom_collect)
    monkeypatch.setattr(rt.subprocess, "Popen", _boom_popen)

    exit_code = rt.main(["tests/"])

    assert exit_code == 2
    assert spawned == {"popen": False, "collect": False}
    captured = capsys.readouterr()
    assert "Another Meridian test run is active" in captured.err
    assert "other-run" in captured.err
    assert "--supersede" in captured.err


def test_main_allow_concurrent_tests_env_var_bypasses_duplicate_lock_untouched(tmp_path, monkeypatch):
    """The documented ``MERIDIAN_ALLOW_CONCURRENT_TESTS=1`` escape hatch
    must actually bypass the lock entirely -- including never touching the
    other run's lock file -- not merely avoid returning exit code 2."""
    lock = rt.TestRunLock(tmp_path)
    lock.path.write_text("424242\t1234.0\t/some/other/cwd\n", encoding="utf-8")

    monkeypatch.setattr(rt, "TestRunLock", lambda repo_root: lock)
    monkeypatch.setattr(rt, "_pid_is_running", lambda pid: True)  # would normally reject
    monkeypatch.setenv("MERIDIAN_ALLOW_CONCURRENT_TESTS", "1")

    exit_code = _run_main_with_fake_pytest(monkeypatch, ["tests/"], exit_code=0)

    assert exit_code == 0
    # The pre-existing lock file must be untouched -- this run never
    # acquired (or released) it.
    assert lock.path.read_text(encoding="utf-8").startswith("424242")


# ---------------------------------------------------------------------------
# 2. Truthful exit-code propagation all the way out of main() (previously
#    only verified at the _run_pytest_observed level, one layer in).
# ---------------------------------------------------------------------------


def test_main_end_to_end_propagates_passing_exit_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    exit_code = _run_main_with_fake_pytest(monkeypatch, ["tests/"], exit_code=0)
    assert exit_code == 0


def test_main_end_to_end_propagates_failing_exit_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    exit_code = _run_main_with_fake_pytest(monkeypatch, ["tests/"], exit_code=1)
    assert exit_code == 1


# ---------------------------------------------------------------------------
# 3. The concrete "lost final exit code" regression: a cleanup-time OSError
#    (lock unlink) must never mask an already-truthful result.
# ---------------------------------------------------------------------------


def test_release_swallows_permission_error_and_still_clears_acquired(tmp_path, monkeypatch, capsys):
    """Unit-level regression test for TestRunLock.release()'s hardening:
    before this fix, release() only caught FileNotFoundError -- any other
    OSError (a PermissionError from a transient Windows sharing violation
    is the realistic case) propagated straight out of release(), which is
    called from main()'s cleanup finally block."""
    lock = rt.TestRunLock(tmp_path)
    assert lock.acquire() is True

    def _raise_unlink(self, *a, **kw):
        raise PermissionError("simulated: file in use by another process")

    monkeypatch.setattr(Path, "unlink", _raise_unlink)

    lock.release()  # must not raise

    assert lock.acquired is False
    captured = capsys.readouterr()
    assert "could not remove test-run lock" in captured.err


def test_acquire_reclaim_survives_transient_unlink_failure_then_succeeds(tmp_path, monkeypatch):
    """Unit-level regression test for the matching hardening in acquire()'s
    stale-lock reclaim path: a one-time transient unlink failure must not
    crash acquisition outright -- it should log and retry, succeeding once
    the transient condition clears."""
    dead_pid = 918_273
    monkeypatch.setattr(rt, "_pid_is_running", lambda pid: pid != dead_pid)

    lock = rt.TestRunLock(tmp_path)
    lock.path.write_text(f"{dead_pid}\t1234.0\t/some/old/cwd\n", encoding="utf-8")

    calls = {"n": 0}
    original_unlink = Path.unlink

    def _flaky_unlink(self, *args, **kwargs):
        if self == lock.path and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError("simulated: transient sharing violation")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    assert lock.acquire() is True
    assert calls["n"] == 1
    lock.release()


def test_main_end_to_end_survives_lock_release_failure_and_still_returns_truthful_exit_code(tmp_path, monkeypatch):
    """The literal bug this item's title names: 'fail loudly instead of
    losing the final exit code'. Plain Python semantics -- a `finally`
    block that raises DISCARDS the `try` block's own `return` value and
    propagates the new exception instead. Force lock cleanup to fail with
    an OSError and assert main() still returns the real, already-computed
    exit code rather than raising (or losing it to an unrelated crash)."""
    monkeypatch.chdir(tmp_path)

    def _raise_unlink(self, *a, **kw):
        raise PermissionError("simulated: file in use by another process")

    monkeypatch.setattr(Path, "unlink", _raise_unlink)

    exit_code = _run_main_with_fake_pytest(monkeypatch, ["tests/"], exit_code=1)

    assert exit_code == 1  # truthful -- NOT an uncaught PermissionError


def test_main_end_to_end_survives_lock_release_failure_on_passing_run(tmp_path, monkeypatch):
    """Same regression, passing case: exit code 0 must survive a failed
    lock-file cleanup too (not just the failing-run case above)."""
    monkeypatch.chdir(tmp_path)

    def _raise_unlink(self, *a, **kw):
        raise PermissionError("simulated: file in use by another process")

    monkeypatch.setattr(Path, "unlink", _raise_unlink)

    exit_code = _run_main_with_fake_pytest(monkeypatch, ["tests/"], exit_code=0)

    assert exit_code == 0
