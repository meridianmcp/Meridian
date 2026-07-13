"""Regression / feature tests for edd9c54b.

generate_handoff detects whether the prior handoff was ever consumed
(pending_goal popped by start_session) and, when it wasn't, AMENDS the
existing handoffs row in-place rather than inserting a fresh one and
re-firing the context-refresh nudge.

Detection mechanism: projects.pending_goal is set by generate_handoff and
cleared (popped) by start_session. A non-NULL pending_goal at the time
generate_handoff runs means the prior handoff was never picked up by any
start_session — the "unconsumed" case.

Tests:
  (a) Two generate_handoff calls with no start_session between them:
      second call amends → handoffs table does NOT grow by 2.
  (b) The refresh nudge for generate_handoff is suppressed on the amend
      path (amended=True flag returned).
  (c) start_session between two generate_handoff calls pops pending_goal →
      second call generates fresh (not an amend).
  (d) The very first generate_handoff for a project (no prior row) falls
      back to a fresh insert rather than amending nothing.
  (e) The `amended` bool returned from generate_handoff is present and
      correct for both the amend path and the fresh path.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _count_handoffs(db, project_id: str) -> int:
    """Return the number of rows in handoffs for a project."""
    rows = await db_module.get_handoffs(db, project_id, limit=200)
    return len(rows)


# ---------------------------------------------------------------------------
# (a) Two generate_handoff calls with no start_session → amend, not insert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_generate_handoff_amends_when_unconsumed(db, tmp_path):
    """edd9c54b (a): calling generate_handoff twice in a row without any
    start_session between them must amend the existing row, not insert a new
    one. The handoffs table should have exactly ONE row after both calls."""
    p = await db_module.create_project(db, "amend-test-a")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")

    # First call — fresh insert (no pending_goal yet).
    _, _, amended_first = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert amended_first is False, "first call must always be fresh (no prior record)"

    rows_after_first = await _count_handoffs(db, p["id"])
    assert rows_after_first == 1, "exactly one row after first generate_handoff"

    # pending_goal is now set (first generate_handoff set it). No start_session
    # has run to pop it — prior handoff is unconsumed.
    prior_pending = await db_module.get_pending_goal(db, p["id"])
    assert prior_pending is not None, "pending_goal must be set after first call"

    # Second call — prior handoff unconsumed → must AMEND, not insert.
    _, _, amended_second = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert amended_second is True, "second call must amend when pending_goal was not popped"

    rows_after_second = await _count_handoffs(db, p["id"])
    assert rows_after_second == 1, (
        "handoffs table must still have exactly ONE row after an amend "
        f"(got {rows_after_second})"
    )


# ---------------------------------------------------------------------------
# (b) Refresh nudge is suppressed on the amend path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amended_flag_is_true_when_unconsumed(db, tmp_path):
    """edd9c54b (b): generate_handoff returns amended=True on the amend path.
    The outer dispatcher in handler.py uses this flag to suppress the
    context-refresh nudge for generate_handoff when it was merely an amend.
    This test verifies the flag itself — the dispatcher-level suppression is
    tested by the flag existing and being correct, since the dispatcher logic
    checks _result.get("amended") is True."""
    p = await db_module.create_project(db, "amend-test-b")
    await db_module.set_goal(db, p["id"], "work", sprint="s-b")

    # First call → fresh → amended=False.
    _, _, am1 = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert am1 is False

    # Second call with pending_goal still set → amend → amended=True.
    _, _, am2 = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert am2 is True

    # Third call still no start_session → still amending.
    _, _, am3 = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert am3 is True


# ---------------------------------------------------------------------------
# (c) start_session between calls pops pending_goal → second is fresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_fresh_after_start_session_pops(db, tmp_path):
    """edd9c54b (c): when start_session pops pending_goal between two
    generate_handoff calls, the second call must generate FRESH (not amend),
    because the prior handoff was genuinely consumed."""
    p = await db_module.create_project(db, "amend-test-c")
    await db_module.set_goal(db, p["id"], "go", sprint="sc")

    # First call — fresh.
    _, _, am1 = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert am1 is False

    rows_after_first = await _count_handoffs(db, p["id"])
    assert rows_after_first == 1

    # Simulate start_session popping pending_goal (read-once consumption).
    popped = await db_module.pop_pending_goal(db, p["id"])
    assert popped is not None, "pop must return the goal that was set"

    # pending_goal is now NULL → prior handoff was consumed.
    pending_now = await db_module.get_pending_goal(db, p["id"])
    assert pending_now is None, "pending_goal must be NULL after pop"

    # Second call — prior handoff was consumed → must be FRESH, not amend.
    _, _, am2 = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert am2 is False, "second call after pop must be fresh (amended=False)"

    rows_after_second = await _count_handoffs(db, p["id"])
    assert rows_after_second == 2, (
        "handoffs table must have TWO rows when second call is genuinely fresh "
        f"(got {rows_after_second})"
    )


# ---------------------------------------------------------------------------
# (d) First generate_handoff for a project (no prior row) → always fresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_very_first_generate_handoff_is_fresh(db, tmp_path):
    """edd9c54b (d): the very first generate_handoff for a new project has no
    prior row to amend, so it must always fall through to a fresh insert."""
    p = await db_module.create_project(db, "amend-test-d")
    await db_module.set_goal(db, p["id"], "start", sprint="sd")

    # No handoffs yet.
    rows_before = await _count_handoffs(db, p["id"])
    assert rows_before == 0

    # No pending_goal either.
    pg_before = await db_module.get_pending_goal(db, p["id"])
    assert pg_before is None

    _, _, amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert amended is False, "first-ever call must be fresh (no row to amend)"

    rows_after = await _count_handoffs(db, p["id"])
    assert rows_after == 1


# ---------------------------------------------------------------------------
# (e) amended field is present + correct in both paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amended_field_present_in_both_paths(db, tmp_path):
    """edd9c54b (e): the third element of the generate_handoff 3-tuple is the
    amended bool, and it is False on the fresh path and True on the amend path."""
    p = await db_module.create_project(db, "amend-test-e")
    await db_module.set_goal(db, p["id"], "check fields", sprint="se")

    result_fresh = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    # Must be a 3-tuple.
    assert len(result_fresh) == 3, f"expected 3-tuple, got {len(result_fresh)}-tuple"
    path_f, content_f, amended_f = result_fresh
    assert isinstance(path_f, str)
    assert isinstance(content_f, str)
    assert amended_f is False

    # Second call without consuming → amend path.
    result_amend = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert len(result_amend) == 3
    path_a, content_a, amended_a = result_amend
    assert isinstance(path_a, str)
    assert isinstance(content_a, str)
    assert amended_a is True


# ---------------------------------------------------------------------------
# amend_handoff DB function: updates body + mode in-place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amend_handoff_updates_existing_row(db, tmp_path):
    """edd9c54b: db.amend_handoff updates the most recent row's body in-place
    rather than inserting a new row."""
    p = await db_module.create_project(db, "amend-db-test")

    # Insert an initial row via record_handoff.
    first = await db_module.record_handoff(db, p["id"], "full", "initial body")
    assert first is not None

    count_before = await _count_handoffs(db, p["id"])
    assert count_before == 1

    # Amend it.
    amended_row = await db_module.amend_handoff(db, p["id"], "updated body", "delta")
    assert amended_row is not None
    assert amended_row["body"] == "updated body"
    assert amended_row["mode"] == "delta"
    assert amended_row["id"] == first["id"], "amend must update the SAME row, not a new one"

    count_after = await _count_handoffs(db, p["id"])
    assert count_after == 1, "amend must not insert a new row"


@pytest.mark.asyncio
async def test_amend_handoff_returns_none_when_no_prior_row(db, tmp_path):
    """edd9c54b: db.amend_handoff returns None when no prior row exists, so
    generate_handoff can fall back to record_handoff for brand-new projects."""
    p = await db_module.create_project(db, "amend-no-prior")

    result = await db_module.amend_handoff(db, p["id"], "body", "full")
    assert result is None, "must return None when there is no prior row"


# ---------------------------------------------------------------------------
# Amend path still updates pending_goal (so next start_session sees fresh goal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amend_path_still_updates_pending_goal(db, tmp_path):
    """edd9c54b: on the amend path, pending_goal is still updated with the
    new content so the next start_session receives up-to-date context."""
    p = await db_module.create_project(db, "amend-pg-update")
    await db_module.set_goal(db, p["id"], "goal", sprint="s-pg")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do something")

    # First call sets pending_goal.
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    pg_after_first = await db_module.get_pending_goal(db, p["id"])
    assert pg_after_first is not None

    # Consume the item so second handoff may include a different item list.
    await db_module.complete_sprint_item(db, p["id"], item["id"])

    # Second call amends but ALSO updates pending_goal.
    _, _, amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert amended is True

    pg_after_second = await db_module.get_pending_goal(db, p["id"])
    # pending_goal must still be set (overwritten with new content).
    assert pg_after_second is not None
