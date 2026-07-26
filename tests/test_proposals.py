from __future__ import annotations

import json

import pytest

from meridian import db as db_module


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
            (1, "created", "Initial proposal"),
            (2, "evidence", "Observed the failure"),
            (3, "decision", "Keep the migration additive"),
        )
    ]
    await db.executemany(
        "INSERT INTO proposal_events "
        "(id, proposal_id, sequence, event_type, content, payload, actor, "
        "session_id, source) "
        f"VALUES ({', '.join([placeholder] * 9)})",
        values,
    )
    await db.commit()

    async with db.execute(
        "SELECT sequence, event_type, content, payload, actor, session_id, source "
        f"FROM proposal_events WHERE proposal_id = {placeholder} ORDER BY sequence",
        (proposal["id"],),
    ) as cur:
        rows = await cur.fetchall()

    assert [row["sequence"] for row in rows] == [1, 2, 3]
    assert [row["event_type"] for row in rows] == [
        "created",
        "evidence",
        "decision",
    ]
    assert json.loads(rows[1]["payload"])["pointer"] == "tests/test_2.py"
    assert rows[2]["actor"] == "Adam"
    assert rows[2]["session_id"] == "session-1"


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
