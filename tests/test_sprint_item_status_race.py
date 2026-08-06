"""Regression test for fa3e3331 — systemic race condition across ALL
sprint-item status transitions (claim/complete/fail/push/skip/provisional),
not just claim_sprint_item.

CONFIRMED BUG: every transition function issued a read-then-write pair with
no atomic from-state condition in the UPDATE's WHERE clause ("WHERE id = ?
AND project_id = ?", nothing else). Two concurrent callers acting on the same
item could both pass any pre-check and both have their UPDATE succeed —
whichever committed last silently won, with no signal to the loser that it
lost a race rather than genuinely completing/skipping/failing/claiming the
item. cursor.rowcount == 0 also collapsed two very different situations
("item doesn't exist" vs "lost a race") into the same ambiguous None return.

FIX: every transition now guards via expected_statuses (claim_sprint_item's
own raw UPDATE gets an explicit AND status NOT IN (...); the rest route
through _update_sprint_item_status's new expected_statuses param). A
race-lost attempt raises SprintItemStatusRace (claim_sprint_item raises the
same ValueError shape its pre-check already used) instead of silently
no-op'ing or clobbering a concurrent winner. Genuine "item not found" still
returns None, unchanged.

ARCH 1B (e0f8f4da): introduced _transition_status as the single atomic
chokepoint that claim/complete/fail/push/skip/patch/start/provisional_complete
all route through. The concurrency-safety property now needs to be proven only
ONCE against that chokepoint. Per-caller tests in this file verify:
  (a) each caller passes the right from_statuses / to_status to the chokepoint
  (b) each caller surfaces a race-lost (chokepoint returns None) in the
      correct way for its own contract (ValueError for claim, SprintItemStatusRace
      for complete/fail/push/skip/start/provisional_complete).
"""
import asyncio
import time

import pytest

from meridian import db as db_module
import meridian.db.sprint_items as _sprint_items_mod  # for monkeypatching after module split


async def _project_with_item(db):
    p = await db_module.create_project(db, "status-race")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "racy item")
    return p, item


@pytest.mark.asyncio
async def test_claim_sprint_item_atomic_race_second_caller_rejected(db):
    """Simulates two concurrent claims: the first commits, the second must
    lose cleanly (raise, matching the pre-check's own error shape) instead
    of silently re-claiming an already in_progress item."""
    p, item = await _project_with_item(db)
    first = await db_module.claim_sprint_item(db, p["id"], item["id"], actor="session-a")
    assert first["status"] == "in_progress"

    with pytest.raises(ValueError, match="in_progress"):
        await db_module.claim_sprint_item(db, p["id"], item["id"], actor="session-b")

    # Only session-a's claim stuck.
    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["actor"] == "session-a"


@pytest.mark.asyncio
async def test_claim_sprint_item_race_lost_between_precheck_and_write(db, monkeypatch):
    """The actual TOCTOU: force claim_sprint_item's pre-check to observe a
    stale 'pending' snapshot while the real row has already been claimed by
    a concurrent winner -- the atomic UPDATE's WHERE guard (not the pre-check)
    must be what rejects the write."""
    p, item = await _project_with_item(db)
    winner = await db_module.claim_sprint_item(db, p["id"], item["id"], actor="winner")
    assert winner["status"] == "in_progress"

    stale_snapshot = dict(winner)
    stale_snapshot["status"] = "pending"  # what the loser's pre-check "saw"

    real_get = db_module.get_sprint_item
    calls = {"n": 0}

    async def _stale_get(db_conn, item_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return stale_snapshot
        return await real_get(db_conn, item_id)

    monkeypatch.setattr(db_module, "get_sprint_item", _stale_get)

    with pytest.raises(ValueError, match="in_progress"):
        await db_module.claim_sprint_item(db, p["id"], item["id"], actor="loser")

    final = await real_get(db, item["id"])
    assert final["actor"] == "winner"


@pytest.mark.asyncio
async def test_complete_sprint_item_race_raises_status_race_not_silent_none(db, monkeypatch):
    """Two concurrent completions on the SAME item: force the item to flip to
    skipped between complete_sprint_item's guard-write and would-be success,
    by racing a real skip_sprint_item call in via a patched _update_sprint_item_status
    wrapper that performs the concurrent write first."""
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    real_update = db_module._update_sprint_item_status
    triggered = {"done": False}

    async def _racy_update(db_conn, project_id, item_id, status, **kwargs):
        if status == "done" and not triggered["done"]:
            triggered["done"] = True
            # A concurrent skip wins the race first.
            await db_module.skip_sprint_item(db_conn, project_id, item_id, reason="raced out")
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    # Patch both db_module and the sprint_items submodule: after the module split,
    # complete_sprint_item's actual call to _update_sprint_item_status goes through
    # sprint_items.py's local namespace, not db_module's. Both patches are needed
    # to intercept the call.
    monkeypatch.setattr(db_module, "_update_sprint_item_status", _racy_update)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _racy_update)

    with pytest.raises(db_module.SprintItemStatusRace) as excinfo:
        await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert excinfo.value.item_id == item["id"]
    assert excinfo.value.current_status == "skipped"

    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "skipped"  # the real winner's terminal state stuck


@pytest.mark.asyncio
async def test_status_race_error_distinguishes_not_found_from_race(db):
    """Genuine 'item does not exist' must still return None (unchanged
    behavior), not raise SprintItemStatusRace — the two cases stay distinct."""
    p, _ = await _project_with_item(db)
    result = await db_module.complete_sprint_item(db, p["id"], "no-such-item-id")
    assert result is None


@pytest.mark.asyncio
async def test_normal_direct_pending_to_done_completion_still_works(db):
    """Non-breaking: the long-standing, widely-used direct pending -> done
    completion path (no explicit claim first) must keep working exactly as
    before — the guard only rejects transitions FROM an already-terminal
    status, not the flexible pending-or-in_progress -> terminal path."""
    p, item = await _project_with_item(db)
    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_skip_fail_push_provisional_reject_already_terminal_item(db):
    p, item = await _project_with_item(db)
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    with pytest.raises(db_module.SprintItemStatusRace):
        await db_module.skip_sprint_item(db, p["id"], item["id"])
    with pytest.raises(db_module.SprintItemStatusRace):
        await db_module.fail_sprint_item(db, p["id"], item["id"])
    with pytest.raises(db_module.SprintItemStatusRace):
        await db_module.push_sprint_item(db, p["id"], item["id"], "v2.0")
    with pytest.raises(db_module.SprintItemStatusRace):
        await db_module.provisional_complete_sprint_item(db, p["id"], item["id"])

    # Still done — none of the rejected attempts mutated it.
    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "done"


@pytest.mark.asyncio
async def test_start_sprint_item_guards_pending_todo_only(db):
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])  # now in_progress

    with pytest.raises(db_module.SprintItemStatusRace):
        await db_module.start_sprint_item(db, p["id"], item["id"])


@pytest.mark.asyncio
async def test_mcp_handler_complete_sprint_item_idempotent_retry_not_status_race(db):
    """a2a027cf — FIXED behavior. This test used to assert the OPPOSITE: that
    re-completing an already-'done' item via the MCP dispatch surfaced
    STATUS_RACE. That was exactly the reported production bug -- a client
    that timed out around the original call and retried saw a misleading
    failure even though the original write had already succeeded. A retry
    against an already-done item is now a plain idempotent success."""
    import meridian.server as srv

    p, item = await _project_with_item(db)
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"]},
        db, "/tmp",
    )
    assert "error" not in result
    assert result["status"] == "done"
    assert result["completion_outcome"] == "already_committed"
    assert "correlation_id" in result


@pytest.mark.asyncio
async def test_mcp_handler_complete_sprint_item_genuine_race_still_status_race(db):
    """A genuine conflicting race (item ends up in a DIFFERENT terminal
    status than the one this call was attempting) must still surface
    STATUS_RACE -- only the "already reached the SAME target status" replay
    case became idempotent, not races in general."""
    import meridian.server as srv

    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.skip_sprint_item(db, p["id"], item["id"], reason="raced out")

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"]},
        db, "/tmp",
    )
    assert result["error"] == "STATUS_RACE"
    assert result["item_id"] == item["id"]
    assert result["current_status"] == "skipped"
    assert "correlation_id" in result
    assert "retry_guidance" in result


# ---------------------------------------------------------------------------
# Adversarial-review follow-up (same fa3e3331): merge_sprint_items,
# split_sprint_item, and requeue_or_fail_stalled_item all do their own
# pre-check-then-raw-UPDATE on sprint_items.status and were missed by the
# first pass of this fix — same race class, different functions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_sprint_item_status_rejects_empty_expected_statuses(db):
    p, item = await _project_with_item(db)
    with pytest.raises(ValueError, match="non-empty"):
        await db_module._update_sprint_item_status(
            db, p["id"], item["id"], "done", expected_statuses=set(),
        )


@pytest.mark.asyncio
async def test_split_sprint_item_rejects_race_lost_close(db, monkeypatch):
    p, item = await _project_with_item(db)

    real_update = db_module._update_sprint_item_status

    async def _racy_update(db_conn, project_id, item_id, status, **kwargs):
        if status == "skipped":
            # A concurrent completion wins before the split's own close-step commits.
            await db_module.complete_sprint_item(db_conn, project_id, item_id)
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    # Patch both db_module and the sprint_items submodule: after the module split,
    # split_sprint_item's actual call to _update_sprint_item_status goes through
    # sprint_items.py's local namespace, not db_module's. Both patches are needed
    # to intercept the call.
    monkeypatch.setattr(db_module, "_update_sprint_item_status", _racy_update)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _racy_update)

    with pytest.raises(ValueError, match="can only split pending or in_progress items, got 'done'"):
        await db_module.split_sprint_item(db, p["id"], item["id"], ["a", "b"])

    # The concurrent winner's completion stuck; no orphaned split children exist.
    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "done"
    remaining = await db_module.get_sprint_items(db, p["id"])
    assert len(remaining) == 1


class _SideEffectThenReal:
    """Wraps aiosqlite's execute() return value (which supports BOTH `await`
    and `async with`) so a side-effect coroutine runs exactly once, right
    before the real statement executes, without breaking the dual protocol
    every other db.execute() call in the codebase relies on."""

    def __init__(self, real_wrapper, side_effect_coro):
        self._real = real_wrapper
        self._side_effect_coro = side_effect_coro

    async def _run_side_effect_once(self):
        if self._side_effect_coro is not None:
            coro, self._side_effect_coro = self._side_effect_coro, None
            await coro

    def __await__(self):
        async def _inner():
            await self._run_side_effect_once()
            return await self._real
        return _inner().__await__()

    async def __aenter__(self):
        await self._run_side_effect_once()
        return await self._real.__aenter__()

    async def __aexit__(self, *exc):
        return await self._real.__aexit__(*exc)


@pytest.mark.asyncio
async def test_merge_sprint_items_closing_loop_atomic_guard(db, monkeypatch):
    """Exercises the closing-loop's atomic guard specifically (not the
    pre-check loop, which only sees a stale snapshot taken before the race).
    b is genuinely 'pending' when merge_sprint_items's pre-check reads it, but
    a concurrent completion of b lands before the closing UPDATE reaches it —
    the merge must raise instead of silently overwriting b's real 'done'
    status back to 'skipped'.

    Note: this test's db fixture is a single shared in-memory SQLite
    connection (real concurrent sessions would each hold their own
    connection/transaction), so the "concurrent" completion below necessarily
    commits on the SAME connection as the in-progress merge loop — meaning it
    also commits a's already-applied closure early, as a real second
    connection's independent transaction would not. That's a test-fixture
    artifact, not a claim about a's transactional isolation; the property
    that matters and IS verified here is the actual exploit this fix closes:
    b's real terminal status must never be silently clobbered back to
    'skipped' by a losing merge."""
    p = await db_module.create_project(db, "merge-race")
    a = await db_module.add_sprint_item(db, p["id"], "v1", "source a")
    b = await db_module.add_sprint_item(db, p["id"], "v1", "source b")

    real_execute = db.execute
    triggered = {"done": False}

    def _patched_execute(sql, params=None):
        real_wrapper = real_execute(sql, params)
        if "merged_into = ?" in sql and params and params[1] == b["id"] and not triggered["done"]:
            triggered["done"] = True
            return _SideEffectThenReal(
                real_wrapper, db_module.complete_sprint_item(db, p["id"], b["id"])
            )
        return real_wrapper

    monkeypatch.setattr(db, "execute", _patched_execute)

    with pytest.raises(ValueError, match=f"cannot merge item '{b['id']}'"):
        await db_module.merge_sprint_items(db, p["id"], [a["id"], b["id"]], "survivor")

    monkeypatch.undo()  # restore real db.execute before using db again below

    # The core invariant this fix closes: b's real completion stuck, never
    # silently overwritten back to 'skipped' by the losing merge attempt.
    final_b = await db_module.get_sprint_item(db, b["id"])
    assert final_b["status"] == "done"


@pytest.mark.asyncio
async def test_requeue_or_fail_stalled_item_noop_when_race_lost_to_completion(db):
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    # The item is no longer in_progress by the time the stall handler acts on
    # it (it completed first) -- must no-op, not clobber the done status.
    result = await db_module.requeue_or_fail_stalled_item(db, p["id"], item["id"])
    assert result is None
    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "done"


# ---------------------------------------------------------------------------
# Smoke test for ARCH 1A module split (7e20868a): assert that all sprint-item
# functions are accessible both via meridian.db.sprint_items (canonical home)
# and via meridian.db (re-exported for backward compat).
# ---------------------------------------------------------------------------


def test_sprint_items_module_exports_expected_functions():
    """Smoke test: meridian.db.sprint_items exports all key functions, and
    meridian.db re-exports them so existing call sites are unaffected."""
    import meridian.db.sprint_items as si
    import meridian.db as db

    public_functions = [
        "add_sprint_item",
        "claim_sprint_item",
        "complete_sprint_item",
        "fail_sprint_item",
        "push_sprint_item",
        "skip_sprint_item",
        "patch_sprint_item",
        "split_sprint_item",
        "merge_sprint_items",
        "add_sprint_item_pointer",
        "get_sprint_item_pointers",
        "delete_sprint_item_pointer",
        "assign_sprint_waves",
        "get_sprint_items",
        "get_sprint_items_for_resource",
        "get_parallelizable_groups",
        "analyze_sprint",
        "requeue_or_fail_stalled_item",
        "handle_session_stall",
        "fan_out_sprint_items",
        "add_subtask",
        "build_sprint_items_xml",
    ]
    classes = ["SprintItemEvidenceRequired", "SprintItemStatusRace"]

    for name in public_functions + classes:
        # Must exist in the submodule (canonical home)
        assert hasattr(si, name), f"meridian.db.sprint_items missing: {name}"
        # Must be re-exported via db.__init__ (backward compat)
        assert hasattr(db, name), f"meridian.db missing re-export of: {name}"
        # Both references must point to the same object (not copies)
        assert getattr(si, name) is getattr(db, name), (
            f"meridian.db.{name} is not the same object as meridian.db.sprint_items.{name}"
        )


# ---------------------------------------------------------------------------
# ARCH 1B (e0f8f4da) — _transition_status chokepoint tests
#
# These tests verify:
# (a) _transition_status from-state guard is atomic under concurrent callers
# (b) each rewired public function still enforces its own validation
# (c) rowcount-0 (race-lost) returns None, matching fa3e3331's no-raise intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_status_from_state_guard_returns_none_on_race(db):
    """_transition_status returns None (not raises) when from_statuses guard
    fails — the row exists but its status changed before this UPDATE committed.
    This is the atomic no-op contract: callers (not the chokepoint) decide
    whether to raise or silently discard a lost race."""
    p, item = await _project_with_item(db)
    # Claim the item to put it in_progress.
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    # Attempt transition from 'pending' — but item is now 'in_progress'.
    result = await _sprint_items_mod._transition_status(
        db, p["id"], item["id"], "done",
        from_statuses=["pending"],  # guard will fail
    )
    assert result is None, (
        "_transition_status must return None (not raise) when from_statuses "
        "guard rejects the transition (rowcount == 0 with item present)"
    )
    # Item must not have been mutated.
    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "in_progress"


@pytest.mark.asyncio
async def test_transition_status_no_from_statuses_always_succeeds(db):
    """_transition_status with from_statuses=None succeeds regardless of current
    status — used by patch_sprint_item for unconditional admin resets."""
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    # No from_statuses guard; item is in_progress but we transition to pending.
    result = await _sprint_items_mod._transition_status(
        db, p["id"], item["id"], "pending",
        from_statuses=None,
    )
    assert result is not None
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_transition_status_invalidates_cache_on_success(db):
    """_transition_status busts _SPRINT_ITEMS_CACHE on every successful write.
    Verifies the shared side-effect that was previously missing from
    claim_sprint_item (pre-ARCH 1B, claim wrote its own UPDATE but never
    called _invalidate_sprint_items_cache)."""
    p, item = await _project_with_item(db)
    # Prime the cache by calling get_sprint_items_cached.
    cached_before = await _sprint_items_mod.get_sprint_items_cached(db, p["id"])
    assert len(cached_before) == 1

    # Claim invalidates via _transition_status.
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    # Cache must have been busted: if we inspect _SPRINT_ITEMS_CACHE the entry
    # should be gone.
    assert p["id"] not in _sprint_items_mod._SPRINT_ITEMS_CACHE, (
        "claim_sprint_item via _transition_status must bust _SPRINT_ITEMS_CACHE; "
        "old code called its own raw UPDATE without calling "
        "_invalidate_sprint_items_cache — this was a pre-ARCH 1B guard gap"
    )


@pytest.mark.asyncio
async def test_transition_status_rejects_empty_from_statuses(db):
    """from_statuses=[] is a caller bug (would render AND status IN ()) and
    must raise ValueError, same as the old expected_statuses=set() guard."""
    p, item = await _project_with_item(db)
    with pytest.raises(ValueError, match="non-empty"):
        await _sprint_items_mod._transition_status(
            db, p["id"], item["id"], "done",
            from_statuses=[],
        )


@pytest.mark.asyncio
async def test_transition_status_claim_sets_claimed_at(db):
    """claimed_at_now=True must stamp claimed_at = datetime('now') so
    claim_sprint_item's contract is preserved through the chokepoint."""
    p, item = await _project_with_item(db)
    result = await _sprint_items_mod._transition_status(
        db, p["id"], item["id"], "in_progress",
        from_statuses=["pending", "todo", "indeterminate", "provisional_complete"],
        claimed_at_now=True,
    )
    assert result is not None
    assert result["status"] == "in_progress"
    assert result.get("claimed_at") is not None, (
        "claimed_at must be set when claimed_at_now=True"
    )


@pytest.mark.asyncio
async def test_claim_sprint_item_via_chokepoint_still_raises_on_blocked_status(db):
    """claim_sprint_item must still raise ValueError with the same message when
    the item is in a blocked status (in_progress/done/failed/skipped) — the
    _transition_status rewire must not change the public error contract."""
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])

    with pytest.raises(ValueError, match="in_progress"):
        await db_module.claim_sprint_item(db, p["id"], item["id"], actor="second")


@pytest.mark.asyncio
async def test_complete_sprint_item_raises_status_race_on_transition_none(db):
    """complete_sprint_item must raise SprintItemStatusRace when _transition_status
    returns None (lost race) — the chokepoint returns None but the caller
    converts it to the appropriate exception."""
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.skip_sprint_item(db, p["id"], item["id"], reason="raced")

    with pytest.raises(db_module.SprintItemStatusRace) as exc_info:
        await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert exc_info.value.current_status == "skipped"


@pytest.mark.asyncio
async def test_patch_sprint_item_status_routes_through_transition(db):
    """patch_sprint_item with a status arg must route through _transition_status
    so cache + live event are guaranteed (6a17e735 fix). Verify that after a
    patch with status='pending', _SPRINT_ITEMS_CACHE is busted."""
    p, item = await _project_with_item(db)
    # Prime the cache.
    await _sprint_items_mod.get_sprint_items_cached(db, p["id"])

    # Patch with a status change.
    result = await db_module.patch_sprint_item(
        db, p["id"], item["id"], status="indeterminate"
    )
    assert result is not None
    assert result["status"] == "indeterminate"
    # Cache must be busted by the chokepoint.
    assert p["id"] not in _sprint_items_mod._SPRINT_ITEMS_CACHE


@pytest.mark.asyncio
async def test_patch_sprint_item_non_status_fields_still_work(db):
    """Regression: patch_sprint_item without status must still update non-status
    fields and return the updated item."""
    p, item = await _project_with_item(db)
    result = await db_module.patch_sprint_item(
        db, p["id"], item["id"], notes="updated notes", priority="urgent"
    )
    assert result is not None
    assert result["notes"] == "updated notes"
    assert result["priority"] == "urgent"
    # Status must be unchanged.
    assert result["status"] == item["status"]


@pytest.mark.asyncio
async def test_complete_sprint_item_evidence_gate_still_enforced(db):
    """required_notes gate must still fire even though complete_sprint_item now
    routes through _transition_status — the gate runs BEFORE the chokepoint."""
    p, item = await _project_with_item(db)
    # Mark it required_notes.
    await db_module.patch_sprint_item(db, p["id"], item["id"], required_notes=True)

    with pytest.raises(db_module.SprintItemEvidenceRequired):
        await db_module.complete_sprint_item(db, p["id"], item["id"])

    # Providing evidence must succeed.
    result = await db_module.complete_sprint_item(
        db, p["id"], item["id"], notes="shipped in #123"
    )
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_transition_status_not_found_returns_none(db):
    """_transition_status must return None (not raise) when the item doesn't
    exist at all — 'not found' and 'race-lost' both return None; callers
    distinguish them by re-fetching if needed."""
    p, _ = await _project_with_item(db)
    result = await _sprint_items_mod._transition_status(
        db, p["id"], "no-such-item", "done",
        from_statuses=["pending"],
    )
    assert result is None


def test_transition_status_exported_on_both_modules():
    """_transition_status must be importable from both the submodule and db.*
    re-export so callers and tests can reference it consistently."""
    import meridian.db.sprint_items as si
    import meridian.db as db_pkg

    assert hasattr(si, "_transition_status"), (
        "meridian.db.sprint_items must expose _transition_status"
    )
    assert hasattr(db_pkg, "_transition_status"), (
        "meridian.db must re-export _transition_status"
    )
    assert si._transition_status is db_pkg._transition_status


# ---------------------------------------------------------------------------
# ARCH 1C (94a70b98) — consolidated per-caller chokepoint passthrough tests
#
# Now that all transition functions route through _transition_status, the
# concurrency-safety proof is centralised in the _transition_status tests
# above.  What each public caller still needs:
#
#   1. It passes the CORRECT from_statuses / to_status to the chokepoint.
#   2. It surfaces a race-lost (chokepoint returns None) in its own expected
#      way — ValueError for claim, SprintItemStatusRace for the rest.
#
# Both properties are verified by mocking _transition_status directly (fast,
# no concurrent threads needed) rather than re-implementing a full async
# concurrency harness per caller.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_status_real_concurrency_exactly_one_winner(db):
    """One real asyncio.gather concurrency test against _transition_status itself.

    Fires N concurrent coroutines all attempting the SAME pending->in_progress
    transition.  The from_statuses guard in the chokepoint's UPDATE must ensure
    exactly one succeeds (rowcount == 1) and the rest return None.  This is the
    single place that proves atomic concurrency-safety — no per-caller copy
    of this harness is needed.
    """
    p = await db_module.create_project(db, "concurrency-chokepoint")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "concurrent item")
    N = 10

    async def _attempt():
        return await _sprint_items_mod._transition_status(
            db, p["id"], item["id"], "in_progress",
            from_statuses=["pending", "todo", "indeterminate", "provisional_complete"],
            claimed_at_now=True,
        )

    results = await asyncio.gather(*[_attempt() for _ in range(N)])
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, (
        f"Expected exactly 1 winner among {N} concurrent _transition_status "
        f"calls but got {len(winners)}: {winners}"
    )
    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Per-caller from_statuses / to_status passthrough verification
#
# Each test monkeypatches _transition_status to capture the arguments it
# receives and returns a realistic fake row, then calls the public function
# and asserts the chokepoint was called with the right parameters.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_sprint_item_passes_active_statuses_to_chokepoint(db, monkeypatch):
    """complete_sprint_item must call _transition_status (via shim) with
    expected_statuses == _ACTIVE_SPRINT_STATUSES and to_status == 'done'."""
    p, item = await _project_with_item(db)
    captured: dict = {}

    real_update = _sprint_items_mod._update_sprint_item_status
    async def _capture_update(db_conn, project_id, item_id, status, **kwargs):
        captured["to_status"] = status
        captured["expected_statuses"] = kwargs.get("expected_statuses")
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    monkeypatch.setattr(db_module, "_update_sprint_item_status", _capture_update)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _capture_update)

    await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert captured["to_status"] == "done"
    assert captured["expected_statuses"] == _sprint_items_mod._ACTIVE_SPRINT_STATUSES


@pytest.mark.asyncio
async def test_fail_sprint_item_passes_active_statuses_to_chokepoint(db, monkeypatch):
    """fail_sprint_item must call the shim with to_status='failed' and
    expected_statuses == _ACTIVE_SPRINT_STATUSES."""
    p, item = await _project_with_item(db)
    captured: dict = {}

    real_update = _sprint_items_mod._update_sprint_item_status
    async def _capture_update(db_conn, project_id, item_id, status, **kwargs):
        captured["to_status"] = status
        captured["expected_statuses"] = kwargs.get("expected_statuses")
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    monkeypatch.setattr(db_module, "_update_sprint_item_status", _capture_update)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _capture_update)

    await db_module.fail_sprint_item(db, p["id"], item["id"], reason="test")
    assert captured["to_status"] == "failed"
    assert captured["expected_statuses"] == _sprint_items_mod._ACTIVE_SPRINT_STATUSES


@pytest.mark.asyncio
async def test_skip_sprint_item_passes_active_statuses_to_chokepoint(db, monkeypatch):
    """skip_sprint_item must call the shim with to_status='skipped' and
    expected_statuses == _ACTIVE_SPRINT_STATUSES."""
    p, item = await _project_with_item(db)
    captured: dict = {}

    real_update = _sprint_items_mod._update_sprint_item_status
    async def _capture_update(db_conn, project_id, item_id, status, **kwargs):
        captured["to_status"] = status
        captured["expected_statuses"] = kwargs.get("expected_statuses")
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    monkeypatch.setattr(db_module, "_update_sprint_item_status", _capture_update)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _capture_update)

    await db_module.skip_sprint_item(db, p["id"], item["id"], reason="no-op")
    assert captured["to_status"] == "skipped"
    assert captured["expected_statuses"] == _sprint_items_mod._ACTIVE_SPRINT_STATUSES


@pytest.mark.asyncio
async def test_push_sprint_item_passes_active_statuses_to_chokepoint(db, monkeypatch):
    """push_sprint_item must call the shim with to_status='pushed' and
    expected_statuses == _ACTIVE_SPRINT_STATUSES."""
    p, item = await _project_with_item(db)
    captured: dict = {}

    real_update = _sprint_items_mod._update_sprint_item_status
    async def _capture_update(db_conn, project_id, item_id, status, **kwargs):
        captured["to_status"] = status
        captured["expected_statuses"] = kwargs.get("expected_statuses")
        captured["pushed_to"] = kwargs.get("pushed_to")
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    monkeypatch.setattr(db_module, "_update_sprint_item_status", _capture_update)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _capture_update)

    await db_module.push_sprint_item(db, p["id"], item["id"], "v2.0")
    assert captured["to_status"] == "pushed"
    assert captured["expected_statuses"] == _sprint_items_mod._ACTIVE_SPRINT_STATUSES
    assert captured["pushed_to"] == "v2.0"


@pytest.mark.asyncio
async def test_start_sprint_item_passes_pending_todo_to_chokepoint(db, monkeypatch):
    """start_sprint_item must call the shim with to_status='in_progress' and
    expected_statuses == {'pending', 'todo'} (narrower than _ACTIVE_SPRINT_STATUSES)."""
    p, item = await _project_with_item(db)
    captured: dict = {}

    real_update = _sprint_items_mod._update_sprint_item_status
    async def _capture_update(db_conn, project_id, item_id, status, **kwargs):
        captured["to_status"] = status
        captured["expected_statuses"] = kwargs.get("expected_statuses")
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    monkeypatch.setattr(db_module, "_update_sprint_item_status", _capture_update)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _capture_update)

    await db_module.start_sprint_item(db, p["id"], item["id"])
    assert captured["to_status"] == "in_progress"
    assert captured["expected_statuses"] == {"pending", "todo"}


@pytest.mark.asyncio
async def test_provisional_complete_passes_active_statuses_to_chokepoint(db, monkeypatch):
    """provisional_complete_sprint_item must call the shim with
    to_status='provisional_complete' and expected_statuses == _ACTIVE_SPRINT_STATUSES."""
    p, item = await _project_with_item(db)
    captured: dict = {}

    real_update = _sprint_items_mod._update_sprint_item_status
    async def _capture_update(db_conn, project_id, item_id, status, **kwargs):
        captured["to_status"] = status
        captured["expected_statuses"] = kwargs.get("expected_statuses")
        return await real_update(db_conn, project_id, item_id, status, **kwargs)

    monkeypatch.setattr(db_module, "_update_sprint_item_status", _capture_update)
    monkeypatch.setattr(_sprint_items_mod, "_update_sprint_item_status", _capture_update)

    await db_module.provisional_complete_sprint_item(db, p["id"], item["id"])
    assert captured["to_status"] == "provisional_complete"
    assert captured["expected_statuses"] == _sprint_items_mod._ACTIVE_SPRINT_STATUSES


@pytest.mark.asyncio
async def test_claim_sprint_item_passes_claimable_statuses_to_chokepoint(db, monkeypatch):
    """claim_sprint_item must call _transition_status directly (not via shim)
    with from_statuses covering all claimable states and claimed_at_now=True."""
    p, item = await _project_with_item(db)
    captured: dict = {}
    _claimable = {"pending", "todo", "indeterminate", "provisional_complete"}

    real_transition = _sprint_items_mod._transition_status
    async def _capture_transition(db_conn, project_id, item_id, to_status,
                                   from_statuses=None, **kwargs):
        captured["to_status"] = to_status
        captured["from_statuses"] = set(from_statuses) if from_statuses is not None else None
        captured["claimed_at_now"] = kwargs.get("claimed_at_now", False)
        return await real_transition(db_conn, project_id, item_id, to_status,
                                     from_statuses=from_statuses, **kwargs)

    monkeypatch.setattr(db_module, "_transition_status", _capture_transition)
    monkeypatch.setattr(_sprint_items_mod, "_transition_status", _capture_transition)

    await db_module.claim_sprint_item(db, p["id"], item["id"], actor="session-x")
    assert captured["to_status"] == "in_progress"
    assert captured["from_statuses"] == _claimable, (
        f"claim_sprint_item must pass from_statuses={_claimable!r} to "
        f"_transition_status, got {captured['from_statuses']!r}"
    )
    assert captured["claimed_at_now"] is True, (
        "claim_sprint_item must pass claimed_at_now=True to _transition_status"
    )


# ---------------------------------------------------------------------------
# Per-caller race-lost contract: each public function that wraps
# _update_sprint_item_status must raise SprintItemStatusRace (not return None
# or raise a different exception) when the chokepoint returns None.
# claim_sprint_item raises ValueError instead — already covered in the
# pre-ARCH 1B tests above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_sprint_item_raises_status_race_on_transition_none(db, monkeypatch):
    """fail_sprint_item must raise SprintItemStatusRace when _transition_status
    returns None (lost race), not silently return None."""
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.skip_sprint_item(db, p["id"], item["id"], reason="raced")

    # Now item is skipped (terminal) — a call to fail should raise.
    with pytest.raises(db_module.SprintItemStatusRace) as exc_info:
        await db_module.fail_sprint_item(db, p["id"], item["id"])
    assert exc_info.value.current_status == "skipped"


@pytest.mark.asyncio
async def test_skip_sprint_item_raises_status_race_on_transition_none(db, monkeypatch):
    """skip_sprint_item must raise SprintItemStatusRace when _transition_status
    returns None (lost race) — item already in terminal status."""
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.fail_sprint_item(db, p["id"], item["id"], reason="raced")

    with pytest.raises(db_module.SprintItemStatusRace) as exc_info:
        await db_module.skip_sprint_item(db, p["id"], item["id"])
    assert exc_info.value.current_status == "failed"


@pytest.mark.asyncio
async def test_push_sprint_item_raises_status_race_on_transition_none(db, monkeypatch):
    """push_sprint_item must raise SprintItemStatusRace when _transition_status
    returns None (lost race) — item already in terminal status."""
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    with pytest.raises(db_module.SprintItemStatusRace) as exc_info:
        await db_module.push_sprint_item(db, p["id"], item["id"], "v2.0")
    assert exc_info.value.current_status == "done"


@pytest.mark.asyncio
async def test_provisional_complete_raises_status_race_on_transition_none(db, monkeypatch):
    """provisional_complete_sprint_item must raise SprintItemStatusRace when
    the item is already in a terminal state."""
    p, item = await _project_with_item(db)
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    with pytest.raises(db_module.SprintItemStatusRace) as exc_info:
        await db_module.provisional_complete_sprint_item(db, p["id"], item["id"])
    assert exc_info.value.current_status == "done"


@pytest.mark.asyncio
async def test_start_sprint_item_raises_status_race_on_transition_none(db, monkeypatch):
    """start_sprint_item must raise SprintItemStatusRace when the item is not
    in pending/todo (e.g. already in_progress from a concurrent claim)."""
    p, item = await _project_with_item(db)
    # Item is already in_progress from _project_with_item which claims it.
    # But _project_with_item only creates + claims. Let's add item explicitly.
    p2 = await db_module.create_project(db, "start-race")
    item2 = await db_module.add_sprint_item(db, p2["id"], "v1", "start-item")
    await db_module.claim_sprint_item(db, p2["id"], item2["id"])

    with pytest.raises(db_module.SprintItemStatusRace) as exc_info:
        await db_module.start_sprint_item(db, p2["id"], item2["id"])


# ---------------------------------------------------------------------------
# 22cad9b8 — claim_parallel_batch atomicity: a batch item that loses the
# claim race (already in_progress under a DIFFERENT actor by the time the
# batch attempt reaches it) must fail the WHOLE batch cleanly, with any
# earlier-in-the-same-call item claim rolled back — no partial-claim state,
# mirroring test_claim_sprint_item_atomic_race_second_caller_rejected above
# but for the batch entry point.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_parallel_batch_atomic_race_second_caller_rejected(db):
    """Two sessions race to batch-claim overlapping work: session-a's batch
    claims item x cleanly. session-b's batch (item y, then item x) must fail
    entirely on x — a structured ITEM_CLAIM_CONFLICT, not a raised exception —
    and item y (claimed earlier in session-b's OWN call) must be rolled back
    to pending rather than left orphaned in_progress under session-b."""
    p = await db_module.create_project(db, "batch-atomic-race")
    pid = p["id"]
    sess_a = await db_module.register_session(db, pid, "session-a")
    sess_b = await db_module.register_session(db, pid, "session-b")
    x = await db_module.add_sprint_item(
        db, pid, "v1", "x", touches_resources=["file:x.py"], prospect_bypass=True,
    )
    y = await db_module.add_sprint_item(
        db, pid, "v1", "y", touches_resources=["file:y.py"], prospect_bypass=True,
    )

    # session-a wins x outright, first.
    winner = await db_module.claim_parallel_batch(db, pid, sess_a["id"], [x["id"]])
    assert winner["ok"] is True
    assert (await db_module.get_sprint_item(db, x["id"]))["status"] == "in_progress"

    # session-b's batch (y then x) must fail entirely on x -- and y, claimed
    # moments earlier in this SAME batch call, must be rolled back.
    loser = await db_module.claim_parallel_batch(db, pid, sess_b["id"], [y["id"], x["id"]])
    assert loser["ok"] is False
    assert loser["error"] == "ITEM_CLAIM_CONFLICT"
    assert loser["item_id"] == x["id"]

    reread_x = await db_module.get_sprint_item(db, x["id"])
    reread_y = await db_module.get_sprint_item(db, y["id"])
    # x is untouched -- still session-a's legitimate claim.
    assert reread_x["status"] == "in_progress"
    assert reread_x["actor"] == sess_a["id"]
    # y was rolled back to pending, not left orphaned in_progress under session-b.
    assert reread_y["status"] == "pending"
    assert reread_y["claimed_at"] is None
    assert (await db_module.get_file_claims(db, "y.py"))["file_lock"] is None


# ---------------------------------------------------------------------------
# a2a027cf — timeout-safe / observable / idempotent complete_sprint_item.
#
# Repeated live reports: complete_sprint_item calls timing out at the client
# around 60s even though the server-side write may have already succeeded; a
# defensive re-query then reveals the item is already done, or a retry
# returns a misleading STATUS_RACE. This section covers:
#   1. DB-level idempotent retry (completion_outcome, no duplicate side
#      effects, gates skipped on replay).
#   2. Observability (correlation_id, phase_timings_ms).
#   3. Bounded advisory work (rollup/task-chain-advance/continuation-state
#      cannot hold an already-committed response hostage).
#   4. Dispatch-level timeout classification (committed / timed_out_before_
#      commit / unknown_outcome).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_sprint_item_idempotent_retry_returns_already_committed(db):
    """Retrying complete_sprint_item on an item that is ALREADY 'done' is a
    no-op success, not an error -- the direct fix for the reported bug."""
    p, item = await _project_with_item(db)

    first = await db_module.complete_sprint_item(db, p["id"], item["id"], notes="shipped")
    assert first["status"] == "done"
    assert first["completion_outcome"] == "committed"

    second = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert second["status"] == "done"
    assert second["completion_outcome"] == "already_committed"
    assert second["correlation_id"]
    # The original evidence/notes are untouched by the no-op retry.
    assert second["notes"] == "shipped"


@pytest.mark.asyncio
async def test_complete_sprint_item_idempotent_retry_skips_ownership_gate(db):
    """An idempotent retry from a DIFFERENT actor than the one who actually
    completed the item must NOT raise SprintItemClaimMismatch -- once the
    item is done, ownership gates (which only make sense for deciding
    whether an active->done transition may proceed) no longer apply. This is
    exactly the cross-session retry scenario the acceptance criteria calls
    out (an orchestrator re-verifying after a timeout, not the same session
    that made the original call)."""
    p = await db_module.create_project(db, "idempotent-cross-actor")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")
    owner = await db_module.register_session(db, p["id"], "owner-session")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db_module.complete_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    # A different actor retries -- must succeed idempotently, not raise.
    retried = await db_module.complete_sprint_item(
        db, p["id"], item["id"], actor="a-totally-different-session",
    )
    assert retried["status"] == "done"
    assert retried["completion_outcome"] == "already_committed"


@pytest.mark.asyncio
async def test_complete_sprint_item_idempotent_retry_no_duplicate_side_effects(db, monkeypatch):
    """A retry against an already-done item must NOT re-run rollup / the
    task-chain advance -- those fire real side effects (e.g. filing a HITL
    handoff on a mixed-ownership chain transition) that must never be
    duplicated by a timeout-and-retry."""
    p, item = await _project_with_item(db)
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    calls = {"rollup": 0, "chain": 0}
    real_rollup = _sprint_items_mod._maybe_rollup_parent
    real_chain = _sprint_items_mod._advance_task_chain

    async def _counting_rollup(*a, **k):
        calls["rollup"] += 1
        return await real_rollup(*a, **k)

    async def _counting_chain(*a, **k):
        calls["chain"] += 1
        return await real_chain(*a, **k)

    monkeypatch.setattr(_sprint_items_mod, "_maybe_rollup_parent", _counting_rollup)
    monkeypatch.setattr(_sprint_items_mod, "_advance_task_chain", _counting_chain)

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert result["completion_outcome"] == "already_committed"
    assert calls["rollup"] == 0
    assert calls["chain"] == 0


@pytest.mark.asyncio
async def test_complete_sprint_item_correlation_id_generated_and_echoed(db):
    """A caller-supplied correlation_id is echoed back verbatim; when omitted
    one is minted so the response always carries one."""
    p, item = await _project_with_item(db)

    result = await db_module.complete_sprint_item(
        db, p["id"], item["id"], correlation_id="my-custom-correlation-id",
    )
    assert result["correlation_id"] == "my-custom-correlation-id"

    item2 = await db_module.add_sprint_item(db, p["id"], "v1", "second racy item")
    auto = await db_module.complete_sprint_item(db, p["id"], item2["id"])
    assert auto["correlation_id"]  # non-empty, freshly minted
    assert auto["correlation_id"] != "my-custom-correlation-id"


@pytest.mark.asyncio
async def test_complete_sprint_item_phase_timings_present(db):
    """The response carries phase_timings_ms with the expected phase keys so
    a slow phase can be identified from the response itself."""
    p, item = await _project_with_item(db)

    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    timings = result["phase_timings_ms"]
    assert isinstance(timings, dict)
    for phase in ("lookup", "evidence_check", "status_transition", "post_commit_advisory"):
        assert phase in timings, f"missing phase {phase!r} in {timings!r}"
        assert isinstance(timings[phase], (int, float))


@pytest.mark.asyncio
async def test_complete_sprint_item_bounded_advisory_work_does_not_hang(db, monkeypatch):
    """A pathologically slow post-commit advisory step (rollup/chain-advance)
    must not hold the response hostage -- it is bounded by
    _ADVISORY_PHASE_TIMEOUT_S and the response comes back with
    advisory_work_deferred=True instead of hanging until the slow step
    finishes. The core status commit (already done by this point) is
    unaffected either way."""
    p, item = await _project_with_item(db)

    monkeypatch.setattr(_sprint_items_mod, "_ADVISORY_PHASE_TIMEOUT_S", 0.05)

    async def _slow_side_effects(*a, **k):
        await asyncio.sleep(5.0)

    monkeypatch.setattr(_sprint_items_mod, "_run_post_commit_side_effects", _slow_side_effects)

    start = time.monotonic()
    result = await db_module.complete_sprint_item(db, p["id"], item["id"])
    elapsed = time.monotonic() - start

    assert result["status"] == "done"
    assert result["completion_outcome"] == "committed"
    assert result.get("advisory_work_deferred") is True
    assert elapsed < 2.0, f"complete_sprint_item took {elapsed}s -- advisory work held the response hostage"

    # The core write is durable even though the advisory tail was deferred.
    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "done"


# ---------------------------------------------------------------------------
# Dispatch-level (meridian.mcp.handler._dispatch_mcp_tool) timeout handling.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_complete_sprint_item_timeout_classifies_committed(db, monkeypatch):
    """If the dispatch-level budget is exceeded but the underlying write had
    ALREADY committed by the time of the timeout, the timeout response must
    classify the outcome as 'committed' (re-derived from a live re-query),
    not report a bare failure."""
    import meridian.server as srv
    from meridian.mcp import handler as mh

    p, item = await _project_with_item(db)
    monkeypatch.setattr(mh, "_COMPLETE_SPRINT_ITEM_DISPATCH_TIMEOUT_S", 0.05)

    from meridian.mcp.handlers import sprint_tools as st_mod
    real_handle = st_mod.handle_complete_sprint_item

    async def _slow_handle(args, db_arg, data_dir, tenant, mcp_tenant_id):
        # Let the real completion commit first, THEN stall past the
        # dispatch-level timeout -- simulates the exact reported scenario:
        # the write landed, but the response was slow to come back.
        result = await real_handle(args, db_arg, data_dir, tenant, mcp_tenant_id)
        await asyncio.sleep(5.0)
        return result

    monkeypatch.setattr(st_mod, "handle_complete_sprint_item", _slow_handle)

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"]},
        db, "/tmp",
    )
    assert result["error"] == "COMPLETE_SPRINT_ITEM_TIMEOUT"
    assert result["completion_outcome"] == "committed"
    assert result["current_status"] == "done"
    assert result["correlation_id"]

    # The write is real and durable.
    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "done"


@pytest.mark.asyncio
async def test_dispatch_complete_sprint_item_timeout_classifies_timed_out_before_commit(db, monkeypatch):
    """If the dispatch-level budget is exceeded BEFORE the underlying write
    ever happens, the timeout response must classify the outcome as
    'timed_out_before_commit' -- the item is still safely retryable."""
    import meridian.server as srv
    from meridian.mcp import handler as mh

    p, item = await _project_with_item(db)
    monkeypatch.setattr(mh, "_COMPLETE_SPRINT_ITEM_DISPATCH_TIMEOUT_S", 0.05)

    from meridian.mcp.handlers import sprint_tools as st_mod

    async def _never_completes(args, db_arg, data_dir, tenant, mcp_tenant_id):
        await asyncio.sleep(5.0)
        raise AssertionError("should have been cancelled by the dispatch timeout")

    monkeypatch.setattr(st_mod, "handle_complete_sprint_item", _never_completes)

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"]},
        db, "/tmp",
    )
    assert result["error"] == "COMPLETE_SPRINT_ITEM_TIMEOUT"
    assert result["completion_outcome"] == "timed_out_before_commit"
    # _project_with_item creates the item without claiming it, so it's
    # still 'pending' -- the exact pre-completion status doesn't matter
    # here, only that it is NOT 'done' (nothing committed).
    assert result["current_status"] == "pending"

    # Nothing committed -- a normal (non-timed-out) retry now succeeds cleanly.
    retried = await db_module.complete_sprint_item(db, p["id"], item["id"])
    assert retried["status"] == "done"
    assert retried["completion_outcome"] == "committed"


@pytest.mark.asyncio
async def test_dispatch_complete_sprint_item_other_tools_unaffected_by_timeout_wrapping(db):
    """The dispatch-level timeout wrapping is scoped ONLY to
    complete_sprint_item -- an unrelated tool call must behave exactly as
    before (sanity check that the conditional wrapping in _dispatch_mcp_tool
    didn't change control flow for every other tool)."""
    import meridian.server as srv

    p, item = await _project_with_item(db)
    result = await srv._dispatch_mcp_tool(
        "get_sprint_items", {"project_id": p["id"]}, db, "/tmp",
    )
    assert isinstance(result, list)
    assert any(it["id"] == item["id"] for it in result)
