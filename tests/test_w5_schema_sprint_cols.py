"""Tests for sprint_items.priority (e08fee30) + blocker_kind (2282a636).

Two new sprint_items columns shipped together:

* ``priority`` — app-layer enum {urgent, high, normal, low}, NOT NULL DEFAULT
  'normal'. Higher-priority PENDING items are surfaced/claimed/grouped FIRST:
  ``get_sprint_items`` and ``get_parallelizable_groups`` order urgent-first
  within their existing ordering. (This is PART 1 of e08fee30 — the ordering
  primitive; a true running-session interrupt/preemption is deliberately
  deferred and out of scope here.)
* ``blocker_kind`` — nullable; NULL = ordinary, 'manual' = blocked on a
  real-world action OUTSIDE Meridian. DISTINCT from milestone_type='human'
  (WHO executes): a manual-blocker is surfaced distinctly and EXCLUDED from
  executor "just claim the next pending" scoping, mirroring milestone_type='human'.

These tests exercise the schema columns, the enum validation on add/patch,
persistence + defaults, urgent-first ordering, the manual-blocker executor-scope
exclusion, and the MCP dispatch pass-through.
"""

from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import server as server_module

# _dispatch_mcp_tool is re-exported from server (importing it directly from
# meridian.mcp.handler at module top-level triggers a circular import).
_dispatch_mcp_tool = server_module._dispatch_mcp_tool


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_has_priority_and_blocker_kind_columns(db):
    """priority + blocker_kind exist on sprint_items after init_db."""
    async with db.execute("PRAGMA table_info(sprint_items)") as cur:
        rows = await cur.fetchall()
    cols = {(r["name"] if isinstance(r, dict) else r[1]) for r in rows}
    assert "priority" in cols
    assert "blocker_kind" in cols


@pytest.mark.asyncio
async def test_migration_is_idempotent(db):
    """Re-running the SQLite migration is a safe no-op (columns already there)."""
    from meridian.db import migrations as _mig

    # Migration already ran inside init_db; running again must not raise.
    await _mig._migrate_sprint_item_priority_blocker(db)
    async with db.execute("PRAGMA table_info(sprint_items)") as cur:
        rows = await cur.fetchall()
    cols = {(r["name"] if isinstance(r, dict) else r[1]) for r in rows}
    assert {"priority", "blocker_kind"} <= cols


# ---------------------------------------------------------------------------
# priority — persistence, default, validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_priority_defaults_to_normal(db):
    """An item added without a priority stores 'normal'."""
    p = await db_module.create_project(db, "prio-default")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "ordinary task")
    assert item["priority"] == "normal"


@pytest.mark.asyncio
async def test_priority_persists(db):
    """A supplied priority round-trips through the DB."""
    p = await db_module.create_project(db, "prio-persist")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "urgent task", priority="urgent"
    )
    assert item["priority"] == "urgent"
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["priority"] == "urgent"


@pytest.mark.asyncio
async def test_add_bad_priority_raises(db):
    """A bad priority enum value raises ValueError, like milestone_type."""
    p = await db_module.create_project(db, "prio-bad-add")
    pid = p["id"]
    with pytest.raises(ValueError):
        await db_module.add_sprint_item(
            db, pid, "v1", "bad prio", priority="critical"
        )


@pytest.mark.asyncio
async def test_patch_priority_persists_and_validates(db):
    """patch_sprint_item sets a valid priority and rejects a bad one."""
    p = await db_module.create_project(db, "prio-patch")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "patchable priority")
    assert item["priority"] == "normal"
    updated = await db_module.patch_sprint_item(
        db, pid, item["id"], priority="high"
    )
    assert updated["priority"] == "high"
    with pytest.raises(ValueError):
        await db_module.patch_sprint_item(db, pid, item["id"], priority="nope")
    # The bad patch left the stored value untouched.
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["priority"] == "high"


@pytest.mark.asyncio
async def test_patch_omitting_priority_leaves_it_unchanged(db):
    """Omitting priority from a patch must not clobber the stored value."""
    p = await db_module.create_project(db, "prio-omit")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "keep priority", priority="urgent"
    )
    # A patch of an unrelated field leaves priority alone.
    await db_module.patch_sprint_item(db, pid, item["id"], notes="some note")
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["priority"] == "urgent"


# ---------------------------------------------------------------------------
# priority — ordering (urgent-first)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sprint_items_orders_urgent_first(db):
    """get_sprint_items returns higher-priority pending items first."""
    p = await db_module.create_project(db, "prio-order")
    pid = p["id"]
    # Insert in a deliberately non-priority order so ordering (not insertion) wins.
    await db_module.add_sprint_item(db, pid, "v1", "low aardvark", priority="low")
    await db_module.add_sprint_item(db, pid, "v1", "normal beaver", priority="normal")
    await db_module.add_sprint_item(db, pid, "v1", "urgent cheetah", priority="urgent")
    await db_module.add_sprint_item(db, pid, "v1", "high dolphin", priority="high")

    items = await db_module.get_sprint_items(db, pid)
    priorities = [it["priority"] for it in items]
    assert priorities == ["urgent", "high", "normal", "low"]


@pytest.mark.asyncio
async def test_same_priority_keeps_oldest_first(db):
    """Within one priority, insertion order (oldest-first) is preserved."""
    p = await db_module.create_project(db, "prio-tie")
    pid = p["id"]
    a = await db_module.add_sprint_item(db, pid, "v1", "wire the auth flow", priority="high")
    b = await db_module.add_sprint_item(db, pid, "v1", "paint the dashboard sidebar", priority="high")
    items = await db_module.get_sprint_items(db, pid)
    ids = [it["id"] for it in items]
    assert ids.index(a["id"]) < ids.index(b["id"])


@pytest.mark.asyncio
async def test_parallelizable_groups_order_urgent_first(db):
    """get_parallelizable_groups colors higher-priority eligible items first.

    Each item declares a DISJOINT resource so they all land in the same
    (fully-parallel) group; the intra-group order then reflects priority.
    """
    p = await db_module.create_project(db, "prio-parallel")
    pid = p["id"]
    await db_module.add_sprint_item(
        db, pid, "v1", "low file a", priority="low",
        touches_resources=["file:a.py"],
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "urgent file b", priority="urgent",
        touches_resources=["file:b.py"],
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "high file c", priority="high",
        touches_resources=["file:c.py"],
    )
    res = await db_module.get_parallelizable_groups(db, pid)
    # All three are disjoint → one group holding all three, priority-ordered.
    assert res["groups"], "expected at least one parallel group"
    first_group = res["groups"][0]
    prios = [it["priority"] for it in first_group]
    assert prios == ["urgent", "high", "low"]


# ---------------------------------------------------------------------------
# blocker_kind — persistence, validation, executor-scope exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocker_kind_defaults_null(db):
    """An ordinary item has blocker_kind NULL."""
    p = await db_module.create_project(db, "blk-default")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "ordinary")
    assert item["blocker_kind"] is None


@pytest.mark.asyncio
async def test_blocker_kind_manual_persists(db):
    """blocker_kind='manual' round-trips through the DB."""
    p = await db_module.create_project(db, "blk-manual")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "publish the package", blocker_kind="manual"
    )
    assert item["blocker_kind"] == "manual"
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["blocker_kind"] == "manual"


@pytest.mark.asyncio
async def test_add_bad_blocker_kind_raises(db):
    """An undefined blocker_kind raises ValueError."""
    p = await db_module.create_project(db, "blk-bad")
    pid = p["id"]
    with pytest.raises(ValueError):
        await db_module.add_sprint_item(
            db, pid, "v1", "bad blocker", blocker_kind="external"
        )


@pytest.mark.asyncio
async def test_manual_blocker_is_distinct_from_milestone_human(db):
    """blocker_kind='manual' is orthogonal to milestone_type='human'.

    A manual-blocker item keeps its normal milestone_type ('task'); the two
    dimensions do not collide.
    """
    p = await db_module.create_project(db, "blk-distinct")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "get an API key", blocker_kind="manual"
    )
    assert item["blocker_kind"] == "manual"
    assert item["milestone_type"] == "task"  # not 'human'


@pytest.mark.asyncio
async def test_manual_blocker_excluded_from_executor_scoping(db):
    """A manual-blocker item is hidden from an executor-scoped listing.

    ``include_human=False`` is the executor-scoping path; a manual-blocker item
    must be excluded there (mirroring milestone_type='human'), yet still visible
    on the full board.
    """
    p = await db_module.create_project(db, "blk-scope")
    pid = p["id"]
    ordinary = await db_module.add_sprint_item(db, pid, "v1", "wire the endpoint")
    manual = await db_module.add_sprint_item(
        db, pid, "v1", "publish to registry", blocker_kind="manual"
    )
    # Full board (default) surfaces BOTH, so a human sees the manual blocker.
    full_ids = {it["id"] for it in await db_module.get_sprint_items(db, pid)}
    assert ordinary["id"] in full_ids
    assert manual["id"] in full_ids
    # Executor scope (include_human=False → include_manual_blocker follows) hides it.
    exec_items = await db_module.get_sprint_items(db, pid, include_human=False)
    exec_ids = {it["id"] for it in exec_items}
    assert ordinary["id"] in exec_ids
    assert manual["id"] not in exec_ids


@pytest.mark.asyncio
async def test_manual_blocker_excluded_from_parallelizable_groups(db):
    """A manual-blocker item never joins a parallel batch (not claimable work)."""
    p = await db_module.create_project(db, "blk-parallel")
    pid = p["id"]
    doable = await db_module.add_sprint_item(
        db, pid, "v1", "refactor module", touches_resources=["file:x.py"]
    )
    manual = await db_module.add_sprint_item(
        db, pid, "v1", "talk to the advisor", blocker_kind="manual",
        touches_resources=["file:y.py"],
    )
    res = await db_module.get_parallelizable_groups(db, pid)
    all_group_ids = {it["id"] for grp in res["groups"] for it in grp}
    assert doable["id"] in all_group_ids
    assert manual["id"] not in all_group_ids


@pytest.mark.asyncio
async def test_patch_sets_and_clears_blocker_kind(db):
    """patch_sprint_item sets blocker_kind='manual' and clears it with ''."""
    p = await db_module.create_project(db, "blk-patch")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "flip blocker")
    assert item["blocker_kind"] is None
    updated = await db_module.patch_sprint_item(
        db, pid, item["id"], blocker_kind="manual"
    )
    assert updated["blocker_kind"] == "manual"
    cleared = await db_module.patch_sprint_item(db, pid, item["id"], blocker_kind="")
    assert cleared["blocker_kind"] is None


@pytest.mark.asyncio
async def test_patch_bad_blocker_kind_raises(db):
    """A bad blocker_kind on patch raises ValueError."""
    p = await db_module.create_project(db, "blk-patch-bad")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "patch bad blocker")
    with pytest.raises(ValueError):
        await db_module.patch_sprint_item(db, pid, item["id"], blocker_kind="weird")


# ---------------------------------------------------------------------------
# MCP dispatch pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_add_sprint_item_forwards_priority_and_blocker(db, tmp_path):
    """add_sprint_item MCP handler forwards priority + blocker_kind to the DB."""
    p = await db_module.create_project(db, "mcp-add")
    pid = p["id"]
    res = await _dispatch_mcp_tool(
        "add_sprint_item",
        {
            "project_id": pid,
            "version": "v1",
            "title": "urgent manual step",
            "priority": "urgent",
            "blocker_kind": "manual",
            "force": True,
        },
        db,
        str(tmp_path),
    )
    assert res.get("priority") == "urgent"
    assert res.get("blocker_kind") == "manual"


@pytest.mark.asyncio
async def test_mcp_add_sprint_item_bad_priority_returns_error(db, tmp_path):
    """A bad priority via MCP surfaces as a structured error, not a crash."""
    p = await db_module.create_project(db, "mcp-add-bad")
    pid = p["id"]
    res = await _dispatch_mcp_tool(
        "add_sprint_item",
        {
            "project_id": pid,
            "version": "v1",
            "title": "bad prio via mcp",
            "priority": "supercritical",
            "force": True,
        },
        db,
        str(tmp_path),
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_mcp_update_sprint_item_forwards_priority_and_blocker(db, tmp_path):
    """update_sprint_item MCP handler forwards priority + blocker_kind."""
    p = await db_module.create_project(db, "mcp-update")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "mcp updatable")
    res = await _dispatch_mcp_tool(
        "update_sprint_item",
        {
            "project_id": pid,
            "item_id": item["id"],
            "priority": "high",
            "blocker_kind": "manual",
        },
        db,
        str(tmp_path),
    )
    assert res.get("priority") == "high"
    assert res.get("blocker_kind") == "manual"
