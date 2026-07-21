"""Tests for sprint item 9d8e858c — default-collapse item_group/parent_id
clusters in get_sprint_items / get_planning_brief / search_all.

Proposal 6ab0aed0: instead of a caller being flooded by every fanned-out
subtask or grouped item individually, items sharing a ``parent_id`` (set by
``add_subtask``/``split_sprint_item``) or ``item_group`` (set by
``add_sprint_item``'s ``group`` arg) collapse into ONE summary row per
cluster by default. ``expand=true`` restores the full ungrouped list.

Coverage:
- The shared helper ``collapse_sprint_item_clusters`` (unit, no DB): a
  fixture of 7 plain item dicts (3 sharing an item_group, 2 sharing a
  parent_id, 2 standalone) collapses to 4 rows with expand=False and passes
  through all 7 with expand=True.
- get_sprint_items (handle_get_sprint_items / _handle_sprint_tools
  dispatch): real DB rows, same 4-vs-7 shape.
- get_planning_brief (_dispatch_mcp_tool): pending_items collapses the same
  way.
- search_all (db_module.search_all): sprint_items collapses the same way.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian import db as db_module
from meridian.mcp import handler as mh
from meridian.mcp.handlers import sprint_tools as st_mod

_DATA_DIR = "/tmp/meridian-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "collapse-test-proj")


def _plain_item_fixture() -> list[dict]:
    """7 synthetic sprint-item dicts: 3 share item_group 'grp-a', 2 share
    parent_id 'parent-1', 2 are standalone (no parent_id, no item_group)."""
    return [
        {"id": "a1", "title": "Group item 1", "status": "todo", "item_group": "grp-a"},
        {"id": "a2", "title": "Group item 2", "status": "done", "item_group": "grp-a"},
        {"id": "a3", "title": "Group item 3", "status": "todo", "item_group": "grp-a"},
        {"id": "c1", "title": "Child 1", "status": "done", "parent_id": "parent-1"},
        {"id": "c2", "title": "Child 2", "status": "todo", "parent_id": "parent-1"},
        {"id": "s1", "title": "Standalone 1", "status": "todo"},
        {"id": "s2", "title": "Standalone 2", "status": "in_progress"},
    ]


# ---------------------------------------------------------------------------
# Unit tests: collapse_sprint_item_clusters (no DB)
# ---------------------------------------------------------------------------

def test_collapse_default_collapses_clusters_to_four_rows():
    items = _plain_item_fixture()
    result = db_module.collapse_sprint_item_clusters(items)  # expand defaults False
    assert len(result) == 4

    collapsed = [r for r in result if r.get("collapsed")]
    standalone = [r for r in result if not r.get("collapsed")]
    assert len(collapsed) == 2
    assert len(standalone) == 2
    assert {r["id"] for r in standalone} == {"s1", "s2"}

    by_kind = {r["cluster_kind"]: r for r in collapsed}
    assert set(by_kind) == {"item_group", "parent_id"}

    grp_row = by_kind["item_group"]
    assert grp_row["item_group_or_parent"] == "grp-a"
    assert grp_row["count"] == 3
    assert grp_row["done"] == 1  # only a2 is done
    assert grp_row["description"]  # non-empty representative title
    assert set(grp_row["ids"]) == {"a1", "a2", "a3"}

    parent_row = by_kind["parent_id"]
    assert parent_row["item_group_or_parent"] == "parent-1"
    assert parent_row["count"] == 2
    assert parent_row["done"] == 1  # only c1 is done
    assert set(parent_row["ids"]) == {"c1", "c2"}


def test_collapse_expand_true_returns_all_seven_unchanged():
    items = _plain_item_fixture()
    result = db_module.collapse_sprint_item_clusters(items, expand=True)
    assert len(result) == 7
    assert result == items  # identical, unmodified — the pre-9d8e858c shape
    assert all(not it.get("collapsed") for it in result)


def test_collapse_parent_id_wins_over_item_group_when_both_set():
    """When an item has both parent_id and item_group, grouping keys off
    parent_id (per the item's spec: 'parent_id if set, else item_group')."""
    items = [
        {"id": "x1", "title": "X1", "status": "todo",
         "parent_id": "p9", "item_group": "grp-z"},
        {"id": "x2", "title": "X2", "status": "todo",
         "parent_id": "p9", "item_group": "grp-other"},
    ]
    result = db_module.collapse_sprint_item_clusters(items)
    assert len(result) == 1
    assert result[0]["collapsed"] is True
    assert result[0]["cluster_kind"] == "parent_id"
    assert result[0]["item_group_or_parent"] == "p9"


def test_collapse_singleton_cluster_passes_through_unchanged():
    """A parent_id/item_group value shared by only ONE item is not collapsed —
    the item is returned exactly as given."""
    items = [
        {"id": "only1", "title": "Only", "status": "todo", "item_group": "lonely-group"},
        {"id": "only2", "title": "Only Parent", "status": "todo", "parent_id": "lonely-parent"},
    ]
    result = db_module.collapse_sprint_item_clusters(items)
    assert len(result) == 2
    assert result == items
    assert all(not it.get("collapsed") for it in result)


def test_collapse_empty_list():
    assert db_module.collapse_sprint_item_clusters([]) == []
    assert db_module.collapse_sprint_item_clusters([], expand=True) == []


# ---------------------------------------------------------------------------
# Integration: get_sprint_items (real DB rows)
# ---------------------------------------------------------------------------

async def _build_real_fixture(db, pid):
    """Builds the same 3/2/2 shape as _plain_item_fixture() but as real rows
    in the DB. The parent used for the parent_id cluster doubles as one of
    the 2 standalone items (it has no parent_id/item_group of its own —
    add_subtask requires a real, non-terminal parent item to exist)."""
    a1 = await db_module.add_sprint_item(db, pid, "v1", "Refactor auth module", group="grp-a")
    a2 = await db_module.add_sprint_item(db, pid, "v1", "Rewrite billing engine", group="grp-a", force=True)
    a3 = await db_module.add_sprint_item(db, pid, "v1", "Migrate cache layer", group="grp-a", force=True)
    await db_module.claim_sprint_item(db, pid, a2["id"])
    await db_module.complete_sprint_item(db, pid, a2["id"])  # 1/3 done

    parent = await db_module.add_sprint_item(db, pid, "v1", "Overhaul deploy pipeline")  # standalone #1
    c1 = await db_module.add_subtask(db, pid, parent["id"], "Child 1")
    c2 = await db_module.add_subtask(db, pid, parent["id"], "Child 2")
    await db_module.claim_sprint_item(db, pid, c1["id"])
    await db_module.complete_sprint_item(db, pid, c1["id"])  # 1/2 done

    s2 = await db_module.add_sprint_item(db, pid, "v1", "Prune stale telemetry dashboards", force=True)  # standalone #2
    return {"a1": a1, "a2": a2, "a3": a3, "parent": parent, "c1": c1, "c2": c2, "s2": s2}


@pytest.mark.asyncio
async def test_get_sprint_items_default_collapses(db, project):
    pid = project["id"]
    ids = await _build_real_fixture(db, pid)

    result = await st_mod.handle_get_sprint_items(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    assert isinstance(result, list)
    assert len(result) == 4

    collapsed = [r for r in result if r.get("collapsed")]
    standalone_ids = {r["id"] for r in result if not r.get("collapsed")}
    assert len(collapsed) == 2
    assert standalone_ids == {ids["parent"]["id"], ids["s2"]["id"]}

    by_kind = {r["cluster_kind"]: r for r in collapsed}
    assert by_kind["item_group"]["count"] == 3
    assert by_kind["item_group"]["done"] == 1
    assert by_kind["parent_id"]["count"] == 2
    assert by_kind["parent_id"]["done"] == 1
    assert by_kind["parent_id"]["item_group_or_parent"] == ids["parent"]["id"]


@pytest.mark.asyncio
async def test_get_sprint_items_expand_true_returns_all_seven(db, project):
    pid = project["id"]
    ids = await _build_real_fixture(db, pid)

    result = await mh._handle_sprint_tools(
        "get_sprint_items", {"project_id": pid, "expand": True},
        db, _DATA_DIR, None, None
    )
    assert result is not mh._MISS
    assert isinstance(result, list)
    assert len(result) == 7
    returned_ids = {r["id"] for r in result}
    assert returned_ids == {v["id"] for v in ids.values()}
    assert all(not r.get("collapsed") for r in result)


@pytest.mark.asyncio
async def test_get_sprint_items_no_cluster_fields_unaffected(db, project):
    """Existing single-item / no-group-or-parent behavior (as exercised by the
    pre-existing sprint_tools dispatch tests) is unaffected by the default
    collapsing — a lone item passes straight through."""
    pid = project["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "Solo item")
    result = await st_mod.handle_get_sprint_items(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    assert any(it.get("id") == item["id"] for it in result)


# ---------------------------------------------------------------------------
# Integration: get_planning_brief
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_planning_brief_pending_items_default_collapses(db, project):
    pid = project["id"]
    await _build_real_fixture(db, pid)

    brief = await mh._dispatch_mcp_tool(
        "get_planning_brief", {"project_id": pid}, db, _DATA_DIR
    )
    # 6 of the 7 fixture items are still pending (a2/c1 were completed);
    # collapsing still yields at most one row per cluster + standalone rows.
    pending = brief["pending_items"]
    collapsed = [r for r in pending if r.get("collapsed")]
    assert collapsed, "expected at least one collapsed cluster row in pending_items"
    for row in collapsed:
        assert "count" in row and "done" in row and "description" in row


@pytest.mark.asyncio
async def test_get_planning_brief_expand_true_restores_full_list(db, project):
    pid = project["id"]
    await _build_real_fixture(db, pid)

    brief = await mh._dispatch_mcp_tool(
        "get_planning_brief", {"project_id": pid, "expand": True}, db, _DATA_DIR
    )
    pending = brief["pending_items"]
    assert all(not r.get("collapsed") for r in pending)
    # Every row keeps the pre-9d8e858c compact shape.
    for row in pending:
        assert set(row.keys()) == {"id", "title", "version"}


# ---------------------------------------------------------------------------
# Integration: search_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_all_sprint_items_default_collapses(db, project):
    pid = project["id"]
    await db_module.add_sprint_item(db, pid, "v1", "Widget rendering fix", group="widget-cluster")
    await db_module.add_sprint_item(db, pid, "v1", "Widget storage cleanup", group="widget-cluster", force=True)
    await db_module.add_sprint_item(db, pid, "v1", "Widget export tooling", group="widget-cluster", force=True)

    result = await db_module.search_all(db, pid, "widget")
    sprint_items = result["sprint_items"]
    assert len(sprint_items) == 1
    assert sprint_items[0]["collapsed"] is True
    assert sprint_items[0]["count"] == 3


@pytest.mark.asyncio
async def test_search_all_expand_true_returns_full_matches(db, project):
    pid = project["id"]
    await db_module.add_sprint_item(db, pid, "v1", "Gadget rendering fix", group="gadget-cluster")
    await db_module.add_sprint_item(db, pid, "v1", "Gadget storage cleanup", group="gadget-cluster", force=True)
    await db_module.add_sprint_item(db, pid, "v1", "Gadget export tooling", group="gadget-cluster", force=True)

    result = await db_module.search_all(db, pid, "gadget", expand=True)
    sprint_items = result["sprint_items"]
    assert len(sprint_items) == 3
    assert all(not it.get("collapsed") for it in sprint_items)
    # snippet/match_type still attached as before.
    assert all("match_type" in it for it in sprint_items)
