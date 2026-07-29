"""Tests for canonical expanded-board snapshots, revisions, and resume diffs (ef665ef8).

Covers meridian/db/board_snapshot.py:
  - build_board_snapshot: byte-stable repeated calls, non-done filtering,
    ordering, version scoping.
  - revision_hash: sensitive to status/depends_on/touches_resources/pointers,
    NOT sensitive to cosmetic fields (title/notes).
  - diff_board_snapshots: added/removed/changed-field reporting, cheap
    matching-hash short circuit.
  - record_board_snapshot_revision / get_latest_board_snapshot_revision:
    monotonic counter, idempotent on an unchanged hash.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import server as srv


async def _project(db, name: str = "board-snapshot") -> str:
    proj = await srv._dispatch_mcp_tool("create_project", {"name": name}, db, "/tmp")
    return proj["id"]


@pytest.mark.asyncio
async def test_snapshot_is_byte_stable_with_no_board_change(db):
    pid = await _project(db)
    a = await db_module.add_sprint_item(db, pid, "v1", "item A", touches_resources=["file:a.py"])
    await db_module.add_sprint_item_pointer(
        db, pid, a["id"], "code",
        [{"uri": "meridian/a.py", "selector": {"type": "symbol", "qualified_name": "foo"}}],
    )
    await db_module.add_sprint_item(db, pid, "v1", "item B", depends_on=a["id"])

    snap1 = await db_module.build_board_snapshot(db, pid)
    snap2 = await db_module.build_board_snapshot(db, pid)

    assert snap1 == snap2
    assert db_module.canonical_json(snap1) == db_module.canonical_json(snap2)
    assert snap1["revision_hash"] == snap2["revision_hash"]
    assert snap1["item_count"] == 2


@pytest.mark.asyncio
async def test_snapshot_excludes_done_items_only(db):
    pid = await _project(db)
    pending = await db_module.add_sprint_item(db, pid, "v1", "still pending item alpha")
    to_finish = await db_module.add_sprint_item(db, pid, "v1", "ship the completion flow")
    to_skip = await db_module.add_sprint_item(db, pid, "v1", "abandon the skip candidate")

    await db_module.complete_sprint_item(db, pid, to_finish["id"])
    await db_module.skip_sprint_item(db, pid, to_skip["id"])

    snap = await db_module.build_board_snapshot(db, pid)
    ids = {it["id"] for it in snap["items"]}

    # Literal "non-done" filter: only status == 'done' is excluded. A skipped
    # item is NOT done, so it stays visible — a resumed session needs to see
    # how a dependency chain actually resolved, not just claimable work.
    assert pending["id"] in ids
    assert to_skip["id"] in ids
    assert to_finish["id"] not in ids
    statuses = {it["id"]: it["status"] for it in snap["items"]}
    assert statuses[to_skip["id"]] == "skipped"


@pytest.mark.asyncio
async def test_snapshot_ordering_is_version_added_at_id(db):
    pid = await _project(db)
    b_v2 = await db_module.add_sprint_item(db, pid, "v2", "bravo release task")
    a_v1 = await db_module.add_sprint_item(db, pid, "v1", "alpha kickoff task")
    c_v1 = await db_module.add_sprint_item(db, pid, "v1", "charlie cleanup task")

    snap = await db_module.build_board_snapshot(db, pid)
    assert snap["ordering"] == "version,added_at,id"
    ids_in_order = [it["id"] for it in snap["items"]]

    # All v1 items must sort before the v2 item.
    v1_ids = {a_v1["id"], c_v1["id"]}
    v2_index = ids_in_order.index(b_v2["id"])
    v1_indices = [ids_in_order.index(i) for i in v1_ids]
    assert all(i < v2_index for i in v1_indices)


@pytest.mark.asyncio
async def test_snapshot_version_filter_scopes_items(db):
    pid = await _project(db)
    v1_item = await db_module.add_sprint_item(db, pid, "v1", "in v1")
    v2_item = await db_module.add_sprint_item(db, pid, "v2", "in v2")

    snap = await db_module.build_board_snapshot(db, pid, version="v1")
    ids = {it["id"] for it in snap["items"]}
    assert v1_item["id"] in ids
    assert v2_item["id"] not in ids
    assert snap["version_filter"] == "v1"


@pytest.mark.asyncio
async def test_revision_hash_changes_on_status_change(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "flip me")
    snap1 = await db_module.build_board_snapshot(db, pid)

    await db_module.skip_sprint_item(db, pid, item["id"])
    snap2 = await db_module.build_board_snapshot(db, pid)

    assert snap1["revision_hash"] != snap2["revision_hash"]


@pytest.mark.asyncio
async def test_revision_hash_changes_on_dependency_change(db):
    pid = await _project(db)
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent")
    child = await db_module.add_sprint_item(db, pid, "v1", "child")
    snap1 = await db_module.build_board_snapshot(db, pid)

    await db_module.patch_sprint_item(db, pid, child["id"], depends_on=parent["id"])
    snap2 = await db_module.build_board_snapshot(db, pid)

    assert snap1["revision_hash"] != snap2["revision_hash"]


@pytest.mark.asyncio
async def test_revision_hash_changes_on_resource_change(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "resourceless")
    snap1 = await db_module.build_board_snapshot(db, pid)

    await db_module.patch_sprint_item(db, pid, item["id"], touches_resources=["file:new_thing.py"])
    snap2 = await db_module.build_board_snapshot(db, pid)

    assert snap1["revision_hash"] != snap2["revision_hash"]


@pytest.mark.asyncio
async def test_revision_hash_changes_on_pointer_change(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "no evidence yet")
    snap1 = await db_module.build_board_snapshot(db, pid)

    await db_module.add_sprint_item_pointer(
        db, pid, item["id"], "code",
        [{"uri": "meridian/foo.py", "selector": {"type": "symbol", "qualified_name": "bar"}}],
    )
    snap2 = await db_module.build_board_snapshot(db, pid)

    assert snap1["revision_hash"] != snap2["revision_hash"]


@pytest.mark.asyncio
async def test_revision_hash_unaffected_by_cosmetic_title_change(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "original title")
    snap1 = await db_module.build_board_snapshot(db, pid)

    await db_module.patch_sprint_item(db, pid, item["id"], title="renamed title")
    snap2 = await db_module.build_board_snapshot(db, pid)

    # The title DID change in the expanded item view...
    titles = {it["id"]: it["title"] for it in snap2["items"]}
    assert titles[item["id"]] == "renamed title"
    # ...but the revision hash only tracks status/depends_on/touches_resources/
    # pointers, so a cosmetic edit must NOT flip it.
    assert snap1["revision_hash"] == snap2["revision_hash"]


@pytest.mark.asyncio
async def test_diff_reports_added_removed_and_changed(db):
    pid = await _project(db)
    to_complete = await db_module.add_sprint_item(db, pid, "v1", "A - will complete")
    to_change = await db_module.add_sprint_item(db, pid, "v1", "B - will change resources")
    snap1 = await db_module.build_board_snapshot(db, pid)

    new_item = await db_module.add_sprint_item(db, pid, "v1", "C - newly added")
    await db_module.complete_sprint_item(db, pid, to_complete["id"])
    await db_module.patch_sprint_item(db, pid, to_change["id"], touches_resources=["file:changed.py"])
    snap2 = await db_module.build_board_snapshot(db, pid)

    diff = db_module.diff_board_snapshots(snap1, snap2)

    assert diff["changed"] is True
    assert {it["id"] for it in diff["added"]} == {new_item["id"]}
    assert {it["id"] for it in diff["removed"]} == {to_complete["id"]}
    changed_ids = {c["id"] for c in diff["changed_items"]}
    assert changed_ids == {to_change["id"]}
    change_entry = next(c for c in diff["changed_items"] if c["id"] == to_change["id"])
    assert "touches_resources" in change_entry["changes"]
    assert change_entry["changes"]["touches_resources"]["old"] == []
    assert change_entry["changes"]["touches_resources"]["new"] == ["file:changed.py"]
    assert diff["previous_revision_hash"] == snap1["revision_hash"]
    assert diff["current_revision_hash"] == snap2["revision_hash"]


@pytest.mark.asyncio
async def test_diff_no_change_short_circuits_on_matching_hash(db):
    pid = await _project(db)
    await db_module.add_sprint_item(db, pid, "v1", "steady item")
    snap1 = await db_module.build_board_snapshot(db, pid)
    snap2 = await db_module.build_board_snapshot(db, pid)

    diff = db_module.diff_board_snapshots(snap1, snap2)

    assert diff["changed"] is False
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["changed_items"] == []
    assert diff["unchanged_count"] == snap2["item_count"]


@pytest.mark.asyncio
async def test_diff_does_not_report_untracked_field_changes(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "title will change")
    snap1 = await db_module.build_board_snapshot(db, pid)

    await db_module.patch_sprint_item(db, pid, item["id"], title="a whole new title")
    snap2 = await db_module.build_board_snapshot(db, pid)

    diff = db_module.diff_board_snapshots(snap1, snap2)
    # Hashes match (title is untracked) so this is a no-op diff even though
    # the expanded item view itself did change.
    assert diff["changed"] is False
    assert diff["changed_items"] == []


@pytest.mark.asyncio
async def test_record_revision_is_monotonic_and_idempotent(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "counter test")

    snap1 = await db_module.build_board_snapshot(db, pid)
    rec1 = await db_module.record_board_snapshot_revision(db, pid, snap1)
    assert rec1["revision_counter"] == 1
    assert rec1["is_new"] is True

    # Recording the SAME snapshot again is a no-op — counter doesn't move.
    rec1_again = await db_module.record_board_snapshot_revision(db, pid, snap1)
    assert rec1_again["revision_counter"] == 1
    assert rec1_again["is_new"] is False

    await db_module.skip_sprint_item(db, pid, item["id"])
    snap2 = await db_module.build_board_snapshot(db, pid)
    rec2 = await db_module.record_board_snapshot_revision(db, pid, snap2)
    assert rec2["revision_counter"] == 2
    assert rec2["is_new"] is True

    latest = await db_module.get_latest_board_snapshot_revision(db, pid)
    assert latest["revision_counter"] == 2
    assert latest["revision_hash"] == snap2["revision_hash"]


@pytest.mark.asyncio
async def test_record_revision_scoped_per_version_bucket(db):
    pid = await _project(db)
    await db_module.add_sprint_item(db, pid, "v1", "v1 item")
    await db_module.add_sprint_item(db, pid, "v2", "v2 item")

    snap_v1 = await db_module.build_board_snapshot(db, pid, version="v1")
    snap_v2 = await db_module.build_board_snapshot(db, pid, version="v2")

    rec_v1 = await db_module.record_board_snapshot_revision(db, pid, snap_v1, version="v1")
    rec_v2 = await db_module.record_board_snapshot_revision(db, pid, snap_v2, version="v2")

    # Independent buckets: each starts its own counter at 1.
    assert rec_v1["revision_counter"] == 1
    assert rec_v2["revision_counter"] == 1

    latest_v1 = await db_module.get_latest_board_snapshot_revision(db, pid, version="v1")
    latest_v2 = await db_module.get_latest_board_snapshot_revision(db, pid, version="v2")
    assert latest_v1["revision_hash"] == snap_v1["revision_hash"]
    assert latest_v2["revision_hash"] == snap_v2["revision_hash"]
    assert latest_v1["revision_hash"] != latest_v2["revision_hash"]


@pytest.mark.asyncio
async def test_pointers_included_and_ordered_deterministically(db):
    pid = await _project(db)
    item = await db_module.add_sprint_item(db, pid, "v1", "multi-pointer item")
    await db_module.add_sprint_item_pointer(
        db, pid, item["id"], "code",
        [{"uri": "meridian/one.py", "selector": {"type": "symbol", "qualified_name": "one"}}],
        label="first",
    )
    await db_module.add_sprint_item_pointer(
        db, pid, item["id"], "code",
        [{"uri": "meridian/two.py", "selector": {"type": "symbol", "qualified_name": "two"}}],
        label="second",
    )

    snap = await db_module.build_board_snapshot(db, pid)
    item_snap = next(it for it in snap["items"] if it["id"] == item["id"])
    assert len(item_snap["pointers"]) == 2
    pointer_ids = [p["id"] for p in item_snap["pointers"]]
    assert pointer_ids == sorted(pointer_ids)
    labels = {p["label"] for p in item_snap["pointers"]}
    assert labels == {"first", "second"}
