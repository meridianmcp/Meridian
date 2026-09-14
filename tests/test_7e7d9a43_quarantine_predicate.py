"""Tests for sprint item 7e7d9a43 — exclude superseded and deferred items from
handoffs and parallel-wave eligibility, current-scope correction.

Confirmed gaps this closes (see the commit-tagged comments at each site for
the full rationale):

1. ``get_parallelizable_groups`` (meridian/db/sprint_items.py) already
   excluded hard-blocked items (blocker_kind in ('superseded',
   'systemic_invalidated_run'), 524e73e6) but had NO deferred-item filter at
   all — unlike get_sprint_items(include_deferred=False) and
   assign_sprint_waves's own deferred check. A future-deferred item could
   still be advertised as a parallel-safe batch member.
2. ``assign_sprint_waves`` (same file) already excluded deferred items
   (5a67c8e0) but never excluded hard-blocked ones — the mirror-image gap.
   A superseded/invalidated item could be persisted a real wave-N/
   wave-urgent label.
3. ``build_continuation_manifest`` (meridian/handoff.py) excluded hard-
   blocked items from its claimable ``pending_item_ids`` (07229675) but
   never excluded deferred ones.
4. ``continuation_gate.compute_continuation_state`` (used by
   get_sprint_progress) had no awareness of ``deferred_until`` at all, so a
   deferred item could appear in ``continuation.actionable_item_ids``.

Both ``get_parallelizable_groups`` and ``assign_sprint_waves`` now report
every excluded hard-blocked/deferred item in a new, unified ``quarantined``
list (never silently dropped) — this file also covers that reporting.

Precedent check (per this item's own instructions): manual-gated items
(blocker_kind='manual') are NOT added to the new ``quarantined`` list in
get_parallelizable_groups/assign_sprint_waves — see
test_5a85a78f_manual_item_exclusion.py, the existing precedent for those two
functions, which shows manual items are simply absent from ``groups``/
``eligible_count`` with no dedicated per-function reporting field; the
"excluded but labeled" treatment for manual items lives one layer up, in
handoff.py's own ``_build_manual_todo_note``/``<exclusions>`` tag, which this
change does not touch.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from meridian import continuation_gate
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import server as srv


def _future_iso(hours: int = 72) -> str:
    return (datetime.utcnow() + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# 1. get_parallelizable_groups — deferred exclusion (NEW) + superseded
#    exclusion (regression, 524e73e6) + quarantined reporting (NEW)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_parallelizable_groups_quarantines_deferred_item_current_version(db):
    p = await db_module.create_project(db, "7e7d9a43-pg-deferred-current")
    deferred = await db_module.add_sprint_item(
        db, p["id"], "current", "backburnered item", deferred_until=_future_iso(),
    )
    normal = await db_module.add_sprint_item(db, p["id"], "current", "normal item")

    res = await db_module.get_parallelizable_groups(db, p["id"], version="current")

    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert deferred["id"] not in eligible_ids
    assert normal["id"] in eligible_ids
    quarantined_ids = {q["id"]: q for q in res["quarantined"]}
    assert deferred["id"] in quarantined_ids
    assert quarantined_ids[deferred["id"]]["reason"] == "deferred"
    assert quarantined_ids[deferred["id"]]["deferred_until"]
    assert res["quarantined_count"] == 1


@pytest.mark.asyncio
async def test_get_parallelizable_groups_quarantines_deferred_item_explicit_version(db):
    """Same predicate applied when scoped to an explicit (non-'current') version."""
    p = await db_module.create_project(db, "7e7d9a43-pg-deferred-v1")
    deferred = await db_module.add_sprint_item(
        db, p["id"], "v1", "backburnered item", deferred_until=_future_iso(),
    )
    normal = await db_module.add_sprint_item(db, p["id"], "v1", "normal item")

    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")

    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert deferred["id"] not in eligible_ids
    assert normal["id"] in eligible_ids
    assert {q["id"] for q in res["quarantined"]} == {deferred["id"]}


@pytest.mark.asyncio
async def test_get_parallelizable_groups_quarantines_superseded_item_and_reports_it(db):
    """Regression (524e73e6 already excluded it) + NEW: now reported in
    ``quarantined`` too, where before it silently vanished with no dedicated
    field at all."""
    p = await db_module.create_project(db, "7e7d9a43-pg-superseded-current")
    superseded = await db_module.add_sprint_item(
        db, p["id"], "current", "old approach", blocker_kind="superseded", force=True,
    )
    invalidated = await db_module.add_sprint_item(
        db, p["id"], "current", "invalidated run",
        blocker_kind="systemic_invalidated_run", force=True,
    )
    normal = await db_module.add_sprint_item(db, p["id"], "current", "normal item")

    res = await db_module.get_parallelizable_groups(db, p["id"], version="current")

    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert superseded["id"] not in eligible_ids
    assert invalidated["id"] not in eligible_ids
    assert normal["id"] in eligible_ids
    quarantined_by_id = {q["id"]: q for q in res["quarantined"]}
    assert quarantined_by_id[superseded["id"]]["reason"] == "superseded"
    assert quarantined_by_id[invalidated["id"]]["reason"] == "systemic_invalidated_run"
    assert res["quarantined_count"] == 2


@pytest.mark.asyncio
async def test_get_parallelizable_groups_manual_item_not_double_counted_in_quarantine(db):
    """Precedent check: a manual-gated item stays excluded from groups (pre-
    existing, 5a85a78f) but is NOT added to the new ``quarantined`` list —
    matching the existing convention that manual items get no dedicated
    per-function reporting field in get_parallelizable_groups."""
    p = await db_module.create_project(db, "7e7d9a43-pg-manual")
    manual = await db_module.add_sprint_item(
        db, p["id"], "current", "configure PyPI trusted publisher",
        blocker_kind="manual",
    )
    normal = await db_module.add_sprint_item(db, p["id"], "current", "normal item")

    res = await db_module.get_parallelizable_groups(db, p["id"], version="current")

    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert manual["id"] not in eligible_ids
    assert normal["id"] in eligible_ids
    assert manual["id"] not in {q["id"] for q in res["quarantined"]}


# ---------------------------------------------------------------------------
# 2. assign_sprint_waves — hard-blocked exclusion (NEW) + deferred exclusion
#    (regression, 5a67c8e0) + quarantined reporting (NEW)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_sprint_waves_excludes_hard_blocked_item_current_version(db):
    p = await db_module.create_project(db, "7e7d9a43-waves-hardblocked-current")
    superseded = await db_module.add_sprint_item(
        db, p["id"], "current", "old approach", blocker_kind="superseded", force=True,
    )
    normal = await db_module.add_sprint_item(db, p["id"], "current", "normal item")

    result = await db_module.assign_sprint_waves(db, p["id"], version="current")

    r_superseded = await db_module.get_sprint_item(db, superseded["id"])
    r_normal = await db_module.get_sprint_item(db, normal["id"])
    assert r_superseded["wave"] is None
    assert superseded["id"] not in {
        item_id for ids in result["waves"].values() for item_id in ids
    }
    assert r_normal["wave"] is not None
    quarantined_by_id = {q["id"]: q for q in result["quarantined"]}
    assert quarantined_by_id[superseded["id"]]["reason"] == "superseded"


@pytest.mark.asyncio
async def test_assign_sprint_waves_still_excludes_deferred_and_reports_quarantine(db):
    """Regression (5a67c8e0) + NEW: now reported in ``quarantined`` too."""
    p = await db_module.create_project(db, "7e7d9a43-waves-deferred-v1")
    deferred = await db_module.add_sprint_item(
        db, p["id"], "v1", "backburnered item", deferred_until=_future_iso(),
    )
    normal = await db_module.add_sprint_item(db, p["id"], "v1", "normal item")

    result = await db_module.assign_sprint_waves(db, p["id"], version="v1")

    r_deferred = await db_module.get_sprint_item(db, deferred["id"])
    assert r_deferred["wave"] is None
    quarantined_by_id = {q["id"]: q for q in result["quarantined"]}
    assert quarantined_by_id[deferred["id"]]["reason"] == "deferred"
    assert quarantined_by_id[deferred["id"]]["deferred_until"]


@pytest.mark.asyncio
async def test_assign_sprint_waves_dependent_of_hard_blocked_parent_still_counts_blocked(db):
    """Dependency-frontier correctness check for the quarantine design: a
    hard-blocked parent is excluded from ``candidates`` (never labelled) but
    MUST remain visible to the function's own dependency bookkeeping so a
    real dependent item is still correctly counted as blocked on it (not
    miscounted as "dependency satisfied" merely because the parent vanished
    from the lookup table)."""
    p = await db_module.create_project(db, "7e7d9a43-waves-dependent-of-blocked")
    parent = await db_module.add_sprint_item(
        db, p["id"], "v1", "superseded parent", blocker_kind="superseded", force=True,
    )
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child item", depends_on=parent["id"],
    )

    result = await db_module.assign_sprint_waves(db, p["id"], version="v1")

    assert result["blocked_count"] == 1
    r_child = await db_module.get_sprint_item(db, child["id"])
    # The child still gets a projected future-wave label (existing
    # "blocked items get a future wave, not NULL" behavior) — the point of
    # this test is blocked_count, not the label's exact value.
    assert child["id"] not in {q["id"] for q in result["quarantined"]}
    assert parent["id"] in {q["id"] for q in result["quarantined"]}


@pytest.mark.asyncio
async def test_assign_sprint_waves_idempotent_with_hard_blocked_item_present(db):
    """Re-running assign_sprint_waves with a hard-blocked item on the board
    must not error and must keep producing the same wave assignment for the
    real items (idempotency, mirrors test_assign_sprint_waves_idempotent)."""
    p = await db_module.create_project(db, "7e7d9a43-waves-idempotent")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "superseded", blocker_kind="superseded", force=True,
    )
    await db_module.add_sprint_item(db, p["id"], "v1", "solo", touches_resources=["file:x.py"])
    first = await db_module.assign_sprint_waves(db, p["id"], version="v1")
    second = await db_module.assign_sprint_waves(db, p["id"], version="v1")
    assert first["waves"] == second["waves"]


# ---------------------------------------------------------------------------
# 3. build_continuation_manifest — deferred exclusion (NEW) + hard-blocked
#    exclusion (regression, 07229675)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuation_manifest_excludes_deferred_item_current_version(db):
    p = await db_module.create_project(db, "7e7d9a43-manifest-deferred")
    deferred = await db_module.add_sprint_item(
        db, p["id"], "current", "backburnered item", deferred_until=_future_iso(),
    )
    normal = await db_module.add_sprint_item(db, p["id"], "current", "normal item")

    manifest = await handoff_module.build_continuation_manifest(
        db, p["id"], version="current",
    )

    assert normal["id"] in manifest["pending_item_ids"]
    assert deferred["id"] not in manifest["pending_item_ids"]
    deferred_ids = {d["id"] for d in manifest["deferred_pending_ids"]}
    assert deferred["id"] in deferred_ids


@pytest.mark.asyncio
async def test_continuation_manifest_still_excludes_hard_blocked_item(db):
    """Regression — 07229675 already excluded this; must still hold."""
    p = await db_module.create_project(db, "7e7d9a43-manifest-hardblocked")
    superseded = await db_module.add_sprint_item(
        db, p["id"], "v1", "old approach", blocker_kind="superseded", force=True,
    )
    normal = await db_module.add_sprint_item(db, p["id"], "v1", "normal item")

    manifest = await handoff_module.build_continuation_manifest(db, p["id"], version="v1")

    assert normal["id"] in manifest["pending_item_ids"]
    assert superseded["id"] not in manifest["pending_item_ids"]
    hard_blocked_ids = {h["id"] for h in manifest["hard_blocked_pending_ids"]}
    assert superseded["id"] in hard_blocked_ids
    # Not double-reported as deferred.
    assert superseded["id"] not in {d["id"] for d in manifest["deferred_pending_ids"]}


# ---------------------------------------------------------------------------
# 4. continuation_gate / get_sprint_progress — deferred exclusion (NEW) +
#    superseded exclusion (regression, already correct via blocker_kind)
# ---------------------------------------------------------------------------


def test_compute_continuation_state_excludes_future_deferred_item():
    items = [
        {"id": "a", "status": "pending"},
        {"id": "b", "status": "pending", "deferred_until": _future_iso()},
    ]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["actionable_item_ids"] == ["a"]
    assert state["deferred_item_ids"] == ["b"]
    assert state["deferred_count"] == 1
    # Deferred items are NOT folded into blocked_count/blocked_item_ids —
    # that field keeps its pure "structural blocker_kind" meaning.
    assert state["blocked_count"] == 0


def test_compute_continuation_state_past_deferred_until_is_actionable():
    """A deferred_until in the PAST is not a backburner any more — actionable."""
    past = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    items = [{"id": "a", "status": "pending", "deferred_until": past}]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["actionable_item_ids"] == ["a"]
    assert state["deferred_count"] == 0


def test_compute_continuation_state_deferred_only_is_terminal_ready():
    items = [{"id": "a", "status": "pending", "deferred_until": _future_iso()}]
    state = continuation_gate.compute_continuation_state(items, execution_mode="autonomous")
    assert state["continuation_required"] is False
    assert state["terminal_ready"] is True
    assert state["deferred_count"] == 1


@pytest.mark.asyncio
async def test_get_sprint_progress_continuation_excludes_deferred_item(db):
    p = await db_module.create_project(db, "7e7d9a43-progress-deferred")
    await db_module.add_sprint_item(
        db, p["id"], "current", "backburnered item", deferred_until=_future_iso(),
    )

    res = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": p["id"], "version": "current"}, db, "/tmp",
    )
    assert res["continuation"]["actionable_item_ids"] == []
    assert res["continuation"]["deferred_count"] == 1
    assert res["continuation"]["terminal_ready"] is True


@pytest.mark.asyncio
async def test_get_sprint_progress_continuation_still_excludes_superseded_item(db):
    """Regression — a hard-blocked item already carries a non-empty
    blocker_kind, so it was already excluded from `actionable` via the
    pre-existing genuine-blocker check; confirm this remains true."""
    p = await db_module.create_project(db, "7e7d9a43-progress-superseded")
    await db_module.add_sprint_item(
        db, p["id"], "current", "old approach", blocker_kind="superseded", force=True,
    )

    res = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": p["id"], "version": "current"}, db, "/tmp",
    )
    assert res["continuation"]["actionable_item_ids"] == []
    assert res["continuation"]["blocked_count"] == 1


@pytest.mark.asyncio
async def test_get_sprint_progress_continuation_mixed_board_only_normal_actionable(db):
    p = await db_module.create_project(db, "7e7d9a43-progress-mixed")
    normal = await db_module.add_sprint_item(db, p["id"], "current", "normal item")
    await db_module.add_sprint_item(
        db, p["id"], "current", "old approach", blocker_kind="superseded", force=True,
    )
    await db_module.add_sprint_item(
        db, p["id"], "current", "backburnered", deferred_until=_future_iso(), force=True,
    )

    res = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": p["id"], "version": "current"}, db, "/tmp",
    )
    assert res["continuation"]["actionable_item_ids"] == [normal["id"]]
    assert res["continuation"]["continuation_required"] is True


# ---------------------------------------------------------------------------
# 5. Cross-consumer agreement — get_parallelizable_groups vs generate_handoff
#    (every mode) for a deferred item, mirroring
#    test_524e73e6_wave_boundary_rendering.py's own superseded-item
#    cross-mode test for the sibling hard-blocked predicate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "compact", "full", "delta"])
async def test_every_handoff_mode_excludes_deferred_item_consistently(db, tmp_path, mode):
    p = await db_module.create_project(db, f"7e7d9a43-deferred-{mode}")
    normal = await db_module.add_sprint_item(db, p["id"], "v1", "normal item")
    deferred = await db_module.add_sprint_item(
        db, p["id"], "v1", "backburnered item", deferred_until=_future_iso(), force=True,
    )

    _path, text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode, version="v1",
    )
    assert normal["id"] in text, mode
    # The deferred item must never appear as claimable executor-facing
    # content in ANY mode (pre-existing 0a65f5cc behavior — regression
    # check, not this item's own fix).
    sprint_items_block = None
    if "<sprint_items>" in text:
        import re as _re
        sprint_items_block = _re.search(
            r"<sprint_items>(.*?)</sprint_items>", text, _re.S,
        ).group(1)
        assert deferred["id"] not in sprint_items_block, mode

    # get_parallelizable_groups (this item's own fix) must agree: the
    # deferred item never appears in a claimable group either.
    groups = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    group_ids = {it["id"] for g in groups["groups"] for it in g}
    assert deferred["id"] not in group_ids, mode
    assert deferred["id"] in {q["id"] for q in groups["quarantined"]}, mode
