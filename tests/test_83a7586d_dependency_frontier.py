"""Tests for 83a7586d — fan-out/fan-in dependency FRONTIER / barrier
representation, and its enforcement at claim time + rendering in
generate_handoff.

Covers the 7 explicit acceptance scenarios from the sprint item:
  (a) one-parent legacy depends_on chain still works
  (b) a fan-out/fan-in DAG (multiple predecessors converging on one item)
  (c) dynamic completion/re-frontiering
  (d) a stale predecessor (claimed but abandoned) does not permanently block
  (e) an in-progress (not yet terminal) predecessor correctly blocks
  (f) resource conflict interaction is distinguished from a dependency block
  (g) multi-project isolation

...plus pure unit coverage for the new meridian.dependency_graph functions,
and a starter/delta/goal/full round-trip proving generate_handoff no longer
raises HandoffStaleReferenceError on a fan-in item's JSON-encoded
``depends_on`` (a confirmed pre-existing gap this item also closes in
meridian/db/board_snapshot.py's find_stale_reference_ids) and correctly
excludes a not-yet-ready fan-in item from every mode's claimable batch.
"""

from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import dependency_graph as dep_graph
from meridian import handoff as handoff_module


def _fan_in(*ids: str) -> str:
    """Build a fan-in barrier depends_on value (JSON id array) for tests."""
    return json.dumps(list(ids))


# ---------------------------------------------------------------------------
# Pure: meridian.dependency_graph new functions
# ---------------------------------------------------------------------------


def test_parse_predecessor_ids_legacy_single_scalar():
    assert dep_graph.parse_predecessor_ids("abc123") == ["abc123"]


def test_parse_predecessor_ids_none_and_empty():
    assert dep_graph.parse_predecessor_ids(None) == []
    assert dep_graph.parse_predecessor_ids("") == []


def test_parse_predecessor_ids_json_array():
    assert dep_graph.parse_predecessor_ids('["a", "b", "c"]') == ["a", "b", "c"]


def test_parse_predecessor_ids_dedupes_preserves_order():
    assert dep_graph.parse_predecessor_ids('["a", "b", "a", "c"]') == ["a", "b", "c"]


def test_parse_predecessor_ids_malformed_json_falls_back_to_literal():
    # Starts with '[' but isn't valid/isn't a list -> treated as one literal id,
    # never silently dropped.
    assert dep_graph.parse_predecessor_ids("[not valid json") == ["[not valid json"]
    assert dep_graph.parse_predecessor_ids("[1, 2]") == ["1", "2"]  # valid JSON list of ints


def test_encode_predecessor_ids_roundtrip():
    assert dep_graph.encode_predecessor_ids([]) is None
    assert dep_graph.encode_predecessor_ids(["a"]) == "a"
    encoded = dep_graph.encode_predecessor_ids(["a", "b", "c"])
    assert dep_graph.parse_predecessor_ids(encoded) == ["a", "b", "c"]


def test_evaluate_frontier_no_predecessors_ready():
    result = dep_graph.evaluate_frontier({"id": "x", "depends_on": None}, {})
    assert result == {
        "predecessor_ids": [], "ready": True, "blocking": [], "predecessor_statuses": {},
    }


def test_evaluate_frontier_single_parent_not_done_blocks():
    item = {"id": "child", "depends_on": "p1"}
    lookup = {"p1": {"id": "p1", "status": "pending"}}
    result = dep_graph.evaluate_frontier(item, lookup)
    assert result["ready"] is False
    assert result["blocking"] == [{"id": "p1", "status": "pending", "reason": "not yet terminal"}]


def test_evaluate_frontier_fan_in_all_predecessors_required():
    item = {"id": "child", "depends_on": _fan_in("p1", "p2", "p3")}
    lookup = {
        "p1": {"status": "done"},
        "p2": {"status": "done"},
        "p3": {"status": "pending"},
    }
    result = dep_graph.evaluate_frontier(item, lookup)
    assert result["ready"] is False
    assert [b["id"] for b in result["blocking"]] == ["p3"]
    # Completing the last one flips it to ready.
    lookup["p3"] = {"status": "done"}
    result2 = dep_graph.evaluate_frontier(item, lookup)
    assert result2["ready"] is True
    assert result2["blocking"] == []


def test_evaluate_frontier_missing_predecessor_blocks():
    item = {"id": "child", "depends_on": "ghost"}
    result = dep_graph.evaluate_frontier(item, {})
    assert result["ready"] is False
    assert result["blocking"] == [
        {"id": "ghost", "status": "missing", "reason": "predecessor not found"}
    ]


def test_evaluate_frontier_failed_predecessor_continue_satisfies():
    item = {"id": "child", "depends_on": "p1", "failure_mode": "continue"}
    lookup = {"p1": {"status": "failed"}}
    assert dep_graph.evaluate_frontier(item, lookup)["ready"] is True


def test_evaluate_frontier_failed_predecessor_stop_blocks():
    item = {"id": "child", "depends_on": "p1", "failure_mode": "stop"}
    lookup = {"p1": {"status": "failed"}}
    result = dep_graph.evaluate_frontier(item, lookup)
    assert result["ready"] is False
    assert result["blocking"][0]["status"] == "failed"


def test_compute_frontier_whole_board():
    items = [
        {"id": "a", "depends_on": None, "status": "pending"},
        {"id": "b", "depends_on": "a", "status": "pending"},
    ]
    frontier = dep_graph.compute_frontier(items)
    assert frontier["a"]["ready"] is True
    assert frontier["b"]["ready"] is False


# ---------------------------------------------------------------------------
# (a) One-parent legacy depends_on chain still works — and is now genuinely
#     ENFORCED at claim time (the confirmed pre-existing gap this item fixes).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_single_parent_claim_blocked_then_allowed(db):
    p = await db_module.create_project(db, "83a7586d-legacy-chain")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child", depends_on=parent["id"],
    )
    result = await db_module.claim_sprint_item(db, p["id"], child["id"])
    assert result["blocked"] is True
    assert result["error"] == "DEPENDENCY_NOT_SATISFIED"
    assert result["predecessor_ids"] == [parent["id"]]

    await db_module.complete_sprint_item(db, p["id"], parent["id"])
    claimed = await db_module.claim_sprint_item(db, p["id"], child["id"])
    assert claimed["status"] == "in_progress"


@pytest.mark.asyncio
async def test_no_dependency_declared_is_unaffected(db):
    """An item with no depends_on at all sees zero behavior change (no
    predecessors to check, short-circuits with no DB access)."""
    p = await db_module.create_project(db, "83a7586d-no-dep")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "solo")
    result = await db_module.claim_sprint_item(db, p["id"], item["id"])
    assert result["status"] == "in_progress"


# ---------------------------------------------------------------------------
# (b) Fan-out/fan-in DAG: multiple predecessors converging on one item.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_in_dag_blocks_until_every_predecessor_done(db):
    p = await db_module.create_project(db, "83a7586d-fan-in")
    p1 = await db_module.add_sprint_item(db, p["id"], "v1", "branch 1")
    p2 = await db_module.add_sprint_item(db, p["id"], "v1", "branch 2")
    p3 = await db_module.add_sprint_item(db, p["id"], "v1", "branch 3")
    join = await db_module.add_sprint_item(
        db, p["id"], "v1", "join item",
        depends_on=_fan_in(p1["id"], p2["id"], p3["id"]),
    )

    blocked0 = await db_module.claim_sprint_item(db, p["id"], join["id"])
    assert blocked0["blocked"] is True
    assert blocked0["error"] == "DEPENDENCY_NOT_SATISFIED"
    assert set(blocked0["predecessor_ids"]) == {p1["id"], p2["id"], p3["id"]}
    assert {b["id"] for b in blocked0["blocking_predecessors"]} == {p1["id"], p2["id"], p3["id"]}

    await db_module.complete_sprint_item(db, p["id"], p1["id"])
    await db_module.complete_sprint_item(db, p["id"], p2["id"])
    blocked1 = await db_module.claim_sprint_item(db, p["id"], join["id"])
    assert blocked1["blocked"] is True
    # Only the still-pending third branch remains a blocker.
    assert [b["id"] for b in blocked1["blocking_predecessors"]] == [p3["id"]]

    await db_module.complete_sprint_item(db, p["id"], p3["id"])
    claimed = await db_module.claim_sprint_item(db, p["id"], join["id"])
    assert claimed["status"] == "in_progress"


# ---------------------------------------------------------------------------
# (c) Dynamic completion / re-frontiering: get_parallelizable_groups reflects
#     the live board on every call, advancing exactly as predecessors finish.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_parallelizable_groups_refrontiers_fan_in_item(db):
    p = await db_module.create_project(db, "83a7586d-refrontier")
    p1 = await db_module.add_sprint_item(
        db, p["id"], "v1", "branch 1", touches_resources=["file:b1.py"],
    )
    p2 = await db_module.add_sprint_item(
        db, p["id"], "v1", "branch 2", touches_resources=["file:b2.py"],
    )
    join = await db_module.add_sprint_item(
        db, p["id"], "v1", "join item", touches_resources=["file:join.py"],
        depends_on=_fan_in(p1["id"], p2["id"]),
    )

    res0 = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible0 = {it["id"] for g in res0["groups"] for it in g}
    blocked0 = {b["id"]: b for b in res0["blocked"]}
    assert join["id"] not in eligible0
    assert join["id"] in blocked0
    assert set(blocked0[join["id"]]["predecessor_ids"]) == {p1["id"], p2["id"]}
    assert len(blocked0[join["id"]]["blocking_predecessors"]) == 2

    await db_module.complete_sprint_item(db, p["id"], p1["id"])
    res1 = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    blocked1 = {b["id"]: b for b in res1["blocked"]}
    eligible1 = {it["id"] for g in res1["groups"] for it in g}
    assert join["id"] not in eligible1
    assert [b["id"] for b in blocked1[join["id"]]["blocking_predecessors"]] == [p2["id"]]

    await db_module.complete_sprint_item(db, p["id"], p2["id"])
    res2 = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible2 = {it["id"] for g in res2["groups"] for it in g}
    assert join["id"] in eligible2
    assert res2["blocked"] == []


# ---------------------------------------------------------------------------
# (d) A stale predecessor (claimed but abandoned) does not permanently block.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_predecessor_is_reconciled_not_permanently_blocking(db):
    p = await db_module.create_project(db, "83a7586d-stale-predecessor")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child", depends_on=parent["id"],
    )
    owner = await db_module.register_session(db, p["id"], "abandoned-owner")
    claimed_parent = await db_module.claim_sprint_item(db, p["id"], parent["id"], actor=owner["id"])
    assert claimed_parent["status"] == "in_progress"

    # Simulate a crashed/killed executor: its session is explicitly closed —
    # the same unconditional "proof of death" signal classify_stale_claim
    # already uses elsewhere in this codebase (56e9b3c7/268d4e9b).
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    # This claim attempt on `child` is refused THIS call (parent isn't done
    # yet) but must not leave `parent` permanently wedged in_progress with a
    # dead claimant nobody can ever reclaim.
    blocked = await db_module.claim_sprint_item(db, p["id"], child["id"])
    assert blocked["blocked"] is True
    assert blocked["error"] == "DEPENDENCY_NOT_SATISFIED"

    reconciled_parent = await db_module.get_sprint_item(db, parent["id"])
    assert reconciled_parent["status"] == "pending"
    assert reconciled_parent.get("claimed_at") is None

    # Forward progress: a fresh session can now reclaim + complete the
    # predecessor, which in turn unblocks the fan-in/legacy child.
    rescuer = await db_module.register_session(db, p["id"], "rescuer")
    reclaimed = await db_module.claim_sprint_item(db, p["id"], parent["id"], actor=rescuer["id"])
    assert reclaimed["status"] == "in_progress"
    await db_module.complete_sprint_item(db, p["id"], parent["id"], actor=rescuer["id"])
    claimed_child = await db_module.claim_sprint_item(db, p["id"], child["id"])
    assert claimed_child["status"] == "in_progress"


# ---------------------------------------------------------------------------
# (e) An in-progress (not yet terminal, NOT stale) predecessor correctly
#     blocks the fan-in item — and is left untouched (no reconciliation).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_in_progress_predecessor_blocks_and_is_not_reset(db):
    p = await db_module.create_project(db, "83a7586d-active-predecessor")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child", depends_on=parent["id"],
    )
    owner = await db_module.register_session(db, p["id"], "live-owner")
    await db_module.claim_sprint_item(db, p["id"], parent["id"], actor=owner["id"])

    blocked = await db_module.claim_sprint_item(db, p["id"], child["id"])
    assert blocked["blocked"] is True
    assert blocked["error"] == "DEPENDENCY_NOT_SATISFIED"
    assert blocked["blocking_predecessors"][0]["status"] == "in_progress"

    # The genuinely-live predecessor claim must be left completely alone.
    still_claimed = await db_module.get_sprint_item(db, parent["id"])
    assert still_claimed["status"] == "in_progress"
    assert still_claimed["actor"] == owner["id"]


# ---------------------------------------------------------------------------
# (f) Resource conflict is distinguished from a dependency block.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resource_conflict_and_dependency_block_are_distinct_error_codes(db):
    p = await db_module.create_project(db, "83a7586d-resource-vs-dependency")
    sess = await db_module.register_session(db, p["id"], "orchestrator")
    other = await db_module.register_session(db, p["id"], "other-holder")

    # Item R: no dependency issue at all, but its declared resource is
    # already held live by a different session.
    r_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "resource-contended item",
        touches_resources=["file:contended.py"], prospect_bypass=True,
    )
    assert (await db_module.claim_file(db, "contended.py", other["id"]))["claimed"] is True
    r_result = await db_module.claim_parallel_batch(db, p["id"], sess["id"], [r_item["id"]])
    assert r_result["ok"] is False
    assert r_result["error"] == "BATCH_RESOURCE_CONFLICT"

    # Item D: no resource conflict at all, but has a genuinely unmet
    # dependency. Must be refused with a DIFFERENT, dependency-specific code.
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "unmet parent")
    d_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "dependency-blocked item",
        touches_resources=["file:unrelated.py"], prospect_bypass=True,
        depends_on=parent["id"],
    )
    d_result = await db_module.claim_parallel_batch(db, p["id"], sess["id"], [d_item["id"]])
    assert d_result["ok"] is False
    assert d_result["error"] == "DEPENDENCY_NOT_SATISFIED"
    assert r_result["error"] != d_result["error"]


# ---------------------------------------------------------------------------
# (g) Multi-project isolation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_project_predecessor_never_satisfies_dependency(db):
    p_a = await db_module.create_project(db, "83a7586d-isolation-a")
    p_b = await db_module.create_project(db, "83a7586d-isolation-b")
    # A real, DONE item -- but it lives in project B.
    foreign_done = await db_module.add_sprint_item(db, p_b["id"], "v1", "done in project B")
    await db_module.complete_sprint_item(db, p_b["id"], foreign_done["id"])

    child = await db_module.add_sprint_item(
        db, p_a["id"], "v1", "child in project A", depends_on=foreign_done["id"],
    )
    frontier = await db_module.get_dependency_frontier(
        db, await db_module.get_sprint_item(db, child["id"]),
    )
    # The foreign item's real 'done' status must NEVER satisfy this project's
    # dependency -- it is treated exactly like a nonexistent id.
    assert frontier["ready"] is False
    assert frontier["blocking"] == [
        {"id": foreign_done["id"], "status": "missing", "reason": "predecessor not found"}
    ]

    result = await db_module.claim_sprint_item(db, p_a["id"], child["id"])
    assert result["blocked"] is True
    assert result["error"] == "DEPENDENCY_NOT_SATISFIED"


@pytest.mark.asyncio
async def test_fan_in_dependency_graphs_do_not_cross_project(db):
    """Two projects each independently declare a fan-in item; completing
    project B's branches must never affect project A's frontier."""
    p_a = await db_module.create_project(db, "83a7586d-isolation-fan-in-a")
    p_b = await db_module.create_project(db, "83a7586d-isolation-fan-in-b")
    a1 = await db_module.add_sprint_item(db, p_a["id"], "v1", "a branch 1", force=True)
    a2 = await db_module.add_sprint_item(db, p_a["id"], "v1", "a branch 2", force=True)
    a_join = await db_module.add_sprint_item(
        db, p_a["id"], "v1", "a join", depends_on=_fan_in(a1["id"], a2["id"]),
    )
    b1 = await db_module.add_sprint_item(db, p_b["id"], "v1", "b branch 1", force=True)
    b2 = await db_module.add_sprint_item(db, p_b["id"], "v1", "b branch 2", force=True)
    await db_module.add_sprint_item(
        db, p_b["id"], "v1", "b join", depends_on=_fan_in(b1["id"], b2["id"]),
    )

    # Complete BOTH of project B's branches.
    await db_module.complete_sprint_item(db, p_b["id"], b1["id"])
    await db_module.complete_sprint_item(db, p_b["id"], b2["id"])

    # Project A's join item must still see both of ITS OWN branches pending.
    a_join_row = await db_module.get_sprint_item(db, a_join["id"])
    frontier_a = await db_module.get_dependency_frontier(db, a_join_row)
    assert frontier_a["ready"] is False
    assert len(frontier_a["blocking"]) == 2


# ---------------------------------------------------------------------------
# generate_handoff round-trip: starter/delta/goal/full all honor the
# frontier, and a fan-in item's JSON depends_on no longer trips the
# pre-existing HandoffStaleReferenceError fail-closed gate (board_snapshot.
# find_stale_reference_ids was single-parent-only before this item).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_generate_handoff_fan_in_with_in_batch_predecessors_every_mode(db, tmp_path, mode):
    """Both of the fan-in item's predecessors are ALSO pending in this same
    goal (dependency-CLOSURE case, mirroring the pre-existing legacy
    single-parent contract — see test_dependency_closure_pulls_in_pending_parent
    in test_handoff_item_selection.py): the item must NOT be hard-excluded,
    it must simply render AFTER its predecessors (dependency order), never
    as concurrently dispatchable right now. Also proves a fan-in item's JSON
    depends_on no longer trips the pre-existing HandoffStaleReferenceError
    fail-closed gate (board_snapshot.find_stale_reference_ids was
    single-parent-only before this item)."""
    p = await db_module.create_project(db, f"83a7586d-handoff-{mode}")
    p1 = await db_module.add_sprint_item(db, p["id"], "v1", "branch 1")
    p2 = await db_module.add_sprint_item(db, p["id"], "v1", "branch 2")
    join = await db_module.add_sprint_item(
        db, p["id"], "v1", "join item",
        depends_on=_fan_in(p1["id"], p2["id"]),
    )
    other = await db_module.add_sprint_item(db, p["id"], "v1", "independent item")

    # Must not raise HandoffStaleReferenceError.
    _path, text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
    )
    assert "excluded_dependency_not_satisfied" not in text
    for iid in (p1["id"], p2["id"], join["id"], other["id"]):
        assert iid in text
    if mode == "goal":
        # Dependency order: both real predecessors must be named before the
        # fan-in item that needs them both done first.
        assert text.index(p1["id"]) < text.index(join["id"])
        assert text.index(p2["id"]) < text.index(join["id"])


@pytest.mark.asyncio
async def test_generate_handoff_excludes_fan_in_item_with_truly_external_predecessor(db, tmp_path):
    """One predecessor is genuinely OUTSIDE this goal's own pending batch
    (a different sprint-version bucket, so it's invisible to this
    version-scoped render) — the fan-in item must be excluded via the
    structured note, never silently presented as claimable."""
    p = await db_module.create_project(db, "83a7586d-handoff-external-block")
    external_parent = await db_module.add_sprint_item(db, p["id"], "v2", "external branch")
    in_batch_parent = await db_module.add_sprint_item(db, p["id"], "v1", "in-batch branch")
    join = await db_module.add_sprint_item(
        db, p["id"], "v1", "join item",
        depends_on=_fan_in(external_parent["id"], in_batch_parent["id"]),
    )
    other = await db_module.add_sprint_item(db, p["id"], "v1", "independent item")

    _path, text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal", version="v1",
    )
    assert "excluded_dependency_not_satisfied" in text
    assert join["id"] in text  # named in the exclusion note, not the claimable batch
    assert other["id"] in text


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_frontier_ready_after_completion(db, tmp_path):
    p = await db_module.create_project(db, "83a7586d-handoff-goal-ready")
    p1 = await db_module.add_sprint_item(db, p["id"], "v1", "branch 1")
    p2 = await db_module.add_sprint_item(db, p["id"], "v1", "branch 2")
    join = await db_module.add_sprint_item(
        db, p["id"], "v1", "join item",
        depends_on=_fan_in(p1["id"], p2["id"]), prospect_bypass=True,
    )
    await db_module.claim_sprint_item(db, p["id"], p1["id"])
    await db_module.complete_sprint_item(db, p["id"], p1["id"])
    await db_module.claim_sprint_item(db, p["id"], p2["id"])
    await db_module.complete_sprint_item(db, p["id"], p2["id"])

    _path, goal_text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert join["id"] in goal_text
    assert "excluded_dependency_not_satisfied" not in goal_text
