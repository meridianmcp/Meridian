"""Tests for meridian.dependency_graph (05553946).

Covers:
  - pure find_dependency_cycle / find_all_dependency_cycles / compute_dependency_graph_digest
  - patch_sprint_item now fails closed (DependencyCycleError, a ValueError
    subclass) on a self-dependency OR any longer depends_on cycle it would
    introduce, with the full cycle path attached
  - patch_sprint_item deliberately still ALLOWS a missing/foreign depends_on
    target (that stale-reference check stays deferred to handoff-render
    time, ee8a6af1 — this item must not regress it)
  - assign_sprint_waves surfaces "cycles" (full paths, informational) and a
    deterministic "graph_digest" for the eligible item set
"""

from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import dependency_graph as dep_graph


# ---------------------------------------------------------------------------
# Pure: find_dependency_cycle
# ---------------------------------------------------------------------------


def test_pure_no_cycle_returns_none():
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": None},
    ]
    assert dep_graph.find_dependency_cycle(items) is None


def test_pure_dangling_edge_is_not_a_cycle():
    items = [{"id": "a", "depends_on": "nonexistent"}]
    assert dep_graph.find_dependency_cycle(items) is None


def test_pure_self_loop_detected_via_proposed_edge():
    items = [{"id": "a", "depends_on": None}]
    cyc = dep_graph.find_dependency_cycle(items, proposed_edge=("a", "a"))
    assert cyc == ["a", "a"]


def test_pure_two_item_cycle_full_path():
    # a -> b already; proposing b -> a closes the loop.
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": None},
    ]
    cyc = dep_graph.find_dependency_cycle(items, proposed_edge=("b", "a"))
    assert cyc is not None
    assert cyc[0] == cyc[-1]
    assert set(cyc[:-1]) == {"a", "b"}


def test_pure_three_item_cycle_full_path():
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": "c"},
        {"id": "c", "depends_on": None},
    ]
    cyc = dep_graph.find_dependency_cycle(items, proposed_edge=("c", "a"))
    assert cyc is not None
    assert cyc[0] == cyc[-1]
    assert set(cyc[:-1]) == {"a", "b", "c"}


def test_pure_clearing_edge_never_creates_cycle():
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": None},
    ]
    # Simulate clearing a's depends_on — can never introduce a cycle.
    assert dep_graph.find_dependency_cycle(items, proposed_edge=("a", None)) is None


def test_pure_preexisting_cycle_detected_without_overlay():
    # A graph that already contains a cycle (e.g. legacy data written before
    # this validator existed) is still reported even with no proposed_edge.
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": "a"},
    ]
    cyc = dep_graph.find_dependency_cycle(items)
    assert cyc is not None
    assert set(cyc[:-1]) == {"a", "b"}


def test_pure_deterministic_across_iteration_order():
    items_forward = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": "a"},
    ]
    items_reversed = list(reversed(items_forward))
    assert dep_graph.find_dependency_cycle(items_forward) == dep_graph.find_dependency_cycle(
        items_reversed
    )


# ---------------------------------------------------------------------------
# Pure: find_all_dependency_cycles
# ---------------------------------------------------------------------------


def test_pure_find_all_cycles_reports_each_distinct_cycle():
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": "a"},
        {"id": "x", "depends_on": "y"},
        {"id": "y", "depends_on": "z"},
        {"id": "z", "depends_on": "x"},
        {"id": "solo", "depends_on": None},
    ]
    cycles = dep_graph.find_all_dependency_cycles(items)
    assert len(cycles) == 2
    node_sets = [frozenset(c[:-1]) for c in cycles]
    assert frozenset({"a", "b"}) in node_sets
    assert frozenset({"x", "y", "z"}) in node_sets


def test_pure_find_all_cycles_dedupes_same_cycle_from_any_member():
    # The SAME cycle is reachable by walking from either "a" or "b" — must
    # be reported once, not twice.
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": "a"},
    ]
    cycles = dep_graph.find_all_dependency_cycles(items)
    assert len(cycles) == 1


def test_pure_find_all_cycles_empty_for_acyclic_graph():
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": "c"},
        {"id": "c", "depends_on": None},
    ]
    assert dep_graph.find_all_dependency_cycles(items) == []


# ---------------------------------------------------------------------------
# Pure: compute_dependency_graph_digest
# ---------------------------------------------------------------------------


def test_pure_digest_deterministic_regardless_of_order():
    items_a = [{"id": "a", "depends_on": "b"}, {"id": "b", "depends_on": None}]
    items_b = [{"id": "b", "depends_on": None}, {"id": "a", "depends_on": "b"}]
    assert dep_graph.compute_dependency_graph_digest(
        items_a
    ) == dep_graph.compute_dependency_graph_digest(items_b)


def test_pure_digest_changes_when_edge_changes():
    base = [{"id": "a", "depends_on": "b"}, {"id": "b", "depends_on": None}]
    changed = [{"id": "a", "depends_on": None}, {"id": "b", "depends_on": None}]
    assert dep_graph.compute_dependency_graph_digest(
        base
    ) != dep_graph.compute_dependency_graph_digest(changed)


def test_pure_digest_changes_when_item_added_or_removed():
    base = [{"id": "a", "depends_on": None}]
    added = [{"id": "a", "depends_on": None}, {"id": "b", "depends_on": None}]
    assert dep_graph.compute_dependency_graph_digest(
        base
    ) != dep_graph.compute_dependency_graph_digest(added)


def test_pure_digest_ignores_non_tracked_fields():
    a1 = [{"id": "a", "depends_on": None, "status": "pending", "title": "one"}]
    a2 = [{"id": "a", "depends_on": None, "status": "done", "title": "two"}]
    assert dep_graph.compute_dependency_graph_digest(
        a1
    ) == dep_graph.compute_dependency_graph_digest(a2)


def test_pure_digest_has_sha256_prefix():
    digest = dep_graph.compute_dependency_graph_digest([{"id": "a", "depends_on": None}])
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


# ---------------------------------------------------------------------------
# DependencyCycleError
# ---------------------------------------------------------------------------


def test_dependency_cycle_error_carries_reason_and_path():
    err = dep_graph.DependencyCycleError(["a", "b", "a"])
    assert err.reason == "cycle"
    assert err.cycle_path == ["a", "b", "a"]
    assert "a -> b -> a" in str(err)
    assert isinstance(err, ValueError)


# ---------------------------------------------------------------------------
# Integration: patch_sprint_item fails closed on cycles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_self_dependency_raises_dependency_cycle_error(db):
    p = await db_module.create_project(db, "depgraph-self")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "solo task")
    with pytest.raises(dep_graph.DependencyCycleError) as excinfo:
        await db_module.patch_sprint_item(db, pid, item["id"], depends_on=item["id"])
    assert excinfo.value.cycle_path == [item["id"], item["id"]]
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["depends_on"] is None


@pytest.mark.asyncio
async def test_patch_two_item_cycle_rejected_with_full_path(db):
    p = await db_module.create_project(db, "depgraph-two-cycle")
    pid = p["id"]
    a = await db_module.add_sprint_item(db, pid, "v1", "item a")
    b = await db_module.add_sprint_item(db, pid, "v1", "item b", depends_on=a["id"])

    with pytest.raises(dep_graph.DependencyCycleError) as excinfo:
        await db_module.patch_sprint_item(db, pid, a["id"], depends_on=b["id"])

    cyc = excinfo.value.cycle_path
    assert cyc[0] == cyc[-1]
    assert set(cyc[:-1]) == {a["id"], b["id"]}
    # a is left untouched.
    stored_a = await db_module.get_sprint_item(db, a["id"])
    assert stored_a["depends_on"] is None
    stored_b = await db_module.get_sprint_item(db, b["id"])
    assert stored_b["depends_on"] == a["id"]


@pytest.mark.asyncio
async def test_patch_three_item_chain_rewire_cycle_rejected(db):
    p = await db_module.create_project(db, "depgraph-three-cycle")
    pid = p["id"]
    a = await db_module.add_sprint_item(db, pid, "v1", "item a")
    b = await db_module.add_sprint_item(db, pid, "v1", "item b", depends_on=a["id"])
    c = await db_module.add_sprint_item(db, pid, "v1", "item c", depends_on=b["id"])

    # a -> ... nothing yet. Rewiring a to depend on c would close a 3-cycle
    # (a -> c -> b -> a).
    with pytest.raises(dep_graph.DependencyCycleError) as excinfo:
        await db_module.patch_sprint_item(db, pid, a["id"], depends_on=c["id"])
    assert set(excinfo.value.cycle_path[:-1]) == {a["id"], b["id"], c["id"]}


@pytest.mark.asyncio
async def test_patch_legitimate_rewire_without_cycle_succeeds(db):
    p = await db_module.create_project(db, "depgraph-legit-rewire")
    pid = p["id"]
    a = await db_module.add_sprint_item(db, pid, "v1", "item a")
    b = await db_module.add_sprint_item(db, pid, "v1", "item b")
    c = await db_module.add_sprint_item(db, pid, "v1", "item c", depends_on=a["id"])

    # Retarget c from a to b — no cycle, must succeed.
    updated = await db_module.patch_sprint_item(db, pid, c["id"], depends_on=b["id"])
    assert updated["depends_on"] == b["id"]


@pytest.mark.asyncio
async def test_patch_missing_dependency_target_still_allowed(db):
    """Regression guard: ee8a6af1's deliberate design (missing depends_on
    targets are tolerated at write time, caught at handoff-render time) must
    not be broken by the new cycle guard — only real cycles are rejected."""
    p = await db_module.create_project(db, "depgraph-missing-ok")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "solo task")
    updated = await db_module.patch_sprint_item(
        db, pid, item["id"], depends_on="ghost-item-id-does-not-exist",
    )
    assert updated["depends_on"] == "ghost-item-id-does-not-exist"


@pytest.mark.asyncio
async def test_patch_cross_project_dependency_still_allowed(db):
    """Same regression guard, for a foreign-project depends_on target."""
    p1 = await db_module.create_project(db, "depgraph-cross-a")
    p2 = await db_module.create_project(db, "depgraph-cross-b")
    foreign = await db_module.add_sprint_item(db, p2["id"], "v1", "foreign item")
    item = await db_module.add_sprint_item(db, p1["id"], "v1", "local item")

    updated = await db_module.patch_sprint_item(
        db, p1["id"], item["id"], depends_on=foreign["id"],
    )
    assert updated["depends_on"] == foreign["id"]


@pytest.mark.asyncio
async def test_add_sprint_item_with_missing_depends_on_still_allowed(db):
    """add_sprint_item itself was never touched by this item (a brand-new id
    can never already participate in a cycle) — confirm it still behaves
    exactly as before."""
    p = await db_module.create_project(db, "depgraph-add-missing")
    pid = p["id"]
    child = await db_module.add_sprint_item(
        db, pid, "v1", "child of a ghost", depends_on="ghost-item-id-does-not-exist",
    )
    assert child["depends_on"] == "ghost-item-id-does-not-exist"


# ---------------------------------------------------------------------------
# Integration: assign_sprint_waves surfaces cycles + graph_digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_sprint_waves_reports_empty_cycles_and_a_digest(db):
    p = await db_module.create_project(db, "depgraph-waves-clean")
    pid = p["id"]
    await db_module.add_sprint_item(db, pid, "v1", "solo task")

    result = await db_module.assign_sprint_waves(db, pid)
    assert result["cycles"] == []
    assert isinstance(result["graph_digest"], str)
    assert result["graph_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_assign_sprint_waves_still_terminates_and_reports_legacy_cycle(db):
    """A cycle written before this item's guard existed (simulated here via a
    raw UPDATE, bypassing patch_sprint_item) must not hang or crash wave
    assignment — _topo_depth_map's lenient depth-0 fallback still applies —
    but it now shows up in the new ``cycles`` diagnostic instead of being
    silently invisible."""
    p = await db_module.create_project(db, "depgraph-waves-legacy-cycle")
    pid = p["id"]
    a = await db_module.add_sprint_item(db, pid, "v1", "item a")
    b = await db_module.add_sprint_item(db, pid, "v1", "item b", depends_on=a["id"])
    # Bypass the guard directly at the DB layer to simulate pre-existing
    # legacy data (patch_sprint_item itself would now refuse this).
    await db.execute(
        "UPDATE sprint_items SET depends_on = ? WHERE id = ?", (b["id"], a["id"]),
    )
    await db.commit()

    result = await db_module.assign_sprint_waves(db, pid)
    assert len(result["cycles"]) == 1
    assert set(result["cycles"][0][:-1]) == {a["id"], b["id"]}
    # Wave assignment still completed (didn't hang/crash) and labelled both items.
    assert result["assigned"] >= 2


@pytest.mark.asyncio
async def test_assign_sprint_waves_digest_changes_when_dependency_edited(db):
    p = await db_module.create_project(db, "depgraph-waves-digest")
    pid = p["id"]
    a = await db_module.add_sprint_item(db, pid, "v1", "item a")
    b = await db_module.add_sprint_item(db, pid, "v1", "item b")

    before = await db_module.assign_sprint_waves(db, pid)
    await db_module.patch_sprint_item(db, pid, b["id"], depends_on=a["id"])
    after = await db_module.assign_sprint_waves(db, pid)

    assert before["graph_digest"] != after["graph_digest"]
