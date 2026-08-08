"""Tests for sprint item 0d95003f — retroactively audit, quarantine, and
explicitly reclassify cross-project records (sprint-item slice).

Covers the two new functions in meridian/db/sprint_items.py:

1. move_sprint_item_to_project — explicit, audited, idempotent move.
   Never infers a destination from title/path; requires actor + reason;
   verifies the caller's assumed source_project_id against the item's
   ACTUAL current project_id before moving anything.
2. find_cross_project_dependency_mismatches — read-only, non-destructive
   audit: sprint items whose depends_on points at an item in a different
   project.

Scope note (see the item's own notes + the accompanying finding): the
item's full ask spans sessions, tasks, notes, proposals, proposal evidence,
pointers, handoff bodies/pending goals, generated files, Redis keys, and
index shards. This file covers the sprint-item slice only — the record type
most directly tied to executor handoffs and completion.
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
    return await db_module.create_project(db, "cross-project-a-0d95003f")


@pytest_asyncio.fixture
async def project_b(db):
    return await db_module.create_project(db, "cross-project-b-0d95003f")


# ---------------------------------------------------------------------------
# move_sprint_item_to_project
# ---------------------------------------------------------------------------

async def test_move_requires_non_empty_reason(db, project_a, project_b):
    item = await db_module.add_sprint_item(db, project_a["id"], "v1", "Do a thing")
    result = await db_module.move_sprint_item_to_project(
        db, item["id"], project_a["id"], project_b["id"], actor="adam", reason="",
    )
    assert result["moved"] is False
    assert "reason" in result["error"]


async def test_move_requires_non_empty_actor(db, project_a, project_b):
    item = await db_module.add_sprint_item(db, project_a["id"], "v1", "Do a thing")
    result = await db_module.move_sprint_item_to_project(
        db, item["id"], project_a["id"], project_b["id"], actor="", reason="misfiled",
    )
    assert result["moved"] is False
    assert "actor" in result["error"]


async def test_move_rejects_unknown_item(db, project_a, project_b):
    result = await db_module.move_sprint_item_to_project(
        db, "nonexistent-item-id", project_a["id"], project_b["id"],
        actor="adam", reason="misfiled",
    )
    assert result["moved"] is False
    assert result["error"] == "item not found"


async def test_move_rejects_wrong_source_project(db, project_a, project_b):
    """The caller's assumed source_project_id must match the item's ACTUAL
    current project — a stale/incorrect caller assumption must never
    silently reassign the wrong item."""
    item = await db_module.add_sprint_item(db, project_a["id"], "v1", "Do a thing")
    result = await db_module.move_sprint_item_to_project(
        db, item["id"], project_b["id"], project_a["id"],  # source/dest swapped vs reality
        actor="adam", reason="misfiled",
    )
    assert result["moved"] is False
    assert "does not match" in result["error"]
    # The item must be untouched.
    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["project_id"] == project_a["id"]


async def test_move_rejects_unknown_destination_project(db, project_a):
    item = await db_module.add_sprint_item(db, project_a["id"], "v1", "Do a thing")
    result = await db_module.move_sprint_item_to_project(
        db, item["id"], project_a["id"], "nonexistent-destination-project",
        actor="adam", reason="misfiled",
    )
    assert result["moved"] is False
    assert result["error"] == "destination project not found"
    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["project_id"] == project_a["id"]


async def test_move_succeeds_and_persists(db, project_a, project_b):
    item = await db_module.add_sprint_item(db, project_a["id"], "v1", "Do a thing")
    result = await db_module.move_sprint_item_to_project(
        db, item["id"], project_a["id"], project_b["id"],
        actor="adam", reason="was filed under the wrong project by mistake",
    )
    assert result["moved"] is True
    assert result["error"] is None
    assert result["item"]["project_id"] == project_b["id"]

    persisted = await db_module.get_sprint_item(db, item["id"])
    assert persisted["project_id"] == project_b["id"]
    # Everything else about the item is preserved (title unchanged, same id).
    assert persisted["title"] == "Do a thing"
    assert persisted["id"] == item["id"]


async def test_move_is_idempotent_when_already_at_destination(db, project_a, project_b):
    item = await db_module.add_sprint_item(db, project_a["id"], "v1", "Do a thing")
    first = await db_module.move_sprint_item_to_project(
        db, item["id"], project_a["id"], project_b["id"], actor="adam", reason="misfiled",
    )
    assert first["moved"] is True

    # Second call, now correctly reflecting the item's real current project —
    # asking to "move" it to where it already is must be a safe no-op.
    second = await db_module.move_sprint_item_to_project(
        db, item["id"], project_b["id"], project_b["id"], actor="adam", reason="misfiled",
    )
    assert second["moved"] is False
    assert second["error"] is None
    assert second["item"]["project_id"] == project_b["id"]


async def test_move_writes_audit_event(db, project_a, project_b):
    item = await db_module.add_sprint_item(db, project_a["id"], "v1", "Do a thing")
    await db_module.move_sprint_item_to_project(
        db, item["id"], project_a["id"], project_b["id"],
        actor="adam", reason="was filed under the wrong project",
    )
    from meridian.db import sprint_items as si_mod

    events = await db_module.get_action_audit_log(
        db, project_id=project_b["id"], event_type=si_mod.CROSS_PROJECT_MOVE_EVENT_TYPE,
    )
    assert len(events) == 1
    assert events[0]["actor"] == "adam"


# ---------------------------------------------------------------------------
# find_cross_project_dependency_mismatches
# ---------------------------------------------------------------------------

async def test_no_mismatches_on_clean_board(db, project_a):
    parent = await db_module.add_sprint_item(db, project_a["id"], "v1", "Parent item")
    await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Child item", depends_on=parent["id"],
    )
    mismatches = await db_module.find_cross_project_dependency_mismatches(db, project_a["id"])
    assert mismatches == []


async def test_detects_cross_project_dependency(db, project_a, project_b):
    """A dependency that stayed behind after a move becomes exactly the kind
    of mismatch this audit exists to surface."""
    dependency = await db_module.add_sprint_item(db, project_a["id"], "v1", "Build the widget backend")
    dependent = await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Wire up the checkout flow",
        depends_on=dependency["id"],
    )
    # Move only the DEPENDENT item away — the dependency stays in project_a.
    moved = await db_module.move_sprint_item_to_project(
        db, dependent["id"], project_a["id"], project_b["id"],
        actor="adam", reason="reclassifying the dependent item only",
    )
    assert moved["moved"] is True

    mismatches = await db_module.find_cross_project_dependency_mismatches(db, project_b["id"])
    assert len(mismatches) == 1
    assert mismatches[0]["item_id"] == dependent["id"]
    assert mismatches[0]["item_project_id"] == project_b["id"]
    assert mismatches[0]["depends_on_id"] == dependency["id"]
    assert mismatches[0]["depends_on_project_id"] == project_a["id"]

    # The audit is read-only: the source project's own view is unaffected.
    assert await db_module.find_cross_project_dependency_mismatches(db, project_a["id"]) == []


async def test_dangling_depends_on_is_not_reported_as_cross_project(db, project_a):
    """A depends_on pointing at an id that doesn't exist at all is a
    different, pre-existing concern — not this audit's job to flag."""
    await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Depends on nothing real", depends_on="does-not-exist",
    )
    mismatches = await db_module.find_cross_project_dependency_mismatches(db, project_a["id"])
    assert mismatches == []


async def test_audit_never_mutates_anything(db, project_a, project_b):
    dependency = await db_module.add_sprint_item(db, project_a["id"], "v1", "Dep")
    dependent = await db_module.add_sprint_item(
        db, project_a["id"], "v1", "Dependent", depends_on=dependency["id"],
    )
    await db_module.move_sprint_item_to_project(
        db, dependent["id"], project_a["id"], project_b["id"], actor="adam", reason="x",
    )
    before = await db_module.get_sprint_item(db, dependency["id"])
    await db_module.find_cross_project_dependency_mismatches(db, project_b["id"])
    after = await db_module.get_sprint_item(db, dependency["id"])
    assert before == after
