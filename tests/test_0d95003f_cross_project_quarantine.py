"""Tests for sprint item 0d95003f — retroactively audit, quarantine, and
explicitly reclassify cross-project records (generic quarantine mechanism
slice).

Covers the new quarantine primitives in meridian/db/workspace.py:

1. quarantine_cross_project_record — flag a (record_type, record_id) as an
   ambiguous/foreign cross-project reference, without moving or deleting
   anything. Idempotent, requires actor + reason.
2. resolve_cross_project_quarantine — close an open quarantine entry with an
   explicit resolution ("moved" | "dismissed_false_positive" |
   "confirmed_correct_project").
3. get_cross_project_quarantine_status / is_cross_project_quarantined —
   read-only status lookups.
4. list_quarantined_cross_project_records — read-only listing of every
   currently-open entry, optionally filtered by project/record_type.

...and the one wired-in consumer in meridian/db/sprint_items.py:

5. audit_and_quarantine_sprint_item_dependency_mismatches — runs
   find_cross_project_dependency_mismatches (already shipped in 4ce87a11)
   and quarantines each mismatch found, proving the generic mechanism
   against a real detector end-to-end.

Scope note (see the item's RESCUE-A note): the item's full ask spans
sessions, tasks, notes, proposals, proposal evidence, pointers, handoff
bodies/pending goals, generated files, Redis keys, and index shards. This
file covers the generic quarantine mechanism itself, which none of those
record types had before — each record type's own mismatch-DETECTION logic
(the genuinely large remaining work) is explicit follow-up, not attempted
here, mirroring how 4ce87a11 scoped down to sprint items only.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from meridian import db as db_module


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project_a(db):
    return await db_module.create_project(db, "cross-project-a-0d95003f-quarantine")


@pytest_asyncio.fixture
async def project_b(db):
    return await db_module.create_project(db, "cross-project-b-0d95003f-quarantine")


# ---------------------------------------------------------------------------
# quarantine_cross_project_record
# ---------------------------------------------------------------------------

async def test_quarantine_requires_non_empty_record_type(db, project_a):
    result = await db_module.quarantine_cross_project_record(
        db, "", "rec-1", project_a["id"], reason="looks foreign", actor="adam",
    )
    assert result["quarantined"] is False
    assert "record_type" in result["error"]


async def test_quarantine_requires_non_empty_record_id(db, project_a):
    result = await db_module.quarantine_cross_project_record(
        db, "session", "", project_a["id"], reason="looks foreign", actor="adam",
    )
    assert result["quarantined"] is False
    assert "record_id" in result["error"]


async def test_quarantine_requires_non_empty_reason(db, project_a):
    result = await db_module.quarantine_cross_project_record(
        db, "session", "sess-1", project_a["id"], reason="", actor="adam",
    )
    assert result["quarantined"] is False
    assert "reason" in result["error"]


async def test_quarantine_requires_non_empty_actor(db, project_a):
    result = await db_module.quarantine_cross_project_record(
        db, "session", "sess-1", project_a["id"], reason="looks foreign", actor="",
    )
    assert result["quarantined"] is False
    assert "actor" in result["error"]


async def test_quarantine_succeeds_and_is_readable(db, project_a):
    result = await db_module.quarantine_cross_project_record(
        db, "session", "sess-1", project_a["id"],
        reason="session referenced from a different project's handoff",
        actor="adam",
        suspected_project_id="some-other-project",
    )
    assert result["quarantined"] is True
    assert result["error"] is None
    entry = result["entry"]
    assert entry["record_type"] == "session"
    assert entry["record_id"] == "sess-1"
    assert entry["project_id"] == project_a["id"]
    assert entry["status"] == "quarantined"
    assert entry["suspected_project_id"] == "some-other-project"
    assert entry["quarantined_by"] == "adam"

    status = await db_module.get_cross_project_quarantine_status(db, "session", "sess-1")
    assert status == entry
    assert await db_module.is_cross_project_quarantined(db, "session", "sess-1") is True


async def test_quarantine_is_idempotent(db, project_a):
    first = await db_module.quarantine_cross_project_record(
        db, "note", "note-1", project_a["id"], reason="first flag", actor="adam",
    )
    assert first["quarantined"] is True

    second = await db_module.quarantine_cross_project_record(
        db, "note", "note-1", project_a["id"], reason="second flag, different text", actor="eve",
    )
    # No new event written — the original entry is returned unchanged.
    assert second["quarantined"] is False
    assert second["error"] is None
    assert second["entry"]["reason"] == "first flag"
    assert second["entry"]["quarantined_by"] == "adam"


async def test_unknown_record_has_no_quarantine_status(db):
    assert await db_module.get_cross_project_quarantine_status(db, "task", "nonexistent") is None
    assert await db_module.is_cross_project_quarantined(db, "task", "nonexistent") is False


# ---------------------------------------------------------------------------
# resolve_cross_project_quarantine
# ---------------------------------------------------------------------------

async def test_resolve_rejects_invalid_resolution(db, project_a):
    await db_module.quarantine_cross_project_record(
        db, "task", "task-1", project_a["id"], reason="foreign", actor="adam",
    )
    result = await db_module.resolve_cross_project_quarantine(
        db, "task", "task-1", resolution="not-a-real-resolution", actor="adam",
    )
    assert result["resolved"] is False
    assert "resolution must be one of" in result["error"]


async def test_resolve_requires_non_empty_actor(db, project_a):
    await db_module.quarantine_cross_project_record(
        db, "task", "task-1", project_a["id"], reason="foreign", actor="adam",
    )
    result = await db_module.resolve_cross_project_quarantine(
        db, "task", "task-1", resolution="dismissed_false_positive", actor="",
    )
    assert result["resolved"] is False
    assert "actor" in result["error"]


async def test_resolve_rejects_when_nothing_open(db):
    result = await db_module.resolve_cross_project_quarantine(
        db, "task", "never-quarantined", resolution="dismissed_false_positive", actor="adam",
    )
    assert result["resolved"] is False
    assert "no open quarantine entry" in result["error"]


async def test_resolve_closes_open_entry(db, project_a):
    await db_module.quarantine_cross_project_record(
        db, "proposal", "prop-1", project_a["id"], reason="foreign origin", actor="adam",
    )
    result = await db_module.resolve_cross_project_quarantine(
        db, "proposal", "prop-1",
        resolution="dismissed_false_positive", actor="eve", note="checked, it's fine",
    )
    assert result["resolved"] is True
    entry = result["entry"]
    assert entry["status"] == "resolved"
    assert entry["resolution"] == "dismissed_false_positive"
    assert entry["resolved_by"] == "eve"
    assert entry["note"] == "checked, it's fine"
    # Original quarantine detail is preserved on the resolved entry.
    assert entry["reason"] == "foreign origin"
    assert entry["quarantined_by"] == "adam"

    assert await db_module.is_cross_project_quarantined(db, "proposal", "prop-1") is False


async def test_resolve_then_requarantine_reopens(db, project_a):
    """A record can be quarantined, resolved, and quarantined again — each
    cycle should be independently correct, not confused by prior history."""
    await db_module.quarantine_cross_project_record(
        db, "pointer", "ptr-1", project_a["id"], reason="round 1", actor="adam",
    )
    await db_module.resolve_cross_project_quarantine(
        db, "pointer", "ptr-1", resolution="dismissed_false_positive", actor="adam",
    )
    assert await db_module.is_cross_project_quarantined(db, "pointer", "ptr-1") is False

    second = await db_module.quarantine_cross_project_record(
        db, "pointer", "ptr-1", project_a["id"], reason="round 2 — flagged again", actor="eve",
    )
    assert second["quarantined"] is True
    status = await db_module.get_cross_project_quarantine_status(db, "pointer", "ptr-1")
    assert status["status"] == "quarantined"
    assert status["reason"] == "round 2 — flagged again"
    assert status["quarantined_by"] == "eve"


# ---------------------------------------------------------------------------
# list_quarantined_cross_project_records
# ---------------------------------------------------------------------------

async def test_list_returns_only_open_entries(db, project_a):
    await db_module.quarantine_cross_project_record(
        db, "handoff_body", "h-1", project_a["id"], reason="stale ref", actor="adam",
    )
    await db_module.quarantine_cross_project_record(
        db, "handoff_body", "h-2", project_a["id"], reason="stale ref", actor="adam",
    )
    await db_module.resolve_cross_project_quarantine(
        db, "handoff_body", "h-2", resolution="moved", actor="adam",
    )

    open_entries = await db_module.list_quarantined_cross_project_records(db, project_id=project_a["id"])
    ids = {e["record_id"] for e in open_entries}
    assert ids == {"h-1"}


async def test_list_filters_by_record_type_and_project(db, project_a, project_b):
    await db_module.quarantine_cross_project_record(
        db, "generated_file", "gf-1", project_a["id"], reason="x", actor="adam",
    )
    await db_module.quarantine_cross_project_record(
        db, "redis_key", "rk-1", project_a["id"], reason="x", actor="adam",
    )
    await db_module.quarantine_cross_project_record(
        db, "generated_file", "gf-2", project_b["id"], reason="x", actor="adam",
    )

    only_files_a = await db_module.list_quarantined_cross_project_records(
        db, project_id=project_a["id"], record_type="generated_file",
    )
    assert [e["record_id"] for e in only_files_a] == ["gf-1"]

    all_files = await db_module.list_quarantined_cross_project_records(db, record_type="generated_file")
    assert {e["record_id"] for e in all_files} == {"gf-1", "gf-2"}


async def test_list_never_mutates_anything(db, project_a):
    await db_module.quarantine_cross_project_record(
        db, "index_shard", "shard-1", project_a["id"], reason="x", actor="adam",
    )
    before = await db_module.get_cross_project_quarantine_status(db, "index_shard", "shard-1")
    await db_module.list_quarantined_cross_project_records(db)
    after = await db_module.get_cross_project_quarantine_status(db, "index_shard", "shard-1")
    assert before == after


# ---------------------------------------------------------------------------
# audit_and_quarantine_sprint_item_dependency_mismatches (sprint_items.py
# consumer wiring the generic mechanism to the existing detector)
# ---------------------------------------------------------------------------

async def test_audit_and_quarantine_flags_real_mismatch(db, project_a, project_b):
    dependency = await db_module.add_sprint_item(db, project_a["id"], "v1", "Build the widget backend")
    dependent = await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Wire up the checkout flow", depends_on=dependency["id"],
    )
    await db_module.move_sprint_item_to_project(
        db, dependent["id"], project_a["id"], project_b["id"],
        actor="adam", reason="reclassifying the dependent item only",
    )

    result = await db_module.audit_and_quarantine_sprint_item_dependency_mismatches(
        db, project_b["id"], actor="rescue-sweep",
    )
    assert len(result["mismatches"]) == 1
    assert len(result["quarantined"]) == 1
    quarantined_entry = result["quarantined"][0]
    assert quarantined_entry["record_type"] == "sprint_item"
    assert quarantined_entry["record_id"] == dependent["id"]
    assert quarantined_entry["suspected_project_id"] == project_a["id"]

    assert await db_module.is_cross_project_quarantined(db, "sprint_item", dependent["id"]) is True


async def test_audit_and_quarantine_is_safe_to_rerun(db, project_a, project_b):
    """Re-running the scan against an unchanged board must not duplicate
    quarantine entries — the second run's mismatch is still reported, but
    nothing new is (re-)quarantined."""
    dependency = await db_module.add_sprint_item(db, project_a["id"], "v1", "Dep")
    dependent = await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Dependent", depends_on=dependency["id"],
    )
    await db_module.move_sprint_item_to_project(
        db, dependent["id"], project_a["id"], project_b["id"], actor="adam", reason="x",
    )

    first = await db_module.audit_and_quarantine_sprint_item_dependency_mismatches(
        db, project_b["id"], actor="rescue-sweep",
    )
    assert len(first["quarantined"]) == 1

    second = await db_module.audit_and_quarantine_sprint_item_dependency_mismatches(
        db, project_b["id"], actor="rescue-sweep",
    )
    assert len(second["mismatches"]) == 1  # still reported as a mismatch
    assert len(second["quarantined"]) == 0  # but not re-flagged


async def test_audit_and_quarantine_clean_board_quarantines_nothing(db, project_a):
    parent = await db_module.add_sprint_item(db, project_a["id"], "v1", "Parent item")
    await db_module.add_sprint_item(db, project_a["id"], "v1", "Child item", depends_on=parent["id"])

    result = await db_module.audit_and_quarantine_sprint_item_dependency_mismatches(
        db, project_a["id"], actor="rescue-sweep",
    )
    assert result["mismatches"] == []
    assert result["quarantined"] == []
