"""867317f6 — transactional/idempotency/tenant-isolation hardening tests for
workspace proposals.

Companion to tests/test_proposals.py (schema + lifecycle-transition-matrix
coverage). This file targets the specific hardening behaviors added for the
867317f6 sprint item:

  * atomic, transactional writes (no partial-write states) on a schema that
    is mid-migration for THIS backend
  * deterministic ProposalSchemaError instead of a raw driver exception
  * idempotent add_workspace_proposal via idempotency_key
  * tenant scoping on proposal reads/writes
  * race-safe status transitions and promotion (exactly one winner)

All tests run against the SQLite backend (the ``db`` fixture — see
tests/conftest.py), matching the rest of this suite's default. The
Postgres-specific compensating-action code paths (guarded by
``hasattr(db, "_pool")`` in meridian/db/workspace.py) are exercised only when
TEST_DATABASE_URL points at a real Postgres instance; that is out of scope
for this pass (see the sprint item's own note on prioritizing the SQLite
core). What IS verified here on SQLite: the schema-drift detection and
classification logic (``ProposalSchemaError``), the idempotency-key
dedup/lookup logic, and the atomic from-state guards -- all backend-agnostic
in behavior even though the "compensating DELETE/UPDATE" branch is a
Postgres-only code path.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module
from meridian.db.workspace import ProposalSchemaError


async def _count(db, table: str) -> int:
    async with db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


# ---------------------------------------------------------------------------
# Idempotency (add_workspace_proposal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_workspace_proposal_idempotency_key_dedupes(db):
    """A repeat call with the same idempotency_key returns the SAME row --
    no second proposal, no second 'created' event."""
    first = await db_module.add_workspace_proposal(
        db, "Ship the thing", "body", idempotency_key="retry-key-1",
    )
    second = await db_module.add_workspace_proposal(
        db, "Ship the thing", "body", idempotency_key="retry-key-1",
    )

    assert second["id"] == first["id"]
    assert await _count(db, "workspace_proposals") == 1

    async with db.execute(
        "SELECT COUNT(*) AS n FROM proposal_events WHERE proposal_id = ? "
        "AND event_type = 'created'",
        (first["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"] if isinstance(row, dict) else row[0]) == 1


@pytest.mark.asyncio
async def test_add_workspace_proposal_without_idempotency_key_always_creates_new_row(db):
    """Baseline: omitting idempotency_key keeps the pre-existing behavior --
    every call is a brand new proposal, even with identical title/body."""
    first = await db_module.add_workspace_proposal(db, "Same title", "same body")
    second = await db_module.add_workspace_proposal(db, "Same title", "same body")

    assert first["id"] != second["id"]
    assert await _count(db, "workspace_proposals") == 2


@pytest.mark.asyncio
async def test_add_workspace_proposal_idempotency_key_scoped_per_tenant(db):
    """The SAME idempotency_key under two DIFFERENT tenants must create two
    separate rows -- the dedup key is tenant-scoped, not global."""
    tenant_a = await db_module.add_workspace_proposal(
        db, "Idea", "body", idempotency_key="shared-key", tenant_id="tenant-a",
    )
    tenant_b = await db_module.add_workspace_proposal(
        db, "Idea", "body", idempotency_key="shared-key", tenant_id="tenant-b",
    )

    assert tenant_a["id"] != tenant_b["id"]
    assert await _count(db, "workspace_proposals") == 2


@pytest.mark.asyncio
async def test_add_workspace_proposal_idempotency_key_unique_index_exists(db):
    """867317f6 -- the dedup guarantee is backed by a real UNIQUE index, not
    just the SELECT-then-INSERT pre-check (which alone cannot close a true
    concurrent-insert race)."""
    async with db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_workspace_proposals_idempotency'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    sql = (row["sql"] if isinstance(row, dict) else row[0]) or ""
    assert "UNIQUE" in sql.upper()


# ---------------------------------------------------------------------------
# Atomicity / deterministic schema-drift errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_workspace_proposal_rolls_back_on_missing_events_table(db):
    """If proposal_events is missing (schema mid-migration on this backend),
    add_workspace_proposal must raise ProposalSchemaError AND must not leave
    an orphan workspace_proposals row with no 'created' event."""
    await db.execute("DROP TABLE proposal_events")
    await db.commit()

    with pytest.raises(ProposalSchemaError):
        await db_module.add_workspace_proposal(db, "Orphan risk", "body")

    assert await _count(db, "workspace_proposals") == 0


@pytest.mark.asyncio
async def test_advance_proposal_status_rolls_back_on_missing_events_table(db):
    """A status transition that can't record its event must not leave the
    proposal silently sitting in the new status with no event on file."""
    proposal = await db_module.add_workspace_proposal(db, "P1", "body")
    await db.execute("DROP TABLE proposal_events")
    await db.commit()

    with pytest.raises(ProposalSchemaError):
        await db_module.advance_workspace_proposal_status(
            db, proposal["id"], "investigating",
        )

    # The status change itself must have been compensated back to 'raw' --
    # recreate proposal_events (schema now "healed") to read the row back.
    await db.execute(
        """CREATE TABLE proposal_events (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            tenant_id TEXT,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            payload TEXT,
            actor TEXT,
            session_id TEXT,
            source TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.commit()
    async with db.execute(
        "SELECT status FROM workspace_proposals WHERE id = ?", (proposal["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert (row["status"] if isinstance(row, dict) else row[0]) == "raw"


@pytest.mark.asyncio
async def test_promote_workspace_proposal_rolls_back_sprint_item_on_missing_events_table(db):
    """A failed 'promoted' event write must not leave a promoted proposal
    with an orphan sprint item and no event history."""
    project = await db_module.create_project(db, "promote-atomicity")
    proposal = await db_module.add_workspace_proposal(db, "Promote me", "body")
    await db.execute("DROP TABLE proposal_events")
    await db.commit()

    with pytest.raises(ProposalSchemaError):
        await db_module.promote_workspace_proposal(db, proposal["id"], project["id"])

    assert await _count(db, "sprint_items") == 0
    async with db.execute(
        "SELECT status, promoted_to_sprint_item_id FROM workspace_proposals "
        "WHERE id = ?",
        (proposal["id"],),
    ) as cur:
        row = await cur.fetchone()
    status = row["status"] if isinstance(row, dict) else row[0]
    linked = row["promoted_to_sprint_item_id"] if isinstance(row, dict) else row[1]
    assert status == "raw"
    assert linked is None


@pytest.mark.asyncio
async def test_append_proposal_update_rolls_back_on_missing_events_table(db):
    proposal = await db_module.add_workspace_proposal(db, "P1", "body")
    await db.execute("DROP TABLE proposal_events")
    await db.commit()

    with pytest.raises(ProposalSchemaError):
        await db_module.append_proposal_update(db, proposal["id"], "evidence text")


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_proposal_update_refuses_cross_tenant_proposal(db):
    proposal = await db_module.add_workspace_proposal(
        db, "P1", "body", tenant_id="tenant-a",
    )

    result = await db_module.append_proposal_update(
        db, proposal["id"], "sneaky update", tenant_id="tenant-b",
    )

    assert result is None
    async with db.execute(
        "SELECT COUNT(*) AS n FROM proposal_events WHERE proposal_id = ?",
        (proposal["id"],),
    ) as cur:
        row = await cur.fetchone()
    # Only the original 'created' event -- the cross-tenant update never landed.
    assert int(row["n"] if isinstance(row, dict) else row[0]) == 1


@pytest.mark.asyncio
async def test_advance_proposal_status_refuses_cross_tenant_proposal(db):
    proposal = await db_module.add_workspace_proposal(
        db, "P1", "body", tenant_id="tenant-a",
    )

    result = await db_module.advance_workspace_proposal_status(
        db, proposal["id"], "investigating", tenant_id="tenant-b",
    )

    assert result is None
    fetched = await db_module.get_workspace_proposals(
        db, status="all", tenant_id="tenant-a",
    )
    assert fetched[0]["status"] == "raw"


@pytest.mark.asyncio
async def test_promote_workspace_proposal_refuses_cross_tenant_proposal(db):
    project = await db_module.create_project(db, "cross-tenant-promote")
    proposal = await db_module.add_workspace_proposal(
        db, "P1", "body", tenant_id="tenant-a",
    )

    with pytest.raises(ValueError, match="not found"):
        await db_module.promote_workspace_proposal(
            db, proposal["id"], project["id"], tenant_id="tenant-b",
        )
    assert await _count(db, "sprint_items") == 0


# ---------------------------------------------------------------------------
# Race-safe status transitions and promotion (exactly one winner)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_proposal_status_concurrent_calls_exactly_one_winner(db):
    """N concurrent callers all trying to move the SAME raw proposal to
    'investigating' must yield exactly one winner; the rest raise ValueError
    instead of silently re-applying (or corrupting) the transition."""
    proposal = await db_module.add_workspace_proposal(db, "Race me", "body")
    n = 8

    async def _attempt():
        try:
            return await db_module.advance_workspace_proposal_status(
                db, proposal["id"], "investigating",
            )
        except ValueError as exc:
            return exc

    results = await asyncio.gather(*[_attempt() for _ in range(n)])
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, ValueError)]

    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}: {results}"
    assert len(losers) == n - 1
    final = await db_module.get_workspace_proposals(db, status="all")
    assert final[0]["status"] == "investigating"

    # Exactly one 'status_changed' event was recorded -- not N.
    async with db.execute(
        "SELECT COUNT(*) AS n FROM proposal_events WHERE proposal_id = ? "
        "AND event_type = 'status_changed'",
        (proposal["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"] if isinstance(row, dict) else row[0]) == 1


@pytest.mark.asyncio
async def test_promote_workspace_proposal_concurrent_calls_create_one_sprint_item(db):
    """N concurrent promote_proposal calls against the SAME proposal must
    create exactly one sprint item -- never two, never zero on the winner."""
    project = await db_module.create_project(db, "concurrent-promote")
    proposal = await db_module.add_workspace_proposal(db, "Promote race", "body")
    n = 6

    async def _attempt():
        try:
            return await db_module.promote_workspace_proposal(
                db, proposal["id"], project["id"],
            )
        except ValueError as exc:
            return exc

    results = await asyncio.gather(*[_attempt() for _ in range(n)])
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, ValueError)]

    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}: {results}"
    assert len(losers) == n - 1
    assert await _count(db, "sprint_items") == 1

    final = await db_module.get_workspace_proposals(db, status="all")
    assert final[0]["status"] == "promoted"
    assert final[0]["promoted_to_sprint_item_id"] is not None
