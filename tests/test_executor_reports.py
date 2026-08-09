"""394bcbdf — R2-E: resource-aware / asynchronously-recoverable / idempotent
completion timeouts. Dispatch-layer coverage.

This file covers the pieces of the design that live in
``meridian/mcp/handler.py`` and ``meridian/process_budget.py``:

  1. The completion-attempt phase registry (accepted -> pending ->
     committed | failed), keyed by correlation_id, and its recovery API
     ``get_completion_attempt`` — the "asynchronously recoverable" half of
     the design: a caller holding only a timed-out response's
     correlation_id can look back at what phase that specific attempt
     reached.
  2. Resource-aware retry-after diagnostics folded into the dispatch-level
     COMPLETE_SPRINT_ITEM_TIMEOUT response.
  3. The server-self ``ProcessBudgetMonitor`` singleton helpers in
     ``meridian/process_budget.py`` (``reset_server_process_monitor``,
     ``get_server_process_monitor``, ``sample_server_process``,
     ``retry_after_seconds_for_report``).
  4. ``meridian.server.lifespan`` wiring the singleton onto ``app.state``.

The underlying dispatch-level timeout classification itself (committed /
timed_out_before_commit / unknown_outcome) is already covered by
tests/test_sprint_item_status_race.py; this file reuses the same
monkeypatch techniques established there to add coverage for the NEW
394bcbdf additions layered on top.
"""
import asyncio

import pytest

# meridian.server imports meridian.mcp.handler at module bottom, and
# meridian.mcp.handler imports meridian.server at module top (by design —
# see handler.py's own module docstring). Importing meridian.server FIRST,
# here, guarantees that ordering resolves correctly regardless of which
# test in this file (or the whole suite) happens to import
# meridian.mcp.handler first — importing handler.py standalone before
# server.py has ever been imported raises a circular-import ImportError.
import meridian.server as _server_bootstrap  # noqa: F401

from meridian import db as db_module


async def _project_with_item(db):
    p = await db_module.create_project(db, "executor-reports")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "reported item")
    return p, item


# ---------------------------------------------------------------------------
# Completion-attempt phase registry — direct unit tests (no DB needed).
# ---------------------------------------------------------------------------


def test_registry_records_phases_in_order_and_latest_phase():
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    mh._record_completion_phase("corr-direct-1", "accepted", item_id="i1", project_id="p1")
    mh._record_completion_phase("corr-direct-1", "pending", item_id="i1", project_id="p1")
    mh._record_completion_phase(
        "corr-direct-1", "committed", item_id="i1", project_id="p1",
        completion_outcome="committed",
    )

    attempt = mh.get_completion_attempt("corr-direct-1")
    assert attempt is not None
    assert [p["phase"] for p in attempt["phases"]] == ["accepted", "pending", "committed"]
    assert attempt["latest_phase"] == "committed"
    assert attempt["item_id"] == "i1"
    assert attempt["project_id"] == "p1"
    assert attempt["phases"][-1]["completion_outcome"] == "committed"


def test_registry_unknown_correlation_id_returns_none():
    from meridian.mcp import handler as mh

    assert mh.get_completion_attempt("no-such-correlation-id-ever") is None


def test_registry_lookup_returns_a_copy_not_the_live_entry():
    """The returned dict must be a defensive copy — mutating it must not
    corrupt the registry's own state."""
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    mh._record_completion_phase("corr-direct-2", "accepted")
    attempt = mh.get_completion_attempt("corr-direct-2")
    attempt["phases"].append({"phase": "tampered", "at": 0})
    attempt["latest_phase"] = "tampered"

    fresh = mh.get_completion_attempt("corr-direct-2")
    assert fresh["latest_phase"] == "accepted"
    assert [p["phase"] for p in fresh["phases"]] == ["accepted"]


def test_registry_bounded_fifo_eviction():
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    total = mh._COMPLETION_ATTEMPT_MAX + 50
    for i in range(total):
        mh._record_completion_phase(f"corr-bulk-{i}", "accepted")

    assert len(mh._completion_attempts) == mh._COMPLETION_ATTEMPT_MAX
    # The oldest 50 were evicted first (FIFO).
    assert mh.get_completion_attempt("corr-bulk-0") is None
    assert mh.get_completion_attempt("corr-bulk-49") is None
    assert mh.get_completion_attempt(f"corr-bulk-{total - 1}") is not None
    mh._completion_attempts.clear()


def test_record_completion_phase_never_raises_on_bad_input():
    """The registry is a diagnostics side channel — it must never break a
    real tool call, even with pathological input."""
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    # None correlation_id is an odd input but must not raise.
    mh._record_completion_phase(None, "accepted")  # type: ignore[arg-type]
    assert mh.get_completion_attempt(None) is not None  # type: ignore[arg-type]
    mh._completion_attempts.clear()


# ---------------------------------------------------------------------------
# Dispatch-level integration: _dispatch_mcp_tool wires the registry + the
# resource-aware timeout diagnostics together.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_normal_completion_records_accepted_pending_committed(db):
    import meridian.server as srv
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    p, item = await _project_with_item(db)

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {"project_id": p["id"], "item_id": item["id"]}, db, "/tmp",
    )
    assert result["status"] == "done"
    corr_id = result["correlation_id"]

    attempt = mh.get_completion_attempt(corr_id)
    assert attempt is not None
    assert [p["phase"] for p in attempt["phases"]] == ["accepted", "pending", "committed"]
    assert attempt["item_id"] == item["id"]
    assert attempt["project_id"] == p["id"]


@pytest.mark.asyncio
async def test_dispatch_idempotent_retry_records_a_second_committed_attempt(db):
    """A distinct retry call (its own correlation_id) against an
    already-done item is its own attempt, also recorded as committed —
    the registry tracks ATTEMPTS, not items."""
    import meridian.server as srv
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    p, item = await _project_with_item(db)

    first = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {"project_id": p["id"], "item_id": item["id"]}, db, "/tmp",
    )
    second = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {"project_id": p["id"], "item_id": item["id"]}, db, "/tmp",
    )
    assert first["correlation_id"] != second["correlation_id"]
    assert second["completion_outcome"] == "already_committed"

    second_attempt = mh.get_completion_attempt(second["correlation_id"])
    assert second_attempt["latest_phase"] == "committed"


@pytest.mark.asyncio
async def test_dispatch_timeout_records_failed_phase_when_not_yet_committed(db, monkeypatch):
    import meridian.server as srv
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    p, item = await _project_with_item(db)
    monkeypatch.setattr(mh, "_COMPLETE_SPRINT_ITEM_DISPATCH_TIMEOUT_S", 0.05)

    from meridian.mcp.handlers import sprint_tools as st_mod

    async def _never_completes(args, db_arg, data_dir, tenant, mcp_tenant_id):
        await asyncio.sleep(5.0)
        raise AssertionError("should have been cancelled by the dispatch timeout")

    monkeypatch.setattr(st_mod, "handle_complete_sprint_item", _never_completes)

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {"project_id": p["id"], "item_id": item["id"]}, db, "/tmp",
    )
    assert result["completion_outcome"] == "timed_out_before_commit"

    attempt = mh.get_completion_attempt(result["correlation_id"])
    assert attempt is not None
    assert attempt["latest_phase"] == "failed"
    assert attempt["phases"][-1]["completion_outcome"] == "timed_out_before_commit"


@pytest.mark.asyncio
async def test_dispatch_timeout_records_committed_phase_when_actually_landed(db, monkeypatch):
    """A dispatch-level timeout whose re-query shows the write actually
    landed must be recorded as 'committed', not 'failed' — the phase
    registry must not misreport a genuinely-successful-but-slow attempt."""
    import meridian.server as srv
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    p, item = await _project_with_item(db)
    monkeypatch.setattr(mh, "_COMPLETE_SPRINT_ITEM_DISPATCH_TIMEOUT_S", 0.05)

    from meridian.mcp.handlers import sprint_tools as st_mod
    real_handle = st_mod.handle_complete_sprint_item

    async def _slow_handle(args, db_arg, data_dir, tenant, mcp_tenant_id):
        result = await real_handle(args, db_arg, data_dir, tenant, mcp_tenant_id)
        await asyncio.sleep(5.0)
        return result

    monkeypatch.setattr(st_mod, "handle_complete_sprint_item", _slow_handle)

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {"project_id": p["id"], "item_id": item["id"]}, db, "/tmp",
    )
    assert result["completion_outcome"] == "committed"

    attempt = mh.get_completion_attempt(result["correlation_id"])
    assert attempt["latest_phase"] == "committed"


@pytest.mark.asyncio
async def test_dispatch_timeout_response_includes_resource_diagnostics(db, monkeypatch):
    import meridian.server as srv
    from meridian.mcp import handler as mh

    p, item = await _project_with_item(db)
    monkeypatch.setattr(mh, "_COMPLETE_SPRINT_ITEM_DISPATCH_TIMEOUT_S", 0.05)

    from meridian.mcp.handlers import sprint_tools as st_mod

    async def _never_completes(args, db_arg, data_dir, tenant, mcp_tenant_id):
        await asyncio.sleep(5.0)

    monkeypatch.setattr(st_mod, "handle_complete_sprint_item", _never_completes)

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {"project_id": p["id"], "item_id": item["id"]}, db, "/tmp",
    )
    assert "resource_diagnostics" in result
    diag = result["resource_diagnostics"]
    assert set(diag.keys()) == {"action", "reason", "retry_after_seconds"}
    assert isinstance(diag["retry_after_seconds"], (int, float))


@pytest.mark.asyncio
async def test_dispatch_timeout_surfaces_real_breach_in_message_and_diagnostics(db, monkeypatch):
    import meridian.server as srv
    from meridian.mcp import handler as mh
    import meridian.process_budget as process_budget_module

    p, item = await _project_with_item(db)
    monkeypatch.setattr(mh, "_COMPLETE_SPRINT_ITEM_DISPATCH_TIMEOUT_S", 0.05)

    from meridian.mcp.handlers import sprint_tools as st_mod

    async def _never_completes(args, db_arg, data_dir, tenant, mcp_tenant_id):
        await asyncio.sleep(5.0)

    monkeypatch.setattr(st_mod, "handle_complete_sprint_item", _never_completes)

    fake_budget = process_budget_module.ProcessBudget(sample_interval_seconds=17.0)
    fake_report = process_budget_module.BudgetReport(
        label="server-self", pid=999, run_id=None, sample=None,
        budget=fake_budget, action="kill", reason="cpu 999.0% exceeds budget 400.0%",
    )
    monkeypatch.setattr(
        process_budget_module, "sample_server_process", lambda *a, **k: fake_report
    )

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {"project_id": p["id"], "item_id": item["id"]}, db, "/tmp",
    )
    diag = result["resource_diagnostics"]
    assert diag["action"] == "kill"
    assert diag["retry_after_seconds"] == 17.0
    assert "resource pressure" in result["message"]
    assert "17" in result["message"]


@pytest.mark.asyncio
async def test_dispatch_timeout_diagnostics_degrade_cleanly_on_sampling_failure(db, monkeypatch):
    """A resource-sampling failure must never turn an already-timed-out
    response into a second, harder failure."""
    import meridian.server as srv
    from meridian.mcp import handler as mh
    import meridian.process_budget as process_budget_module

    p, item = await _project_with_item(db)
    monkeypatch.setattr(mh, "_COMPLETE_SPRINT_ITEM_DISPATCH_TIMEOUT_S", 0.05)

    from meridian.mcp.handlers import sprint_tools as st_mod

    async def _never_completes(args, db_arg, data_dir, tenant, mcp_tenant_id):
        await asyncio.sleep(5.0)

    monkeypatch.setattr(st_mod, "handle_complete_sprint_item", _never_completes)

    def _boom(*a, **k):
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(process_budget_module, "sample_server_process", _boom)

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item", {"project_id": p["id"], "item_id": item["id"]}, db, "/tmp",
    )
    assert result["error"] == "COMPLETE_SPRINT_ITEM_TIMEOUT"
    assert result["resource_diagnostics"]["action"] == "none"
    assert result["resource_diagnostics"]["reason"] == "unavailable"


@pytest.mark.asyncio
async def test_other_tools_unaffected_by_phase_registry(db):
    """The registry only ever tracks complete_sprint_item attempts — an
    unrelated tool call must not appear in it."""
    import meridian.server as srv
    from meridian.mcp import handler as mh

    mh._completion_attempts.clear()
    p, item = await _project_with_item(db)
    result = await srv._dispatch_mcp_tool(
        "get_sprint_items", {"project_id": p["id"]}, db, "/tmp",
    )
    assert isinstance(result, list)
    assert len(mh._completion_attempts) == 0


# ---------------------------------------------------------------------------
# process_budget.py — server-self monitor singleton helpers.
# ---------------------------------------------------------------------------


def test_sample_server_process_reports_own_pid():
    import os
    import meridian.process_budget as process_budget_module

    process_budget_module.reset_server_process_monitor()
    report = process_budget_module.sample_server_process()
    assert report.pid == os.getpid()
    assert report.label == "server-self"


def test_reset_server_process_monitor_creates_a_fresh_singleton_each_time():
    import meridian.process_budget as process_budget_module

    m1 = process_budget_module.reset_server_process_monitor()
    m2 = process_budget_module.get_server_process_monitor()
    assert m1 is m2, "get_server_process_monitor must return the same singleton reset() created"

    m3 = process_budget_module.reset_server_process_monitor()
    assert m3 is not m1, "reset_server_process_monitor must create a NEW monitor, not reuse state"


def test_get_server_process_monitor_lazily_creates_when_never_reset():
    import meridian.process_budget as process_budget_module

    process_budget_module._SERVER_PROCESS_MONITOR = None
    monitor = process_budget_module.get_server_process_monitor()
    assert monitor is not None
    assert monitor.label == "server-self"
    # Idempotent on repeated access.
    assert process_budget_module.get_server_process_monitor() is monitor


@pytest.mark.parametrize(
    "action,reason,expected",
    [
        ("none", "within_budget", 0.0),
        ("none", "no_sample", 0.0),
        ("none", "budget_disabled", 0.0),
        ("quiesce", "memory exceeds budget", 30.0),
        ("kill", "memory exceeds budget", 30.0),
        ("none", "backoff_cooldown", 30.0),
    ],
)
def test_retry_after_seconds_for_report_mapping(action, reason, expected):
    import meridian.process_budget as process_budget_module

    budget = process_budget_module.ProcessBudget(sample_interval_seconds=30.0)
    report = process_budget_module.BudgetReport(
        label="x", pid=1, run_id=None, sample=None, budget=budget,
        action=action, reason=reason,
    )
    assert process_budget_module.retry_after_seconds_for_report(report) == expected


# ---------------------------------------------------------------------------
# server.py lifespan wiring.
# ---------------------------------------------------------------------------


def test_lifespan_initializes_process_budget_monitor_on_app_state(client):
    """The 'client' fixture (tests/conftest.py) runs the REAL FastAPI
    lifespan via TestClient — confirms meridian.server.lifespan actually
    calls process_budget.reset_server_process_monitor() and stores the
    result on app.state, not just that the helper exists in isolation."""
    import meridian.process_budget as process_budget_module

    monitor = client.app.state.process_budget_monitor
    assert isinstance(monitor, process_budget_module.ProcessBudgetMonitor)
    assert monitor.label == "server-self"
