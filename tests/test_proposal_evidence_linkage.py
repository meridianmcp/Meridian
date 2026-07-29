"""Tests for sprint item 6cdc5df3 — first-class proposal-to-evidence linkage.

Covers meridian.db.proposal_links (the durable, typed link primitive that
replaces the informal item_group-prefix convention for tying a proposal id to
notes/findings/sprint items/decisions/artifacts), its wiring into
promote_workspace_proposal (proposal-promotion coverage lives in
test_core.py, alongside the rest of the workspace-proposal lifecycle tests),
and the handoff read path (meridian.handoff.build_proposal_evidence_for_handoff
plus its MCP and HTTP surfaces) that answers "what's linked to proposal X".
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# Migration — table + indexes, idempotent; not inline in either base literal.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposal_evidence_links_migration_creates_table_and_indexes_idempotently():
    """The guarded SQLite migration creates the table + all three indexes, and
    is idempotent (safe to re-run). Exercised against a bare connection that
    has NEITHER to start, mirroring
    test_sprint_item_pointers_migration_creates_table_and_index_idempotently
    in tests/test_core.py."""
    import aiosqlite
    from meridian.db.proposal_links import _migrate_proposal_evidence_links

    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        await _migrate_proposal_evidence_links(conn)
        # Re-run must be a no-op (idempotent) and not raise.
        await _migrate_proposal_evidence_links(conn)

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='proposal_evidence_links'"
        ) as cur:
            assert await cur.fetchone() is not None
        for index_name in (
            "idx_proposal_evidence_links_unique",
            "idx_proposal_evidence_links_proposal",
            "idx_proposal_evidence_links_entity",
        ):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ) as cur:
                assert await cur.fetchone() is not None, index_name
        async with conn.execute("PRAGMA table_info(proposal_evidence_links)") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        assert cols == {
            "id", "project_id", "proposal_id", "entity_type", "entity_id",
            "label", "created_by", "created_at",
        }
    finally:
        await conn.close()


def test_proposal_evidence_links_not_inline_in_base_literals():
    """proposal_evidence_links must NOT appear in either base CREATE_TABLES
    literal — like docx_merge_manifests, the guarded migration is its ONLY
    creation path (fresh or upgrading DB alike), so there is no risk of an
    unguarded-index-on-a-migration-added-table startup crash (2026-07-04)."""
    from meridian.pg_adapter import CREATE_TABLES_CORE
    from meridian.db import CREATE_TABLES

    for name, literal in (
        ("CREATE_TABLES_CORE", CREATE_TABLES_CORE),
        ("CREATE_TABLES", CREATE_TABLES),
    ):
        assert "proposal_evidence_links" not in literal, name


@pytest.mark.asyncio
async def test_link_proposal_evidence_works_through_full_init_db(db):
    """Sanity check that the migration is actually wired into init_db's
    startup chain (not just directly callable) — the `db` fixture goes
    through the real init_db path."""
    project = await db_module.create_project(db, "proposal-migration-check")
    row = await db_module.link_proposal_evidence(
        db, project["id"], "prop-migration", "artifact", "outputs/report.docx",
        label="Migration check artifact",
    )
    assert row["proposal_id"] == "prop-migration"
    assert row["entity_type"] == "artifact"


# ---------------------------------------------------------------------------
# link_proposal_evidence — validation.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_proposal_evidence_invalid_entity_type_raises(db):
    project = await db_module.create_project(db, "link-invalid-type")
    with pytest.raises(ValueError, match="entity_type must be one of"):
        await db_module.link_proposal_evidence(
            db, project["id"], "prop-1", "bogus-type", "whatever",
        )


@pytest.mark.asyncio
async def test_link_proposal_evidence_empty_proposal_id_raises(db):
    project = await db_module.create_project(db, "link-empty-proposal")
    note = await db_module.add_project_note(db, project["id"], "N", "body")
    with pytest.raises(ValueError, match="proposal_id must be a non-empty string"):
        await db_module.link_proposal_evidence(
            db, project["id"], "   ", "note", note["id"],
        )


@pytest.mark.asyncio
async def test_link_proposal_evidence_empty_entity_id_raises(db):
    project = await db_module.create_project(db, "link-empty-entity")
    with pytest.raises(ValueError, match="entity_id must be a non-empty string"):
        await db_module.link_proposal_evidence(
            db, project["id"], "prop-1", "note", "",
        )


@pytest.mark.asyncio
async def test_link_proposal_evidence_missing_entity_raises(db):
    """A note/finding/sprint_item/decision entity_id that does not exist is
    rejected — a link can never silently point at nothing."""
    project = await db_module.create_project(db, "link-missing-entity")
    with pytest.raises(ValueError, match="does not exist"):
        await db_module.link_proposal_evidence(
            db, project["id"], "prop-1", "note", "nonexistent-note-id",
        )


@pytest.mark.asyncio
async def test_link_proposal_evidence_cross_project_raises(db):
    """A note that exists but belongs to a DIFFERENT project is rejected."""
    project_a = await db_module.create_project(db, "link-cross-a")
    project_b = await db_module.create_project(db, "link-cross-b")
    note = await db_module.add_project_note(db, project_a["id"], "N", "body")
    with pytest.raises(ValueError, match="belongs to a different project"):
        await db_module.link_proposal_evidence(
            db, project_b["id"], "prop-1", "note", note["id"],
        )


@pytest.mark.asyncio
async def test_link_proposal_evidence_artifact_has_no_entity_existence_check(db):
    """'artifact' has no backing table — any non-empty entity_id is accepted,
    with label carrying the human-readable description."""
    project = await db_module.create_project(db, "link-artifact-freeform")
    row = await db_module.link_proposal_evidence(
        db, project["id"], "prop-artifact", "artifact",
        "outputs/quarterly_report.docx", label="Q3 merged financial report",
    )
    assert row["entity_id"] == "outputs/quarterly_report.docx"
    assert row["label"] == "Q3 merged financial report"


# ---------------------------------------------------------------------------
# link_proposal_evidence — one link per evidence kind.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_proposal_evidence_note(db):
    project = await db_module.create_project(db, "link-note")
    note = await db_module.add_project_note(db, project["id"], "Investigation notes", "body text")
    link = await db_module.link_proposal_evidence(
        db, project["id"], "prop-note", "note", note["id"], label="Investigation notes",
    )
    assert link["entity_type"] == "note"
    assert link["entity_id"] == note["id"]
    assert link["proposal_id"] == "prop-note"
    assert link["project_id"] == project["id"]


@pytest.mark.asyncio
async def test_link_proposal_evidence_finding(db):
    project = await db_module.create_project(db, "link-finding")
    finding = await db_module.store_finding(
        db, project["id"], "the root cause is X", title="Root cause finding",
    )
    link = await db_module.link_proposal_evidence(
        db, project["id"], "prop-finding", "finding", finding["id"],
    )
    assert link["entity_type"] == "finding"
    assert link["entity_id"] == finding["id"]


@pytest.mark.asyncio
async def test_link_proposal_evidence_sprint_item(db):
    project = await db_module.create_project(db, "link-sprint-item")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Ship the thing")
    link = await db_module.link_proposal_evidence(
        db, project["id"], "prop-item", "sprint_item", item["id"],
    )
    assert link["entity_type"] == "sprint_item"
    assert link["entity_id"] == item["id"]


@pytest.mark.asyncio
async def test_link_proposal_evidence_decision(db):
    project = await db_module.create_project(db, "link-decision")
    decision = await db_module.pin_decision(
        db, project["id"], "Use psycopg3", "asyncpg has DLL issues on Windows",
    )
    link = await db_module.link_proposal_evidence(
        db, project["id"], "prop-decision", "decision", decision["id"],
    )
    assert link["entity_type"] == "decision"
    assert link["entity_id"] == decision["id"]


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_proposal_evidence_is_idempotent(db):
    """Linking the exact same (project, proposal, entity_type, entity_id)
    tuple twice returns the SAME row and never duplicates it."""
    project = await db_module.create_project(db, "link-idempotent")
    note = await db_module.add_project_note(db, project["id"], "N", "body")
    first = await db_module.link_proposal_evidence(
        db, project["id"], "prop-dup", "note", note["id"],
    )
    second = await db_module.link_proposal_evidence(
        db, project["id"], "prop-dup", "note", note["id"], label="different label",
    )
    assert first["id"] == second["id"]
    links = await db_module.get_proposal_links(db, project["id"], "prop-dup")
    assert len(links) == 1


@pytest.mark.asyncio
async def test_link_proposal_evidence_same_entity_different_proposal_is_two_links(db):
    """The SAME note linked under two DIFFERENT proposal ids produces two
    independent link rows (the uniqueness is per-proposal, not per-entity)."""
    project = await db_module.create_project(db, "link-two-proposals")
    note = await db_module.add_project_note(db, project["id"], "Shared note", "body")
    await db_module.link_proposal_evidence(db, project["id"], "prop-x", "note", note["id"])
    await db_module.link_proposal_evidence(db, project["id"], "prop-y", "note", note["id"])
    ev_x = await db_module.get_proposal_evidence(db, project["id"], "prop-x")
    ev_y = await db_module.get_proposal_evidence(db, project["id"], "prop-y")
    assert len(ev_x["notes"]) == 1
    assert len(ev_y["notes"]) == 1


# ---------------------------------------------------------------------------
# Core acceptance case — query EVERYTHING linked to one proposal id, across
# all five evidence kinds, in a single call.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_proposal_evidence_combines_all_entity_types(db):
    project = await db_module.create_project(db, "evidence-full-bundle")
    proposal_id = "b7308039"  # mirrors the real informal-prefix ids cited in AGENTS.md

    note = await db_module.add_project_note(db, project["id"], "Design note", "body")
    finding = await db_module.store_finding(db, project["id"], "measured 3x speedup")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Implement the fix")
    decision = await db_module.pin_decision(db, project["id"], "Adopt approach A", "because")

    await db_module.link_proposal_evidence(db, project["id"], proposal_id, "note", note["id"])
    await db_module.link_proposal_evidence(db, project["id"], proposal_id, "finding", finding["id"])
    await db_module.link_proposal_evidence(db, project["id"], proposal_id, "sprint_item", item["id"])
    await db_module.link_proposal_evidence(db, project["id"], proposal_id, "decision", decision["id"])
    await db_module.link_proposal_evidence(
        db, project["id"], proposal_id, "artifact", "outputs/design.docx",
        label="Design doc export",
    )

    evidence = await db_module.get_proposal_evidence(db, project["id"], proposal_id)
    assert evidence["proposal_id"] == proposal_id
    assert evidence["project_id"] == project["id"]
    assert evidence["link_count"] == 5
    assert evidence["unresolved"] == []

    assert len(evidence["notes"]) == 1
    assert evidence["notes"][0]["id"] == note["id"]
    assert evidence["notes"][0]["_proposal_link_label"] is None

    assert len(evidence["findings"]) == 1
    assert evidence["findings"][0]["id"] == finding["id"]

    assert len(evidence["sprint_items"]) == 1
    assert evidence["sprint_items"][0]["id"] == item["id"]

    assert len(evidence["decisions"]) == 1
    assert evidence["decisions"][0]["id"] == decision["id"]

    assert len(evidence["artifacts"]) == 1
    assert evidence["artifacts"][0]["entity_id"] == "outputs/design.docx"
    assert evidence["artifacts"][0]["label"] == "Design doc export"


@pytest.mark.asyncio
async def test_get_proposal_evidence_empty_proposal_returns_empty_buckets(db):
    project = await db_module.create_project(db, "evidence-empty")
    evidence = await db_module.get_proposal_evidence(db, project["id"], "prop-never-linked")
    assert evidence["link_count"] == 0
    assert evidence["notes"] == []
    assert evidence["findings"] == []
    assert evidence["sprint_items"] == []
    assert evidence["decisions"] == []
    assert evidence["artifacts"] == []
    assert evidence["unresolved"] == []


@pytest.mark.asyncio
async def test_get_proposal_evidence_surfaces_unresolved_when_target_deleted(db):
    """A link whose target row was hard-deleted after linking is surfaced
    under 'unresolved', not silently dropped."""
    project = await db_module.create_project(db, "evidence-unresolved")
    note = await db_module.add_project_note(db, project["id"], "Will be deleted", "body")
    await db_module.link_proposal_evidence(db, project["id"], "prop-stale", "note", note["id"])

    # Hard-delete the note directly, bypassing any app-level guard.
    await db.execute("DELETE FROM project_notes WHERE id = ?", (note["id"],))
    await db.commit()

    evidence = await db_module.get_proposal_evidence(db, project["id"], "prop-stale")
    assert evidence["notes"] == []
    assert evidence["link_count"] == 1
    assert len(evidence["unresolved"]) == 1
    assert evidence["unresolved"][0]["entity_id"] == note["id"]


@pytest.mark.asyncio
async def test_get_proposal_evidence_scoped_to_project(db):
    """A proposal id linked in one project is invisible when queried under a
    different project_id — links are project-scoped, not global."""
    project_a = await db_module.create_project(db, "evidence-scope-a")
    project_b = await db_module.create_project(db, "evidence-scope-b")
    note = await db_module.add_project_note(db, project_a["id"], "N", "body")
    await db_module.link_proposal_evidence(
        db, project_a["id"], "prop-shared-id", "note", note["id"],
    )
    ev_a = await db_module.get_proposal_evidence(db, project_a["id"], "prop-shared-id")
    ev_b = await db_module.get_proposal_evidence(db, project_b["id"], "prop-shared-id")
    assert ev_a["link_count"] == 1
    assert ev_b["link_count"] == 0


# ---------------------------------------------------------------------------
# get_proposal_ids_for_project — reverse discovery.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_proposal_ids_for_project_lists_distinct_ids(db):
    project = await db_module.create_project(db, "proposal-ids-list")
    note_a = await db_module.add_project_note(db, project["id"], "A", "body")
    note_b = await db_module.add_project_note(db, project["id"], "B", "body")
    await db_module.link_proposal_evidence(db, project["id"], "prop-a", "note", note_a["id"])
    await db_module.link_proposal_evidence(db, project["id"], "prop-b", "note", note_b["id"])
    # Second link under prop-a should not duplicate it in the id list.
    await db_module.link_proposal_evidence(db, project["id"], "prop-a", "note", note_b["id"])

    ids = await db_module.get_proposal_ids_for_project(db, project["id"])
    assert set(ids) == {"prop-a", "prop-b"}


@pytest.mark.asyncio
async def test_get_proposal_ids_for_project_empty_when_no_links(db):
    project = await db_module.create_project(db, "proposal-ids-empty")
    ids = await db_module.get_proposal_ids_for_project(db, project["id"])
    assert ids == []


# ---------------------------------------------------------------------------
# unlink_proposal_evidence.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unlink_proposal_evidence_removes_row(db):
    project = await db_module.create_project(db, "unlink-basic")
    note = await db_module.add_project_note(db, project["id"], "N", "body")
    link = await db_module.link_proposal_evidence(
        db, project["id"], "prop-unlink", "note", note["id"],
    )
    removed = await db_module.unlink_proposal_evidence(db, link["id"])
    assert removed is True
    evidence = await db_module.get_proposal_evidence(db, project["id"], "prop-unlink")
    assert evidence["link_count"] == 0


@pytest.mark.asyncio
async def test_unlink_proposal_evidence_missing_id_returns_false(db):
    removed = await db_module.unlink_proposal_evidence(db, "nonexistent-link-id")
    assert removed is False


# ---------------------------------------------------------------------------
# Handoff read path — "what's linked to proposal X" from generate_handoff.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_proposal_evidence_for_handoff_returns_hydrated_list(db):
    project = await db_module.create_project(db, "handoff-evidence-hydrated")
    note = await db_module.add_project_note(db, project["id"], "Handoff-linked note", "body")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Handoff-linked item")
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-handoff", "note", note["id"],
    )
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-handoff", "sprint_item", item["id"],
    )

    result = await handoff_module.build_proposal_evidence_for_handoff(db, project["id"])
    assert result is not None
    assert len(result) == 1
    bundle = result[0]
    assert bundle["proposal_id"] == "prop-handoff"
    assert len(bundle["notes"]) == 1
    assert bundle["notes"][0]["id"] == note["id"]
    assert len(bundle["sprint_items"]) == 1
    assert bundle["sprint_items"][0]["id"] == item["id"]

    # The whole bundle must survive a JSON round trip unchanged — this is
    # what "queryable from a handoff" actually requires: the linkage
    # serializes correctly through the same read path a real handoff caller
    # (MCP / HTTP) uses.
    reloaded = _json.loads(_json.dumps(result))
    assert reloaded == result


@pytest.mark.asyncio
async def test_build_proposal_evidence_for_handoff_empty_when_no_links(db):
    project = await db_module.create_project(db, "handoff-evidence-empty")
    result = await handoff_module.build_proposal_evidence_for_handoff(db, project["id"])
    assert result == []


@pytest.mark.asyncio
async def test_build_proposal_evidence_for_handoff_never_raises(db, monkeypatch):
    project = await db_module.create_project(db, "handoff-evidence-guarded")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(db_module, "get_proposal_ids_for_project", _boom)
    result = await handoff_module.build_proposal_evidence_for_handoff(db, project["id"])
    assert result is None


# ---------------------------------------------------------------------------
# MCP tool surface — generate_handoff includes proposal_evidence.
# ---------------------------------------------------------------------------

def _mcp_call(client, name, arguments):
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert r.status_code == 200
    return r.json()


def _result(resp):
    assert resp.get("result") is not None, resp
    return _json.loads(resp["result"]["content"][0]["text"])


@pytest.mark.parametrize("mode", ["full", "goal"])
def test_mcp_generate_handoff_includes_proposal_evidence_field(client, mode):
    """Every generate_handoff mode carries the (possibly empty)
    proposal_evidence field, mirroring capability_contract's coverage."""
    pid = client.post("/projects", json={"name": f"mcp-proposal-evidence-{mode}"}).json()["id"]
    result = _result(_mcp_call(client, "generate_handoff", {
        "project_id": pid, "mode": mode,
    }))
    assert "proposal_evidence" in result
    assert result["proposal_evidence"] == []


def test_mcp_generate_handoff_surfaces_promoted_proposal_evidence(client):
    """End-to-end via public MCP tools only: add_workspace_proposal ->
    promote_proposal (which now auto-links the resulting sprint item, see
    db.workspace.promote_workspace_proposal) -> generate_handoff must surface
    that linkage under proposal_evidence. This is the concrete "queryable
    from a handoff" acceptance case: nothing here talks to the DB layer
    directly, only the same MCP surface a real executor session uses."""
    pid = client.post("/projects", json={"name": "mcp-proposal-evidence-promoted"}).json()["id"]

    proposal = _result(_mcp_call(client, "add_workspace_proposal", {
        "title": "Ship the linkage feature", "body": "context for the proposal",
    }))
    promo = _result(_mcp_call(client, "promote_proposal", {
        "proposal_id": proposal["id"], "project_id": pid,
    }))
    assert "error" not in promo
    sprint_item_id = promo["sprint_item_id"]

    result = _result(_mcp_call(client, "generate_handoff", {
        "project_id": pid, "mode": "full",
    }))
    bundles = result["proposal_evidence"]
    assert bundles, "expected the promoted proposal's evidence to be surfaced"
    matching = [b for b in bundles if b["proposal_id"] == proposal["id"]]
    assert len(matching) == 1
    bundle = matching[0]
    assert any(si["id"] == sprint_item_id for si in bundle["sprint_items"])


# ---------------------------------------------------------------------------
# HTTP surface — POST /projects/{id}/handoff includes proposal_evidence.
# ---------------------------------------------------------------------------

def test_http_handoff_endpoint_includes_proposal_evidence_field(client):
    pid = client.post("/projects", json={"name": "http-proposal-evidence"}).json()["id"]
    r = client.post(f"/projects/{pid}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert "proposal_evidence" in body
    assert body["proposal_evidence"] == []


def test_http_planner_handoff_includes_proposal_evidence_field(client):
    pid = client.post("/projects", json={"name": "http-proposal-evidence-planner"}).json()["id"]
    r = client.get(f"/projects/{pid}/handoff/planner")
    assert r.status_code == 200
    body = r.json()
    assert "proposal_evidence" in body
