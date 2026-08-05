from __future__ import annotations

import json

import aiosqlite
import pytest

from meridian import db as db_module
from meridian import pg_adapter


async def _table_columns(db, table: str) -> set[str]:
    if hasattr(db, "_pool"):
        async with db.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_name = %s",
            (table,),
        ) as cur:
            rows = await cur.fetchall()
        return {
            row["name"] if isinstance(row, dict) else row[0]
            for row in rows
        }

    async with db.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return {row["name"] for row in rows}


@pytest.mark.asyncio
async def test_proposal_events_schema_supports_append_only_provenance(db):
    """The event stream carries lifecycle content and its provenance."""
    columns = await _table_columns(db, "proposal_events")

    assert columns >= {
        "id",
        "proposal_id",
        "tenant_id",
        "sequence",
        "event_type",
        "content",
        "payload",
        "actor",
        "session_id",
        "source",
        "created_at",
    }


@pytest.mark.asyncio
async def test_proposal_schema_supports_family_and_activity_discovery(db):
    columns = await _table_columns(db, "workspace_proposals")
    assert columns >= {"family_id", "last_activity_at"}


@pytest.mark.asyncio
async def test_postgres_proposal_migration_uses_text_activity_column():
    """The Postgres upgrade must not fail on legacy schemas.

    _TS is a formatted text expression.  The migration must therefore add the
    activity column as TEXT; TIMESTAMPTZ would make PostgreSQL roll back the
    whole migration before the proposal read path can use it.
    """

    class CaptureConnection:
        def __init__(self):
            self.script = ""

        async def executescript(self, sql):
            self.script = sql

    conn = CaptureConnection()
    await pg_adapter._migrate_pg_workspace_proposals(conn)

    assert (
        "ALTER TABLE workspace_proposals ADD COLUMN IF NOT EXISTS "
        "last_activity_at TEXT NOT NULL DEFAULT"
    ) in conn.script
    assert "last_activity_at TIMESTAMPTZ" not in conn.script


@pytest.mark.asyncio
async def test_postgres_migration_is_registered_for_workspace_proposals():
    """The self-healing migration remains in the Postgres migration queue."""

    assert any(
        pg_adapter._migrate_pg_workspace_proposals in migrations
        for migrations in (
            pg_adapter._PG_MIGRATIONS_CORE,
            pg_adapter._PG_MIGRATIONS_HOSTED,
            pg_adapter._PG_MIGRATIONS_LATE,
        )
    )


@pytest.mark.asyncio
async def test_proposal_discovery_filters_family_and_sorts_by_activity(db):
    first = await db_module.add_workspace_proposal(
        db, "First", "body", family_id="family-a"
    )
    second = await db_module.add_workspace_proposal(
        db, "Second", "body", family_id="family-a"
    )
    await db_module.add_workspace_proposal(
        db, "Other family", "body", family_id="family-b"
    )
    await db.execute(
        "UPDATE workspace_proposals SET last_activity_at = ? WHERE id = ?",
        ("2026-01-01 00:00:01", first["id"]),
    )
    await db.execute(
        "UPDATE workspace_proposals SET last_activity_at = ? WHERE id = ?",
        ("2026-01-01 00:00:02", second["id"]),
    )
    await db.commit()

    rows = await db_module.get_workspace_proposals(
        db, family_id="family-a", sort_by="activity"
    )

    assert [row["id"] for row in rows] == [second["id"], first["id"]]
    assert {row["family_id"] for row in rows} == {"family-a"}


@pytest.mark.asyncio
async def test_proposal_events_preserve_ordered_history_and_payload(db):
    """Multiple event rows retain their order and structured provenance."""
    proposal = await db_module.add_workspace_proposal(db, "P1", "body")
    placeholder = "%s" if hasattr(db, "_pool") else "?"
    values = [
        (
            f"event-{sequence}",
            proposal["id"],
            sequence,
            event_type,
            content,
            json.dumps({"pointer": f"tests/test_{sequence}.py"}),
            "Adam",
            "session-1",
            "test",
        )
        for sequence, event_type, content in (
            (2, "evidence", "Observed the failure"),
            (3, "decision", "Keep the migration additive"),
            (4, "next_step", "Add a resumable status"),
        )
    ]
    insert_sql = (
        "INSERT INTO proposal_events "
        "(id, proposal_id, sequence, event_type, content, payload, actor, "
        "session_id, source) "
        f"VALUES ({', '.join([placeholder] * 9)})"
    )
    for value in values:
        await db.execute(insert_sql, value)
    await db.commit()

    async with db.execute(
        "SELECT sequence, event_type, content, payload, actor, session_id, source "
        f"FROM proposal_events WHERE proposal_id = {placeholder} ORDER BY sequence",
        (proposal["id"],),
    ) as cur:
        rows = await cur.fetchall()

    assert [row["sequence"] for row in rows] == [1, 2, 3, 4]
    assert [row["event_type"] for row in rows] == [
        "created",
        "evidence",
        "decision",
        "next_step",
    ]
    assert json.loads(rows[1]["payload"])["pointer"] == "tests/test_2.py"
    assert rows[3]["actor"] == "Adam"
    assert rows[3]["session_id"] == "session-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "next_status"),
    [
        ("raw", "investigating"),
        ("raw", "rejected"),
        ("investigating", "rejected"),
        ("investigating", "raw"),
        ("rejected", "raw"),
    ],
)
async def test_proposal_lifecycle_transition_matrix(db, current, next_status):
    """Every non-promotion edge in the proposal state machine is explicit."""
    proposal = await db_module.add_workspace_proposal(db, "P1", "body")
    if current == "investigating":
        await db_module.advance_workspace_proposal_status(
            db, proposal["id"], "investigating"
        )
    elif current == "rejected":
        await db_module.advance_workspace_proposal_status(
            db, proposal["id"], "rejected"
        )

    updated = await db_module.advance_workspace_proposal_status(
        db, proposal["id"], next_status
    )

    assert updated is not None
    assert updated["status"] == next_status


@pytest.mark.asyncio
async def test_append_proposal_update_is_ordered_and_does_not_mutate_proposal(db):
    proposal = await db_module.add_workspace_proposal(
        db, "P1", "original body", tenant_id="tenant-1"
    )

    event = await db_module.append_proposal_update(
        db,
        proposal["id"],
        "Observed a second signal",
        event_type="evidence",
        payload={"pointer": "tests/test_proposals.py:120"},
        actor="Adam",
        session_id="session-2",
        source="executor",
        tenant_id="tenant-1",
    )

    assert event is not None
    assert event["sequence"] == 2
    assert event["event_type"] == "evidence"
    assert event["actor"] == "Adam"
    assert json.loads(event["payload"])["pointer"] == "tests/test_proposals.py:120"
    rows = await db_module.get_workspace_proposals(
        db, status="all", tenant_id="tenant-1"
    )
    assert rows[0]["body"] == "original body"


@pytest.mark.asyncio
async def test_paused_proposal_can_resume_with_a_distinct_event(db):
    proposal = await db_module.add_workspace_proposal(db, "P1", "body")

    paused = await db_module.advance_workspace_proposal_status(
        db, proposal["id"], "paused"
    )
    resumed = await db_module.advance_workspace_proposal_status(
        db, proposal["id"], "investigating"
    )

    assert paused is not None and paused["status"] == "paused"
    assert resumed is not None and resumed["status"] == "investigating"
    async with db.execute(
        "SELECT event_type FROM proposal_events "
        "WHERE proposal_id = ? ORDER BY sequence",
        (proposal["id"],),
    ) as cur:
        event_types = [row["event_type"] for row in await cur.fetchall()]
    assert event_types == ["created", "status_changed", "resumed"]


@pytest.mark.asyncio
async def test_legacy_proposal_schema_is_rebuilt_for_resumable_statuses():
    legacy = await aiosqlite.connect(":memory:")
    legacy.row_factory = aiosqlite.Row
    await legacy.execute(
        """CREATE TABLE workspace_proposals (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT,
            status TEXT NOT NULL DEFAULT 'raw'
                CHECK (status IN ('raw', 'investigating', 'promoted', 'rejected')),
            promoted_to_sprint_item_id TEXT,
            tenant_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await legacy.execute(
        "INSERT INTO workspace_proposals (id, title, body) VALUES (?, ?, ?)",
        ("legacy-1", "Legacy", "Keep the evidence"),
    )

    await db_module._migrate_workspace_proposals(legacy)
    await legacy.execute(
        "UPDATE workspace_proposals SET status = 'paused' WHERE id = ?",
        ("legacy-1",),
    )
    await legacy.commit()

    async with legacy.execute(
        "SELECT status FROM workspace_proposals WHERE id = ?", ("legacy-1",)
    ) as cur:
        row = await cur.fetchone()
    assert row["status"] == "paused"
    assert await _table_columns(legacy, "proposal_events") >= {
        "proposal_id",
        "sequence",
        "event_type",
    }
    await legacy.close()
