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


# ---------------------------------------------------------------------------
# amend_handoff (63b602ff) — explicit, scoped handoff amendment.
#
# The public counterpart to the automatic amend-vs-fresh detection tested
# above: instead of implicitly targeting "the most recent handoffs row" via
# pending_goal, amend_handoff targets a caller-named source_handoff_id with a
# typed patch, reusing 3af86d28's handoff_corrections data structure and
# regenerate_handoff_correction renderer.
# ---------------------------------------------------------------------------


async def _seed_handoff(db, name: str, tmp_path, *, session_id=None, sprint="s1"):
    """Create a project with a goal + one FRESH generated handoff row.
    Returns (project, handoff_row)."""
    p = await db_module.create_project(db, name)
    await db_module.set_goal(db, p["id"], "ship it", sprint=sprint)
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, session_id=session_id,
    )
    rows = await db_module.get_handoffs(db, p["id"], limit=1)
    return p, rows[0]


@pytest.mark.asyncio
async def test_amend_handoff_rejects_unknown_source(db, tmp_path):
    p = await db_module.create_project(db, "amend-explicit-unknown-source")
    with pytest.raises(handoff_module.HandoffAmendError):
        await handoff_module.amend_handoff(
            db, p["id"], "nonexistent-handoff-id", str(tmp_path),
            blocker_classification="other",
        )


@pytest.mark.asyncio
async def test_amend_handoff_rejects_cross_project_source(db, tmp_path):
    p1, h1 = await _seed_handoff(db, "amend-explicit-proj-a", tmp_path)
    p2 = await db_module.create_project(db, "amend-explicit-proj-b")
    with pytest.raises(handoff_module.HandoffAmendError):
        await handoff_module.amend_handoff(
            db, p2["id"], h1["id"], str(tmp_path),
            blocker_classification="other",
        )


@pytest.mark.asyncio
async def test_amend_handoff_rejects_cross_session_source(db, tmp_path):
    p = await db_module.create_project(db, "amend-explicit-cross-session")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    session_a = await db_module.register_session(db, p["id"], "executor-a")
    session_b = await db_module.register_session(db, p["id"], "executor-b")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, session_id=session_a["id"],
    )
    h = (await db_module.get_handoffs(db, p["id"], limit=1))[0]

    with pytest.raises(handoff_module.HandoffAmendError):
        await handoff_module.amend_handoff(
            db, p["id"], h["id"], str(tmp_path),
            session_id=session_b["id"], blocker_classification="other",
        )


@pytest.mark.asyncio
async def test_amend_handoff_version_mismatch_raises(db, tmp_path):
    p = await db_module.create_project(db, "amend-explicit-version-mismatch")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    session = await db_module.register_session(
        db, p["id"], "executor-v1", sprint_version="v1",
    )
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, session_id=session["id"],
    )
    h = (await db_module.get_handoffs(db, p["id"], limit=1))[0]

    with pytest.raises(handoff_module.HandoffAmendError):
        await handoff_module.amend_handoff(
            db, p["id"], h["id"], str(tmp_path),
            session_id=session["id"], version="v2",
            blocker_classification="scope_stale",
        )


@pytest.mark.asyncio
async def test_amend_handoff_non_material_patch_records_without_regenerating(db, tmp_path):
    """An empty typed patch (no item/pointer/wave changes, no
    force_regenerate) records a pure evidence row and leaves the source
    handoff live and executable -- no invalidation, no new revision."""
    p, h = await _seed_handoff(db, "amend-explicit-nonmaterial", tmp_path)

    result = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(tmp_path),
        blocker_classification="environment_blocked",
        correction_rationale="waiting on a flaky CI runner, no fix yet",
        status="blocked",
    )

    assert result["amended"] is False
    assert result["new_handoff_id"] is None
    assert result["invalidated_source"] is None
    assert result["correction"]["status"] == "blocked"
    assert result["correction"]["investigation_evidence"] == (
        "waiting on a flaky CI runner, no fix yet"
    )

    # The source row must still be live (not invalidated).
    refetched_source = await db_module.get_handoff(db, h["id"])
    assert bool(refetched_source["invalidated"]) is False

    # No new revision landed -- still exactly one handoffs row.
    rows = await db_module.get_handoffs(db, p["id"], limit=10)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_amend_handoff_material_patch_invalidates_and_regenerates(db, tmp_path):
    """A material patch (here: add_item_ids) invalidates the source and
    produces a new revision, mirroring regenerate_handoff_correction's own
    invalidate-source-preserve-body-produce-new-row guarantees."""
    p, h = await _seed_handoff(db, "amend-explicit-material", tmp_path)
    original_body = h["body"]
    item = await db_module.add_sprint_item(db, p["id"], "s1", "new item to pull in")

    result = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(tmp_path),
        add_item_ids=[item["id"]],
        blocker_classification="scope_stale",
        correction_rationale="a new item landed on the board mid-session",
    )

    assert result["amended"] is True
    assert result["new_handoff_id"] is not None
    assert result["new_handoff_id"] != h["id"]
    assert result["new_token"]
    assert bool(result["invalidated_source"]["invalidated"]) is True
    assert result["invalidated_source"]["body"] == original_body  # untouched
    assert result["correction"]["status"] == "verified"
    assert result["correction"]["requested_scope"]["add_item_ids"] == [item["id"]]
    assert item["id"] not in result["invalid_add_item_ids"]

    # Source preserved verbatim, independently re-fetched.
    refetched_source = await db_module.get_handoff(db, h["id"])
    assert refetched_source["body"] == original_body
    assert bool(refetched_source["invalidated"]) is True

    # Exactly two handoffs rows now exist (source + new revision).
    rows = await db_module.get_handoffs(db, p["id"], limit=10)
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {h["id"], result["new_handoff_id"]}


@pytest.mark.asyncio
async def test_amend_handoff_replace_item_ids_expands_to_remove_and_add(db, tmp_path):
    p, h = await _seed_handoff(db, "amend-explicit-replace", tmp_path)
    old_item = await db_module.add_sprint_item(db, p["id"], "s1", "stale item")
    new_item = await db_module.add_sprint_item(db, p["id"], "s1", "replacement item")

    result = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(tmp_path),
        replace_item_ids={old_item["id"]: new_item["id"]},
        blocker_classification="scope_stale",
    )

    scope = result["correction"]["requested_scope"]
    assert scope["replace_item_ids"] == [
        {"old_id": old_item["id"], "new_id": new_item["id"]}
    ]
    assert scope["add_item_ids"] == [new_item["id"]]
    assert scope["remove_item_ids"] == [old_item["id"]]
    assert result["amended"] is True


@pytest.mark.asyncio
async def test_amend_handoff_force_regenerate_with_empty_patch(db, tmp_path):
    """force_regenerate=True triggers invalidation + a new revision even
    when no item/pointer/wave change was requested."""
    p, h = await _seed_handoff(db, "amend-explicit-force", tmp_path)

    result = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(tmp_path),
        blocker_classification="capability_unavailable",
        force_regenerate=True,
    )

    assert result["amended"] is True
    assert result["new_handoff_id"] is not None
    assert bool(result["invalidated_source"]["invalidated"]) is True


@pytest.mark.asyncio
async def test_amend_handoff_invalid_add_item_ids_surfaced_not_hard_failed(db, tmp_path):
    """An add_item_ids entry that does not resolve on the live board is
    surfaced back in invalid_add_item_ids rather than raising -- best-effort
    validation, matching the sprint-item spec."""
    p, h = await _seed_handoff(db, "amend-explicit-invalid-ids", tmp_path)

    result = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(tmp_path),
        add_item_ids=["totally-made-up-item-id"],
        blocker_classification="scope_stale",
    )

    assert result["amended"] is True  # still material -- regeneration proceeds
    assert "totally-made-up-item-id" in result["invalid_add_item_ids"]


@pytest.mark.asyncio
async def test_amend_handoff_idempotency_key_dedups(db, tmp_path):
    p, h = await _seed_handoff(db, "amend-explicit-idem", tmp_path)

    first = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(tmp_path),
        blocker_classification="other", idempotency_key="amend-retry-1",
    )
    second = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(tmp_path),
        blocker_classification="other", idempotency_key="amend-retry-1",
    )

    assert first["correction"]["id"] == second["correction"]["id"]
    listed = await handoff_module.list_handoff_corrections(db, p["id"])
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_amend_handoff_reuses_handoff_corrections_row(db, tmp_path):
    """amend_handoff must persist through the SAME handoff_corrections table
    3af86d28 shipped -- not a parallel storage mechanism."""
    p, h = await _seed_handoff(db, "amend-explicit-reuse", tmp_path)

    result = await handoff_module.amend_handoff(
        db, p["id"], h["id"], str(tmp_path),
        blocker_classification="pointer_unresolved",
        correction_rationale="pointer target moved",
        pointer_repairs=[{
            "source_type": "code",
            "targets": [{"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
        }],
    )

    stored = await handoff_module.get_handoff_correction(db, result["correction"]["id"])
    assert stored is not None
    assert stored["source_handoff_id"] == h["id"]
    assert stored["blocker_classification"] == "pointer_unresolved"
    assert stored["investigation_evidence"] == "pointer target moved"
    assert len(stored["added_pointers"]) == 1
    assert stored["pointer_repair_report"] is not None  # written by regenerate
