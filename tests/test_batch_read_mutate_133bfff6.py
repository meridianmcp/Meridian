"""Tests for sprint item 133bfff6 -- generalized batch_read and batch_mutate
for domain-aware MCP operations.

Covers:

1. ``batch_read`` (``meridian.batch_read``): independent requests genuinely
   run CONCURRENTLY (proven via overlapping start/end timestamps on injected
   fake adapters, not just a wall-clock threshold); a request with
   ``depends_on`` waits only for its own declared prerequisite(s), not the
   whole batch; a failed prerequisite propagates as ``DEPENDENCY_FAILED``
   without executing the dependent; a ``depends_on`` cycle is detected and
   rejected per-request; duplicate (same adapter+operation+normalized-args+
   depends_on) requests coalesce to one execution (``cache_hit``/
   ``coalesced_with``); a non-default ``cache_policy`` opts a request out of
   coalescing; unknown adapter/operation surface as per-request errors;
   call-level contract violations (empty/non-list requests, missing/
   duplicate ``request_id``) raise ``BatchReadRequestError``; the real
   ``sprint_board`` adapter reads through the existing
   ``get_sprint_items``/``get_sprint_item_pointers`` DB functions and
   enforces project isolation on a cross-project ``sprint_item_id``.
2. ``batch_mutate`` (``meridian.batch_mutate`` +
   ``meridian.db.batch_management.execute_mixed_mutation_batch``): a mixed
   ``sprint_item_pointer`` + ``sprint_item_update`` batch commits atomically;
   a genuine mid-mutation failure (monkeypatched, mirroring
   ``tests/test_batch_management_writes.py``'s established technique) rolls
   back an ALREADY-APPLIED entry of the OTHER kind, proving rollback works
   across the mixed adapter set, not just within one; ``best_effort`` commits
   partially and reports the failure; a repeated call with the same
   ``idempotency_key`` replays without double-applying; an entry naming a
   different ``project_id`` than the batch's own is rejected outright
   (never silently ignored); a cross-project ``item_id`` is still caught by
   the existing NOT_FOUND check; sprint-item CREATION is refused through
   this surface (unknown ``kind``, or ``action="create"`` on a
   ``sprint_item_update`` entry); request-shape validation mirrors
   ``batch_ops``'s established wording.
"""
from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from meridian import batch_mutate as bmut_module
from meridian import batch_read as br_module
from meridian import db as db_module
from meridian.db import batch_management as bm


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_batch_management_writes.py's local style --
# the shared `db` fixture comes from tests/conftest.py)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "batch-rw-test-proj")


@pytest_asyncio.fixture
async def other_project(db):
    return await db_module.create_project(db, "batch-rw-other-proj")


def _sleepy_adapters(call_log: list, default_delay: float = 0.15):
    """A fake adapter registry recording (key, start, end) perf_counter
    timestamps for each 'sleep' call, plus a 'count' operation that counts
    real invocations -- used to prove concurrency/coalescing without
    depending on any real DB or backend-specific parallelism."""

    async def _sleep_op(db, project_id, args):
        delay = args.get("delay", default_delay)
        key = args.get("key", "?")
        start = time.perf_counter()
        await asyncio.sleep(delay)
        end = time.perf_counter()
        call_log.append((key, start, end))
        return {"key": key, "slept": delay}

    counter = {"n": 0}

    async def _count_op(db, project_id, args):
        counter["n"] += 1
        await asyncio.sleep(0.01)
        return {"call_number": counter["n"]}

    return {"test": {"sleep": _sleep_op, "count": _count_op}}, counter


# ---------------------------------------------------------------------------
# batch_read: concurrency + dependency ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_independent_requests_run_concurrently(db, project):
    call_log: list = []
    registry, _ = _sleepy_adapters(call_log, default_delay=0.15)
    requests = [
        {"request_id": f"r{i}", "adapter": "test", "operation": "sleep", "args": {"key": f"r{i}"}}
        for i in range(4)
    ]
    t0 = time.perf_counter()
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, adapters=registry,
    )
    wall_elapsed = time.perf_counter() - t0
    assert all(r["status"] == "ok" for r in resp["results"])
    # 4 requests x 0.15s each -- serialized that is >=0.6s. Assert BOTH a
    # generous wall-clock bound (jitter-tolerant) AND the deterministic
    # overlap proof below (all starts precede any finish).
    assert wall_elapsed < 0.45, f"expected concurrent dispatch, took {wall_elapsed:.3f}s"
    starts = [s for _, s, _ in call_log]
    ends = [e for _, _, e in call_log]
    assert max(starts) < min(ends), "requests did not overlap -- looks serial, not concurrent"


@pytest.mark.asyncio
async def test_dependency_waits_only_for_declared_prereq(db, project):
    call_log: list = []
    registry, _ = _sleepy_adapters(call_log)
    requests = [
        {"request_id": "a", "adapter": "test", "operation": "sleep",
         "args": {"key": "a", "delay": 0.15}},
        {"request_id": "b", "adapter": "test", "operation": "sleep",
         "args": {"key": "b", "delay": 0.03}, "depends_on": ["a"]},
        {"request_id": "c", "adapter": "test", "operation": "sleep",
         "args": {"key": "c", "delay": 0.03}},
    ]
    resp = await br_module.batch_read(
        db, project_id=project["id"], requests=requests, adapters=registry,
    )
    assert all(r["status"] == "ok" for r in resp["results"])
    times = {k: (s, e) for k, s, e in call_log}
    a_start, a_end = times["a"]
    b_start, _ = times["b"]
    c_start, _ = times["c"]
    assert b_start >= a_end, "b must wait for its declared prerequisite a"
    assert c_start < a_end, "c (independent) must NOT wait for a -- not the whole batch"


@pytest.mark.asyncio
async def test_dependency_failure_propagates_without_executing_dependent(db, project):
    executed = {"b": False}

    async def _fail_op(db, project_id, args):
        raise ValueError("boom")

    async def _ok_op(db, project_id, args):
        executed["b"] = True
        return {"ok": True}

    registry = {"test": {"fail": _fail_op, "ok": _ok_op}}
    requests = [
        {"request_id": "a", "adapter": "test", "operation": "fail"},
        {"request_id": "b", "adapter": "test", "operation": "ok", "depends_on": ["a"]},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests, adapters=registry)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert by_id["a"]["status"] == "error"
    assert by_id["a"]["error_code"] == "VALIDATION_ERROR"
    assert by_id["b"]["status"] == "error"
    assert by_id["b"]["error_code"] == "DEPENDENCY_FAILED"
    assert executed["b"] is False


@pytest.mark.asyncio
async def test_dependency_cycle_detected_per_request(db, project):
    requests = [
        {"request_id": "a", "adapter": "sprint_board", "operation": "get_sprint_items",
         "depends_on": ["b"]},
        {"request_id": "b", "adapter": "sprint_board", "operation": "get_sprint_items",
         "depends_on": ["a"]},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert by_id["a"]["error_code"] == "DEPENDENCY_CYCLE"
    assert by_id["b"]["error_code"] == "DEPENDENCY_CYCLE"


# ---------------------------------------------------------------------------
# batch_read: coalescing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_requests_coalesce_to_one_execution(db, project):
    call_log: list = []
    registry, counter = _sleepy_adapters(call_log)
    requests = [
        {"request_id": "r1", "adapter": "test", "operation": "count", "args": {"x": 1}},
        {"request_id": "r2", "adapter": "test", "operation": "count", "args": {"x": 1}},
        {"request_id": "r3", "adapter": "test", "operation": "count", "args": {"x": 2}},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests, adapters=registry)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert counter["n"] == 2, "only 2 DISTINCT (adapter, operation, args) should actually execute"
    assert by_id["r1"]["cache_hit"] is False
    assert by_id["r1"]["coalesced_with"] is None
    assert by_id["r2"]["cache_hit"] is True
    assert by_id["r2"]["coalesced_with"] == "r1"
    assert by_id["r2"]["result"] == by_id["r1"]["result"]
    assert by_id["r3"]["cache_hit"] is False


@pytest.mark.asyncio
async def test_cache_policy_opts_out_of_coalescing(db, project):
    call_log: list = []
    registry, counter = _sleepy_adapters(call_log)
    requests = [
        {"request_id": "r1", "adapter": "test", "operation": "count", "args": {"x": 1}},
        {"request_id": "r2", "adapter": "test", "operation": "count", "args": {"x": 1},
         "cache_policy": "no_cache"},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests, adapters=registry)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert counter["n"] == 2, "cache_policy='no_cache' must force its own fresh execution"
    assert by_id["r2"]["cache_hit"] is False
    assert by_id["r2"]["coalesced_with"] is None


# ---------------------------------------------------------------------------
# batch_read: structural / contract errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_adapter_and_operation_are_per_request_errors(db, project):
    requests = [
        {"request_id": "a", "adapter": "does_not_exist", "operation": "whatever"},
        {"request_id": "b", "adapter": "sprint_board", "operation": "does_not_exist"},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert by_id["a"]["error_code"] == "ADAPTER_NOT_FOUND"
    assert by_id["b"]["error_code"] == "OPERATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_call_level_contract_violations_raise(db, project):
    with pytest.raises(br_module.BatchReadRequestError):
        await br_module.batch_read(db, project_id=project["id"], requests=[])
    with pytest.raises(br_module.BatchReadRequestError):
        await br_module.batch_read(
            db, project_id=project["id"],
            requests=[{"adapter": "sprint_board", "operation": "get_sprint_items"}],
        )
    with pytest.raises(br_module.BatchReadRequestError):
        await br_module.batch_read(
            db, project_id=project["id"],
            requests=[
                {"request_id": "dup", "adapter": "sprint_board", "operation": "get_sprint_items"},
                {"request_id": "dup", "adapter": "sprint_board", "operation": "get_sprint_items"},
            ],
        )
    with pytest.raises(br_module.BatchReadRequestError):
        await br_module.batch_read(
            db, project_id=project["id"],
            requests=[{"request_id": "a", "adapter": "sprint_board", "operation": "get_sprint_items"}],
            max_requests=0,
        )


# ---------------------------------------------------------------------------
# batch_read: real sprint_board adapter + project isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sprint_board_adapter_real_reads_and_cross_project_isolation(db, project, other_project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Batch read test item")
    ptr = await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "file",
        [{"uri": "meridian/db/batch_management.py",
                     "selector": {"type": "range", "start_line": 1, "end_line": 5}}], label="src",
    )
    other_item = await db_module.add_sprint_item(db, other_project["id"], "v1", "Other project item")

    requests = [
        {"request_id": "items", "adapter": "sprint_board", "operation": "get_sprint_items",
         "args": {"status": "pending"}},
        {"request_id": "ptrs", "adapter": "sprint_board", "operation": "get_sprint_item_pointers",
         "args": {"sprint_item_id": item["id"]}},
        {"request_id": "cross", "adapter": "sprint_board", "operation": "get_sprint_item_pointers",
         "args": {"sprint_item_id": other_item["id"]}},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}

    assert by_id["items"]["status"] == "ok"
    assert any(it["id"] == item["id"] for it in by_id["items"]["result"])

    assert by_id["ptrs"]["status"] == "ok"
    assert any(p["id"] == ptr["id"] for p in by_id["ptrs"]["result"])

    # Cross-project sprint_item_id must never leak the other project's pointers.
    assert by_id["cross"]["status"] == "error"
    assert by_id["cross"]["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# batch_mutate: mixed-kind success + mid-mutation rollback across kinds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_mutate_mixed_all_or_nothing_success(db, project):
    item1 = await db_module.add_sprint_item(db, project["id"], "v1", "Alpha widget priority target", priority="normal")
    item2 = await db_module.add_sprint_item(db, project["id"], "v1", "Bravo gadget pointer target")

    entries = [
        {"kind": "sprint_item_update", "item_id": item1["id"], "priority": "high", "correlation_key": "u1"},
        {"kind": "sprint_item_pointer", "sprint_item_id": item2["id"], "source_type": "file",
         "targets": [{"uri": "meridian/db/batch_management.py",
                     "selector": {"type": "range", "start_line": 1, "end_line": 5}}], "correlation_key": "p1"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="mutate-success-1",
    )
    assert resp["status"] == "ok"
    assert resp["committed_count"] == 2
    assert resp["failures"] == []
    assert resp["rollback_status"] == "none"
    assert resp["request_id"]

    updated = await db_module.get_sprint_item(db, item1["id"])
    assert updated["priority"] == "high"
    ptrs = await db_module.get_sprint_item_pointers(db, item2["id"])
    assert len(ptrs) == 1


@pytest.mark.asyncio
async def test_batch_mutate_mixed_mid_mutation_abort_rolls_back_across_kinds(db, project, monkeypatch):
    """Genuine mid-mutation failure (monkeypatch -- mirrors
    test_batch_management_writes.py's established technique, since a
    pointer's own validation is pure/complete and never fails mid-mutation
    naturally). Proves the mixed engine's compensation loop correctly
    reverts an ALREADY-APPLIED entry of a DIFFERENT kind (sprint_item_update)
    when a LATER entry of another kind (sprint_item_pointer) fails."""
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Mixed rollback target", priority="normal")

    async def _flaky_add_pointer(*args, **kwargs):
        raise RuntimeError("simulated transient DB failure")

    monkeypatch.setattr(bm.db_module, "add_sprint_item_pointer", _flaky_add_pointer)

    entries = [
        {"kind": "sprint_item_update", "item_id": item["id"], "priority": "high", "correlation_key": "u1"},
        {"kind": "sprint_item_pointer", "sprint_item_id": item["id"], "source_type": "file",
         "targets": [{"uri": "meridian/db/batch_management.py",
                     "selector": {"type": "range", "start_line": 1, "end_line": 5}}], "correlation_key": "p1"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="mixed-rollback-1",
    )
    assert resp["status"] == "failed"
    assert resp["rollback_status"] == "rolled_back"
    by_ck = {r["correlation_key"]: r for r in resp["results"]}
    assert by_ck["u1"]["status"] == "rolled_back"
    assert by_ck["p1"]["status"] == "error"

    reverted = await db_module.get_sprint_item(db, item["id"])
    assert reverted["priority"] == "normal"


@pytest.mark.asyncio
async def test_batch_mutate_best_effort_partial_commit(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Best effort target")
    entries = [
        {"kind": "sprint_item_update", "item_id": item["id"], "priority": "high", "correlation_key": "u1"},
        {"kind": "sprint_item_update", "item_id": "not-a-real-id", "priority": "low", "correlation_key": "u2"},
        {"kind": "sprint_item_pointer", "sprint_item_id": item["id"], "source_type": "file",
         "targets": [{"uri": "meridian/db/batch_management.py",
                     "selector": {"type": "range", "start_line": 1, "end_line": 5}}], "correlation_key": "p1"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="best_effort",
        idempotency_key="best-effort-1",
    )
    assert resp["status"] == "partial"
    assert resp["committed_count"] == 2
    assert len(resp["failures"]) == 1
    assert resp["failures"][0]["correlation_key"] == "u2"
    assert resp["rollback_status"] == "none"


# ---------------------------------------------------------------------------
# batch_mutate: idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_mutate_idempotent_replay_does_not_double_apply(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Idempotency target")
    entries = [
        {"kind": "sprint_item_pointer", "sprint_item_id": item["id"], "source_type": "file",
         "targets": [{"uri": "meridian/db/batch_management.py",
                     "selector": {"type": "range", "start_line": 1, "end_line": 5}}], "correlation_key": "p1"},
    ]
    resp1 = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="replay-key-1",
    )
    resp2 = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="replay-key-1",
    )
    assert resp1["idempotent_replay"] is False
    assert resp2["idempotent_replay"] is True
    assert resp2["results"] == resp1["results"]

    ptrs = await db_module.get_sprint_item_pointers(db, item["id"])
    assert len(ptrs) == 1, "a replayed call must NOT re-apply the mutation"


# ---------------------------------------------------------------------------
# batch_mutate: project/tenant isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_mutate_rejects_entry_with_conflicting_project_id(db, project, other_project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Isolation target")
    entries = [
        {"kind": "sprint_item_pointer", "sprint_item_id": item["id"], "source_type": "file",
         "targets": [{"uri": "meridian/db/batch_management.py",
                     "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
         "project_id": other_project["id"], "correlation_key": "p1"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="isolation-1",
    )
    assert resp["status"] == "rejected"
    assert resp["results"][0]["error_code"] == bm.ERROR_VALIDATION

    ptrs = await db_module.get_sprint_item_pointers(db, item["id"])
    assert ptrs == [], "a cross-project entry must never mutate anything"


@pytest.mark.asyncio
async def test_batch_mutate_update_cross_project_item_id_not_found(db, project, other_project):
    other_item = await db_module.add_sprint_item(db, other_project["id"], "v1", "Other project's item")
    entries = [
        {"kind": "sprint_item_update", "item_id": other_item["id"], "priority": "high", "correlation_key": "u1"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="cross-item-1",
    )
    assert resp["status"] == "rejected"
    assert resp["results"][0]["error_code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# batch_mutate: entry-kind restriction (no sprint-item CREATE via this surface)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_mutate_rejects_unknown_kind(db, project):
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"],
        entries=[{"kind": "sprint_note", "title": "x", "body": "y"}],
        mode="all_or_nothing", idempotency_key="bad-kind-1",
    )
    assert resp["status"] == "rejected"
    assert resp["results"][0]["error_code"] == bm.ERROR_VALIDATION


@pytest.mark.asyncio
async def test_batch_mutate_rejects_create_action_on_update_kind(db, project):
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"],
        entries=[{"kind": "sprint_item_update", "action": "create", "title": "sneaky create"}],
        mode="all_or_nothing", idempotency_key="bad-action-1",
    )
    assert resp["status"] == "rejected"
    assert resp["results"][0]["error_code"] == bm.ERROR_VALIDATION
    items = await db_module.get_sprint_items(db, project["id"])
    assert not any(it["title"] == "sneaky create" for it in items)


# ---------------------------------------------------------------------------
# batch_mutate: request-shape validation (mirrors batch_ops's own tests)
# ---------------------------------------------------------------------------

def test_validate_batch_mutate_request_shape_requires_mode():
    with pytest.raises(bmut_module.BatchMutateRequestError):
        bmut_module.validate_batch_mutate_request_shape({"idempotency_key": "x"})


def test_validate_batch_mutate_request_shape_requires_idempotency_key_present():
    with pytest.raises(bmut_module.BatchMutateRequestError):
        bmut_module.validate_batch_mutate_request_shape({"mode": "all_or_nothing"})


def test_validate_batch_mutate_request_shape_allows_null_idempotency_key():
    # Must not raise -- the KEY must be present, the VALUE may be None.
    bmut_module.validate_batch_mutate_request_shape({"mode": "all_or_nothing", "idempotency_key": None})


def test_validate_batch_mutate_request_shape_rejects_bad_mode():
    with pytest.raises(bmut_module.BatchMutateRequestError):
        bmut_module.validate_batch_mutate_request_shape(
            {"mode": "whenever", "idempotency_key": "x"}
        )


# ---------------------------------------------------------------------------
# MCP handler-level: arg validation (handler.py dispatch-table wiring is
# deferred -- see this item's code note -- so these call the handler
# functions directly, same as the underlying engines above).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_batch_read_requires_project_id(db):
    import meridian.server as _server_module  # noqa: F401 -- import-cycle guard, mirrors test_batch_management_schemas.py
    from meridian.mcp.handlers import sprint_tools as st_mod

    result = await st_mod.handle_batch_read({"requests": []}, db, "/tmp", None, None)
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_batch_mutate_requires_project_id(db):
    import meridian.server as _server_module  # noqa: F401 -- import-cycle guard
    from meridian.mcp.handlers import sprint_tools as st_mod

    result = await st_mod.handle_batch_mutate(
        {"entries": [], "mode": "all_or_nothing", "idempotency_key": "x"}, db, "/tmp", None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_batch_read_end_to_end(db, project):
    import meridian.server as _server_module  # noqa: F401
    from meridian.mcp.handlers import sprint_tools as st_mod

    await db_module.add_sprint_item(db, project["id"], "v1", "Handler-level read target")
    args = {
        "project_id": project["id"],
        "requests": [
            {"request_id": "r1", "adapter": "sprint_board", "operation": "get_sprint_items"},
        ],
    }
    result = await st_mod.handle_batch_read(args, db, "/tmp", None, None)
    assert result["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_handle_batch_mutate_end_to_end(db, project):
    import meridian.server as _server_module  # noqa: F401
    from meridian.mcp.handlers import sprint_tools as st_mod

    item = await db_module.add_sprint_item(db, project["id"], "v1", "Handler-level mutate target")
    args = {
        "project_id": project["id"],
        "entries": [
            {"kind": "sprint_item_update", "item_id": item["id"], "priority": "urgent"},
        ],
        "mode": "all_or_nothing",
        "idempotency_key": "handler-e2e-1",
    }
    result = await st_mod.handle_batch_mutate(args, db, "/tmp", None, None)
    assert result["status"] == "ok"
    assert result["committed_count"] == 1
