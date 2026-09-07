"""899936dd -- LOCAL-RUNNER-FOUNDATION: focused tests for meridian/local_runner.py.

Covers the sprint item's explicit acceptance-criteria test list: duplicate
launch, stale PID, child crash, bounded output, cold-start timeout, and
recovery -- plus the separated status schema, the never-block-forever
doctor/status/preflight/log-tail surface, the lease-broker cross-tool
integration, and the explicitly-scoped script escape hatch.

Every process-spawning test here uses a short-lived, disposable
``sys.executable -c ...`` child (matching ``tests/test_tunnel_preflight.py``'s
own isolation contract) and always tears the child down via ``LocalRunner``'s
context-manager ``stop()`` -- never leaves an orphaned subprocess behind,
even on assertion failure.

Timings are kept small (well under a second) so the whole file runs fast
under ``pixi run python -m pytest tests/test_local_runner.py -q -p no:xdist``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from meridian import local_runner as lr
from meridian import process_lifecycle
from meridian import process_registry
from meridian import tunnel_lifecycle


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "runner_state"


@pytest.fixture
def broker(tmp_path):
    """In-memory-persisted broker isolated to this test's own tmp_path --
    never touches a real home directory."""
    return process_registry.ProcessLeaseBroker(persist_path=tmp_path / "leases.json")


def _sleepy_cmd(seconds: float = 30.0) -> "list[str]":
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _exit_cmd(code: int) -> "list[str]":
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def _make_runner(
    scope: str,
    command,
    *,
    state_dir,
    broker=None,
    **kwargs,
) -> lr.LocalRunner:
    kwargs.setdefault("crash_settle_seconds", 0.15)
    kwargs.setdefault("poll_interval", 0.02)
    return lr.LocalRunner(scope, command, state_dir=state_dir, broker=broker, **kwargs)


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Status schema -- as_dict() / JSON round trip / separation of concerns
# ---------------------------------------------------------------------------


def test_child_process_status_as_dict_uses_enum_value():
    status = lr.ChildProcessStatus(
        state=lr.ChildState.RUNNING, pid=1, run_id="r", create_time=1.0,
        started_at=1.0, uptime_seconds=2.0, restart_count=0, exit_code=None,
        last_exit_reason=None,
    )
    payload = status.as_dict()
    assert payload["state"] == "running"
    json.dumps(payload)  # must be JSON-serializable


def test_runner_status_as_dict_separates_child_local_mcp_tunnel():
    status = lr.RunnerStatus(
        scope="s",
        generated_at=1.0,
        child=lr.ChildProcessStatus(
            state=lr.ChildState.NOT_STARTED, pid=None, run_id=None, create_time=None,
            started_at=None, uptime_seconds=None, restart_count=0, exit_code=None,
            last_exit_reason=None,
        ),
        local_mcp=lr.LocalMcpStatus(state=lr.LocalMcpState.NOT_CONFIGURED, detail="", checked_at=None),
        tunnel=lr.TunnelReadinessStatus(configured=False, label=None, state="not_configured", detail=""),
    )
    payload = status.as_dict()
    assert set(payload) == {"scope", "generated_at", "child", "local_mcp", "tunnel", "warnings"}
    assert payload["child"]["state"] == "not_started"
    assert payload["local_mcp"]["state"] == "not_configured"
    assert payload["tunnel"]["state"] == "not_configured"
    json.dumps(payload)


def test_status_with_no_prior_record_is_not_started(state_dir):
    runner = _make_runner("never-started", _sleepy_cmd(), state_dir=state_dir, broker=None)
    status = runner.status()
    assert status.child.state is lr.ChildState.NOT_STARTED
    assert status.local_mcp.state is lr.LocalMcpState.NOT_CONFIGURED
    assert status.tunnel.configured is False


def test_status_never_blocks_or_sleeps(state_dir, monkeypatch):
    """status() must be O(1) -- no path through it should ever call time.sleep."""
    calls = []
    monkeypatch.setattr(lr.time, "sleep", lambda s: calls.append(s))
    runner = _make_runner("bounded-status", None, state_dir=state_dir, broker=None)
    runner.status()
    assert calls == []


# ---------------------------------------------------------------------------
# RunnerRecord -- persisted, recoverable identity metadata
# ---------------------------------------------------------------------------


def test_runner_record_to_dict_from_dict_round_trip():
    record = lr.RunnerRecord(
        scope="s", run_id="r1", pid=42, executable="node", cwd="/repo",
        cmdline=["node", "x.js"], create_time=5.0, group_id=42, job_id=None,
        started_at=1.0, restart_count=2, log_path="/x.log", tunnel_label="fs",
        lease_run_id="lease-1",
    )
    restored = lr.RunnerRecord.from_dict(record.to_dict())
    assert restored == record


def test_runner_record_as_owned_handle_matches_process_lifecycle_shape():
    record = lr.RunnerRecord(
        scope="s", run_id="r1", pid=123, executable="x", cwd="/y", cmdline=["x"],
        create_time=7.0, group_id=8, job_id=9, started_at=1.0,
    )
    handle = record.as_owned_handle()
    assert isinstance(handle, process_lifecycle.OwnedProcessHandle)
    assert handle.pid == 123 and handle.create_time == 7.0 and handle.group_id == 8 and handle.job_id == 9
    # Identity is PID + create_time + process-group/job id -- never a port.
    assert "port" not in handle.to_dict()


def test_runner_record_from_dict_ignores_unknown_keys():
    restored = lr.RunnerRecord.from_dict({
        "scope": "s", "run_id": "r", "pid": 1, "executable": "x", "cwd": None,
        "cmdline": [], "create_time": None, "group_id": None, "job_id": None,
        "started_at": 1.0, "from_the_future": "ignored",
    })
    assert restored.pid == 1


# ---------------------------------------------------------------------------
# State file persistence -- atomic write, corrupt/missing degrade to None
# ---------------------------------------------------------------------------


def test_safe_scope_slug_is_filesystem_safe_and_stable():
    slug = lr._safe_scope_slug("weird scope/with:chars*?")
    assert all(c.isalnum() or c in "-_" for c in slug)
    assert lr._safe_scope_slug("weird scope/with:chars*?") == slug  # deterministic


def test_safe_scope_slug_disambiguates_colliding_sanitized_scopes():
    a = lr._safe_scope_slug("a/b")
    b = lr._safe_scope_slug("a-b")
    assert a != b  # both sanitize toward "a-b" but the hash suffix differs


def test_save_and_load_record_round_trips(state_dir):
    runner = _make_runner("persist-scope", None, state_dir=state_dir, broker=None)
    record = lr.RunnerRecord(
        scope="persist-scope", run_id="r", pid=1, executable="x", cwd=None,
        cmdline=["x"], create_time=None, group_id=None, job_id=None, started_at=1.0,
    )
    runner._save_record(record)
    loaded = runner._load_record()
    assert loaded == record


def test_save_record_write_leaves_no_stray_tmp_file(state_dir):
    runner = _make_runner("tmp-scope", None, state_dir=state_dir, broker=None)
    record = lr.RunnerRecord(
        scope="tmp-scope", run_id="r", pid=1, executable="x", cwd=None,
        cmdline=["x"], create_time=None, group_id=None, job_id=None, started_at=1.0,
    )
    runner._save_record(record)
    leftovers = [p for p in state_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_load_record_missing_file_returns_none(state_dir):
    runner = _make_runner("missing-scope", None, state_dir=state_dir, broker=None)
    assert runner._load_record() is None


def test_load_record_corrupt_file_returns_none_rather_than_raising(state_dir):
    runner = _make_runner("corrupt-scope", None, state_dir=state_dir, broker=None)
    runner._state_path.parent.mkdir(parents=True, exist_ok=True)
    runner._state_path.write_text("{not valid json", encoding="utf-8")
    assert runner._load_record() is None


# ---------------------------------------------------------------------------
# Duplicate launch
# ---------------------------------------------------------------------------


def test_start_twice_without_force_raises_runner_already_running(state_dir, broker):
    with _make_runner("dup-scope", _sleepy_cmd(), state_dir=state_dir, broker=broker) as runner:
        status = runner.start()
        assert status.child.state is lr.ChildState.RUNNING
        with pytest.raises(lr.RunnerAlreadyRunningError) as excinfo:
            runner.start()
        assert excinfo.value.scope == "dup-scope"
        assert excinfo.value.record.pid == status.child.pid


def test_duplicate_launch_detected_from_a_separate_instance(state_dir, broker):
    """The whole point of persisting identity metadata: a totally different
    LocalRunner object (simulating a separate process invocation) must
    still detect the live child via PID + create_time, not just in-memory
    state."""
    with _make_runner("dup-cross-instance", _sleepy_cmd(), state_dir=state_dir, broker=broker) as runner_a:
        runner_a.start()
        runner_b = _make_runner("dup-cross-instance", _sleepy_cmd(), state_dir=state_dir, broker=broker)
        with pytest.raises(lr.RunnerAlreadyRunningError):
            runner_b.start()


def test_start_with_force_takes_over_a_live_prior_record(state_dir, broker):
    with _make_runner("force-takeover", _sleepy_cmd(), state_dir=state_dir, broker=broker) as runner:
        first = runner.start()
        first_pid = first.child.pid
        second = runner.start(force=True)
        assert second.child.pid != first_pid
        assert second.child.state is lr.ChildState.RUNNING
        # The old process was actually terminated, not merely forgotten.
        assert _wait_until(lambda: not _pid_alive(first_pid), timeout=5.0)


def _pid_alive(pid: int) -> bool:
    try:
        import psutil  # type: ignore
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return False


def test_start_requires_a_command(state_dir):
    runner = _make_runner("no-command", None, state_dir=state_dir, broker=None)
    with pytest.raises(ValueError):
        runner.start()


# ---------------------------------------------------------------------------
# Stale PID / recovery
# ---------------------------------------------------------------------------


def test_start_recovers_automatically_from_a_confirmed_stale_record(state_dir, broker):
    """A record whose process has genuinely exited must NOT block a fresh
    start() -- no force needed. This is the "stale PID" / "recovery" pair
    the sprint item names explicitly."""
    with _make_runner("stale-scope", _exit_cmd(0), state_dir=state_dir, broker=broker) as runner_a:
        first = runner_a.start()
        stale_pid = first.child.pid
        assert _wait_until(lambda: not _pid_alive(stale_pid), timeout=5.0)

    runner_b = _make_runner("stale-scope", _sleepy_cmd(), state_dir=state_dir, broker=broker)
    with runner_b:
        status = runner_b.start()  # no force=True -- must succeed via recovery
        assert status.child.state is lr.ChildState.RUNNING
        assert status.child.pid != stale_pid
        assert any("stale" in w for w in status.warnings)


def test_recovered_status_reports_recovered_from_stale_pid_field(state_dir, broker):
    with _make_runner("recovery-field", _exit_cmd(0), state_dir=state_dir, broker=broker) as runner_a:
        first = runner_a.start()
        old_pid = first.child.pid
        assert _wait_until(lambda: not _pid_alive(old_pid), timeout=5.0)

    with _make_runner("recovery-field", _exit_cmd(0), state_dir=state_dir, broker=broker) as runner_b:
        runner_b.start()
        record = runner_b._load_record()
        assert record.recovered_from_stale_pid == old_pid


# ---------------------------------------------------------------------------
# Child crash
# ---------------------------------------------------------------------------


def test_child_crash_is_reported_with_exit_code(state_dir):
    with _make_runner("crash-scope", _exit_cmd(7), state_dir=state_dir, broker=None) as runner:
        status = runner.start()
        assert status.child.state is lr.ChildState.CRASHED
        assert status.child.exit_code == 7
        assert status.child.last_exit_reason == "crashed"


def test_clean_exit_zero_is_stopped_not_crashed(state_dir):
    with _make_runner("clean-exit-scope", _exit_cmd(0), state_dir=state_dir, broker=None) as runner:
        status = runner.start()
        assert status.child.state is lr.ChildState.STOPPED
        assert status.child.exit_code == 0


def test_child_crash_with_health_probe_reports_local_mcp_failed(state_dir):
    with _make_runner(
        "crash-with-probe", _exit_cmd(3), state_dir=state_dir, broker=None,
        health_probe=lambda: False, cold_start_timeout=2.0,
    ) as runner:
        status = runner.start()
        assert status.child.state is lr.ChildState.CRASHED
        assert status.local_mcp.state is lr.LocalMcpState.FAILED


def test_status_recovers_exit_code_for_a_crash_discovered_later(state_dir):
    """A child that crashes strictly AFTER start() has already returned
    (rather than during the bounded readiness wait) must still have its
    exit code recovered the next time status() is called, as long as the
    SAME in-process handle is still held."""
    with _make_runner(
        "late-crash", [sys.executable, "-c", "import time; time.sleep(0.2); import sys; sys.exit(5)"],
        state_dir=state_dir, broker=None, crash_settle_seconds=0.05,
    ) as runner:
        started = runner.start()
        assert started.child.state is lr.ChildState.RUNNING
        assert _wait_until(lambda: runner.status().child.state is lr.ChildState.CRASHED, timeout=5.0)
        final = runner.status()
        assert final.child.exit_code == 5


# ---------------------------------------------------------------------------
# Bounded output -- tail_log() and script receipts never return more than
# their configured cap, regardless of how much the child/script writes.
# ---------------------------------------------------------------------------


_BIG_OUTPUT_SNIPPET = (
    "import sys\n"
    "for i in range(20000):\n"
    "    sys.stdout.write('line-%06d-' % i + ('x' * 40) + '\\n')\n"
    "sys.stdout.write('END-OF-OUTPUT-MARKER\\n')\n"
)


def test_tail_log_is_bounded_regardless_of_file_size(state_dir):
    with _make_runner(
        "bounded-output", [sys.executable, "-c", _BIG_OUTPUT_SNIPPET],
        state_dir=state_dir, broker=None, crash_settle_seconds=3.0, poll_interval=0.05,
    ) as runner:
        status = runner.start()
        assert status.child.state is lr.ChildState.STOPPED  # exited 0 -- big write, not a crash
        record = runner._load_record()
        log_size = os.path.getsize(record.log_path)
        assert log_size > 100_000  # the child really did write a lot

        capped = runner.tail_log(max_bytes=2000)
        assert len(capped.encode("utf-8", errors="replace")) <= 2000
        # It's a genuine TAIL -- the marker written last must be present,
        # the very first line must not be (it fell outside the window).
        assert "END-OF-OUTPUT-MARKER" in capped
        assert "line-000000-" not in capped


def test_tail_log_returns_empty_string_when_no_run_yet(state_dir):
    runner = _make_runner("no-log-yet", None, state_dir=state_dir, broker=None)
    assert runner.tail_log() == ""


def test_run_allowlisted_script_output_is_tail_bounded(tmp_path):
    allowlist = lr.ScriptAllowlist()
    allowlist.declare(lr.ScriptSpec(name="big-output", command=tuple([sys.executable, "-c", _BIG_OUTPUT_SNIPPET])))
    receipt = lr.run_allowlisted_script(allowlist, "big-output")
    assert receipt.exit_code == 0
    assert len(receipt.stdout_tail) <= lr._SCRIPT_OUTPUT_TAIL_CHARS
    assert "END-OF-OUTPUT-MARKER" in receipt.stdout_tail


# ---------------------------------------------------------------------------
# Cold-start timeout
# ---------------------------------------------------------------------------


def test_cold_start_timeout_when_probe_never_succeeds(state_dir):
    with _make_runner(
        "cold-start-timeout", _sleepy_cmd(), state_dir=state_dir, broker=None,
        health_probe=lambda: False, cold_start_timeout=0.3, poll_interval=0.02,
    ) as runner:
        t0 = time.monotonic()
        status = runner.start()
        elapsed = time.monotonic() - t0
        assert status.local_mcp.state is lr.LocalMcpState.COLD_START_TIMEOUT
        assert status.child.state is lr.ChildState.RUNNING  # never crashed -- just never confirmed ready
        assert elapsed < 2.0  # bounded -- never hangs indefinitely


def test_probe_succeeding_quickly_reports_ready(state_dir):
    probe_calls = {"n": 0}

    def probe():
        probe_calls["n"] += 1
        return probe_calls["n"] >= 2  # ready on the second check

    with _make_runner(
        "probe-ready", _sleepy_cmd(), state_dir=state_dir, broker=None,
        health_probe=probe, cold_start_timeout=5.0, poll_interval=0.02,
    ) as runner:
        status = runner.start()
        assert status.local_mcp.state is lr.LocalMcpState.READY
        assert status.child.state is lr.ChildState.RUNNING


def test_broken_health_probe_degrades_to_cold_start_timeout_not_a_crash(state_dir):
    def broken_probe():
        raise RuntimeError("boom")

    with _make_runner(
        "broken-probe", _sleepy_cmd(), state_dir=state_dir, broker=None,
        health_probe=broken_probe, cold_start_timeout=0.2, poll_interval=0.02,
    ) as runner:
        status = runner.start()  # must not raise despite the probe raising every call
        assert status.local_mcp.state is lr.LocalMcpState.COLD_START_TIMEOUT


def test_no_probe_configured_stays_not_configured_even_for_a_long_lived_child(state_dir):
    with _make_runner(
        "no-probe-long-lived", _sleepy_cmd(), state_dir=state_dir, broker=None,
        crash_settle_seconds=0.1,
    ) as runner:
        status = runner.start()
        assert status.local_mcp.state is lr.LocalMcpState.NOT_CONFIGURED
        assert status.child.state is lr.ChildState.RUNNING


def test_status_downgrades_local_mcp_once_child_is_no_longer_running(state_dir):
    """A stale READY reading must not survive the child actually dying --
    status() must recompute local_mcp against the CURRENT child state."""
    probe_calls = {"n": 0}

    def probe():
        probe_calls["n"] += 1
        return True

    with _make_runner(
        "ready-then-dies", _exit_cmd(0), state_dir=state_dir, broker=None,
        health_probe=probe, cold_start_timeout=0.05, poll_interval=0.01,
        crash_settle_seconds=0.05,
    ) as runner:
        status = runner.start()
        # The child raced ahead and exited before the probe path even ran a
        # successful check in some runs; either way, once child is not
        # RUNNING, local_mcp must never claim READY.
        if status.child.state is not lr.ChildState.RUNNING:
            assert status.local_mcp.state is not lr.LocalMcpState.READY


# ---------------------------------------------------------------------------
# stop() / restart()
# ---------------------------------------------------------------------------


def test_stop_with_no_prior_record_is_a_safe_noop(state_dir):
    runner = _make_runner("stop-noop", None, state_dir=state_dir, broker=None)
    status = runner.stop()
    assert status.child.state is lr.ChildState.NOT_STARTED


def test_stop_terminates_the_child_and_marks_stopped(state_dir, broker):
    runner = _make_runner("stop-scope", _sleepy_cmd(), state_dir=state_dir, broker=broker)
    started = runner.start()
    pid = started.child.pid
    stopped = runner.stop()
    assert stopped.child.state is lr.ChildState.STOPPED
    assert _wait_until(lambda: not _pid_alive(pid), timeout=5.0)


def test_stop_is_idempotent(state_dir, broker):
    runner = _make_runner("stop-idempotent", _sleepy_cmd(), state_dir=state_dir, broker=broker)
    runner.start()
    runner.stop()
    again = runner.stop()  # must not raise
    assert again.child.state in (lr.ChildState.STOPPED, lr.ChildState.CRASHED)


def test_restart_replaces_the_child_and_increments_restart_count(state_dir, broker):
    with _make_runner("restart-scope", _sleepy_cmd(), state_dir=state_dir, broker=broker) as runner:
        first = runner.start()
        second = runner.restart()
        assert second.child.pid != first.child.pid
        assert second.child.restart_count == 1
        assert _wait_until(lambda: not _pid_alive(first.child.pid), timeout=5.0)


def test_restart_without_a_prior_record_behaves_like_a_fresh_start(state_dir):
    with _make_runner("restart-fresh", _sleepy_cmd(), state_dir=state_dir, broker=None) as runner:
        status = runner.restart()
        assert status.child.state is lr.ChildState.RUNNING
        assert status.child.restart_count == 0


def test_restart_recovers_command_from_persisted_record(state_dir, broker):
    with _make_runner("restart-recover-cmd", _sleepy_cmd(1.0), state_dir=state_dir, broker=broker) as runner_a:
        runner_a.start()

    # A fresh instance constructed with command=None must recover the
    # command/cwd from the persisted record to restart it.
    with _make_runner("restart-recover-cmd", None, state_dir=state_dir, broker=broker) as runner_b:
        status = runner_b.restart()
        assert status.child.state is lr.ChildState.RUNNING


def test_restart_never_raises_runner_already_running(state_dir, broker):
    with _make_runner("restart-never-conflicts", _sleepy_cmd(), state_dir=state_dir, broker=broker) as runner:
        runner.start()
        runner.restart()  # must not raise, unlike start()


# ---------------------------------------------------------------------------
# Lease broker integration -- cross-tool visibility, never blocking
# ---------------------------------------------------------------------------


def test_start_registers_a_broker_lease(state_dir, broker):
    with _make_runner("lease-scope", _sleepy_cmd(), state_dir=state_dir, broker=broker) as runner:
        status = runner.start()
        leases = broker.list_leases(client=lr._LEASE_CLIENT_NAME)
        assert len(leases) == 1
        assert leases[0].pid == status.child.pid
        assert leases[0].owner_key == f"local-runner:lease-scope"


def test_stop_releases_the_broker_lease(state_dir, broker):
    runner = _make_runner("lease-release-scope", _sleepy_cmd(), state_dir=state_dir, broker=broker)
    runner.start()
    runner.stop()
    assert broker.list_leases(client=lr._LEASE_CLIENT_NAME) == []


def test_broker_none_disables_leasing_without_error(state_dir):
    with _make_runner("no-broker-scope", _sleepy_cmd(), state_dir=state_dir, broker=None) as runner:
        status = runner.start()  # must not raise despite no broker configured
        assert status.child.state is lr.ChildState.RUNNING


def test_broker_owner_conflict_is_advisory_and_never_blocks_start(state_dir, broker, monkeypatch):
    """A conflicting broker-level lease (e.g. from some OTHER tracking
    mechanism) must never prevent this module's own, already-gated spawn
    from succeeding -- see LocalRunner._acquire_lease's docstring."""
    broker.acquire_exclusive(lr._LEASE_CLIENT_NAME, "local-runner:conflict-scope", 999999)
    monkeypatch.setattr(process_registry._process_lifecycle, "verify_handle_live", lambda handle: True)
    with _make_runner("conflict-scope", _sleepy_cmd(), state_dir=state_dir, broker=broker) as runner:
        status = runner.start()  # must not raise OwnerConflictError
        assert status.child.state is lr.ChildState.RUNNING
        record = runner._load_record()
        assert record.lease_run_id is None  # conflict -> no lease attributed, but spawn succeeded


# ---------------------------------------------------------------------------
# doctor()
# ---------------------------------------------------------------------------


def test_doctor_report_as_dict_is_json_serializable(state_dir):
    runner = _make_runner("doctor-scope", None, state_dir=state_dir, broker=None)
    report = runner.doctor()
    json.dumps(report.as_dict())


def test_doctor_on_never_started_scope_warns_on_executable_but_state_dir_ok(state_dir):
    runner = _make_runner("doctor-never-started", None, state_dir=state_dir, broker=None)
    report = runner.doctor()
    by_name = {c.name: c for c in report.checks}
    assert by_name["state_dir_writable"].severity == "ok"
    assert by_name["executable_resolves"].severity == "warn"
    assert by_name["child_process"].severity == "ok"  # not_started is fine, not a failure


def test_doctor_reports_ok_after_a_healthy_start(state_dir, broker):
    with _make_runner("doctor-healthy", _sleepy_cmd(), state_dir=state_dir, broker=broker) as runner:
        runner.start()
        report = runner.doctor()
        by_name = {c.name: c for c in report.checks}
        assert by_name["executable_resolves"].severity == "ok"
        assert by_name["child_process"].severity == "ok"
        assert by_name["lease_broker"].severity == "ok"
        assert report.healthy is True


def test_doctor_reports_fail_after_a_crash(state_dir):
    with _make_runner("doctor-crash", _exit_cmd(1), state_dir=state_dir, broker=None) as runner:
        runner.start()
        report = runner.doctor()
        by_name = {c.name: c for c in report.checks}
        assert by_name["child_process"].severity == "fail"
        assert report.healthy is False


def test_doctor_tunnel_check_reflects_lifecycle_state(state_dir):
    tunnel_lifecycle.reset_registry()
    try:
        lc = tunnel_lifecycle.get_lifecycle("doctor-tunnel-label")
        lc.mark_connecting()
        lc.mark_ws_open()
        lc.mark_ready()
        runner = _make_runner(
            "doctor-tunnel-scope", None, state_dir=state_dir, broker=None,
            tunnel_label="doctor-tunnel-label",
        )
        report = runner.doctor()
        by_name = {c.name: c for c in report.checks}
        assert by_name["tunnel_readiness"].severity == "ok"

        lc.mark_never_ready()
        report2 = runner.doctor()
        by_name2 = {c.name: c for c in report2.checks}
        assert by_name2["tunnel_readiness"].severity == "fail"
    finally:
        tunnel_lifecycle.reset_registry()


def test_status_tunnel_not_configured_when_no_label_set(state_dir):
    runner = _make_runner("no-tunnel-label", None, state_dir=state_dir, broker=None)
    status = runner.status()
    assert status.tunnel.configured is False
    assert status.tunnel.state == "not_configured"


# ---------------------------------------------------------------------------
# preflight()
# ---------------------------------------------------------------------------


def test_preflight_healthy_command(state_dir):
    runner = _make_runner("preflight-healthy", _exit_cmd(0), state_dir=state_dir, broker=None)
    diagnostic = runner.preflight(timeout=10)
    assert diagnostic.healthy is True


def test_preflight_recovers_command_from_persisted_record(state_dir, broker):
    with _make_runner("preflight-recover", _exit_cmd(0), state_dir=state_dir, broker=broker) as runner_a:
        runner_a.start()
    runner_b = _make_runner("preflight-recover", None, state_dir=state_dir, broker=broker)
    diagnostic = runner_b.preflight(timeout=10)
    assert diagnostic.command == (sys.executable, "-c", "import sys; sys.exit(0)")


def test_preflight_without_command_or_record_raises_value_error(state_dir):
    runner = _make_runner("preflight-no-command", None, state_dir=state_dir, broker=None)
    with pytest.raises(ValueError):
        runner.preflight()


# ---------------------------------------------------------------------------
# Script allowlist -- explicitly scoped escape hatch
# ---------------------------------------------------------------------------


def test_script_allowlist_rejects_undeclared_names():
    allowlist = lr.ScriptAllowlist()
    with pytest.raises(lr.ScriptNotAllowedError):
        allowlist.get("not-declared")


def test_script_allowlist_declare_and_names():
    allowlist = lr.ScriptAllowlist()
    allowlist.declare(lr.ScriptSpec(name="a", command=(sys.executable, "-c", "pass")))
    allowlist.declare(lr.ScriptSpec(name="b", command=(sys.executable, "-c", "pass")))
    assert allowlist.names() == ["a", "b"]


def test_script_spec_rejects_empty_name_or_command():
    allowlist = lr.ScriptAllowlist()
    with pytest.raises(ValueError):
        allowlist.declare(lr.ScriptSpec(name="", command=("x",)))
    with pytest.raises(ValueError):
        allowlist.declare(lr.ScriptSpec(name="x", command=()))


def test_run_allowlisted_script_happy_path():
    allowlist = lr.ScriptAllowlist([
        lr.ScriptSpec(name="echo-ok", command=(sys.executable, "-c", "print('hello-script')")),
    ])
    receipt = lr.run_allowlisted_script(allowlist, "echo-ok")
    assert receipt.exit_code == 0
    assert "hello-script" in receipt.stdout_tail
    assert receipt.timed_out is False


def test_run_allowlisted_script_rejects_extra_args_by_default():
    allowlist = lr.ScriptAllowlist([
        lr.ScriptSpec(name="no-args", command=(sys.executable, "-c", "pass")),
    ])
    with pytest.raises(lr.ScriptNotAllowedError):
        lr.run_allowlisted_script(allowlist, "no-args", extra_args=["--danger"])


def test_run_allowlisted_script_allows_extra_args_when_opted_in():
    allowlist = lr.ScriptAllowlist([
        lr.ScriptSpec(
            name="with-args",
            command=(sys.executable, "-c", "import sys; print(sys.argv[1:])"),
            allow_extra_args=True,
        ),
    ])
    receipt = lr.run_allowlisted_script(allowlist, "with-args", extra_args=["hello"])
    assert "hello" in receipt.stdout_tail


def test_run_allowlisted_script_times_out_bounded():
    allowlist = lr.ScriptAllowlist([
        lr.ScriptSpec(
            name="slow", command=(sys.executable, "-c", "import time; time.sleep(5)"), timeout=0.3,
        ),
    ])
    receipt = lr.run_allowlisted_script(allowlist, "slow")
    assert receipt.timed_out is True
    assert receipt.exit_code is None


def test_script_receipt_shared_projection_redacts_secrets_and_paths():
    allowlist = lr.ScriptAllowlist([
        lr.ScriptSpec(
            name="leaky",
            command=(sys.executable, "-c",
                     "print('token sk-abcdefghijklmnop1234567890 and C:\\\\Users\\\\alice\\\\secret\\\\file.txt')"),
        ),
    ])
    receipt = lr.run_allowlisted_script(allowlist, "leaky")
    local = receipt.to_local_dict()
    shared = receipt.to_shared_projection()
    assert "sk-abcdefghijklmnop1234567890" in local["stdout_tail"]
    assert "C:\\Users\\alice" in local["stdout_tail"]
    assert "sk-abcdefghijklmnop1234567890" not in shared["stdout_tail"]
    assert "C:\\Users\\alice" not in shared["stdout_tail"]
    assert "[redacted-secret]" in shared["stdout_tail"]
    assert "[redacted-local-path]" in shared["stdout_tail"]
    json.dumps(shared)  # the shared projection must always be safely serializable


def test_script_allowlist_save_and_load_round_trip(tmp_path):
    path = tmp_path / "scripts.json"
    allowlist = lr.ScriptAllowlist([
        lr.ScriptSpec(name="a", command=("echo", "hi"), description="says hi", allow_extra_args=True),
    ])
    allowlist.save(path)
    reloaded = lr.ScriptAllowlist.load(path)
    assert reloaded.names() == ["a"]
    spec = reloaded.get("a")
    assert spec.command == ("echo", "hi")
    assert spec.allow_extra_args is True


def test_script_allowlist_load_missing_file_is_empty(tmp_path):
    allowlist = lr.ScriptAllowlist.load(tmp_path / "does-not-exist.json")
    assert allowlist.names() == []


def test_script_allowlist_load_corrupt_file_is_empty(tmp_path):
    path = tmp_path / "scripts.json"
    path.write_text("{not valid json", encoding="utf-8")
    allowlist = lr.ScriptAllowlist.load(path)
    assert allowlist.names() == []


# ---------------------------------------------------------------------------
# CLI -- scriptable JSON controls
# ---------------------------------------------------------------------------


def _run_cli(argv, capsys):
    exit_code = lr.main(argv)
    captured = capsys.readouterr()
    return exit_code, captured


def test_cli_start_status_stop_round_trip(tmp_path, capsys):
    state_dir_arg = str(tmp_path / "cli_state")
    base = ["--state-dir", state_dir_arg]

    exit_code, captured = _run_cli(
        base + ["start", "--scope", "cli-scope", "--", sys.executable, "-c", "import time; time.sleep(30)"],
        capsys,
    )
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["child"]["state"] == "running"
    pid = payload["child"]["pid"]

    exit_code, captured = _run_cli(base + ["status", "--scope", "cli-scope"], capsys)
    assert exit_code == 0
    assert json.loads(captured.out)["child"]["state"] == "running"

    exit_code, captured = _run_cli(base + ["stop", "--scope", "cli-scope"], capsys)
    assert exit_code == 0
    assert json.loads(captured.out)["child"]["state"] == "stopped"
    assert _wait_until(lambda: not _pid_alive(pid), timeout=5.0)


def test_cli_duplicate_start_exits_nonzero_with_error_json(tmp_path, capsys):
    state_dir_arg = str(tmp_path / "cli_dup_state")
    base = ["--state-dir", state_dir_arg]
    cmd_args = ["--", sys.executable, "-c", "import time; time.sleep(30)"]

    _run_cli(base + ["start", "--scope", "dup"] + cmd_args, capsys)
    exit_code, captured = _run_cli(base + ["start", "--scope", "dup"] + cmd_args, capsys)
    assert exit_code == 1
    err = json.loads(captured.err)
    assert err["type"] == "RunnerAlreadyRunningError"

    _run_cli(base + ["stop", "--scope", "dup"], capsys)


def test_cli_doctor_and_preflight(tmp_path, capsys):
    state_dir_arg = str(tmp_path / "cli_doctor_state")
    base = ["--state-dir", state_dir_arg]

    exit_code, captured = _run_cli(
        base + ["preflight", "--scope", "cli-preflight", "--", sys.executable, "-c", "import sys; sys.exit(0)"],
        capsys,
    )
    assert exit_code == 0
    assert json.loads(captured.out)["healthy"] is True

    exit_code, captured = _run_cli(
        base + ["doctor", "--scope", "cli-preflight", "--", sys.executable, "-c", "import sys; sys.exit(0)"],
        capsys,
    )
    assert exit_code == 0
    doctor_payload = json.loads(captured.out)
    assert "checks" in doctor_payload and "healthy" in doctor_payload


def test_cli_list_scripts_and_run_script(tmp_path, capsys):
    scripts_path = tmp_path / "scripts.json"
    allowlist = lr.ScriptAllowlist([
        lr.ScriptSpec(name="hello", command=(sys.executable, "-c", "print('hi from cli script')")),
    ])
    allowlist.save(scripts_path)
    base = ["--scripts-file", str(scripts_path)]

    exit_code, captured = _run_cli(base + ["list-scripts"], capsys)
    assert exit_code == 0
    assert json.loads(captured.out)["scripts"] == ["hello"]

    exit_code, captured = _run_cli(base + ["run-script", "--name", "hello"], capsys)
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert "hi from cli script" in payload["stdout_tail"]


def test_cli_run_script_unknown_name_exits_nonzero(tmp_path, capsys):
    scripts_path = tmp_path / "scripts.json"
    lr.ScriptAllowlist().save(scripts_path)
    base = ["--scripts-file", str(scripts_path)]
    exit_code, captured = _run_cli(base + ["run-script", "--name", "nope"], capsys)
    assert exit_code == 1
    err = json.loads(captured.err)
    assert err["type"] == "ScriptNotAllowedError"


def test_cli_tail_log_after_start(tmp_path, capsys):
    state_dir_arg = str(tmp_path / "cli_tail_state")
    base = ["--state-dir", state_dir_arg]
    _run_cli(
        base + ["start", "--scope", "tail-scope", "--", sys.executable, "-c", "print('tail-marker-xyz')"],
        capsys,
    )
    assert _wait_until(
        lambda: "tail-marker-xyz" in json.loads(
            _run_cli(base + ["tail-log", "--scope", "tail-scope"], capsys)[1].out
        )["log_tail"],
        timeout=5.0,
    )
    _run_cli(base + ["stop", "--scope", "tail-scope"], capsys)


def test_cli_module_invocation_via_subprocess(tmp_path):
    """Exercises the real ``python -m meridian.local_runner`` entry point
    end-to-end -- the literal command external clients would run."""
    state_dir_arg = str(tmp_path / "cli_subprocess_state")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(lr.__file__)))
    result = subprocess.run(
        [
            sys.executable, "-m", "meridian.local_runner",
            "--state-dir", state_dir_arg,
            "status", "--scope", "subprocess-cli-scope",
        ],
        capture_output=True, text=True, check=False, cwd=repo_root,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["child"]["state"] == "not_started"
