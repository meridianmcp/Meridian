"""Regression test for 00dbeed0.

generate_handoff(mode='delta')'s "Completed since last handoff" list was scoped
by since_ts read from _SESSION_HANDOFF_STATE — a plain in-memory, per-process
Python dict. That dict is empty after every redeploy/restart and is not shared
across prod's multiple Fly machines/regions, so since_ts silently falls back to
None in either case, and _completed_after(x, None) returns True for everything —
delta then dumps the ENTIRE project history instead of a genuinely compact delta.
Confirmed live: 496KB+ for a project with weeks of history.

Fix: read the prior handoff's timestamp from the ALREADY-DURABLE handoffs table
(record_handoff/get_handoffs, keyed by session_id) instead of the in-memory dict.
This test simulates the exact failure mode: a session's PRIOR handoff exists only
in the DB (as it would after a redeploy clears the in-memory dict), and asserts
delta correctly scopes to items completed after that DB-recorded timestamp rather
than falling back to "since forever".
"""
import asyncio
import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


@pytest.mark.asyncio
async def test_delta_scopes_by_durable_prior_handoff_not_memory_cache(db, tmp_path, monkeypatch):
    # Simulate a fresh process: the in-memory cache has nothing for this session,
    # even though a real handoff for it exists (durably) in the DB.
    monkeypatch.setattr(handoff_module, "_SESSION_HANDOFF_STATE", {})

    p = await db_module.create_project(db, "delta-durable")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    s = await db_module.register_session(db, p["id"], "sess-durable")

    # An OLD item, completed well before the "prior handoff" boundary. A real
    # gap is needed here (not just before the next item) because _completed_after
    # uses >= : a completed_at that lands in the SAME second as since_ts counts
    # as "at or after" the boundary, so old_item must clearly precede it.
    old_item = await db_module.add_sprint_item(db, p["id"], "v1", "old shipped thing")
    await db_module.complete_sprint_item(db, p["id"], old_item["id"])
    await asyncio.sleep(1.1)

    # Record a prior handoff DURABLY (as generate_handoff itself would have,
    # before this process restarted) — this is the only trace of "last handoff"
    # available, since the in-memory cache was just wiped above.
    await db_module.record_handoff(db, p["id"], "delta", "prior handoff body", s["id"])

    # A brief real pause so the new item's completed_at is unambiguously after
    # the prior handoff's created_at (both are second-resolution timestamps).
    await asyncio.sleep(1.1)

    new_item = await db_module.add_sprint_item(db, p["id"], "v1", "new shipped thing", force=True)
    await db_module.complete_sprint_item(db, p["id"], new_item["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=s["id"],
    )

    assert "new shipped thing" in content
    assert "old shipped thing" not in content, (
        "delta fell back to the full history instead of scoping by the "
        "durably-recorded prior handoff timestamp"
    )


@pytest.mark.asyncio
async def test_delta_with_no_prior_handoff_at_all_still_shows_everything(db, tmp_path, monkeypatch):
    """A session's genuinely FIRST delta call (no prior handoff anywhere) has no
    "last handoff" boundary to read from the handoffs table. 7732e096: it is
    NOT unbounded, though -- since_ts falls back to this session's own
    ``created_at`` (session start), so items completed at/after the session
    started still show. This test's single item is added+completed AFTER the
    session is registered, so it lands inside that window either way; see
    tests/test_7732e096_delta_session_scope.py for the case that proves items
    completed BEFORE the session started are correctly excluded."""
    monkeypatch.setattr(handoff_module, "_SESSION_HANDOFF_STATE", {})

    p = await db_module.create_project(db, "delta-first-ever")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    s = await db_module.register_session(db, p["id"], "sess-first")

    item = await db_module.add_sprint_item(db, p["id"], "v1", "only shipped thing")
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=s["id"],
    )
    assert "only shipped thing" in content


@pytest.mark.asyncio
async def test_delta_prefers_memory_cache_when_present_no_extra_db_cost_semantics(db, tmp_path, monkeypatch):
    """When the in-memory cache DOES have a value (the common warm-process case),
    the DB lookup result must not silently override a MORE RECENT in-memory
    timestamp with an older DB one -- the durable lookup only fills the gap when
    the in-memory cache is empty for this session, it does not replace a fresher
    in-memory value with a stale DB row from an earlier call."""
    p = await db_module.create_project(db, "delta-warm-cache")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    s = await db_module.register_session(db, p["id"], "sess-warm")

    old_item = await db_module.add_sprint_item(db, p["id"], "v1", "shipped before warm boundary")
    await db_module.complete_sprint_item(db, p["id"], old_item["id"])
    await asyncio.sleep(1.1)  # see the >= boundary note in the test above

    # First real delta call — this both records a durable handoff row AND
    # populates the in-memory cache for this session (normal warm-process path).
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=s["id"],
    )

    await asyncio.sleep(1.1)
    new_item = await db_module.add_sprint_item(db, p["id"], "v1", "shipped after warm boundary", force=True)
    await db_module.complete_sprint_item(db, p["id"], new_item["id"])

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=s["id"],
    )
    assert "shipped after warm boundary" in content
    assert "shipped before warm boundary" not in content
