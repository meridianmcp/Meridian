"""Tests for bc9259b8 — worker stall auto-retry + stall_count.

When a worker session closes (or goes stale) with a sprint item still
in_progress instead of completing it, the item is re-queued to pending while it
is within the stall-retry budget, then failed silently once exhausted.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


async def _in_progress_item(db, project_id, title="work"):
    item = await db_module.add_sprint_item(db, project_id, "v1", title)
    await db_module.claim_sprint_item(db, project_id, item["id"])
    return item


# ── core requeue/fail budget ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_requeue_within_budget_then_fail(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    item = await _in_progress_item(db, p["id"])

    # Stall 1 → requeued, stall_count 1
    r1 = await db_module.requeue_or_fail_stalled_item(db, p["id"], item["id"], session_id=s["id"])
    assert r1["action"] == "requeued"
    assert r1["stall_count"] == 1
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] == "pending"
    assert stored["claimed_at"] is None

    # Re-claim, stall 2 → still requeued, stall_count 2
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    r2 = await db_module.requeue_or_fail_stalled_item(db, p["id"], item["id"], session_id=s["id"])
    assert r2["action"] == "requeued"
    assert r2["stall_count"] == 2

    # Re-claim, stall 3 → exceeds budget → failed
    await db_module.claim_sprint_item(db, p["id"], item["id"])
    r3 = await db_module.requeue_or_fail_stalled_item(db, p["id"], item["id"], session_id=s["id"])
    assert r3["action"] == "failed"
    assert r3["stall_count"] == 3
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] == "failed"
    assert stored["stall_count"] == 3


@pytest.mark.asyncio
async def test_requeue_or_fail_noop_when_not_in_progress(db):
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "pending-item")
    # status is pending, not in_progress → no-op
    assert await db_module.requeue_or_fail_stalled_item(db, p["id"], item["id"]) is None


@pytest.mark.asyncio
async def test_requeue_or_fail_noop_for_unknown_or_wrong_project(db):
    p = await db_module.create_project(db, "alpha")
    other = await db_module.create_project(db, "beta")
    item = await _in_progress_item(db, p["id"])
    assert await db_module.requeue_or_fail_stalled_item(db, p["id"], "nope") is None
    # wrong project id
    assert await db_module.requeue_or_fail_stalled_item(db, other["id"], item["id"]) is None


@pytest.mark.asyncio
async def test_failed_item_records_last_session_log(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    item = await _in_progress_item(db, p["id"])
    await db_module.log_task(db, s["id"], p["id"], "tried the migration", "in_progress")
    # Bump straight past budget
    await db.execute(
        "UPDATE sprint_items SET stall_count = 2 WHERE id = ?", (item["id"],)
    )
    await db.commit()
    r = await db_module.requeue_or_fail_stalled_item(db, p["id"], item["id"], session_id=s["id"])
    assert r["action"] == "failed"
    stored = await db_module.get_sprint_item(db, item["id"])
    assert "tried the migration" in (stored["notes"] or "")


# ── close_session integration (graceful close without complete) ─────────────

@pytest.mark.asyncio
async def test_close_session_requeues_linked_item(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    item = await _in_progress_item(db, p["id"])
    # Link the session to the item via a task_log row.
    await db_module.log_task(db, s["id"], p["id"], "did stuff", "in_progress", sprint_item_id=item["id"])
    await db_module.close_session(db, s["id"])
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] == "pending"
    assert stored["stall_count"] == 1


@pytest.mark.asyncio
async def test_close_session_does_not_touch_completed_item(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    item = await _in_progress_item(db, p["id"])
    await db_module.log_task(db, s["id"], p["id"], "shipped it", "done", sprint_item_id=item["id"])
    await db_module.complete_sprint_item(db, p["id"], item["id"])
    await db_module.close_session(db, s["id"])
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] == "done"
    assert stored["stall_count"] == 0


@pytest.mark.asyncio
async def test_close_session_link_via_worktree(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    item = await _in_progress_item(db, p["id"])
    await db_module.register_worktree(
        db, s["id"], p["id"], item_id=item["id"], branch="wt/x", path="/tmp/wt",
    )
    await db_module.close_session(db, s["id"])
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] == "pending"
    assert stored["stall_count"] == 1


@pytest.mark.asyncio
async def test_handle_session_stall_returns_lists(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    item = await _in_progress_item(db, p["id"])
    await db_module.log_task(db, s["id"], p["id"], "wip", "in_progress", sprint_item_id=item["id"])
    res = await db_module.handle_session_stall(db, s["id"])
    assert item["id"] in res["requeued"]
    assert res["failed"] == []


@pytest.mark.asyncio
async def test_handle_session_stall_fails_after_budget(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    item = await _in_progress_item(db, p["id"])
    await db.execute("UPDATE sprint_items SET stall_count = 2 WHERE id = ?", (item["id"],))
    await db.commit()
    await db_module.log_task(db, s["id"], p["id"], "wip", "in_progress", sprint_item_id=item["id"])
    res = await db_module.handle_session_stall(db, s["id"])
    assert item["id"] in res["failed"]


# ── crashed worker recovery via expire_inactive_sessions ────────────────────

@pytest.mark.asyncio
async def test_expire_inactive_sessions_routes_through_stall_logic(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    item = await _in_progress_item(db, p["id"])
    # Worker logged an in_progress task claimed by the session, linked to the item.
    await db.execute(
        "INSERT INTO task_log (id, session_id, project_id, description, status, "
        "claimed_by, claimed_at, sprint_item_id) "
        "VALUES (?, ?, ?, ?, 'in_progress', ?, datetime('now'), ?)",
        (db_module._new_id(), s["id"], p["id"], "wip", s["id"], item["id"]),
    )
    # Make the session look stale (last_seen far in the past).
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-48 hours') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()
    await db_module.expire_inactive_sessions(db, max_age_hours=24)
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["status"] == "pending"
    assert stored["stall_count"] == 1
