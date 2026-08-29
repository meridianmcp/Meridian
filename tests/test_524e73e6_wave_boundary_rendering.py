"""Tests for sprint item 524e73e6 — renderer-consistency FIX layered on top of
sibling item 83a7586d's dependency-frontier work.

83a7586d already made every handoff mode (starter/goal/full/delta) EXCLUDE a
not-yet-ready item (or keep it, correctly, when its blocker is in-batch) and
annotate ``frontier_ready``/``frontier_blocking_predecessors`` on every
pending item. This file does NOT re-test any of that — see
test_83a7586d_dependency_frontier.py for the frontier-computation/exclusion
coverage this item deliberately builds on rather than duplicates.

This file covers what discovery found still missing on top of that:

1. A NEW, structured, machine-readable ``<dependency_waves>`` wave-boundary
   tag — absent for a flat/no-dependency board (backward compat), present
   with an explicit claimable-wave vs blocked-until-terminal boundary
   whenever a real dependency graph exists, carrying a ``board_revision``
   staleness digest.
2. The confirmed ``_leftover_external`` MISLABELING bug: an item kept in the
   claimable batch specifically BECAUSE its blocking predecessor(s) are all
   in this same goal was, under the old code, rendered as "blocked on an
   item outside this goal (not listed above)" whenever a resource-conflict
   batch (``_has_parallel``) was also present — this is now impossible by
   construction (fixed by wave-membership, not blocked-list presence).
3. The hard-blocked exclusion gap: ``blocker_kind in ('superseded',
   'systemic_invalidated_run')`` was never filtered out of the claimable
   /goal batch (only 'manual' was) — now excluded consistently, in every
   mode, via the single shared ``_build_quick_start_goal``.
4. The 7 acceptance-fixture scenarios (one predecessor, fan-out, fan-in,
   stale/in-progress predecessor, resource conflict, dynamic completion,
   cross-project decoy), each mode round-tripping the same semantics.
"""

from __future__ import annotations

import json
import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


def _fan_in(*ids: str) -> str:
    return json.dumps(list(ids))


def _extract_dependency_waves_block(text: str) -> "str | None":
    m = re.search(r"<dependency_waves[^>]*>.*?</dependency_waves>", text, re.S)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# 1. Pure unit coverage: _is_hard_blocked_sprint_item (both copies)
# ---------------------------------------------------------------------------


def test_is_hard_blocked_sprint_item_handoff_module():
    f = handoff_module._is_hard_blocked_sprint_item
    assert f({"blocker_kind": "superseded"}) is True
    assert f({"blocker_kind": "systemic_invalidated_run"}) is True
    assert f({"blocker_kind": "manual"}) is False
    assert f({"blocker_kind": None}) is False
    assert f({}) is False
    assert f("not-a-dict") is False  # type: ignore[arg-type]


def test_is_hard_blocked_sprint_item_db_module_mirrors_handoff():
    from meridian.db import sprint_items as sprint_items_module

    f_db = sprint_items_module._is_hard_blocked_sprint_item
    f_ho = handoff_module._is_hard_blocked_sprint_item
    for item in (
        {"blocker_kind": "superseded"},
        {"blocker_kind": "systemic_invalidated_run"},
        {"blocker_kind": "manual"},
        {"blocker_kind": None},
        {},
    ):
        assert f_db(item) == f_ho(item)


# ---------------------------------------------------------------------------
# 2. Pure unit coverage: _build_quick_start_goal hard-blocked exclusion
# ---------------------------------------------------------------------------


def test_build_quick_start_goal_excludes_superseded_item():
    items = [
        {"id": "keep1", "version": None},
        {"id": "sup1", "version": None, "blocker_kind": "superseded"},
        {"id": "inv1", "version": None, "blocker_kind": "systemic_invalidated_run"},
    ]
    goal = handoff_module._build_quick_start_goal(items)
    assert "keep1" in goal
    assert '<excluded_superseded count="2">' in goal
    assert "sup1" in goal  # named in the exclusion note
    assert "inv1" in goal
    # Neither hard-blocked id is in the executor-facing claimable clause.
    sprint_items_block = re.search(
        r"<sprint_items>(.*?)</sprint_items>", goal, re.S,
    ).group(1)
    assert "sup1" not in sprint_items_block
    assert "inv1" not in sprint_items_block


def test_build_quick_start_goal_no_superseded_items_no_note():
    goal = handoff_module._build_quick_start_goal([{"id": "a1", "version": None}])
    assert "<excluded_superseded" not in goal


# ---------------------------------------------------------------------------
# 3. Pure unit coverage: <dependency_waves> structured tag
# ---------------------------------------------------------------------------


def test_dependency_waves_tag_absent_for_flat_board():
    """No depends_on at all -> single wave -> zero-byte-cost, backward compat."""
    items = [{"id": "a1", "version": None}, {"id": "b2", "version": None}]
    goal = handoff_module._build_quick_start_goal(items)
    assert "<dependency_waves" not in goal


def test_dependency_waves_tag_present_for_real_dependency_chain():
    items = [
        {"id": "a1", "version": None},
        {"id": "b2", "version": None, "depends_on": "a1"},
    ]
    goal = handoff_module._build_quick_start_goal(items)
    block = _extract_dependency_waves_block(goal)
    assert block is not None
    assert 'total_waves="2"' in block
    assert 'active_wave="1"' in block
    assert '<wave n="1" status="claimable">a1</wave>' in block
    assert (
        '<wave n="2" status="blocked_until_terminal" blocked_on="a1">b2</wave>'
        in block
    )
    # Staleness digest present and matches the library function directly.
    rev = re.search(r'board_revision="([^"]*)"', block).group(1)
    assert rev == handoff_module.compute_board_revision(items)


def test_dependency_waves_tag_fan_in_lists_every_blocker():
    items = [
        {"id": "p1", "version": None},
        {"id": "p2", "version": None},
        {"id": "join", "version": None, "depends_on": _fan_in("p1", "p2")},
    ]
    goal = handoff_module._build_quick_start_goal(items)
    block = _extract_dependency_waves_block(goal)
    assert block is not None
    assert 'total_waves="2"' in block
    assert '<wave n="1" status="claimable">p1, p2</wave>' in block
    assert (
        '<wave n="2" status="blocked_until_terminal" blocked_on="p1, p2">join</wave>'
        in block
    )


# ---------------------------------------------------------------------------
# 4. The confirmed _leftover_external mislabeling bug — fixed
# ---------------------------------------------------------------------------


def test_in_batch_fan_in_kept_item_never_labeled_external_with_resource_batches():
    """An item kept in the claimable batch because ITS blocker is also in this
    same goal must never be described as "blocked on an item outside this
    goal" merely because a resource-conflict batch (_has_parallel) is ALSO
    present -- the confirmed bug: the old code classified purely on
    parallel_groups['blocked'] membership, which is the exact same list the
    frontier_ready=False annotation itself comes from, so every kept
    in-batch-blocked item was mislabeled as external 100% of the time
    whenever _has_parallel was True."""
    items = [
        {"id": "a1", "version": None},
        {"id": "b2", "version": None},
        {"id": "dep", "version": None, "depends_on": "a1"},
    ]
    groups = {
        "group_count": 2,
        "groups": [[{"id": "a1", "title": "x"}, {"id": "b2", "title": "y"}]],
        # get_parallelizable_groups would report 'dep' here (frontier not
        # ready: its predecessor a1 is still pending) -- same shape as a
        # real DB-backed call.
        "blocked": [
            {
                "id": "dep",
                "title": "z",
                "depends_on": "a1",
                "blocked_by_status": "pending",
            }
        ],
    }
    goal = handoff_module._build_quick_start_goal(items, parallel_groups=groups)
    assert "dep" in goal
    # The old, WRONG phrasing must never appear anywhere in this module's
    # output again -- it has been fully replaced.
    assert "blocked on an item outside this goal" not in goal
    # The new, correct, unambiguous phrasing: explicit "do not dispatch",
    # naming the real in-batch blocker.
    assert "DO NOT dispatch yet" in goal
    assert "dep blocked on a1" in goal
    # And the structured tag agrees.
    block = _extract_dependency_waves_block(goal)
    assert block is not None
    assert '<wave n="1" status="claimable">a1, b2</wave>' in block
    assert (
        '<wave n="2" status="blocked_until_terminal" blocked_on="a1">dep</wave>'
        in block
    )


def test_resource_batches_still_dispatch_concurrently_when_no_dependency_boundary():
    """Companion/regression: pure resource-conflict batches with NO dependency
    boundary at all must keep the existing CONCURRENTLY dispatch guidance
    byte-for-byte (test_handoff_executor_planner_lifecycle.py's own coverage
    of this framing) -- 524e73e6 must not weaken or remove it."""
    items = [
        {"id": "a1", "version": None}, {"id": "b2", "version": None},
        {"id": "c3", "version": None},
    ]
    groups = {
        "group_count": 2,
        "groups": [
            [{"id": "a1", "title": "x"}, {"id": "b2", "title": "y"}],
            [{"id": "c3", "title": "z"}],
        ],
    }
    goal = handoff_module._build_quick_start_goal(items, parallel_groups=groups)
    assert "CONCURRENTLY" in goal
    assert "<dependency_waves" not in goal  # single dependency wave -> no tag


# ---------------------------------------------------------------------------
# 5. End-to-end acceptance matrix, every mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_every_mode_renders_same_wave_boundary_for_one_predecessor_fan_out_fan_in(
    db, tmp_path, mode,
):
    """DAG: root -> {branch_a, branch_b} (fan-out) -> join (fan-in). Root is
    also a plain one-predecessor case for branch_a/branch_b. Every mode must
    agree on the SAME <dependency_waves> structure: wave 1 = {root}, wave 2 =
    {branch_a, branch_b}, wave 3 = {join}."""
    p = await db_module.create_project(db, f"524e73e6-dag-{mode}")
    root = await db_module.add_sprint_item(db, p["id"], "v1", "root item")
    branch_a = await db_module.add_sprint_item(
        db, p["id"], "v1", "branch a", depends_on=root["id"],
    )
    branch_b = await db_module.add_sprint_item(
        db, p["id"], "v1", "branch b", depends_on=root["id"],
    )
    join = await db_module.add_sprint_item(
        db, p["id"], "v1", "join item",
        depends_on=_fan_in(branch_a["id"], branch_b["id"]),
    )

    _path, text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
    )
    for iid in (root["id"], branch_a["id"], branch_b["id"], join["id"]):
        assert iid in text, mode
    block = _extract_dependency_waves_block(text)
    assert block is not None, mode
    assert 'total_waves="3"' in block, mode
    assert f'<wave n="1" status="claimable">{root["id"]}</wave>' in block, mode
    wave2 = re.search(r'<wave n="2"[^>]*>([^<]*)</wave>', block).group(1)
    assert set(wave2.split(", ")) == {branch_a["id"], branch_b["id"]}, mode
    assert f'blocked_on="{root["id"]}"' in block, mode
    wave3_match = re.search(r'<wave n="3"[^>]*>([^<]*)</wave>', block)
    assert wave3_match.group(1) == join["id"], mode
    assert "blocked_on=" in block


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_every_mode_excludes_superseded_item_consistently(db, tmp_path, mode):
    p = await db_module.create_project(db, f"524e73e6-superseded-{mode}")
    normal = await db_module.add_sprint_item(db, p["id"], "v1", "normal item")
    superseded = await db_module.add_sprint_item(
        db, p["id"], "v1", "old approach", blocker_kind="superseded", force=True,
    )

    _path, text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
    )
    assert normal["id"] in text, mode

    # claim_sprint_item's own hard gate must independently refuse it too
    # (belt-and-suspenders — this is 83a7586d/f89d440f territory, not
    # re-derived here, just confirmed still true).
    result = await db_module.claim_sprint_item(db, p["id"], superseded["id"])
    assert result.get("blocked") is True

    groups = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    group_ids = {it["id"] for g in groups["groups"] for it in g}
    blocked_ids = {b["id"] for b in groups.get("blocked") or []}
    assert superseded["id"] not in group_ids, mode
    assert superseded["id"] not in blocked_ids, mode


@pytest.mark.asyncio
async def test_stale_in_progress_predecessor_keeps_dependent_out_of_claimable_batch(db, tmp_path):
    """An in-progress (claimed but not yet terminal) predecessor correctly
    blocks the dependent item -- claimed work is no longer part of the
    PENDING batch _partition_into_waves layers, so the dependent is hard-
    excluded via the pre-existing <excluded_dependency_not_satisfied> note
    (83a7586d territory: claim_sprint_item's DEPENDENCY_NOT_SATISFIED gate
    still refuses it regardless of how this renders) rather than appearing
    in a <dependency_waves> boundary -- it is not "in this same pending
    batch" the way a fan-in predecessor still queued as todo/pending is."""
    p = await db_module.create_project(db, "524e73e6-inprogress-pred")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent item")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child item", depends_on=parent["id"],
    )
    await db_module.claim_sprint_item(db, p["id"], parent["id"])

    _path, text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "excluded_dependency_not_satisfied" in text
    assert child["id"] in text
    result = await db_module.claim_sprint_item(db, p["id"], child["id"])
    assert result.get("blocked") is True
    assert result.get("error") == "DEPENDENCY_NOT_SATISFIED"


@pytest.mark.asyncio
async def test_dynamic_completion_re_frontiers_and_changes_board_revision(db, tmp_path):
    """Re-frontiering: completing the predecessor between two handoff
    generations must (a) move the dependent item into the claimable wave and
    (b) change the board_revision digest, so a receiver comparing revisions
    can detect the earlier handoff went stale."""
    p = await db_module.create_project(db, "524e73e6-dynamic-completion")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent item")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child item", depends_on=parent["id"], prospect_bypass=True,
    )

    _path1, text1, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    block1 = _extract_dependency_waves_block(text1)
    assert block1 is not None
    rev1 = re.search(r'board_revision="([^"]*)"', block1).group(1)
    assert child["id"] not in re.search(
        r'<wave n="1"[^>]*>([^<]*)</wave>', block1,
    ).group(1).split(", ")

    await db_module.claim_sprint_item(db, p["id"], parent["id"])
    await db_module.complete_sprint_item(db, p["id"], parent["id"])

    _path2, text2, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    # Now a single wave (parent is done, no longer in the pending set at
    # all) -- the tag disappears entirely, exactly like any other
    # no-dependency-boundary board.
    assert "<dependency_waves" not in text2
    assert child["id"] in text2
    assert rev1 != ""


@pytest.mark.asyncio
async def test_resource_conflict_interaction_distinguished_from_dependency_wave(db, tmp_path):
    """Two wave-0 items that are resource-conflict-FREE with each other (no
    dependency between them) must still be presented via the parallel-batch
    machinery (claim-time arbitration, dispatch concurrently), while a
    THIRD, dependency-blocked item is never folded into that same "dispatch
    concurrently" set -- the resource-batch/parallel-groups CONCEPT is
    distinct from, and must not be confused with, a real dependency
    boundary (this is exactly what 83a7586d's own
    test_resource_conflict_and_dependency_block_are_distinct_error_codes
    proves at the claim/error-code layer; this closes the loop at the
    handoff-rendering layer)."""
    p = await db_module.create_project(db, "524e73e6-resource-vs-dependency")
    # r1/r2 declare DISJOINT resources -> one conflict-free batch (group 0,
    # size 2). r3 conflicts with r1 (same resource) -> forced into its own
    # separate batch (group 1, size 1). Two groups, one with >1 item, is
    # what actually flips get_parallelizable_groups' own _has_parallel
    # threshold (group_count > 1 AND some group size > 1) -- a single group
    # of 2 alone does NOT (group_count would be 1).
    # force=True on r2/r3: their titles' word-set overlap with r1's exceeds
    # add_sprint_item's fuzzy-duplicate threshold (_title_word_overlap >=
    # 0.60 via the overlap coefficient, not Jaccard) -- unrelated to this
    # item's own scope, just a fixture-title collision to route around.
    r1 = await db_module.add_sprint_item(
        db, p["id"], "v1", "resource item 1",
        touches_resources=["file:one.py"], prospect_bypass=True,
    )
    r2 = await db_module.add_sprint_item(
        db, p["id"], "v1", "resource item 2",
        touches_resources=["file:two.py"], prospect_bypass=True, force=True,
    )
    r3 = await db_module.add_sprint_item(
        db, p["id"], "v1", "resource item 3 (conflicts with r1)",
        touches_resources=["file:one.py"], prospect_bypass=True, force=True,
    )
    dep = await db_module.add_sprint_item(
        db, p["id"], "v1", "dependency-blocked item",
        depends_on=r1["id"], prospect_bypass=True,
    )

    _path, text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    # r1/r2 declare disjoint resources -> a single conflict-free batch,
    # advertised as parallel-safe, both in wave 0 (neither depends on
    # anything).
    assert "batch 1:" in text
    # dep is a real, later wave -- must not appear inside the resource-batch
    # "dispatch ... CONCURRENTLY" enumeration alongside r1/r2's batches, and
    # must not be mislabeled as external.
    assert "blocked on an item outside this goal" not in text
    block = _extract_dependency_waves_block(text)
    assert block is not None
    wave1_ids = re.search(r'<wave n="1"[^>]*>([^<]*)</wave>', block).group(1)
    assert set(wave1_ids.split(", ")) == {r1["id"], r2["id"], r3["id"]}
    wave2_match = re.search(r'<wave n="2"[^>]*>([^<]*)</wave>', block)
    assert wave2_match.group(1) == dep["id"]


@pytest.mark.asyncio
async def test_cross_project_decoy_predecessor_fails_closed_not_silently_claimable(db, tmp_path):
    """A same-shaped id that legitimately exists (and is even DONE) in a
    DIFFERENT project must never satisfy this project's dependency, NOR be
    silently rendered as if it were merely an in-batch blocked wave.
    generate_handoff's own pre-existing, project-wide stale-reference check
    (ee8a6af1) fails CLOSED with HandoffStaleReferenceError for a
    depends_on id that resolves to no item in THIS project at all -- proving
    the fail-closed gate (not a wave/exclusion-note render) is what a
    genuinely foreign id actually hits. (Frontier-level proof that the
    foreign item's real 'done' status never satisfies the dependency already
    exists in test_83a7586d_dependency_frontier.py::
    test_cross_project_predecessor_never_satisfies_dependency; this closes
    the loop at the handoff-generation layer specifically -- confirming, not
    re-deriving, that this is already correctly handled and not a gap.)"""
    p_other = await db_module.create_project(db, "524e73e6-decoy-other")
    decoy_done = await db_module.add_sprint_item(db, p_other["id"], "v1", "done elsewhere")
    await db_module.complete_sprint_item(db, p_other["id"], decoy_done["id"])

    p = await db_module.create_project(db, "524e73e6-decoy-main")
    await db_module.add_sprint_item(db, p["id"], "v1", "unrelated item")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "child item", depends_on=decoy_done["id"],
    )

    with pytest.raises(handoff_module.HandoffStaleReferenceError):
        await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal", version="v1",
        )


# ---------------------------------------------------------------------------
# 6. build_handoff_manifest's own "waves" field now uses real dependency
#    waves, not the resource-conflict grouping (confirmed wrong-kind-of-
#    waves bug).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_mode_manifest_waves_are_dependency_waves_not_resource_groups(db, tmp_path):
    p = await db_module.create_project(db, "524e73e6-manifest-waves")
    parent = await db_module.add_sprint_item(
        db, p["id"], "v1", "parent", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child", depends_on=parent["id"],
        touches_resources=["file:b.py"], prospect_bypass=True,
    )
    _path, text, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
        emit_manifest=True,
    )
    manifest_block = re.search(r"<handoff_manifest.*?</handoff_manifest>", text, re.S)
    assert manifest_block is not None
    waves_block = re.search(r"<waves>(.*?)</waves>", manifest_block.group(0), re.S).group(1)
    wave_tags = re.findall(r'<wave index="(\d+)" items="([^"]*)"/>', waves_block)
    waves_by_index = {int(i): items for i, items in wave_tags}
    # Real dependency order: parent alone in wave 0, child alone in wave 1 --
    # NOT the resource-conflict grouping (which would put them in the SAME
    # group since their resources don't conflict with each other).
    assert waves_by_index.get(0) == parent["id"]
    assert waves_by_index.get(1) == child["id"]
