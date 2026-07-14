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
"""
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
async def test_mcp_handler_complete_sprint_item_surfaces_status_race(db):
    import meridian.server as srv

    p, item = await _project_with_item(db)
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    result = await srv._dispatch_mcp_tool(
        "complete_sprint_item",
        {"project_id": p["id"], "item_id": item["id"]},
        db, "/tmp",
    )
    assert result["error"] == "STATUS_RACE"
    assert result["item_id"] == item["id"]
    assert result["current_status"] == "done"


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
