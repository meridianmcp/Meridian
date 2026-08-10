"""Tests for sprint item 5a744f81 — first-class proposal lineage records and
typed isolated successor versions.

Covers meridian.db.proposal_lineage (the durable, typed proposal-to-PROPOSAL
relation primitive: parent/related identity, a closed relation_type enum,
deterministic sequence/version metadata, tenant scoping, idempotency,
uniqueness, and cycle prevention) plus a regression check that the EXISTING
proposal system (proposal_events, family_id, proposal_evidence_links,
promote_workspace_proposal) is provably unaffected by this new, additive
table.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import db as db_module


async def _count(db, table: str) -> int:
    async with db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


# ---------------------------------------------------------------------------
# Migration — table + indexes, idempotent; not inline in either base literal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposal_lineage_migration_creates_table_and_indexes_idempotently():
    """The guarded SQLite migration creates the table + all three indexes,
    and is idempotent (safe to re-run). Exercised against a bare connection
    that has NEITHER to start, mirroring
    test_proposal_evidence_links_migration_creates_table_and_indexes_idempotently."""
    import aiosqlite
    from meridian.db.proposal_lineage import _migrate_proposal_lineage

    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        await _migrate_proposal_lineage(conn)
        # Re-run must be a no-op (idempotent) and not raise.
        await _migrate_proposal_lineage(conn)

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='proposal_lineage'"
        ) as cur:
            assert await cur.fetchone() is not None
        for index_name in (
            "idx_proposal_lineage_unique",
            "idx_proposal_lineage_from",
            "idx_proposal_lineage_to",
        ):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ) as cur:
                assert await cur.fetchone() is not None, index_name
        async with conn.execute("PRAGMA table_info(proposal_lineage)") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        assert cols == {
            "id", "tenant_id", "from_proposal_id", "to_proposal_id",
            "relation_type", "sequence", "label", "created_by", "created_at",
        }
    finally:
        await conn.close()


def test_proposal_lineage_not_inline_in_base_literals():
    """proposal_lineage must NOT appear in either base CREATE_TABLES literal
    — like proposal_evidence_links, the guarded migration is its ONLY
    creation path (fresh or upgrading DB alike), so there is no risk of an
    unguarded-index-on-a-migration-added-table startup crash (2026-07-04)."""
    from meridian.pg_adapter import CREATE_TABLES_CORE
    from meridian.db import CREATE_TABLES

    for name, literal in (
        ("CREATE_TABLES_CORE", CREATE_TABLES_CORE),
        ("CREATE_TABLES", CREATE_TABLES),
    ):
        assert "proposal_lineage" not in literal, name


@pytest.mark.asyncio
async def test_link_proposal_lineage_works_through_full_init_db(db):
    """Sanity check that the migration is actually wired into init_db's
    startup chain (not just directly callable) — the `db` fixture goes
    through the real init_db path."""
    p1 = await db_module.add_workspace_proposal(db, "Root idea", "body")
    p2 = await db_module.add_workspace_proposal(db, "Better idea", "body")
    row = await db_module.link_proposal_lineage(
        db, p2["id"], p1["id"], "supersedes",
    )
    assert row["from_proposal_id"] == p2["id"]
    assert row["to_proposal_id"] == p1["id"]
    assert row["relation_type"] == "supersedes"
    assert row["sequence"] == 1


# ---------------------------------------------------------------------------
# link_proposal_lineage — validation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_proposal_lineage_invalid_relation_type_raises(db):
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    p2 = await db_module.add_workspace_proposal(db, "B", "body")
    with pytest.raises(ValueError, match="relation_type must be one of"):
        await db_module.link_proposal_lineage(db, p2["id"], p1["id"], "bogus-type")


@pytest.mark.asyncio
async def test_link_proposal_lineage_empty_from_id_raises(db):
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    with pytest.raises(ValueError, match="from_proposal_id must be a non-empty string"):
        await db_module.link_proposal_lineage(db, "   ", p1["id"], "supersedes")


@pytest.mark.asyncio
async def test_link_proposal_lineage_empty_to_id_raises(db):
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    with pytest.raises(ValueError, match="to_proposal_id must be a non-empty string"):
        await db_module.link_proposal_lineage(db, p1["id"], "  ", "supersedes")


@pytest.mark.asyncio
async def test_link_proposal_lineage_self_relation_raises(db):
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    with pytest.raises(ValueError, match="cannot have a lineage relation to itself"):
        await db_module.link_proposal_lineage(db, p1["id"], p1["id"], "supersedes")


@pytest.mark.asyncio
async def test_link_proposal_lineage_unknown_from_proposal_raises(db):
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    with pytest.raises(ValueError, match="does not exist"):
        await db_module.link_proposal_lineage(db, "nope-does-not-exist", p1["id"], "supersedes")


@pytest.mark.asyncio
async def test_link_proposal_lineage_unknown_to_proposal_raises(db):
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    with pytest.raises(ValueError, match="does not exist"):
        await db_module.link_proposal_lineage(db, p1["id"], "nope-does-not-exist", "supersedes")


@pytest.mark.asyncio
async def test_link_proposal_lineage_all_relation_types_accepted(db):
    """Every documented relation_type is a valid, storable value."""
    from meridian.db.proposal_lineage import VALID_RELATION_TYPES

    root = await db_module.add_workspace_proposal(db, "root", "body")
    for i, rel in enumerate(VALID_RELATION_TYPES):
        successor = await db_module.add_workspace_proposal(db, f"succ-{i}", "body")
        row = await db_module.link_proposal_lineage(db, successor["id"], root["id"], rel)
        assert row["relation_type"] == rel


# ---------------------------------------------------------------------------
# Tenant scoping — never crosses a tenant/workspace boundary.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_proposal_lineage_cross_tenant_stored_ids_rejected(db):
    """Two proposals stored under DIFFERENT tenant_id values must never be
    linkable, regardless of what (if any) tenant_id the caller asserts."""
    p_a = await db_module.add_workspace_proposal(db, "A", "body", tenant_id="tenant-a")
    p_b = await db_module.add_workspace_proposal(db, "B", "body", tenant_id="tenant-b")
    with pytest.raises(ValueError, match="must not cross tenant/workspace boundaries"):
        await db_module.link_proposal_lineage(db, p_b["id"], p_a["id"], "supersedes")


@pytest.mark.asyncio
async def test_link_proposal_lineage_caller_scope_mismatch_rejected(db):
    """Both proposals belong to the SAME tenant as each other ('tenant-a'),
    but the caller is authenticated as a DIFFERENT tenant ('tenant-b') --
    this must still be refused, not silently allowed just because the two
    proposals agree with each other."""
    p1 = await db_module.add_workspace_proposal(db, "A", "body", tenant_id="tenant-a")
    p2 = await db_module.add_workspace_proposal(db, "B", "body", tenant_id="tenant-a")
    with pytest.raises(ValueError, match="different tenant than the requesting scope"):
        await db_module.link_proposal_lineage(
            db, p2["id"], p1["id"], "supersedes", tenant_id="tenant-b",
        )


@pytest.mark.asyncio
async def test_link_proposal_lineage_same_tenant_allowed(db):
    p1 = await db_module.add_workspace_proposal(db, "A", "body", tenant_id="tenant-a")
    p2 = await db_module.add_workspace_proposal(db, "B", "body", tenant_id="tenant-a")
    row = await db_module.link_proposal_lineage(
        db, p2["id"], p1["id"], "supersedes", tenant_id="tenant-a",
    )
    assert row["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
async def test_link_proposal_lineage_legacy_null_tenant_rows_permissive(db):
    """A proposal created with no tenant_id (self-host / pre-isolation) can
    still be linked under an authenticated tenant scope -- mirrors
    _ws_tenant_clause's 'NULL matches everything' rule."""
    p1 = await db_module.add_workspace_proposal(db, "A", "body")  # tenant_id=None
    p2 = await db_module.add_workspace_proposal(db, "B", "body")  # tenant_id=None
    row = await db_module.link_proposal_lineage(
        db, p2["id"], p1["id"], "supersedes", tenant_id="tenant-a",
    )
    assert row["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
async def test_get_proposal_lineage_links_isolated_by_tenant(db):
    """A proposal's lineage rows aren't visible to a query scoped under a
    different tenant."""
    p1 = await db_module.add_workspace_proposal(db, "A", "body", tenant_id="tenant-a")
    p2 = await db_module.add_workspace_proposal(db, "B", "body", tenant_id="tenant-a")
    await db_module.link_proposal_lineage(
        db, p2["id"], p1["id"], "supersedes", tenant_id="tenant-a",
    )
    same_tenant = await db_module.get_proposal_lineage_links(db, p1["id"], tenant_id="tenant-a")
    assert len(same_tenant) == 1
    other_tenant = await db_module.get_proposal_lineage_links(db, p1["id"], tenant_id="tenant-b")
    assert other_tenant == []


# ---------------------------------------------------------------------------
# Idempotency + uniqueness.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_proposal_lineage_repeat_call_is_a_noop(db):
    """Creating the exact same relation twice returns the SAME row and does
    NOT create a duplicate."""
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    p2 = await db_module.add_workspace_proposal(db, "B", "body")
    first = await db_module.link_proposal_lineage(db, p2["id"], p1["id"], "supersedes")
    second = await db_module.link_proposal_lineage(db, p2["id"], p1["id"], "supersedes")
    assert first["id"] == second["id"]
    assert await _count(db, "proposal_lineage") == 1


@pytest.mark.asyncio
async def test_link_proposal_lineage_different_relation_type_is_a_new_row(db):
    """The SAME (from, to) pair with a DIFFERENT relation_type is a distinct
    relation, not deduped against the first."""
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    p2 = await db_module.add_workspace_proposal(db, "B", "body")
    await db_module.link_proposal_lineage(db, p2["id"], p1["id"], "supersedes")
    await db_module.link_proposal_lineage(db, p2["id"], p1["id"], "duplicates")
    assert await _count(db, "proposal_lineage") == 2


@pytest.mark.asyncio
async def test_link_proposal_lineage_unique_index_exists(db):
    """The COALESCE-normalized unique index actually exists at the schema
    level (belt-and-suspenders for the idempotent-insert pre-check above --
    a genuine concurrent-insert race is caught here, not just by the
    application-level pre-check)."""
    async with db.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='index' "
        "AND name='idx_proposal_lineage_unique'"
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"] if isinstance(row, dict) else row[0]) == 1


@pytest.mark.asyncio
async def test_link_proposal_lineage_concurrent_calls_yield_one_row(db):
    """N concurrent callers creating the EXACT same relation must all
    resolve to the SAME row -- no ValueError, no duplicate row (this is
    idempotency, not a status-transition race, so every caller is a
    'winner')."""
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    p2 = await db_module.add_workspace_proposal(db, "B", "body")
    n = 8

    async def _attempt():
        return await db_module.link_proposal_lineage(db, p2["id"], p1["id"], "supersedes")

    results = await asyncio.gather(*[_attempt() for _ in range(n)])
    ids = {r["id"] for r in results}
    assert len(ids) == 1, f"expected exactly 1 distinct row id, got {ids}"
    assert await _count(db, "proposal_lineage") == 1


# ---------------------------------------------------------------------------
# Cycle prevention.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_proposal_lineage_two_cycle_rejected(db):
    """A supersedes B supersedes A must be rejected."""
    a = await db_module.add_workspace_proposal(db, "A", "body")
    b = await db_module.add_workspace_proposal(db, "B", "body")
    await db_module.link_proposal_lineage(db, a["id"], b["id"], "supersedes")
    with pytest.raises(ValueError, match="would create a cycle"):
        await db_module.link_proposal_lineage(db, b["id"], a["id"], "supersedes")
    # The rejected edge was never written.
    assert await _count(db, "proposal_lineage") == 1


@pytest.mark.asyncio
async def test_link_proposal_lineage_three_cycle_rejected(db):
    """A -> B -> C -> A (mixed relation types) must be rejected on the
    closing edge."""
    a = await db_module.add_workspace_proposal(db, "A", "body")
    b = await db_module.add_workspace_proposal(db, "B", "body")
    c = await db_module.add_workspace_proposal(db, "C", "body")
    await db_module.link_proposal_lineage(db, a["id"], b["id"], "refines")
    await db_module.link_proposal_lineage(db, b["id"], c["id"], "continues")
    with pytest.raises(ValueError, match="would create a cycle"):
        await db_module.link_proposal_lineage(db, c["id"], a["id"], "forks")
    assert await _count(db, "proposal_lineage") == 2


@pytest.mark.asyncio
async def test_link_proposal_lineage_diamond_shape_allowed(db):
    """Not every re-convergent graph is a cycle: two DIFFERENT successors of
    the same root, later both referenced by one merge proposal, is a DAG
    (diamond shape) and must be accepted."""
    root = await db_module.add_workspace_proposal(db, "root", "body")
    left = await db_module.add_workspace_proposal(db, "left", "body")
    right = await db_module.add_workspace_proposal(db, "right", "body")
    merged = await db_module.add_workspace_proposal(db, "merged", "body")
    await db_module.link_proposal_lineage(db, left["id"], root["id"], "forks")
    await db_module.link_proposal_lineage(db, right["id"], root["id"], "forks")
    await db_module.link_proposal_lineage(db, merged["id"], left["id"], "continues")
    # merged -> right is fine: right cannot reach merged (no path right->...->merged).
    row = await db_module.link_proposal_lineage(db, merged["id"], right["id"], "continues")
    assert row["to_proposal_id"] == right["id"]
    assert await _count(db, "proposal_lineage") == 4


# ---------------------------------------------------------------------------
# Version/sequence metadata + read helpers.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_proposal_lineage_sequence_increments_per_target(db):
    """sequence is a deterministic, monotonically increasing counter scoped
    to to_proposal_id -- the Nth relation recorded against a given
    proposal, in creation order."""
    root = await db_module.add_workspace_proposal(db, "root", "body")
    s1 = await db_module.add_workspace_proposal(db, "s1", "body")
    s2 = await db_module.add_workspace_proposal(db, "s2", "body")
    s3 = await db_module.add_workspace_proposal(db, "s3", "body")
    r1 = await db_module.link_proposal_lineage(db, s1["id"], root["id"], "supersedes")
    r2 = await db_module.link_proposal_lineage(db, s2["id"], root["id"], "refines")
    r3 = await db_module.link_proposal_lineage(db, s3["id"], root["id"], "duplicates")
    assert (r1["sequence"], r2["sequence"], r3["sequence"]) == (1, 2, 3)


@pytest.mark.asyncio
async def test_get_proposal_successors_ordered_by_sequence(db):
    root = await db_module.add_workspace_proposal(db, "root", "body")
    s1 = await db_module.add_workspace_proposal(db, "s1", "body")
    s2 = await db_module.add_workspace_proposal(db, "s2", "body")
    await db_module.link_proposal_lineage(db, s1["id"], root["id"], "supersedes")
    await db_module.link_proposal_lineage(db, s2["id"], root["id"], "refines")
    successors = await db_module.get_proposal_successors(db, root["id"])
    assert [s["from_proposal_id"] for s in successors] == [s1["id"], s2["id"]]


@pytest.mark.asyncio
async def test_get_proposal_lineage_links_both_directions(db):
    a = await db_module.add_workspace_proposal(db, "A", "body")
    b = await db_module.add_workspace_proposal(db, "B", "body")
    c = await db_module.add_workspace_proposal(db, "C", "body")
    await db_module.link_proposal_lineage(db, b["id"], a["id"], "supersedes")
    await db_module.link_proposal_lineage(db, c["id"], b["id"], "refines")
    # b appears as both a 'from' (b->a) and a 'to' (c->b) endpoint.
    links = await db_module.get_proposal_lineage_links(db, b["id"])
    assert len(links) == 2
    types = {(link["from_proposal_id"], link["to_proposal_id"]) for link in links}
    assert types == {(b["id"], a["id"]), (c["id"], b["id"])}


@pytest.mark.asyncio
async def test_get_proposal_ancestors_walks_deterministic_chain(db):
    """R -> S1 -> S2 -> S3 (each supersedes the previous): S3's ancestor
    chain is [S2, S1, R] in that order."""
    root = await db_module.add_workspace_proposal(db, "root", "body")
    s1 = await db_module.add_workspace_proposal(db, "s1", "body")
    s2 = await db_module.add_workspace_proposal(db, "s2", "body")
    s3 = await db_module.add_workspace_proposal(db, "s3", "body")
    await db_module.link_proposal_lineage(db, s1["id"], root["id"], "supersedes")
    await db_module.link_proposal_lineage(db, s2["id"], s1["id"], "supersedes")
    await db_module.link_proposal_lineage(db, s3["id"], s2["id"], "supersedes")

    chain = await db_module.get_proposal_ancestors(db, s3["id"])
    assert [c["to_proposal_id"] for c in chain] == [s2["id"], s1["id"], root["id"]]

    # A root with no outgoing relation has an empty ancestor chain.
    assert await db_module.get_proposal_ancestors(db, root["id"]) == []


@pytest.mark.asyncio
async def test_unlink_proposal_lineage_removes_row(db):
    p1 = await db_module.add_workspace_proposal(db, "A", "body")
    p2 = await db_module.add_workspace_proposal(db, "B", "body")
    row = await db_module.link_proposal_lineage(db, p2["id"], p1["id"], "supersedes")
    assert await db_module.unlink_proposal_lineage(db, row["id"]) is True
    assert await db_module.unlink_proposal_lineage(db, row["id"]) is False
    assert await _count(db, "proposal_lineage") == 0


# ---------------------------------------------------------------------------
# Regression — proposal_events / family_id / evidence links / promotion are
# provably UNCHANGED by this new, additive table.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposal_lineage_does_not_disturb_proposal_events_or_family_id(db):
    """Creating lineage relations alongside a proposal's normal lifecycle
    (create, update, promote) leaves proposal_events, family_id, and
    promoted_to_sprint_item_id exactly as they would be without this
    module."""
    project = await db_module.create_project(db, "lineage-regression")
    root = await db_module.add_workspace_proposal(
        db, "Root", "body", family_id="fam-123",
    )
    successor = await db_module.add_workspace_proposal(
        db, "Successor", "body", family_id="fam-123",
    )
    await db_module.append_proposal_update(db, successor["id"], "investigating this")
    await db_module.link_proposal_lineage(db, successor["id"], root["id"], "supersedes")

    # family_id is untouched -- still exactly what was passed at creation.
    fetched_root = (await db_module.get_workspace_proposals(db, status="all"))
    root_row = next(p for p in fetched_root if p["id"] == root["id"])
    succ_row = next(p for p in fetched_root if p["id"] == successor["id"])
    assert root_row["family_id"] == "fam-123"
    assert succ_row["family_id"] == "fam-123"

    # proposal_events carries exactly the events the existing lifecycle
    # produces (created + update) -- nothing extra written by lineage.
    async with db.execute(
        "SELECT event_type FROM proposal_events WHERE proposal_id = ? ORDER BY sequence",
        (successor["id"],),
    ) as cur:
        rows = await cur.fetchall()
    event_types = [r["event_type"] if isinstance(r, dict) else r[0] for r in rows]
    assert event_types == ["created", "update"]

    # Promotion still works and still creates exactly one evidence link, as
    # before -- unaffected by the lineage row that already exists for this
    # proposal.
    result = await db_module.promote_workspace_proposal(db, successor["id"], project["id"])
    assert result["sprint_item_id"]
    evidence = await db_module.get_proposal_evidence(db, project["id"], successor["id"])
    assert evidence["link_count"] == 1
    assert evidence["sprint_items"][0]["id"] == result["sprint_item_id"]

    # And the lineage row itself is still exactly there, untouched.
    lineage_links = await db_module.get_proposal_lineage_links(db, successor["id"])
    assert len(lineage_links) == 1
    assert lineage_links[0]["relation_type"] == "supersedes"
