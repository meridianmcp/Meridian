"""Tests for enforced wave grouping on sprint items (58a45b92).

A stored, deterministic `wave` label replaces recompute-every-time parallel
grouping: assign_sprint_waves auto-fills it from the conflict-free groups, and
update_sprint_item(wave=...) hand-edits it. Runs on both backends via the `db`
fixture.
"""
from datetime import datetime, timedelta

import pytest

from meridian import db as db_module
from meridian import server as srv
from meridian.mcp_tools import (
    _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _TITLE_OVERRIDES, _TOOL_EXAMPLES,
)


def _future_iso(hours: int = 48) -> str:
    """An ISO-8601 timestamp `hours` in the future (UTC, no tz suffix)."""
    return (datetime.utcnow() + timedelta(hours=hours)).isoformat()


async def _project(db):
    proj = await srv._dispatch_mcp_tool("create_project", {"name": "waves"}, db, "/tmp")
    return proj["id"]


@pytest.mark.asyncio
async def test_wave_column_exists_and_defaults_null(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "unassigned item")
    assert "wave" in item
    assert item["wave"] is None


@pytest.mark.asyncio
async def test_add_and_update_wave_roundtrip(db):
    pid = await _project(db)
    created = await srv._dispatch_mcp_tool(
        "add_sprint_item",
        {"project_id": pid, "version": "v1", "title": "pin me", "wave": "wave-3"},
        db, "/tmp",
    )
    assert created["wave"] == "wave-3"

    # Hand-edit via update_sprint_item.
    updated = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": created["id"], "wave": "wave-9"},
        db, "/tmp",
    )
    assert updated["wave"] == "wave-9"

    # Empty string clears it (unassigned).
    cleared = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": created["id"], "wave": ""},
        db, "/tmp",
    )
    assert cleared["wave"] is None

    # Omitting wave leaves it untouched.
    await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": created["id"], "wave": "wave-2"},
        db, "/tmp",
    )
    touched = await srv._dispatch_mcp_tool(
        "update_sprint_item",
        {"project_id": pid, "item_id": created["id"], "notes": "no wave here"},
        db, "/tmp",
    )
    assert touched["wave"] == "wave-2"


@pytest.mark.asyncio
async def test_assign_sprint_waves_maps_conflict_free_groups(db):
    pid = await _project(db)
    # A and B share file:a.py (conflict -> different waves). C is disjoint (co-batches
    # with A in the first wave).
    a = await db_module.add_sprint_item(db, pid, "v1", "edit a one", touches_resources=["file:a.py"])
    b = await db_module.add_sprint_item(db, pid, "v1", "edit a two", touches_resources=["file:a.py"], force=True)
    c = await db_module.add_sprint_item(db, pid, "v1", "edit c", touches_resources=["file:c.py"])

    result = await srv._dispatch_mcp_tool(
        "assign_sprint_waves", {"project_id": pid}, db, "/tmp",
    )
    assert result["assigned"] == 3
    assert result["wave_count"] == 2

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])
    rc = await db_module.get_sprint_item(db, c["id"])
    # All labelled.
    assert ra["wave"] and rb["wave"] and rc["wave"]
    # The two conflicting items (share file:a.py) land in DIFFERENT waves; which of
    # A/B is wave-1 vs wave-2 depends on the deterministic-but-uuid-tied sort order.
    assert ra["wave"] != rb["wave"]
    assert {ra["wave"], rb["wave"]} == {"wave-1", "wave-2"}
    # C is disjoint from both, so first-fit always co-batches it into wave-1.
    assert rc["wave"] == "wave-1"
    # The returned mapping partitions all three ids across exactly two waves.
    assert c["id"] in result["waves"]["wave-1"]
    assert len(result["waves"]["wave-2"]) == 1
    assert set(result["waves"]["wave-1"]) | set(result["waves"]["wave-2"]) == {
        a["id"], b["id"], c["id"]
    }


@pytest.mark.asyncio
async def test_assign_sprint_waves_skips_deferred_items(db):
    """5a67c8e0 — a future-deferred pending item must NOT receive a wave label.

    ``deferred_until`` in the future leaves ``status='pending'`` untouched (see
    ``claim_sprint_item`` / ``_is_deferred``), so the candidate filter in
    ``assign_sprint_waves`` must explicitly exclude it the same way it already
    excludes manual-blocker items — otherwise a backburnered item silently gets
    a real wave label on every run.
    """
    pid = await _project(db)
    deferred = await db_module.add_sprint_item(
        db, pid, "v1", "backburnered item", deferred_until=_future_iso(72),
    )
    normal = await db_module.add_sprint_item(db, pid, "v1", "normal item")

    result = await db_module.assign_sprint_waves(db, pid)

    r_deferred = await db_module.get_sprint_item(db, deferred["id"])
    r_normal = await db_module.get_sprint_item(db, normal["id"])

    # The deferred item must stay unlabelled (and unassigned).
    assert r_deferred["wave"] is None
    assert deferred["id"] not in {
        item_id for ids in result["waves"].values() for item_id in ids
    }
    # The normal item in the same call DOES get labelled.
    assert r_normal["wave"] is not None
    assert result["assigned"] == 1


@pytest.mark.asyncio
async def test_assign_sprint_waves_idempotent(db):
    pid = await _project(db)
    await db_module.add_sprint_item(db, pid, "v1", "solo", touches_resources=["file:x.py"])
    first = await db_module.assign_sprint_waves(db, pid)
    second = await db_module.assign_sprint_waves(db, pid)
    assert first["waves"] == second["waves"]


def test_assign_sprint_waves_registered_as_write_tool():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "assign_sprint_waves" in by_name
    tool = by_name["assign_sprint_waves"]
    assert tool["inputSchema"]["required"] == []
    assert "project_name" in tool["inputSchema"]["properties"]
    # It mutates -> must NOT be advertised read-only.
    assert "assign_sprint_waves" not in _READ_ONLY_TOOLS
    assert _TITLE_OVERRIDES["assign_sprint_waves"] == "Assign Sprint Waves"
    assert "assign_sprint_waves" in _TOOL_EXAMPLES


# ---------------------------------------------------------------------------
# 90955d26 — assign_sprint_waves projects depends_on-blocked items into future
# waves rather than dropping them with wave=NULL. Covers:
#   * a simple A→B chain (B lands in wave-2, not NULL)
#   * a three-level chain A→B→C
#   * resource conflicts inside the first (unblocked) wave still split into
#     wave-1 / wave-2, and the blocked dep then lands in wave-3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_sprint_waves_projects_dep_into_future_wave(db):
    """A depends_on-blocked item receives a future-wave label, not NULL."""
    pid = await _project(db)
    # A is a root (no dep), B depends on A.
    a = await db_module.add_sprint_item(db, pid, "v1", "root item A")
    b = await db_module.add_sprint_item(
        db, pid, "v1", "dep item B", depends_on=a["id"], force=True
    )
    result = await db_module.assign_sprint_waves(db, pid)

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])

    # Both must be assigned a wave (neither NULL).
    assert ra["wave"] is not None, "root item A should have a wave label"
    assert rb["wave"] is not None, "dep item B should have a wave label, not NULL"
    # B's wave must be numerically later than A's wave.
    a_num = int(ra["wave"].split("-")[1])
    b_num = int(rb["wave"].split("-")[1])
    assert b_num > a_num, (
        f"Dep item B (wave-{b_num}) must be in a later wave than root A (wave-{a_num})"
    )
    # assigned count includes the blocked item.
    assert result["assigned"] == 2
    # wave_count reflects at least 2 distinct waves.
    assert result["wave_count"] >= 2


@pytest.mark.asyncio
async def test_assign_sprint_waves_three_level_chain(db):
    """A→B→C three-level chain: each level lands in a strictly later wave."""
    pid = await _project(db)
    a = await db_module.add_sprint_item(db, pid, "v1", "level 0 root")
    b = await db_module.add_sprint_item(
        db, pid, "v1", "level 1 dep", depends_on=a["id"], force=True
    )
    c = await db_module.add_sprint_item(
        db, pid, "v1", "level 2 dep", depends_on=b["id"], force=True
    )
    await db_module.assign_sprint_waves(db, pid)

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])
    rc = await db_module.get_sprint_item(db, c["id"])

    assert ra["wave"] and rb["wave"] and rc["wave"], "all three must be labelled"
    wa = int(ra["wave"].split("-")[1])
    wb = int(rb["wave"].split("-")[1])
    wc = int(rc["wave"].split("-")[1])
    assert wa < wb < wc, (
        f"Expected wave order A<B<C but got wave-{wa}/wave-{wb}/wave-{wc}"
    )


@pytest.mark.asyncio
async def test_assign_sprint_waves_conflict_plus_dep(db):
    """Resource conflict in layer 0 splits into wave-1/wave-2; blocked dep gets wave-3."""
    pid = await _project(db)
    # Two items share file:x.py — they'll conflict -> separate sub-waves within layer 0.
    a = await db_module.add_sprint_item(
        db, pid, "v1", "edit x first", touches_resources=["file:x.py"]
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "edit x second", touches_resources=["file:x.py"], force=True
    )
    # C depends on A and will be projected into a future layer.
    c = await db_module.add_sprint_item(
        db, pid, "v1", "depends on a", depends_on=a["id"], force=True
    )
    result = await db_module.assign_sprint_waves(db, pid)

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])
    rc = await db_module.get_sprint_item(db, c["id"])

    # A and B must be in different waves (resource conflict).
    assert ra["wave"] != rb["wave"], "conflicting items must be in different waves"
    # C's wave must be later than A's wave (dependency).
    wa = int(ra["wave"].split("-")[1])
    wc = int(rc["wave"].split("-")[1])
    assert wc > wa, (
        f"Dep item C (wave-{wc}) must be later than A (wave-{wa})"
    )
    # All three are assigned.
    assert result["assigned"] == 3
    # At least 2 waves (layer-0 conflict + layer-1 dep).
    assert result["wave_count"] >= 2


@pytest.mark.asyncio
async def test_topo_depth_map_basic():
    """_topo_depth_map correctly computes depth for a simple chain."""
    items = [
        {"id": "a", "depends_on": None},
        {"id": "b", "depends_on": "a"},
        {"id": "c", "depends_on": "b"},
    ]
    dm = db_module._topo_depth_map(items)
    assert dm["a"] == 0
    assert dm["b"] == 1
    assert dm["c"] == 2


@pytest.mark.asyncio
async def test_topo_depth_map_cycle_safe():
    """_topo_depth_map handles a dependency cycle without infinite recursion."""
    items = [
        {"id": "a", "depends_on": "b"},
        {"id": "b", "depends_on": "a"},
    ]
    dm = db_module._topo_depth_map(items)
    # Both items should have a depth (0 is fine — cycle treated as root).
    assert "a" in dm and "b" in dm


@pytest.mark.asyncio
async def test_topo_depth_map_external_dep_is_root():
    """_topo_depth_map treats an external (out-of-set) dep as depth 0 (root)."""
    items = [
        {"id": "a", "depends_on": "external-id-not-in-set"},
        {"id": "b", "depends_on": "a"},
    ]
    dm = db_module._topo_depth_map(items)
    assert dm["a"] == 0  # external dep → root
    assert dm["b"] == 1  # in-set dep on a → wave 1


# ---------------------------------------------------------------------------
# 2a176d6d — regression tied directly to the 2026-08-04 V026-batch6 audit's
# literal example: assign_sprint_waves must split two items that each
# declare "file:<path>:<symbol>" (a single extra colon, NOT the "::"
# symbol: convention) on the SAME real file into DIFFERENT waves, not
# co-batch them as if the trailing ":<symbol>" suffix made them disjoint
# files. Exercises the same wave-coloring path assign_sprint_waves shares
# with get_parallelizable_groups (both color via _resource_sets_conflict).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_sprint_waves_splits_colon_symbol_suffixed_same_file(db):
    pid = await _project(db)
    a = await db_module.add_sprint_item(
        db, pid, "v1", "touch funcA on sprint_items.py",
        touches_resources=["file:sprint_items.py:funcA"],
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "touch funcB on sprint_items.py",
        touches_resources=["file:sprint_items.py:funcB"], force=True,
    )
    result = await db_module.assign_sprint_waves(db, pid)
    assert result["assigned"] == 2

    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])
    # Both real declarations touch the SAME file, so they must NOT land in
    # the same wave despite the different trailing ":<symbol>" suffix.
    assert ra["wave"] != rb["wave"]


# ---------------------------------------------------------------------------
# 0d0cada7 — lease-local scheduler contract: dynamic recomputation (no wave
# is a persisted, immutable plan — get_parallelizable_groups is authoritative
# and live on every call), lease/TTL expiry unblocking a resource-contended
# item, and cross-project isolation. Complements the plan_generation/
# resource_blocked/claim_granularity coverage in tests/test_resource_locks.py.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_refresh_adds_newly_unblocked_item_without_restart(db):
    """One worker completing item A must make dependency-blocked item B
    immediately visible on the VERY NEXT get_parallelizable_groups call —
    no assign_sprint_waves rerun, no executor restart, just a live recompute
    from the authoritative board."""
    pid = await _project(db)
    sess = await db_module.register_session(db, pid, "w1")
    a = await db_module.add_sprint_item(db, pid, "v1", "root item")
    b = await db_module.add_sprint_item(
        db, pid, "v1", "depends on root", depends_on=a["id"], force=True,
    )

    before = await db_module.get_parallelizable_groups(db, pid, version="v1")
    before_eligible_ids = {it["id"] for grp in before["groups"] for it in grp}
    assert a["id"] in before_eligible_ids
    assert b["id"] not in before_eligible_ids
    assert any(x["id"] == b["id"] for x in before["blocked"])

    await db_module.claim_sprint_item(db, pid, a["id"], actor=sess["id"])
    await db_module.complete_sprint_item(db, pid, a["id"], actor=sess["id"])

    after = await db_module.get_parallelizable_groups(db, pid, version="v1")
    after_eligible_ids = {it["id"] for grp in after["groups"] for it in grp}
    assert b["id"] in after_eligible_ids
    assert not any(x["id"] == b["id"] for x in after["blocked"])
    assert before["plan_generation"] != after["plan_generation"]


@pytest.mark.asyncio
async def test_resource_lease_expiry_unblocks_resource_contended_item(db):
    """A resource_blocked item becomes genuinely claimable again once the
    blocking lock's TTL lapses — the scheduler contract's 'release ... at
    ... heartbeat expiry' path, observed end-to-end through
    get_parallelizable_groups rather than the raw lock table."""
    pid = await _project(db)
    holder = await db_module.register_session(db, pid, "holder")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "touches a file", touches_resources=["file:leased.py"],
    )
    pre = await db_module.claim_file(db, "leased.py", holder["id"])
    assert pre["claimed"] is True

    mid = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert mid["resource_blocked_count"] == 1
    assert mid["resource_blocked"][0]["id"] == item["id"]

    # Force the lock's TTL to have already lapsed (mirrors
    # test_expire_resource_locks_by_ttl's pattern in test_resource_locks.py).
    await db.execute(
        "UPDATE file_locks SET expires_at = datetime('now', '-1 hour') "
        "WHERE file_path = 'leased.py'"
    )
    await db.commit()

    after = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert after["resource_blocked"] == []
    assert after["resource_blocked_count"] == 0
    assert mid["plan_generation"] != after["plan_generation"]


@pytest.mark.asyncio
async def test_cross_project_isolation_item_not_found_before_plan_generation_check(db):
    """A cross-project item id must be refused as ITEM_NOT_FOUND — never
    reaching (and never satisfied or contradicted by) the plan_generation
    staleness check, and never touching the OTHER project's item at all.
    Dynamic replanning must never cross a project boundary."""
    pid_a = (await db_module.create_project(db, "0d0cada7-cross-a"))["id"]
    pid_b = (await db_module.create_project(db, "0d0cada7-cross-b"))["id"]
    sess_b = await db_module.register_session(db, pid_b, "w1")
    item_a = await db_module.add_sprint_item(
        db, pid_a, "v1", "lives in project A", touches_resources=["file:a.py"],
    )

    result = await db_module.claim_parallel_batch(
        db, pid_b, sess_b["id"], [item_a["id"]], plan_generation="whatever-stale-value",
    )
    assert result["ok"] is False
    assert result["error"] == "ITEM_NOT_FOUND"
    assert "expected_plan_generation" not in result

    # Project A's item is completely untouched.
    reread = await db_module.get_sprint_item(db, item_a["id"])
    assert reread["status"] == "pending"
    assert reread["project_id"] == pid_a


@pytest.mark.asyncio
async def test_cross_project_groups_never_mix_items(db):
    """Two projects each declaring the identical resource path never appear
    in each other's get_parallelizable_groups output — grouping/plan
    computation is strictly project-scoped."""
    pid_a = (await db_module.create_project(db, "0d0cada7-nomix-a"))["id"]
    pid_b = (await db_module.create_project(db, "0d0cada7-nomix-b"))["id"]
    item_a = await db_module.add_sprint_item(
        db, pid_a, "v1", "same path a", touches_resources=["file:shared_name.py"],
    )
    item_b = await db_module.add_sprint_item(
        db, pid_b, "v1", "same path b", touches_resources=["file:shared_name.py"],
    )
    res_a = await db_module.get_parallelizable_groups(db, pid_a, version="v1")
    res_b = await db_module.get_parallelizable_groups(db, pid_b, version="v1")
    ids_a = {it["id"] for grp in res_a["groups"] for it in grp}
    ids_b = {it["id"] for grp in res_b["groups"] for it in grp}
    assert ids_a == {item_a["id"]}
    assert ids_b == {item_b["id"]}
    assert item_b["id"] not in ids_a
    assert item_a["id"] not in ids_b
