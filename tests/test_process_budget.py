"""9c8336c4 -- host-local memory/CPU budgets + quarantine.

Covers:
1. ``load_host_budget_config`` -- defaults, env var overrides, explicit
   opt-out, invalid-value fallback.
2. ``sample_process`` -- injected proc_factory, missing-attribute fallback
   (POSIX-shaped pmem object), exception -> None.
3. ``ProcessBudgetMonitor.evaluate`` -- disabled/no-sample/within-budget/
   quiesce-then-kill escalation, memory- and CPU-triggered breaches, peak
   tracking, backoff cooldown gating.
4. ``ProcessBudgetMonitor.record_kill_outcome`` -- survivor backoff
   (doubling, capped) vs. confirmed-gone reset.
5. ``BudgetReport.to_dict`` -- JSON-safe shape.
"""
from __future__ import annotations

import pytest

from meridian import process_budget as pb


# ---------------------------------------------------------------------------
# 1. load_host_budget_config
# ---------------------------------------------------------------------------


def test_load_host_budget_config_defaults(monkeypatch):
    for name in (
        pb.ENABLED_ENV, pb.MAX_MEMORY_MB_ENV, pb.MAX_CPU_PERCENT_ENV,
        pb.SAMPLE_SECONDS_ENV, pb.GRACE_SECONDS_ENV, pb.MAX_BACKOFF_SECONDS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = pb.load_host_budget_config()
    assert cfg.enabled is True
    assert cfg.max_memory_bytes == pytest.approx(6144.0 * 1024 * 1024)
    assert cfg.max_cpu_percent == pytest.approx(400.0)
    assert cfg.sample_interval_seconds == pytest.approx(30.0)
    assert cfg.grace_seconds == pytest.approx(15.0)
    assert cfg.max_backoff_seconds == pytest.approx(300.0)


def test_load_host_budget_config_env_overrides(monkeypatch):
    monkeypatch.setenv(pb.MAX_MEMORY_MB_ENV, "1024")
    monkeypatch.setenv(pb.MAX_CPU_PERCENT_ENV, "150")
    monkeypatch.setenv(pb.SAMPLE_SECONDS_ENV, "5")
    monkeypatch.setenv(pb.GRACE_SECONDS_ENV, "2")
    monkeypatch.setenv(pb.MAX_BACKOFF_SECONDS_ENV, "60")
    cfg = pb.load_host_budget_config()
    assert cfg.max_memory_bytes == pytest.approx(1024 * 1024 * 1024)
    assert cfg.max_cpu_percent == pytest.approx(150.0)
    assert cfg.sample_interval_seconds == pytest.approx(5.0)
    assert cfg.grace_seconds == pytest.approx(2.0)
    assert cfg.max_backoff_seconds == pytest.approx(60.0)


def test_load_host_budget_config_explicit_opt_out(monkeypatch):
    monkeypatch.setenv(pb.ENABLED_ENV, "0")
    cfg = pb.load_host_budget_config()
    assert cfg.enabled is False

    monkeypatch.setenv(pb.ENABLED_ENV, "false")
    assert pb.load_host_budget_config().enabled is False

    monkeypatch.setenv(pb.ENABLED_ENV, "yes")
    assert pb.load_host_budget_config().enabled is True


def test_load_host_budget_config_invalid_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(pb.MAX_MEMORY_MB_ENV, "not-a-number")
    cfg = pb.load_host_budget_config()
    assert cfg.max_memory_bytes == pytest.approx(6144.0 * 1024 * 1024)


def test_load_host_budget_config_zero_memory_disables_only_memory_ceiling(monkeypatch):
    monkeypatch.setenv(pb.MAX_MEMORY_MB_ENV, "0")
    cfg = pb.load_host_budget_config()
    assert cfg.max_memory_bytes is None
    assert cfg.max_cpu_percent == pytest.approx(400.0)  # untouched


# ---------------------------------------------------------------------------
# 2. sample_process
# ---------------------------------------------------------------------------


class _FakeMemFull:
    """Windows-shaped pmem: rss + peak_wset/private/pagefile all present."""

    rss = 500 * 1024 * 1024
    peak_wset = 600 * 1024 * 1024
    private = 480 * 1024 * 1024
    pagefile = 520 * 1024 * 1024


class _FakeMemPosix:
    """POSIX-shaped pmem: only rss (+ a few POSIX-only fields psutil exposes
    that this module never reads)."""

    rss = 300 * 1024 * 1024
    vms = 900 * 1024 * 1024


class _FakeProc:
    def __init__(self, pid, mem, cpu=12.5, raise_on_cpu=False):
        self.pid = pid
        self._mem = mem
        self._cpu = cpu
        self._raise_on_cpu = raise_on_cpu

    def memory_info(self):
        return self._mem

    def cpu_percent(self, interval=None):
        if self._raise_on_cpu:
            raise RuntimeError("boom")
        return self._cpu


def test_sample_process_windows_shaped_fields():
    sample = pb.sample_process(123, proc_factory=lambda pid: _FakeProc(pid, _FakeMemFull()))
    assert sample is not None
    assert sample.pid == 123
    assert sample.current_bytes == _FakeMemFull.rss
    assert sample.peak_bytes == _FakeMemFull.peak_wset
    assert sample.private_bytes == _FakeMemFull.private
    assert sample.commit_bytes == _FakeMemFull.pagefile
    assert sample.cpu_percent == pytest.approx(12.5)
    assert sample.sample_time > 0


def test_sample_process_posix_shaped_missing_fields_are_none():
    sample = pb.sample_process(456, proc_factory=lambda pid: _FakeProc(pid, _FakeMemPosix()))
    assert sample is not None
    assert sample.current_bytes == _FakeMemPosix.rss
    # Windows-only fields must never be fabricated on a POSIX-shaped sample.
    assert sample.peak_bytes is None
    assert sample.private_bytes is None
    assert sample.commit_bytes is None


def test_sample_process_cpu_percent_failure_degrades_to_none():
    sample = pb.sample_process(
        789, proc_factory=lambda pid: _FakeProc(pid, _FakeMemPosix(), raise_on_cpu=True)
    )
    assert sample is not None
    assert sample.cpu_percent is None
    assert sample.current_bytes == _FakeMemPosix.rss


def test_sample_process_gone_or_erroring_returns_none():
    def _boom(pid):
        raise ProcessLookupError("gone")

    assert pb.sample_process(1, proc_factory=_boom) is None


def test_sample_process_no_psutil_available_returns_none(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert pb.sample_process(1) is None


# ---------------------------------------------------------------------------
# 3. ProcessBudgetMonitor.evaluate
# ---------------------------------------------------------------------------


def _budget(**overrides) -> pb.ProcessBudget:
    base = dict(
        enabled=True, max_memory_bytes=1000, max_cpu_percent=90.0,
        sample_interval_seconds=10.0, grace_seconds=5.0, max_backoff_seconds=100.0,
    )
    base.update(overrides)
    return pb.ProcessBudget(**base)


def _sample(current_bytes=None, cpu_percent=None) -> pb.BudgetSample:
    return pb.BudgetSample(pid=1, sample_time=1.0, current_bytes=current_bytes, cpu_percent=cpu_percent)


def test_evaluate_disabled_budget_always_none():
    monitor = pb.ProcessBudgetMonitor("test", _budget(enabled=False))
    report = monitor.evaluate(1, _sample(current_bytes=999999))
    assert report.action == "none"
    assert report.reason == "budget_disabled"


def test_evaluate_no_sample_is_none():
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    report = monitor.evaluate(1, None)
    assert report.action == "none"
    assert report.reason == "no_sample"


def test_evaluate_within_budget_is_none():
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    report = monitor.evaluate(1, _sample(current_bytes=500, cpu_percent=10.0), now=0.0)
    assert report.action == "none"
    assert report.reason == "within_budget"


def test_evaluate_memory_breach_quiesces_then_kills():
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    r1 = monitor.evaluate(1, _sample(current_bytes=2000), now=0.0)
    assert r1.action == "quiesce"
    assert "memory" in r1.reason

    r2 = monitor.evaluate(1, _sample(current_bytes=2000), now=10.0)
    assert r2.action == "kill"
    assert "memory" in r2.reason


def test_evaluate_cpu_breach_quiesces_then_kills():
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    r1 = monitor.evaluate(1, _sample(current_bytes=1, cpu_percent=95.0), now=0.0)
    assert r1.action == "quiesce"
    assert "cpu" in r1.reason
    r2 = monitor.evaluate(1, _sample(current_bytes=1, cpu_percent=95.0), now=10.0)
    assert r2.action == "kill"


def test_evaluate_recovery_between_breaches_resets_escalation():
    """A single transient spike must never kill -- recovering for one sample
    resets the consecutive-breach counter, so a LATER breach starts back at
    "quiesce", not "kill"."""
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    r1 = monitor.evaluate(1, _sample(current_bytes=2000), now=0.0)
    assert r1.action == "quiesce"

    r2 = monitor.evaluate(1, _sample(current_bytes=500), now=10.0)  # recovered
    assert r2.action == "none"
    assert r2.reason == "within_budget"

    r3 = monitor.evaluate(1, _sample(current_bytes=2000), now=20.0)
    assert r3.action == "quiesce"  # not "kill" -- escalation restarted


def test_evaluate_tracks_peak_current_bytes_across_calls():
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    monitor.evaluate(1, _sample(current_bytes=100), now=0.0)
    monitor.evaluate(1, _sample(current_bytes=900), now=1.0)
    r3 = monitor.evaluate(1, _sample(current_bytes=300), now=2.0)
    assert r3.peak_current_bytes == 900


def test_evaluate_no_sample_resets_consecutive_breach_streak():
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    r1 = monitor.evaluate(1, _sample(current_bytes=2000), now=0.0)
    assert r1.action == "quiesce"
    # process vanished / psutil hiccup between samples
    monitor.evaluate(1, None, now=5.0)
    r3 = monitor.evaluate(1, _sample(current_bytes=2000), now=10.0)
    assert r3.action == "quiesce"  # restarted, not "kill"


# ---------------------------------------------------------------------------
# 4. record_kill_outcome -- bounded backoff
# ---------------------------------------------------------------------------


def test_record_kill_outcome_survivor_applies_backoff_and_gates_evaluate():
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    monitor.evaluate(1, _sample(current_bytes=2000), now=0.0)   # quiesce
    monitor.evaluate(1, _sample(current_bytes=2000), now=10.0)  # kill
    monitor.record_kill_outcome(survived=True, now=10.0)

    # Immediately re-evaluating (before the backoff window elapses) must not
    # recommend action again -- "bounded watchdog backoff": never hammer a
    # survivor every tick.
    r = monitor.evaluate(1, _sample(current_bytes=2000), now=11.0)
    assert r.action == "none"
    assert r.reason == "backoff_cooldown"


def test_record_kill_outcome_survivor_backoff_doubles_and_caps():
    monitor = pb.ProcessBudgetMonitor("test", _budget(sample_interval_seconds=10.0, max_backoff_seconds=25.0))
    monitor.record_kill_outcome(survived=True, now=0.0)
    assert monitor._backoff_seconds == pytest.approx(10.0)  # first backoff == sample interval
    monitor.record_kill_outcome(survived=True, now=0.0)
    assert monitor._backoff_seconds == pytest.approx(20.0)  # doubled
    monitor.record_kill_outcome(survived=True, now=0.0)
    assert monitor._backoff_seconds == pytest.approx(25.0)  # capped at max_backoff_seconds


def test_record_kill_outcome_confirmed_gone_resets_state():
    monitor = pb.ProcessBudgetMonitor("test", _budget())
    monitor.evaluate(1, _sample(current_bytes=2000), now=0.0)   # quiesce
    monitor.evaluate(1, _sample(current_bytes=2000), now=10.0)  # kill
    monitor.record_kill_outcome(survived=False, now=10.0)

    # No backoff -- a fresh process at this pid can be evaluated immediately.
    r = monitor.evaluate(1, _sample(current_bytes=2000), now=10.5)
    assert r.action == "quiesce"  # escalation restarted from zero


# ---------------------------------------------------------------------------
# 5. BudgetReport.to_dict
# ---------------------------------------------------------------------------


def test_budget_report_to_dict_is_json_safe():
    monitor = pb.ProcessBudgetMonitor("mylabel", _budget(), run_id="run-1")
    report = monitor.evaluate(1, _sample(current_bytes=2000, cpu_percent=5.0), now=0.0)
    data = report.to_dict()
    assert data["label"] == "mylabel"
    assert data["pid"] == 1
    assert data["run_id"] == "run-1"
    assert data["action"] == "quiesce"
    assert "memory" in data["reason"]
    assert data["budget"]["enabled"] is True
    assert data["sample"]["current_bytes"] == 2000
    assert data["sample"]["cpu_percent"] == pytest.approx(5.0)

    import json
    json.dumps(data)  # must not raise


def test_budget_report_to_dict_handles_none_sample():
    monitor = pb.ProcessBudgetMonitor("mylabel", _budget())
    report = monitor.evaluate(1, None)
    data = report.to_dict()
    assert data["sample"] is None
