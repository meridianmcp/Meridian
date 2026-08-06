"""Tests for sprint item 86e4ae44 -- shared transactional batch-management
write engine (``meridian.db.batch_management``).

Covers:

1. Call-level contract violations (bad entry_kind/mode, empty/oversized
   entries) raise BatchEngineError before anything is attempted.
2. ``sprint_item`` entry kind: create (all_or_nothing success, best_effort
   partial, all_or_nothing rollback via the duplicate-title guard,
   validation errors), and update (success, rollback restores prior field
   values, NOT_FOUND for a missing/cross-project item_id).
3. ``sprint_item_pointer`` entry kind: create success, pre-mutation
   rejection of a malformed pointer (nothing written), and a genuine
   mid-mutation abort + compensating delete (simulated via monkeypatch,
   since a pointer's own validation is pure and complete -- a real
   malformed pointer is always caught in Phase 1, never mid-mutation).
4. ``sprint_note`` entry kind: create success (explicit + batch-level
   default session_id), and a mid-mutation abort + compensating delete
   (monkeypatch, same rationale as (3): add_session_note has no
   validate-then-fail path of its own to trigger naturally).
5. Idempotency: replay returns the identical stored result without
   re-executing; different idempotency_key or entry_kind creates separate
   receipts/rows; a lost race (duplicate receipt id) degrades to "return
   without raising", mirroring add_workspace_proposal's convention.
6. Deterministic input-order result ordering, correlation_key echoing, and
   BatchEntryResult/BatchResult (de)serialization round-trips.
7. Compatibility: fan_out_sprint_items and add_sprint_item_pointer keep
   their existing external behavior untouched (regression guard for the
   "deliberately preserve, don't reroute" decision documented in
   batch_management.py's module docstring).
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian.db import batch_management as bm


# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_ba4f879b_sprint_tools_dispatch.py's local style
# -- the shared `db` fixture comes from tests/conftest.py)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "batch-mgmt-test-proj")


@pytest_asyncio.fixture
async def session(db, project):
    return await db_module.register_session(db, project["id"], "batch-mgmt-session")


async def _count(db, table: str) -> int:
    async with db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


# ---------------------------------------------------------------------------
# Call-level contract violations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bad_entry_kind_raises(db, project):
    with pytest.raises(bm.BatchEngineError):
        await bm.execute_batch(
            db, project_id=project["id"], entry_kind="not_a_kind",
            entries=[{"title": "x"}],
        )


@pytest.mark.asyncio
async def test_bad_mode_raises(db, project):
    with pytest.raises(bm.BatchEngineError):
        await bm.execute_batch(
            db, project_id=project["id"], entry_kind="sprint_item",
            entries=[{"title": "x"}], mode="whenever",
        )


@pytest.mark.asyncio
async def test_empty_entries_raises(db, project):
    with pytest.raises(bm.BatchEngineError):
        await bm.execute_batch(
            db, project_id=project["id"], entry_kind="sprint_item", entries=[],
        )


@pytest.mark.asyncio
async def test_missing_project_id_raises(db):
    with pytest.raises(bm.BatchEngineError):
        await bm.execute_batch(
            db, project_id="", entry_kind="sprint_item", entries=[{"title": "x"}],
        )


@pytest.mark.asyncio
async def test_max_entries_exceeded_raises(db, project):
    entries = [{"action": "create", "title": f"Item {i}"} for i in range(5)]
    with pytest.raises(bm.BatchEngineError):
        await bm.execute_batch(
            db, project_id=project["id"], entry_kind="sprint_item",
            entries=entries, max_entries=3,
        )
    # Nothing was attempted -- the check runs before any adapter call.
    items = await db_module.get_sprint_items(db, project["id"])
    assert items == []


# ---------------------------------------------------------------------------
# sprint_item -- create
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sprint_item_create_all_or_nothing_success(db, project):
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[
            {"action": "create", "title": "Alpha item", "version": "v1",
             "correlation_key": "a"},
            {"action": "create", "title": "Beta item", "version": "v1",
             "correlation_key": "b"},
        ],
        mode="all_or_nothing",
    )
    assert result.status == "ok"
    assert result.created_count == 2
    assert result.error_count == 0
    assert [r.status for r in result.results] == ["ok", "ok"]
    assert [r.correlation_key for r in result.results] == ["a", "b"]
    ids = result.ordered_ids()
    assert all(ids)
    items = await db_module.get_sprint_items(db, project["id"])
    assert {i["title"] for i in items} == {"Alpha item", "Beta item"}


@pytest.mark.asyncio
async def test_sprint_item_create_all_or_nothing_rollback_on_duplicate(db, project):
    # Pre-seed an item so the second batch entry trips the duplicate guard
    # (>=60% word-overlap) only once add_sprint_item is actually called --
    # this is a MUTATION-time failure (add_sprint_item validates the guard
    # via a live DB scan, not a pure pre-check), exercising the real
    # create-then-compensate rollback path, not the Phase-1 reject path.
    await db_module.add_sprint_item(db, project["id"], "v1", "Refactor the parser")

    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[
            {"action": "create", "title": "Totally unrelated item", "version": "v1"},
            {"action": "create", "title": "Refactor the parser", "version": "v1"},
        ],
        mode="all_or_nothing",
    )
    assert result.status == "failed"
    assert result.results[0].status == "rolled_back"
    assert result.results[1].status == "error"
    assert result.results[1].error_code == bm.ERROR_DUPLICATE
    assert result.results[1].retryable is False

    items = await db_module.get_sprint_items(db, project["id"])
    titles = [i["title"] for i in items]
    # Only the pre-seeded item remains -- "Totally unrelated item" was
    # created then rolled back.
    assert "Totally unrelated item" not in titles
    assert titles.count("Refactor the parser") == 1


@pytest.mark.asyncio
async def test_sprint_item_create_best_effort_partial(db, project):
    await db_module.add_sprint_item(db, project["id"], "v1", "Existing unique title here")

    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[
            {"action": "create", "title": "Existing unique title here", "version": "v1"},
            {"action": "create", "title": "Genuinely new item", "version": "v1"},
        ],
        mode="best_effort",
    )
    assert result.status == "partial"
    assert result.results[0].status == "error"
    assert result.results[0].error_code == bm.ERROR_DUPLICATE
    assert result.results[1].status == "ok"

    items = await db_module.get_sprint_items(db, project["id"])
    assert any(i["title"] == "Genuinely new item" for i in items)


@pytest.mark.asyncio
async def test_sprint_item_create_missing_title_rejected(db, project):
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "create"}],
        mode="all_or_nothing",
    )
    assert result.status == "rejected"
    assert result.results[0].error_code == bm.ERROR_VALIDATION
    items = await db_module.get_sprint_items(db, project["id"])
    assert items == []


@pytest.mark.asyncio
async def test_sprint_item_create_bad_priority_all_or_nothing_writes_nothing(db, project):
    """A semantic validation error only add_sprint_item itself enforces
    (bad priority enum) is only discoverable at apply-time -- but
    add_sprint_item raises BEFORE its own INSERT, so the FIRST entry (which
    is the one with the bad enum) never gets a row, and there is nothing
    earlier in the batch to roll back either."""
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "create", "title": "Bad enum item", "priority": "urgent-ish"}],
        mode="all_or_nothing",
    )
    assert result.status == "failed"
    assert result.results[0].status == "error"
    assert result.results[0].error_code == bm.ERROR_VALIDATION
    items = await db_module.get_sprint_items(db, project["id"])
    assert items == []


@pytest.mark.asyncio
async def test_sprint_item_create_invalid_action_rejected(db, project):
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "delete", "title": "x"}],
        mode="all_or_nothing",
    )
    assert result.status == "rejected"
    assert result.results[0].error_code == bm.ERROR_VALIDATION


# ---------------------------------------------------------------------------
# sprint_item -- update
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sprint_item_update_success(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Item to update")
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "update", "item_id": item["id"], "notes": "updated notes"}],
        mode="all_or_nothing",
    )
    assert result.status == "ok"
    assert result.results[0].id == item["id"]
    refetched = await db_module.get_sprint_item(db, item["id"])
    assert refetched["notes"] == "updated notes"


@pytest.mark.asyncio
async def test_sprint_item_update_not_found(db, project):
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "update", "item_id": "nonexistent-id", "notes": "x"}],
        mode="all_or_nothing",
    )
    assert result.status == "rejected"
    assert result.results[0].error_code == bm.ERROR_NOT_FOUND


@pytest.mark.asyncio
async def test_sprint_item_update_cross_project_not_found(db, project):
    """Preserves project scoping: an item_id that exists but belongs to a
    DIFFERENT project must not be updatable through this project's batch."""
    other_project = await db_module.create_project(db, "other-proj")
    other_item = await db_module.add_sprint_item(
        db, other_project["id"], "v1", "Belongs to other project"
    )
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "update", "item_id": other_item["id"], "notes": "x"}],
        mode="all_or_nothing",
    )
    assert result.status == "rejected"
    assert result.results[0].error_code == bm.ERROR_NOT_FOUND
    # Untouched.
    refetched = await db_module.get_sprint_item(db, other_item["id"])
    assert refetched["notes"] != "x"


@pytest.mark.asyncio
async def test_sprint_item_update_rollback_restores_prior_values(db, project):
    item_a = await db_module.add_sprint_item(
        db, project["id"], "v1", "Item A", notes="original notes", human_id="adam",
    )
    item_b = await db_module.add_sprint_item(db, project["id"], "v1", "Item B")

    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[
            {"action": "update", "item_id": item_a["id"], "notes": "changed notes",
             "human_id": "someone-else"},
            # priority enum is invalid -> patch_sprint_item raises BEFORE any
            # UPDATE for THIS entry, but item_a's update already committed.
            {"action": "update", "item_id": item_b["id"], "priority": "not-a-real-priority"},
        ],
        mode="all_or_nothing",
    )
    assert result.status == "failed"
    assert result.results[0].status == "rolled_back"
    assert result.results[1].status == "error"

    reverted = await db_module.get_sprint_item(db, item_a["id"])
    assert reverted["notes"] == "original notes"
    assert reverted["human_id"] == "adam"


@pytest.mark.asyncio
async def test_sprint_item_update_no_patchable_fields_rejected(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Bare update")
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "update", "item_id": item["id"]}],
        mode="all_or_nothing",
    )
    assert result.status == "rejected"
    assert result.results[0].error_code == bm.ERROR_VALIDATION


# ---------------------------------------------------------------------------
# sprint_item_pointer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pointer_create_success(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Pointer target item")
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item_pointer",
        entries=[
            {"sprint_item_id": item["id"], "source_type": "code",
             "targets": [{"uri": "file:a.py",
                          "selector": {"type": "symbol", "qualified_name": "a.b"}}],
             "correlation_key": "ptr-1"},
        ],
        mode="all_or_nothing",
    )
    assert result.status == "ok"
    assert result.results[0].correlation_key == "ptr-1"
    pointers = await db_module.get_sprint_item_pointers(db, item["id"])
    assert len(pointers) == 1
    assert pointers[0]["source_type"] == "code"


@pytest.mark.asyncio
async def test_pointer_create_malformed_rejected_writes_nothing(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Pointer target item 2")
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item_pointer",
        entries=[
            {"sprint_item_id": item["id"], "source_type": "code", "targets": "not-a-list"},
        ],
        mode="all_or_nothing",
    )
    assert result.status == "rejected"
    assert result.results[0].error_code == bm.ERROR_VALIDATION
    pointers = await db_module.get_sprint_item_pointers(db, item["id"])
    assert pointers == []


@pytest.mark.asyncio
async def test_pointer_create_missing_sprint_item_id_rejected(db, project):
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item_pointer",
        entries=[{"source_type": "code",
                  "targets": [{"uri": "file:a.py", "selector": {"type": "range",
                                                                 "start_line": 1, "end_line": 2}}]}],
        mode="all_or_nothing",
    )
    assert result.status == "rejected"
    assert result.results[0].error_code == bm.ERROR_VALIDATION


@pytest.mark.asyncio
async def test_pointer_mid_mutation_abort_rolls_back_earlier_pointer(db, project, monkeypatch):
    """Simulates an unexpected failure DURING mutation (not caught by the
    pure pre-validation phase) to exercise the real compensate() path for
    the sprint_item_pointer entry kind."""
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Pointer rollback target")

    real_add_pointer = db_module.add_sprint_item_pointer
    call_count = {"n": 0}

    async def _flaky_add_pointer(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated transient DB failure")
        return await real_add_pointer(*args, **kwargs)

    monkeypatch.setattr(bm.db_module, "add_sprint_item_pointer", _flaky_add_pointer)

    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item_pointer",
        entries=[
            {"sprint_item_id": item["id"], "source_type": "code",
             "targets": [{"uri": "file:a.py", "selector": {"type": "symbol",
                                                             "qualified_name": "a.b"}}]},
            {"sprint_item_id": item["id"], "source_type": "code",
             "targets": [{"uri": "file:c.py", "selector": {"type": "symbol",
                                                             "qualified_name": "c.d"}}]},
        ],
        mode="all_or_nothing",
    )
    assert result.status == "failed"
    assert result.results[0].status == "rolled_back"
    assert result.results[1].status == "error"
    assert result.results[1].error_code == bm.ERROR_INTERNAL
    assert result.results[1].retryable is True

    pointers = await db_module.get_sprint_item_pointers(db, item["id"])
    assert pointers == []


# ---------------------------------------------------------------------------
# sprint_note
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_note_create_success_with_explicit_session(db, project, session):
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_note",
        entries=[{"session_id": session["id"], "title": "Note A", "body": "Body A"}],
        mode="best_effort",
    )
    assert result.status == "ok"
    notes = await db_module.get_session_notes(db, session["id"])
    assert any(n["title"] == "Note A" for n in notes)


@pytest.mark.asyncio
async def test_note_create_uses_batch_level_session_default(db, project, session):
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_note",
        entries=[{"title": "Default session note", "body": "Body"}],
        mode="best_effort", session_id=session["id"],
    )
    assert result.status == "ok"
    notes = await db_module.get_session_notes(db, session["id"])
    assert any(n["title"] == "Default session note" for n in notes)


@pytest.mark.asyncio
async def test_note_create_missing_session_rejected(db, project):
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_note",
        entries=[{"title": "Orphan note", "body": "Body"}],
        mode="all_or_nothing",
    )
    assert result.status == "rejected"
    assert result.results[0].error_code == bm.ERROR_VALIDATION


@pytest.mark.asyncio
async def test_note_mid_mutation_abort_rolls_back_earlier_note(db, project, session, monkeypatch):
    real_add_note = db_module.add_session_note
    call_count = {"n": 0}

    async def _flaky_add_note(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated transient DB failure")
        return await real_add_note(*args, **kwargs)

    monkeypatch.setattr(bm.db_module, "add_session_note", _flaky_add_note)

    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_note",
        entries=[
            {"session_id": session["id"], "title": "Note 1", "body": "Body 1"},
            {"session_id": session["id"], "title": "Note 2", "body": "Body 2"},
        ],
        mode="all_or_nothing",
    )
    assert result.status == "failed"
    assert result.results[0].status == "rolled_back"
    assert result.results[1].status == "error"

    notes = await db_module.get_session_notes(db, session["id"])
    assert notes == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotency_key_replays_identical_result(db, project):
    entries = [{"action": "create", "title": "Idempotent item", "version": "v1"}]
    first = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item", entries=entries,
        mode="all_or_nothing", idempotency_key="retry-1",
    )
    second = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item", entries=entries,
        mode="all_or_nothing", idempotency_key="retry-1",
    )
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert first.ordered_ids() == second.ordered_ids()

    items = await db_module.get_sprint_items(db, project["id"])
    assert sum(1 for i in items if i["title"] == "Idempotent item") == 1


@pytest.mark.asyncio
async def test_idempotency_key_replay_ignores_changed_entries(db, project):
    """Matches add_workspace_proposal's documented idempotency-key
    semantics: the SAME key replays the ORIGINAL result even if the caller
    passes different entries on retry -- the key alone is the identity."""
    first = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "create", "title": "First title", "version": "v1"}],
        mode="all_or_nothing", idempotency_key="same-key",
    )
    second = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "create", "title": "Different title", "version": "v1"}],
        mode="all_or_nothing", idempotency_key="same-key",
    )
    assert second.ordered_ids() == first.ordered_ids()
    items = await db_module.get_sprint_items(db, project["id"])
    assert not any(i["title"] == "Different title" for i in items)


@pytest.mark.asyncio
async def test_idempotency_key_scoped_per_entry_kind(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Pointer host")
    await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[{"action": "create", "title": "Shared key item", "version": "v1"}],
        mode="all_or_nothing", idempotency_key="shared-key",
    )
    pointer_result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item_pointer",
        entries=[{"sprint_item_id": item["id"], "source_type": "code",
                  "targets": [{"uri": "file:a.py",
                               "selector": {"type": "symbol", "qualified_name": "a.b"}}]}],
        mode="all_or_nothing", idempotency_key="shared-key",
    )
    # The SAME key under a DIFFERENT entry_kind is a fresh receipt, not a
    # replay of the sprint_item call.
    assert pointer_result.idempotent_replay is False
    assert pointer_result.entry_kind == "sprint_item_pointer"


@pytest.mark.asyncio
async def test_idempotency_key_rejected_batch_also_replays(db, project):
    """A REJECTED (nothing written) result is itself recorded and replayed --
    a retried malformed batch gets the identical rejection, not a fresh
    validation pass."""
    entries = [{"action": "create"}]  # missing title -> rejected
    first = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item", entries=entries,
        mode="all_or_nothing", idempotency_key="rejected-key",
    )
    second = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item", entries=entries,
        mode="all_or_nothing", idempotency_key="rejected-key",
    )
    assert first.status == "rejected"
    assert second.status == "rejected"
    assert second.idempotent_replay is True


@pytest.mark.asyncio
async def test_write_batch_receipt_lost_race_returns_without_raising(db, project):
    """White-box: a concurrent duplicate insert (same deterministic receipt
    id) must not raise -- it degrades to a no-op, mirroring
    add_workspace_proposal's winner-refetch convention."""
    entries = [{"action": "create", "title": "Race item", "version": "v1"}]
    normalized_result = bm.BatchResult(
        status="ok", mode="all_or_nothing", entry_kind="sprint_item",
        project_id=project["id"], idempotency_key="race-key", results=[],
    )
    # First writer wins.
    await bm._write_batch_receipt(
        db, tenant_id=None, project_id=project["id"], entry_kind="sprint_item",
        idempotency_key="race-key", actor=None, result=normalized_result,
    )
    # A second writer racing with the identical scope/key must not raise.
    await bm._write_batch_receipt(
        db, tenant_id=None, project_id=project["id"], entry_kind="sprint_item",
        idempotency_key="race-key", actor=None, result=normalized_result,
    )
    rid = bm._receipt_id(
        tenant_id=None, project_id=project["id"], entry_kind="sprint_item",
        idempotency_key="race-key",
    )
    async with db.execute(
        "SELECT COUNT(*) AS n FROM action_audit_log WHERE id = ?", (rid,)
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"] if isinstance(row, dict) else row[0]) == 1


def test_is_unique_violation_heuristic():
    assert bm._is_unique_violation(Exception("UNIQUE constraint failed: x.y"))
    assert bm._is_unique_violation(Exception("duplicate key value violates unique constraint"))
    assert not bm._is_unique_violation(Exception("no such table: x"))


def test_receipt_id_is_deterministic_and_scope_sensitive():
    kwargs = dict(project_id="p1", entry_kind="sprint_item", idempotency_key="k1")
    id_a = bm._receipt_id(tenant_id=None, **kwargs)
    id_b = bm._receipt_id(tenant_id=None, **kwargs)
    assert id_a == id_b
    id_diff_tenant = bm._receipt_id(tenant_id="tenant-x", **kwargs)
    assert id_diff_tenant != id_a
    id_diff_kind = bm._receipt_id(
        tenant_id=None, project_id="p1", entry_kind="sprint_item_pointer", idempotency_key="k1"
    )
    assert id_diff_kind != id_a


# ---------------------------------------------------------------------------
# Deterministic ordering / correlation keys / (de)serialization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_result_ordering_matches_input_order_best_effort(db, project):
    await db_module.add_sprint_item(db, project["id"], "v1", "Already exists title")
    result = await bm.execute_batch(
        db, project_id=project["id"], entry_kind="sprint_item",
        entries=[
            {"action": "create", "title": "Already exists title", "correlation_key": "dup"},
            {"action": "create", "title": "Fresh one", "correlation_key": "fresh"},
            {"action": "create", "title": "Also fresh", "correlation_key": "fresh2"},
        ],
        mode="best_effort",
    )
    assert [r.index for r in result.results] == [0, 1, 2]
    assert [r.correlation_key for r in result.results] == ["dup", "fresh", "fresh2"]
    assert result.results[0].status == "error"
    assert result.results[1].status == "ok"
    assert result.results[2].status == "ok"


def test_batch_entry_result_round_trip():
    original = bm.BatchEntryResult(
        index=2, correlation_key="ck", status="ok", id="abc123",
        outcome={"action": "create"}, error_code=None, error_message=None, retryable=False,
    )
    round_tripped = bm.BatchEntryResult.from_dict(json.loads(json.dumps(original.to_dict())))
    assert round_tripped == original


def test_batch_result_round_trip():
    entry = bm.BatchEntryResult(index=0, correlation_key=None, status="ok", id="x")
    original = bm.BatchResult(
        status="ok", mode="all_or_nothing", entry_kind="sprint_item",
        project_id="p1", idempotency_key="k1", results=[entry],
    )
    payload = json.loads(json.dumps(original.to_dict(), default=str))
    rebuilt = bm.BatchResult.from_dict(payload)
    assert rebuilt.status == "ok"
    assert rebuilt.entry_kind == "sprint_item"
    assert rebuilt.results[0].id == "x"
    assert rebuilt.idempotent_replay is True  # from_dict always marks a replay


def test_batch_result_created_and_error_counts():
    results = [
        bm.BatchEntryResult(index=0, correlation_key=None, status="ok", id="a"),
        bm.BatchEntryResult(index=1, correlation_key=None, status="error", error_code="X"),
        bm.BatchEntryResult(index=2, correlation_key=None, status="rolled_back", id="c"),
    ]
    result = bm.BatchResult(
        status="failed", mode="all_or_nothing", entry_kind="sprint_item",
        project_id="p1", idempotency_key=None, results=results,
    )
    assert result.created_count == 1
    assert result.error_count == 2
    assert result.ordered_ids() == ["a", None, None]


# ---------------------------------------------------------------------------
# Compatibility regression guards: fan_out_sprint_items / add_sprint_item_pointer
# keep their existing external behavior (deliberately NOT rerouted -- see
# batch_management.py's module docstring).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fan_out_sprint_items_still_skips_duplicate_guard(db, project):
    """fan_out_sprint_items must still accept near-duplicate titles without
    the batch engine's duplicate guard -- proves it was NOT rerouted through
    execute_batch's sprint_item (add_sprint_item-backed) create path."""
    await db_module.add_sprint_item(db, project["id"], "v1", "Refactor the module loader")
    ids = await db_module.fan_out_sprint_items(
        db, project["id"], [{"title": "Refactor the module loader", "version": "v1"}]
    )
    assert len(ids) == 1
    items = await db_module.get_sprint_items(db, project["id"])
    assert sum(1 for i in items if i["title"] == "Refactor the module loader") == 2


@pytest.mark.asyncio
async def test_add_sprint_item_pointer_unchanged_direct_call(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Direct pointer target")
    pointer = await db_module.add_sprint_item_pointer(
        db, project["id"], item["id"], "code",
        [{"uri": "file:z.py", "selector": {"type": "symbol", "qualified_name": "z.q"}}],
    )
    assert pointer["source_type"] == "code"
    assert pointer["sprint_item_id"] == item["id"]
