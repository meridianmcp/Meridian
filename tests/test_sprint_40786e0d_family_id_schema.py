"""40786e0d — workspace-proposal persistence and family_id schema compatibility.

The sprint item asked for three things, each covered here:

  * explicit schema diagnostics: sqlite3 words a missing column in an INSERT
    as "table workspace_proposals has no column named family_id", which the
    old classifier did not match, so a legacy database surfaced as a raw
    OperationalError instead of ProposalSchemaError;
  * a backward-compatible insert path: a caller that does not pass family_id
    can still file a proposal on a database whose workspace_proposals table
    predates the family_id column, while a caller that DOES pass family_id
    gets an actionable ProposalSchemaError and no half-written row;
  * a formal filing path: a proposal with line-by-line child work items is
    filed with family_id and project scope, then promoted with one
    implementation child per line, each with a durable evidence link.

Runs on the default SQLite backend (the ``db`` fixture). The legacy schema is
simulated by dropping the family_id column and its index from the isolated
test database.
"""
from __future__ import annotations

import sqlite3

import pytest

from meridian import db as db_module
from meridian.db import workspace as workspace_mod
from meridian.db.workspace import ProposalSchemaError, _is_proposal_schema_drift_error


def _count_from_row(row) -> int:
    return int(row["n"] if isinstance(row, dict) else row[0])


async def _drop_family_id(db) -> None:
    """Make the isolated test database look like one that predates family_id."""
    await db.execute("DROP INDEX IF EXISTS idx_workspace_proposals_family")
    await db.execute("ALTER TABLE workspace_proposals DROP COLUMN family_id")


async def _proposal_count(db) -> int:
    async with db.execute("SELECT COUNT(*) AS n FROM workspace_proposals") as cur:
        row = await cur.fetchone()
    return _count_from_row(row)


# ---------------------------------------------------------------------------
# Schema-drift classification (explicit diagnostics)
# ---------------------------------------------------------------------------


def test_drift_classifier_matches_sqlite_insert_with_missing_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE workspace_proposals (id TEXT, title TEXT)")
    with pytest.raises(sqlite3.OperationalError) as info:
        conn.execute(
            "INSERT INTO workspace_proposals (id, title, family_id) VALUES (?, ?, ?)",
            ("a", "b", "c"),
        )
    conn.close()
    assert "has no column named family_id" in str(info.value)
    assert _is_proposal_schema_drift_error(info.value) is True


def test_drift_classifier_still_matches_sqlite_select_and_missing_table():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError) as info:
        conn.execute("SELECT family_id FROM nowhere")
    conn.close()
    assert _is_proposal_schema_drift_error(info.value) is True


def test_drift_classifier_matches_postgres_undefined_column_text():
    exc = RuntimeError(
        'column "family_id" of relation "workspace_proposals" does not exist'
    )
    assert _is_proposal_schema_drift_error(exc) is True


def test_drift_classifier_ignores_unique_violation_and_value_errors():
    assert not _is_proposal_schema_drift_error(
        sqlite3.IntegrityError("UNIQUE constraint failed: workspace_proposals.idempotency_key")
    )
    assert not _is_proposal_schema_drift_error(ValueError("Project 'x' not found"))


# ---------------------------------------------------------------------------
# Insert path against migrated and legacy schemas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_family_id_round_trips_on_migrated_schema(db):
    prop = await db_module.add_workspace_proposal(
        db, "Migrated schema", "body", family_id="fam-40786e0d",
    )
    assert prop["family_id"] == "fam-40786e0d"


@pytest.mark.asyncio
async def test_proposal_without_family_id_files_on_legacy_schema(db):
    """Backward-compatible path: no family_id requested, column missing ->
    the proposal still files, with the same created event as usual."""
    await _drop_family_id(db)

    prop = await db_module.add_workspace_proposal(db, "Legacy schema", "body")

    assert prop["title"] == "Legacy schema"
    assert prop["status"] == "raw"
    assert await _proposal_count(db) == 1
    async with db.execute(
        "SELECT COUNT(*) AS n FROM proposal_events "
        "WHERE proposal_id = ? AND event_type = 'created'",
        (prop["id"],),
    ) as cur:
        assert _count_from_row(await cur.fetchone()) == 1


@pytest.mark.asyncio
async def test_family_id_on_legacy_schema_fails_precisely_and_writes_nothing(db):
    """Explicit diagnostics: family_id requested, column missing -> a
    ProposalSchemaError that names the column and the fix, and no row."""
    await _drop_family_id(db)

    with pytest.raises(ProposalSchemaError) as info:
        await db_module.add_workspace_proposal(
            db, "Needs family", "body", family_id="fam-40786e0d",
        )

    msg = str(info.value)
    assert "family_id" in msg
    assert "retry without family_id" in msg
    assert "run pending migrations" in msg
    assert await _proposal_count(db) == 0


# ---------------------------------------------------------------------------
# Formal filing: line-by-line child work items with evidence pointers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_formal_filing_creates_one_child_per_line_with_evidence_pointers(db):
    project = await db_module.create_project(db, "40786e0d-line-by-line")
    body = (
        "Restore the family_id insert path.\n"
        "- Classify the sqlite INSERT missing-column message\n"
        "- Fall back to an INSERT without family_id when the caller omitted it\n"
        "- Name the missing column when the caller supplied it\n"
    )
    line_items = [line[2:].strip() for line in body.splitlines() if line.startswith("- ")]
    assert len(line_items) == 3

    prop = await db_module.add_workspace_proposal(
        db, "Formal filing with children", body,
        family_id="fam-40786e0d", project_id=project["id"],
    )

    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], project["id"],
        children=[{"title": title} for title in line_items],
    )

    root_id = result["root_sprint_item_id"]
    child_ids = {child["id"] for child in result["children"]}
    assert len(result["children"]) == len(line_items)
    assert {child["title"] for child in result["children"]} == set(line_items)

    # Each line is a real child sprint item parented to the root investigation.
    async with db.execute(
        "SELECT id FROM sprint_items WHERE parent_id = ?", (root_id,),
    ) as cur:
        rows = [db_module._row_to_dict(r) for r in await cur.fetchall()]
    assert {row["id"] for row in rows} == child_ids

    # Each child carries a durable evidence link back to the proposal (the pointer).
    async with db.execute(
        "SELECT entity_id FROM proposal_evidence_links "
        "WHERE proposal_id = ? AND entity_type = 'sprint_item'",
        (prop["id"],),
    ) as cur:
        linked = {db_module._row_to_dict(r)["entity_id"] for r in await cur.fetchall()}
    assert child_ids <= linked

    # The proposal kept its family_id and is now promoted to the root item.
    async with db.execute(
        "SELECT family_id, status, promoted_to_sprint_item_id "
        "FROM workspace_proposals WHERE id = ?",
        (prop["id"],),
    ) as cur:
        row = db_module._row_to_dict(await cur.fetchone())
    assert row["family_id"] == "fam-40786e0d"
    assert row["status"] == "promoted"
    assert row["promoted_to_sprint_item_id"] == root_id
