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
3. Release/manifest parity (7ac7f633): ``batch_read``/``batch_mutate`` are
   registered in ``meridian.mcp_tools._MCP_TOOLS_LIST`` with stable schemas,
   correctly categorised/annotated, advertise the IDENTICAL schema over the
   stdio transport (``_shared_tool``), are picked up by the connector/
   tool-manifest generator (``meridian.tool_manifest.build_tool_manifest``),
   and are wired into ``meridian.mcp.handler``'s per-tool dispatch table --
   mirroring ``tests/test_batch_management_schemas.py``'s coverage of the
   sibling ``execute_batch`` tool (627187b8).
"""
from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

import meridian.server as server_module  # noqa: F401 -- load before mcp.handler (import-cycle guard)
from meridian import batch_mutate as bmut_module
from meridian import batch_read as br_module
from meridian import db as db_module
from meridian import mcp_tools
from meridian import tool_manifest as tool_manifest_module
from meridian.db import batch_management as bm
from meridian.mcp import handler as handler_module


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
# batch_read: profile adapter (PROFILE-7 77369699)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profile_adapter_get_effective_profile_happy_path(db, project):
    # claim_verification_mode is one of the 3 genuinely-new PROFILE-1 fields
    # (legacy_source="profile_layers") -- unlike a legacy ProjectSettings
    # field (e.g. auto_worktrees), it is never silently re-populated by the
    # synthetic project layer's legacy-settings seed, so the workspace
    # override set here is guaranteed to survive to the merged result.
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"claim_verification_mode": "strict"})
    requests = [
        {"request_id": "eff", "adapter": "profile", "operation": "get_effective_profile"},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert by_id["eff"]["status"] == "ok"
    result = by_id["eff"]["result"]
    assert result["project_id"] == project["id"]
    assert result["generation_key"]  # the flagship op returns effective profile metadata
    assert result["fields"]["claim_verification_mode"] == "strict"


@pytest.mark.asyncio
async def test_profile_adapter_get_profile_layer_list_and_revisions_happy_paths(db, project):
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"max_pinned_decisions": 15})
    await db_module.transition_hosted_default_lifecycle(db, "global", "active")

    requests = [
        {"request_id": "one", "adapter": "profile", "operation": "get_profile_layer",
         "args": {"scope_type": "workspace", "scope_id": "singleton"}},
        {"request_id": "listed", "adapter": "profile", "operation": "list_profile_layers",
         "args": {"scope_type": "workspace"}},
        {"request_id": "rev", "adapter": "profile", "operation": "get_profile_layer_revisions",
         "args": {"scope_id": "global"}},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}

    assert by_id["one"]["status"] == "ok"
    assert by_id["one"]["result"]["fields"] == {"max_pinned_decisions": 15}

    assert by_id["listed"]["status"] == "ok"
    assert any(
        layer["scope_type"] == "workspace" and layer["scope_id"] == "singleton"
        for layer in by_id["listed"]["result"]
    )

    assert by_id["rev"]["status"] == "ok"
    assert isinstance(by_id["rev"]["result"], list)
    assert by_id["rev"]["result"][0]["scope_id"] == "global"


@pytest.mark.asyncio
async def test_profile_adapter_list_profile_layers_is_project_isolated(db, project, other_project):
    """PROFILE-7 review fix (security): list_profile_layers is a
    bulk-enumeration primitive requiring no prior knowledge of any other
    project's identifiers -- unlike get_profile_layer/get_profile_layer_revisions,
    which need the caller to already know the exact scope_id. 'project' and
    'session' scope_type rows are each tied to ONE project, so this must
    filter to the CALLING project_id rather than exposing every project's
    rows. hosted_default/workspace/user rows are not project-scoped and
    must still pass through unfiltered."""
    await db_module.set_profile_layer(db, "project", project["id"], fields={"claim_verification_mode": "strict"})
    await db_module.set_profile_layer(db, "project", other_project["id"], fields={"claim_verification_mode": "loose"})
    await db_module.set_profile_layer(db, "workspace", "singleton", fields={"max_pinned_decisions": 9})

    own_session = await db_module.register_session(db, project["id"], "own-sess")
    other_session = await db_module.register_session(db, other_project["id"], "other-sess")
    await db_module.set_profile_layer(db, "session", own_session["id"], fields={"max_pinned_decisions": 3})
    await db_module.set_profile_layer(db, "session", other_session["id"], fields={"max_pinned_decisions": 7})

    requests = [
        {"request_id": "by_project", "adapter": "profile", "operation": "list_profile_layers",
         "args": {"scope_type": "project"}},
        {"request_id": "by_session", "adapter": "profile", "operation": "list_profile_layers",
         "args": {"scope_type": "session"}},
        {"request_id": "unfiltered", "adapter": "profile", "operation": "list_profile_layers", "args": {}},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}

    assert by_id["by_project"]["status"] == "ok"
    project_scope_ids = {layer["scope_id"] for layer in by_id["by_project"]["result"]}
    assert project_scope_ids == {project["id"]}  # never the other project's row

    assert by_id["by_session"]["status"] == "ok"
    session_scope_ids = {layer["scope_id"] for layer in by_id["by_session"]["result"]}
    assert session_scope_ids == {own_session["id"]}  # never the other project's session

    assert by_id["unfiltered"]["status"] == "ok"
    unfiltered_rows = by_id["unfiltered"]["result"]
    unfiltered_project_ids = {r["scope_id"] for r in unfiltered_rows if r["scope_type"] == "project"}
    unfiltered_session_ids = {r["scope_id"] for r in unfiltered_rows if r["scope_type"] == "session"}
    assert unfiltered_project_ids == {project["id"]}
    assert unfiltered_session_ids == {own_session["id"]}
    # non-project-scoped rows still pass through unfiltered
    assert any(r["scope_type"] == "workspace" and r["scope_id"] == "singleton" for r in unfiltered_rows)


@pytest.mark.asyncio
async def test_profile_adapter_bad_args_is_validation_error(db, project):
    requests = [
        {"request_id": "bad", "adapter": "profile", "operation": "get_profile_layer",
         "args": {"scope_type": "workspace"}},  # missing required scope_id
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert by_id["bad"]["status"] == "error"
    assert by_id["bad"]["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_profile_adapter_requests_coalesce_like_any_other_adapter(db, project):
    """Confirms the profile adapter isn't special-cased out of the generic
    coalescing machinery already proven for sprint_board above."""
    requests = [
        {"request_id": "a", "adapter": "profile", "operation": "get_effective_profile"},
        {"request_id": "b", "adapter": "profile", "operation": "get_effective_profile"},
    ]
    resp = await br_module.batch_read(db, project_id=project["id"], requests=requests)
    by_id = {r["request_id"]: r for r in resp["results"]}
    assert by_id["a"]["cache_hit"] is False
    assert by_id["b"]["cache_hit"] is True
    assert by_id["b"]["coalesced_with"] == "a"
    assert by_id["b"]["result"] == by_id["a"]["result"]


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
# batch_mutate: profile_layer kind (PROFILE-7 77369699)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_mutate_profile_layer_all_or_nothing_success(db, project):
    entries = [
        {"kind": "profile_layer", "scope_type": "workspace", "scope_id": "singleton",
         "fields": {"auto_worktrees": 0}, "correlation_key": "pl1"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="profile-layer-success-1",
    )
    assert resp["status"] == "ok"
    assert resp["committed_count"] == 1
    assert resp["failures"] == []
    assert resp["rollback_status"] == "none"

    layer = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert layer["fields"] == {"auto_worktrees": 0}
    assert layer["revision"] == 1


@pytest.mark.asyncio
async def test_batch_mutate_profile_layer_stale_revision_surfaces_conflict(db, project):
    saved = await db_module.set_profile_layer(db, "workspace", "singleton", fields={"auto_worktrees": 1})
    entries = [
        {"kind": "profile_layer", "scope_type": "workspace", "scope_id": "singleton",
         "fields": {"auto_worktrees": 0}, "expected_revision": saved["revision"] + 5,
         "correlation_key": "pl-conflict"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="profile-layer-conflict-1",
    )
    # A stale-revision failure happens mid-APPLY (not pre-mutation validate),
    # same reason the mixed cross-kind rollback test below ends up "failed",
    # not "rejected" -- see execute_mixed_mutation_batch's phase split.
    assert resp["status"] == "failed"
    result = resp["results"][0]
    assert result["error_code"] == bm.ERROR_CONFLICT
    payload = result["outcome"]["payload"]
    assert payload["expected_revision"] == saved["revision"] + 5
    assert payload["actual_revision"] == saved["revision"]
    assert payload["scope_type"] == "workspace"
    assert payload["scope_id"] == "singleton"

    unchanged = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert unchanged["fields"]["auto_worktrees"] == 1


@pytest.mark.asyncio
async def test_batch_mutate_profile_layer_rollback_restores_prior_and_deletes_new(db, project, monkeypatch):
    """Mirrors test_batch_mutate_mixed_mid_mutation_abort_rolls_back_across_kinds's
    monkeypatch technique (a later, DIFFERENT-kind entry fails mid-mutation,
    triggering compensation of every already-applied entry), but exercises
    BOTH profile_layer compensation branches in one batch: entry 0 UPDATES an
    existing layer (prior revision > 0) -- compensation must restore its
    exact prior fields AND prior revision (restoring fields via
    set_profile_layer alone would bump revision a SECOND time -- PROFILE-7
    review fix), not just delete it; entry 1 CREATES a brand-new layer
    (prior revision 0) -- compensation must delete it back to the
    never-configured state."""
    existing = await db_module.set_profile_layer(
        db, "workspace", "singleton", fields={"auto_worktrees": 1, "max_pinned_decisions": 5},
    )
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Profile rollback pointer target")

    async def _flaky_add_pointer(*args, **kwargs):
        raise RuntimeError("simulated transient DB failure")

    monkeypatch.setattr(bm.db_module, "add_sprint_item_pointer", _flaky_add_pointer)

    entries = [
        {"kind": "profile_layer", "scope_type": "workspace", "scope_id": "singleton",
         "fields": {"auto_worktrees": 0}, "correlation_key": "update-existing"},
        {"kind": "profile_layer", "scope_type": "user", "scope_id": "brand-new-user",
         "fields": {"max_pinned_decisions": 40}, "correlation_key": "create-new"},
        {"kind": "sprint_item_pointer", "sprint_item_id": item["id"], "source_type": "file",
         "targets": [{"uri": "meridian/db/batch_management.py",
                     "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
         "correlation_key": "p1"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="profile-layer-rollback-1",
    )
    assert resp["status"] == "failed"
    assert resp["rollback_status"] == "rolled_back"
    by_ck = {r["correlation_key"]: r for r in resp["results"]}
    assert by_ck["update-existing"]["status"] == "rolled_back"
    assert by_ck["create-new"]["status"] == "rolled_back"
    assert by_ck["p1"]["status"] == "error"

    reverted_existing = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert reverted_existing["fields"] == existing["fields"]  # exact prior content restored
    # PROFILE-7 review fix: the apply already bumped revision once (existing
    # -> existing+1); a compensate that restores content via
    # set_profile_layer would bump it AGAIN, leaving the row's revision two
    # higher than before the batch ran even though its content matches --
    # must land back on the exact prior revision instead.
    assert reverted_existing["revision"] == existing["revision"]

    reverted_new = await db_module.get_profile_layer(db, "user", "brand-new-user")
    assert reverted_new["revision"] == 0  # deleted back to never-configured
    assert reverted_new["fields"] == {}


@pytest.mark.asyncio
async def test_batch_mutate_profile_layer_rejects_entry_with_conflicting_project_id(db, project, other_project):
    entries = [
        {"kind": "profile_layer", "scope_type": "workspace", "scope_id": "singleton",
         "fields": {"auto_worktrees": 0}, "project_id": other_project["id"],
         "correlation_key": "pl-cross"},
    ]
    resp = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="profile-layer-cross-1",
    )
    assert resp["status"] == "rejected"
    assert resp["results"][0]["error_code"] == bm.ERROR_VALIDATION

    layer = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert layer["revision"] == 0, "a cross-project entry must never mutate anything"


@pytest.mark.asyncio
async def test_batch_mutate_profile_layer_idempotent_replay_does_not_double_apply(db, project):
    entries = [
        {"kind": "profile_layer", "scope_type": "workspace", "scope_id": "singleton",
         "fields": {"auto_worktrees": 0}, "correlation_key": "pl1"},
    ]
    resp1 = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="profile-layer-replay-1",
    )
    resp2 = await bmut_module.batch_mutate(
        db, project_id=project["id"], entries=entries, mode="all_or_nothing",
        idempotency_key="profile-layer-replay-1",
    )
    assert resp1["idempotent_replay"] is False
    assert resp2["idempotent_replay"] is True
    assert resp2["results"] == resp1["results"]

    layer = await db_module.get_profile_layer(db, "workspace", "singleton")
    assert layer["revision"] == 1, "a replayed call must NOT re-apply the mutation"


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
# MCP handler-level: arg validation. These call the handler functions
# directly (same as the underlying engines above) to isolate handler-level
# argument validation from the dispatch-table wiring itself, which is
# covered separately below (7ac7f633) now that _standard_dispatch in
# meridian/mcp/handler.py routes both tool names.
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


# ---------------------------------------------------------------------------
# Release / manifest parity (7ac7f633) -- mirrors
# tests/test_batch_management_schemas.py's coverage of the sibling
# execute_batch tool (627187b8): schema registration, annotations,
# category/role/tier, stdio-transport schema identity, connector/tool
# manifest generation, and (new here) the mcp.handler per-tool dispatch
# table itself.
# ---------------------------------------------------------------------------

def _find_tool(name: str) -> dict:
    return next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == name)


@pytest.mark.parametrize("tool_name", ["batch_read", "batch_mutate"])
def test_registered_in_mcp_tools_list_exactly_once(tool_name):
    names = [t["name"] for t in mcp_tools._MCP_TOOLS_LIST]
    assert names.count(tool_name) == 1


def test_batch_read_schema_required_fields():
    tool = _find_tool("batch_read")
    schema = tool["inputSchema"]
    assert schema["required"] == ["requests"]
    props = schema["properties"]
    assert "project_id" in props
    assert "project_name" in props
    assert "alternative to project_id" in props["project_name"]["description"]
    request_item_schema = props["requests"]["items"]
    assert set(request_item_schema["required"]) == {"request_id", "adapter", "operation"}


def test_batch_mutate_schema_required_fields():
    tool = _find_tool("batch_mutate")
    schema = tool["inputSchema"]
    assert set(schema["required"]) == {"entries", "mode", "idempotency_key"}
    props = schema["properties"]
    assert "project_id" in props
    assert "project_name" in props
    assert "alternative to project_id" in props["project_name"]["description"]
    assert set(props["mode"]["enum"]) == set(bm.BATCH_MODES)


def test_batch_read_annotations_read_only_not_destructive():
    tool = _find_tool("batch_read")
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["annotations"]["idempotentHint"] is True


def test_batch_mutate_annotations_not_readonly_not_destructive():
    tool = _find_tool("batch_mutate")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False


@pytest.mark.parametrize("tool_name", ["batch_read", "batch_mutate"])
def test_category_role_tier(tool_name):
    assert mcp_tools._TOOL_CATEGORY.get(tool_name) == "sprint-management"
    assert mcp_tools._TOOL_ROLE_RELEVANCE.get(tool_name) in ("both", "executor", "planner")
    assert mcp_tools._TOOL_WORKFLOW_TIER.get(tool_name) == "common-support"


@pytest.mark.parametrize("tool_name", ["batch_read", "batch_mutate"])
def test_has_example(tool_name):
    assert tool_name in mcp_tools._TOOL_EXAMPLES


# ---------------------------------------------------------------------------
# stdio transport advertises the IDENTICAL schema object (via _shared_tool),
# not a hand-copied duplicate that can drift.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["batch_read", "batch_mutate"])
async def test_stdio_tool_schema_is_the_shared_schema(db, monkeypatch, tool_name):
    import mcp.types as mcp_types

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)

    server, _run_stdio = server_module.build_mcp_server()
    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    stdio_tool = next(t for t in listed.root.tools if t.name == tool_name)

    canonical = _find_tool(tool_name)
    assert stdio_tool.description == canonical["description"]
    assert stdio_tool.inputSchema == canonical["inputSchema"]


# ---------------------------------------------------------------------------
# Connector / tool manifest generation picks both tools up automatically.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", ["batch_read", "batch_mutate"])
def test_connector_manifest_includes_tool(tool_name):
    manifest = tool_manifest_module.build_tool_manifest(mcp_tools._MCP_TOOLS_LIST)
    names = [t["name"] for t in manifest["tools"]]
    assert tool_name in names
    entry = next(t for t in manifest["tools"] if t["name"] == tool_name)
    assert entry["summary"]  # non-empty first-sentence summary
    assert manifest["count"] == len(mcp_tools._MCP_TOOLS_LIST)


@pytest.mark.parametrize("tool_name", ["batch_read", "batch_mutate"])
def test_tool_manifest_revision_changes_if_tool_removed(tool_name):
    rev_full = tool_manifest_module.tool_manifest_revision(mcp_tools._MCP_TOOLS_LIST)
    without_tool = [t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] != tool_name]
    rev_without = tool_manifest_module.tool_manifest_revision(without_tool)
    assert rev_full != rev_without


# ---------------------------------------------------------------------------
# meridian.mcp.handler per-tool dispatch table -- proves _standard_dispatch
# inside _handle_sprint_tools actually routes these two tool names end to
# end. The handler-level tests above call handle_batch_read/
# handle_batch_mutate directly and so do NOT exercise this wiring; a
# regression here (e.g. a typo'd dict key removing the entry) would still
# pass those tests while silently breaking the real MCP call path.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_table_routes_batch_read(db, project):
    await db_module.add_sprint_item(db, project["id"], "v1", "Dispatch-table read target")
    args = {
        "project_id": project["id"],
        "requests": [
            {"request_id": "r1", "adapter": "sprint_board", "operation": "get_sprint_items"},
        ],
    }
    result = await handler_module._handle_sprint_tools(
        "batch_read", args, db, "/tmp", None, None,
    )
    assert result is not handler_module._MISS
    assert result["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_dispatch_table_routes_batch_mutate(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Dispatch-table mutate target")
    args = {
        "project_id": project["id"],
        "entries": [
            {"kind": "sprint_item_update", "item_id": item["id"], "priority": "low"},
        ],
        "mode": "all_or_nothing",
        "idempotency_key": "dispatch-table-mutate-1",
    }
    result = await handler_module._handle_sprint_tools(
        "batch_mutate", args, db, "/tmp", None, None,
    )
    assert result is not handler_module._MISS
    assert result["status"] == "ok"
    assert result["committed_count"] == 1
