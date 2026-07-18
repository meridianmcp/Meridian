"""Tests for sprint item f78d7644 — urgent items get an immediate wave-urgent
lane that runs alongside an in-progress megasprint, and single-executor
sessions are told to yield to it at the next checkpoint.

Covers:
  * assign_sprint_waves carves ready urgent items into a dedicated
    ``wave-urgent`` lane instead of the sequential ``wave-N`` numbering.
  * urgent items behind an unmet dependency are NOT carved out (priority
    never skips real dependency order) and flow through normal layering.
  * urgent items with a resource conflict against each other split into
    ``wave-urgent-2``, etc.
  * the presence of urgent items does not perturb wave-N numbering for
    normal-priority items.
  * get_sprint_progress surfaces a non-empty ``urgent_wave`` block when
    urgent items are queued in the wave-urgent lane.
  * _board_change_for_session escalates its message (``urgent: True``) when
    an item added after session start is urgent-priority.
"""
import itertools

import pytest

from meridian import db as db_module
from meridian import server as srv

_project_counter = itertools.count()


async def _project(db):
    name = f"urgent-wave-{next(_project_counter)}"
    proj = await srv._dispatch_mcp_tool("create_project", {"name": name}, db, "/tmp")
    return proj["id"]


@pytest.mark.asyncio
async def test_urgent_item_gets_dedicated_wave_urgent_label(db):
    pid = await _project(db)
    normal = await db_module.add_sprint_item(db, pid, "v1", "normal work")
    urgent = await db_module.add_sprint_item(
        db, pid, "v1", "prod is broken", priority="urgent", force=True
    )

    result = await db_module.assign_sprint_waves(db, pid)

    r_normal = await db_module.get_sprint_item(db, normal["id"])
    r_urgent = await db_module.get_sprint_item(db, urgent["id"])

    assert r_urgent["wave"] == "wave-urgent"
    assert r_normal["wave"] == "wave-1"
    assert result["urgent_wave_count"] == 1
    assert result["urgent_assigned"] == 1
    assert result["waves"]["wave-urgent"] == [urgent["id"]]
    assert result["waves"]["wave-1"] == [normal["id"]]


@pytest.mark.asyncio
async def test_urgent_item_blocked_on_dependency_is_not_carved_out(db):
    pid = await _project(db)
    parent = await db_module.add_sprint_item(db, pid, "v1", "root work")
    urgent_child = await db_module.add_sprint_item(
        db, pid, "v1", "urgent but depends on root",
        priority="urgent", depends_on=parent["id"], force=True,
    )

    result = await db_module.assign_sprint_waves(db, pid)

    r_parent = await db_module.get_sprint_item(db, parent["id"])
    r_child = await db_module.get_sprint_item(db, urgent_child["id"])

    # Not carved into wave-urgent — its dependency isn't done yet.
    assert r_child["wave"] != "wave-urgent"
    assert r_child["wave"] is not None and r_child["wave"].startswith("wave-")
    assert r_parent["wave"] == "wave-1"
    # Child still lands strictly after the parent.
    wa = int(r_parent["wave"].split("-")[1])
    wc = int(r_child["wave"].split("-")[1])
    assert wc > wa
    assert result["urgent_wave_count"] == 0


@pytest.mark.asyncio
async def test_urgent_item_carved_out_once_dependency_done(db):
    pid = await _project(db)
    parent = await db_module.add_sprint_item(db, pid, "v1", "root work")
    urgent_child = await db_module.add_sprint_item(
        db, pid, "v1", "urgent, parent now done",
        priority="urgent", depends_on=parent["id"], force=True,
    )
    await db_module.claim_sprint_item(db, pid, parent["id"], actor="tester")
    await db_module.complete_sprint_item(db, pid, parent["id"])

    result = await db_module.assign_sprint_waves(db, pid)

    r_child = await db_module.get_sprint_item(db, urgent_child["id"])
    assert r_child["wave"] == "wave-urgent"
    assert result["urgent_wave_count"] == 1


@pytest.mark.asyncio
async def test_urgent_items_with_resource_conflict_split_sub_waves(db):
    pid = await _project(db)
    u1 = await db_module.add_sprint_item(
        db, pid, "v1", "urgent edit a one",
        priority="urgent", touches_resources=["file:a.py"],
    )
    u2 = await db_module.add_sprint_item(
        db, pid, "v1", "urgent edit a two",
        priority="urgent", touches_resources=["file:a.py"], force=True,
    )

    result = await db_module.assign_sprint_waves(db, pid)

    r1 = await db_module.get_sprint_item(db, u1["id"])
    r2 = await db_module.get_sprint_item(db, u2["id"])
    assert r1["wave"] != r2["wave"]
    assert {r1["wave"], r2["wave"]} == {"wave-urgent", "wave-urgent-2"}
    assert result["urgent_wave_count"] == 2
    assert result["urgent_assigned"] == 2


@pytest.mark.asyncio
async def test_urgent_carve_out_does_not_perturb_normal_wave_numbering(db):
    """Normal items keep the same wave-N numbering whether or not an urgent
    item is present — the urgent lane is orthogonal, not interleaved."""
    pid = await _project(db)
    a = await db_module.add_sprint_item(db, pid, "v1", "edit a one", touches_resources=["file:a.py"])
    b = await db_module.add_sprint_item(db, pid, "v1", "edit a two", touches_resources=["file:a.py"], force=True)
    c = await db_module.add_sprint_item(db, pid, "v1", "edit c", touches_resources=["file:c.py"])

    baseline = await db_module.assign_sprint_waves(db, pid)
    ra = await db_module.get_sprint_item(db, a["id"])
    rb = await db_module.get_sprint_item(db, b["id"])
    rc = await db_module.get_sprint_item(db, c["id"])
    baseline_waves = {a["id"]: ra["wave"], b["id"]: rb["wave"], c["id"]: rc["wave"]}

    # Inject an urgent item and reassign.
    urgent = await db_module.add_sprint_item(
        db, pid, "v1", "urgent hotfix", priority="urgent", force=True
    )
    result = await db_module.assign_sprint_waves(db, pid)

    ra2 = await db_module.get_sprint_item(db, a["id"])
    rb2 = await db_module.get_sprint_item(db, b["id"])
    rc2 = await db_module.get_sprint_item(db, c["id"])
    r_urgent = await db_module.get_sprint_item(db, urgent["id"])

    assert {a["id"]: ra2["wave"], b["id"]: rb2["wave"], c["id"]: rc2["wave"]} == baseline_waves
    assert r_urgent["wave"] == "wave-urgent"
    assert result["urgent_wave_count"] == 1


@pytest.mark.asyncio
async def test_get_sprint_progress_surfaces_urgent_wave(db):
    pid = await _project(db)
    await db_module.add_sprint_item(db, pid, "v1", "normal work")
    urgent = await db_module.add_sprint_item(
        db, pid, "v1", "prod is broken", priority="urgent", force=True
    )
    await db_module.assign_sprint_waves(db, pid)

    res = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": pid}, db, "/tmp",
    )
    assert res["urgent_wave"]["count"] == 1
    assert res["urgent_wave"]["item_ids"] == [urgent["id"]]

    # No urgent items queued -> no urgent_wave key.
    pid2 = await _project(db)
    await db_module.add_sprint_item(db, pid2, "v1", "normal only")
    res2 = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": pid2}, db, "/tmp",
    )
    assert "urgent_wave" not in res2


@pytest.mark.asyncio
async def test_board_change_escalates_for_urgent_item(db):
    """_board_change_for_session flags urgent injected items distinctly from
    a routine new-item notice, telling the session to yield at the next
    checkpoint rather than merely 'pick up after the current item'."""
    pid = await _project(db)
    s = await db_module.register_session(db, pid, "exec")
    await db.execute(
        "UPDATE sessions SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (s["id"],)
    )
    await db.commit()

    # Routine (non-urgent) injected item first.
    await db_module.add_sprint_item(db, pid, "v1", "injected normal work")
    res = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": pid, "session_id": s["id"]}, db, "/tmp",
    )
    assert res["board_change"]["new_items_since_session_start"] >= 1
    assert "urgent" not in res["board_change"]

    # Now inject an urgent item too.
    await db_module.add_sprint_item(
        db, pid, "v1", "prod is on fire", priority="urgent", force=True
    )
    res2 = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": pid, "session_id": s["id"]}, db, "/tmp",
    )
    assert res2["board_change"]["urgent"] is True
    assert res2["board_change"]["urgent_items_since_session_start"] >= 1
    assert "yield" in res2["board_change"]["message"].lower()
