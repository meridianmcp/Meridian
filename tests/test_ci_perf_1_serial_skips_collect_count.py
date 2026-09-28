"""CI-PERF-1: --serial must not pay for the collect_count() preflight.

``build_run_args`` is called with ``effective_count=0`` whenever ``--serial``
is passed, which always selects ``-p no:xdist`` regardless of the real
collected-test count -- so the whole point of running an extra
``pytest --collect-only`` subprocess first (counting tests to decide
scheduling) is moot in that branch. Before this fix, ``main()`` ran
``collect_count()`` unconditionally, spending a full redundant pytest
subprocess spawn on every already-serial invocation (exactly the
``--serial`` steps in `pixi.toml`'s ``test``/``test-cov``/``test-pg`` tasks
for ``test_rate_limit_serial.py`` and the ``subprocess_isolated`` sweep).
"""

from __future__ import annotations

import scripts.run_tests as rt


def _isolated_lock(tmp_path, monkeypatch):
    """Give TestRunLock a repo_root under tmp_path so this test never
    touches the real, shared meridian-pytest-*.lock file in the system
    temp dir (which a real concurrent test run may be holding)."""
    lock = rt.TestRunLock(tmp_path)
    monkeypatch.setattr(rt, "TestRunLock", lambda repo_root: lock)
    return lock


def test_serial_flag_skips_collect_count_preflight(tmp_path, monkeypatch):
    lock = _isolated_lock(tmp_path, monkeypatch)

    def _fail_if_called(args):
        raise AssertionError(
            "collect_count() must be skipped when --serial already forces "
            "serial scheduling"
        )

    monkeypatch.setattr(rt, "collect_count", _fail_if_called)

    captured: dict = {}

    def _fake_run_pytest_observed(run_args, tracker, **kwargs):
        captured["run_args"] = run_args
        tracker.mark_terminal(rt.STATE_PASSED, exit_code=0)
        return 0

    monkeypatch.setattr(rt, "_run_pytest_observed", _fake_run_pytest_observed)

    exit_code = rt.main(["--serial", "tests/test_rate_limit_serial.py", "-q"])

    assert exit_code == 0
    # Serial scheduling still applied even though the real count was
    # never collected.
    run_args = captured["run_args"]
    assert "-p" in run_args and "no:xdist" in run_args
    # collected_count stays None (its existing Optional default) rather
    # than a fabricated value, since the preflight never ran.
    record = rt.TestRunTracker.load_record(lock.state_path)
    assert record is not None
    assert record.collected_count is None


def test_serial_flag_prints_skip_notice_instead_of_a_fake_count(tmp_path, monkeypatch, capsys):
    _isolated_lock(tmp_path, monkeypatch)
    monkeypatch.setattr(
        rt, "collect_count",
        lambda args: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    monkeypatch.setattr(
        rt, "_run_pytest_observed",
        lambda run_args, tracker, **kwargs: (tracker.mark_terminal(rt.STATE_PASSED, exit_code=0), 0)[1],
    )

    exit_code = rt.main(["--serial", "tests/test_rate_limit_serial.py", "-q"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "serial (forced)" in out
    assert "collection preflight skipped" in out


def test_non_serial_path_still_runs_collect_count_preflight(tmp_path, monkeypatch):
    """Regression guard: only the already-forced-serial branch should skip
    the preflight -- the normal (non---serial) path must still call
    collect_count() to decide auto vs. serial scheduling."""
    _isolated_lock(tmp_path, monkeypatch)

    calls = []

    def _fake_collect_count(args):
        calls.append(args)
        return 5, 0  # small selection -> serial threshold path

    monkeypatch.setattr(rt, "collect_count", _fake_collect_count)

    captured: dict = {}

    def _fake_run_pytest_observed(run_args, tracker, **kwargs):
        captured["run_args"] = run_args
        tracker.mark_terminal(rt.STATE_PASSED, exit_code=0)
        return 0

    monkeypatch.setattr(rt, "_run_pytest_observed", _fake_run_pytest_observed)

    exit_code = rt.main(["tests/test_something.py", "-q"])

    assert exit_code == 0
    assert len(calls) == 1
    assert "-p" in captured["run_args"] and "no:xdist" in captured["run_args"]
