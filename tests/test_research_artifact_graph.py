"""Tests for sprint item b558892a — the research artifact graph: typed
nodes/edges linking claims, citations, code, runs, outputs, documents, and
executor decisions.

Covers:
  * meridian.research_graph — the pure closed-vocabulary/identity-key layer.
  * meridian.db.research_graph — the persistence layer (create_node /
    replace_node_revision / create_edge / get_current_node /
    list_node_revisions / get_unresolved_edges / get_claim_evidence /
    get_artifact_document_lineage), on both SQLite (default) and Postgres
    (via the `anydb` fixture, skipped locally without TEST_DATABASE_URL).
  * The two small, dependency-free identity-helper functions added to the
    meridian-docs and meridian-outputs extension packages so a caller with
    both installed can build a research-graph-shaped identity reference
    without either extension taking on a core dependency.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from meridian import db as db_module
from meridian import research_graph as rg


async def _count(db, table: str) -> int:
    async with db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


# ---------------------------------------------------------------------------
# meridian.research_graph — pure vocabulary + identity-key builders.
# ---------------------------------------------------------------------------


def test_node_types_and_edge_types_are_the_documented_sets():
    assert rg.NODE_TYPES == {
        "claim", "citation", "code", "run", "output", "document", "decision",
    }
    assert rg.EDGE_TYPES == {
        "supports", "contradicts", "evidences", "cites", "produces",
        "derived_from", "documents", "implements", "references",
    }
    # Every edge kind is documented in EDGE_DIRECTIONALITY -- no silent gaps.
    assert set(rg.EDGE_DIRECTIONALITY) == rg.EDGE_TYPES


def test_validate_node_type_accepts_all_and_rejects_unknown():
    for nt in rg.NODE_TYPES:
        assert rg.validate_node_type(nt) == nt
        assert rg.validate_node_type(nt.upper()) == nt  # case-insensitive
    with pytest.raises(ValueError, match="node_type must be one of"):
        rg.validate_node_type("bogus")
    with pytest.raises(ValueError, match="node_type must be one of"):
        rg.validate_node_type(None)


def test_validate_edge_kind_accepts_all_and_rejects_unknown():
    for ek in rg.EDGE_TYPES:
        assert rg.validate_edge_kind(ek) == ek
    with pytest.raises(ValueError, match="edge_kind must be one of"):
        rg.validate_edge_kind("bogus-kind")


def test_code_identity_key():
    assert rg.code_identity_key("meridian/db/__init__.py") == "meridian/db/__init__.py"
    assert (
        rg.code_identity_key("meridian/db/__init__.py", "get_project")
        == "meridian/db/__init__.py::get_project"
    )
    with pytest.raises(ValueError, match="non-empty file_path"):
        rg.code_identity_key("")


def test_output_identity_key_prefers_sha256_over_path():
    assert rg.output_identity_key(sha256="deadbeef") == "sha256:deadbeef"
    assert rg.output_identity_key(path="out/foo.csv") == "out/foo.csv"
    assert (
        rg.output_identity_key(path="out/foo.csv", sha256="deadbeef")
        == "sha256:deadbeef"
    )
    with pytest.raises(ValueError, match="requires at least one of"):
        rg.output_identity_key()


def test_document_identity_key():
    assert rg.document_identity_key("report.docx") == "report.docx"
    assert rg.document_identity_key("report.docx", "p42") == "report.docx::p42"
    with pytest.raises(ValueError, match="non-empty source"):
        rg.document_identity_key("")


def test_citation_identity_key_preference_order():
    assert rg.citation_identity_key(zotero_key="ABCD1234") == "zotero:ABCD1234"
    assert rg.citation_identity_key(doi="10.1/xyz") == "doi:10.1/xyz"
    assert rg.citation_identity_key(raw="Smith 2020") == "Smith 2020"
    assert (
        rg.citation_identity_key(zotero_key="Z1", doi="10.1/xyz") == "zotero:Z1"
    )
    with pytest.raises(ValueError, match="requires one of"):
        rg.citation_identity_key()


def test_run_decision_claim_identity_keys():
    assert rg.run_identity_key("run-1") == "run-1"
    assert rg.decision_identity_key("dec-1") == "dec-1"
    assert rg.claim_identity_key("claim-1") == "claim-1"
    with pytest.raises(ValueError):
        rg.run_identity_key("  ")
    with pytest.raises(ValueError):
        rg.decision_identity_key("")
    with pytest.raises(ValueError):
        rg.claim_identity_key(None)


# ---------------------------------------------------------------------------
# Migration — table + indexes, idempotent; not inline in either base literal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_graph_migration_creates_tables_and_indexes_idempotently():
    import aiosqlite

    from meridian.db.research_graph import _migrate_research_graph

    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        await _migrate_research_graph(conn)
        await _migrate_research_graph(conn)  # re-run must be a no-op

        for table_name in ("research_nodes", "research_edges"):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ) as cur:
                assert await cur.fetchone() is not None, table_name
        for index_name in (
            "idx_research_nodes_identity_revision",
            "idx_research_nodes_identity",
            "idx_research_nodes_project",
            "idx_research_edges_unique",
            "idx_research_edges_from",
            "idx_research_edges_to",
        ):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ) as cur:
                assert await cur.fetchone() is not None, index_name

        async with conn.execute("PRAGMA table_info(research_nodes)") as cur:
            node_cols = {r["name"] for r in await cur.fetchall()}
        assert node_cols == {
            "id", "project_id", "node_type", "identity_key", "external_ref",
            "revision", "title", "status", "seq", "supersedes_id",
            "superseded_by", "created_by", "created_at", "updated_at",
        }
        async with conn.execute("PRAGMA table_info(research_edges)") as cur:
            edge_cols = {r["name"] for r in await cur.fetchall()}
        assert edge_cols == {
            "id", "project_id", "edge_kind", "from_node_type",
            "from_identity_key", "from_node_id", "to_node_type",
            "to_identity_key", "to_node_id", "label", "created_by",
            "created_at", "resolved_at",
        }
    finally:
        await conn.close()


def test_research_graph_not_inline_in_base_literals():
    from meridian.db import CREATE_TABLES
    from meridian.pg_adapter import CREATE_TABLES_CORE

    for name, literal in (
        ("CREATE_TABLES_CORE", CREATE_TABLES_CORE),
        ("CREATE_TABLES", CREATE_TABLES),
    ):
        assert "research_nodes" not in literal, name
        assert "research_edges" not in literal, name


@pytest.mark.asyncio
async def test_research_graph_wired_into_full_init_db(db):
    """Sanity check the migration is actually wired into init_db's startup
    chain (not just directly callable) -- the `db` fixture goes through the
    real init_db path."""
    project = await db_module.create_project(db, "rg-wiring")
    node = await db_module.create_node(
        db, project["id"], "code", "meridian/foo.py", revision="sha1",
    )
    assert node["node_type"] == "code"
    assert node["status"] == "active"


# ---------------------------------------------------------------------------
# create_node — validation, idempotency, append-only semantics.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_node_rejects_unknown_node_type(db):
    project = await db_module.create_project(db, "rg-1")
    with pytest.raises(ValueError, match="node_type must be one of"):
        await db_module.create_node(db, project["id"], "bogus", "x")


@pytest.mark.asyncio
async def test_create_node_rejects_blank_identity_key(db):
    project = await db_module.create_project(db, "rg-2")
    with pytest.raises(ValueError, match="non-empty identity_key"):
        await db_module.create_node(db, project["id"], "code", "   ")


@pytest.mark.asyncio
async def test_create_node_rejects_secret_looking_title(db):
    project = await db_module.create_project(db, "rg-3")
    with pytest.raises(ValueError, match="Refusing to persist"):
        await db_module.create_node(
            db, project["id"], "code", "meridian/foo.py",
            title="sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )


@pytest.mark.asyncio
async def test_create_node_all_node_types_accepted(db):
    project = await db_module.create_project(db, "rg-4")
    for i, nt in enumerate(sorted(rg.NODE_TYPES)):
        node = await db_module.create_node(db, project["id"], nt, f"identity-{i}")
        assert node["node_type"] == nt
        assert node["status"] == "active"


@pytest.mark.asyncio
async def test_create_node_repeat_call_same_identity_and_revision_is_a_noop(db):
    project = await db_module.create_project(db, "rg-5")
    first = await db_module.create_node(
        db, project["id"], "output", "sha256:aaa", revision="aaa",
    )
    second = await db_module.create_node(
        db, project["id"], "output", "sha256:aaa", revision="aaa",
    )
    assert first["id"] == second["id"]
    assert await _count(db, "research_nodes") == 1


@pytest.mark.asyncio
async def test_create_node_different_revision_is_a_new_row_both_active(db):
    """Pure append-only path: two revisions of the SAME identity, neither
    explicitly superseded, both coexist as 'active' -- the acceptance
    criteria's 'append-only' write mode."""
    project = await db_module.create_project(db, "rg-6")
    r1 = await db_module.create_node(
        db, project["id"], "code", "meridian/foo.py", revision="sha1",
    )
    r2 = await db_module.create_node(
        db, project["id"], "code", "meridian/foo.py", revision="sha2",
    )
    assert r1["id"] != r2["id"]
    assert r1["status"] == "active"
    assert r2["status"] == "active"
    assert await _count(db, "research_nodes") == 2
    # get_current_node deterministically resolves to the NEWEST (highest seq).
    current = await db_module.get_current_node(db, project["id"], "code", "meridian/foo.py")
    assert current["id"] == r2["id"]


@pytest.mark.asyncio
async def test_create_node_concurrent_calls_yield_one_row(db):
    project = await db_module.create_project(db, "rg-7")
    n = 8

    async def _attempt():
        return await db_module.create_node(
            db, project["id"], "citation", "zotero:ABCD", revision="v1",
        )

    results = await asyncio.gather(*[_attempt() for _ in range(n)])
    ids = {r["id"] for r in results}
    assert len(ids) == 1
    assert await _count(db, "research_nodes") == 1


@pytest.mark.asyncio
async def test_create_node_stores_and_round_trips_external_ref(db):
    project = await db_module.create_project(db, "rg-8")
    node = await db_module.create_node(
        db, project["id"], "citation", "zotero:ABCD",
        external_ref={"zotero_key": "ABCD", "title": "A paper"},
    )
    assert node["external_ref"] == {"zotero_key": "ABCD", "title": "A paper"}
    fetched = await db_module.get_node(db, project["id"], node["id"])
    assert fetched["external_ref"] == {"zotero_key": "ABCD", "title": "A paper"}


@pytest.mark.asyncio
async def test_list_node_revisions_full_history_in_order(db):
    project = await db_module.create_project(db, "rg-9")
    r1 = await db_module.create_node(db, project["id"], "code", "f.py", revision="v1")
    r2 = await db_module.create_node(db, project["id"], "code", "f.py", revision="v2")
    r3 = await db_module.create_node(db, project["id"], "code", "f.py", revision="v3")
    history = await db_module.list_node_revisions(db, project["id"], "code", "f.py")
    assert [h["id"] for h in history] == [r1["id"], r2["id"], r3["id"]]


# ---------------------------------------------------------------------------
# replace_node_revision — transactionally replaceable write semantics.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_node_revision_atomically_supersedes_old_row(db):
    project = await db_module.create_project(db, "rg-10")
    old = await db_module.create_node(
        db, project["id"], "output", "out/report.csv", revision="v1",
        title="report v1",
    )
    new = await db_module.replace_node_revision(
        db, project["id"], old["id"], revision="v2", title="report v2",
    )
    assert new["id"] != old["id"]
    assert new["status"] == "active"
    assert new["supersedes_id"] == old["id"]
    assert new["title"] == "report v2"
    # identity_key/node_type are inherited from the old row.
    assert new["node_type"] == "output"
    assert new["identity_key"] == "out/report.csv"

    refreshed_old = await db_module.get_node(db, project["id"], old["id"])
    assert refreshed_old["status"] == "superseded"
    assert refreshed_old["superseded_by"] == new["id"]

    current = await db_module.get_current_node(db, project["id"], "output", "out/report.csv")
    assert current["id"] == new["id"]

    # Both rows persist -- nothing hard-deleted.
    history = await db_module.list_node_revisions(db, project["id"], "output", "out/report.csv")
    assert {h["id"] for h in history} == {old["id"], new["id"]}


@pytest.mark.asyncio
async def test_replace_node_revision_inherits_external_ref_when_omitted(db):
    project = await db_module.create_project(db, "rg-11")
    old = await db_module.create_node(
        db, project["id"], "document", "report.docx",
        external_ref={"source": "report.docx"},
        revision="hash1",
    )
    new = await db_module.replace_node_revision(
        db, project["id"], old["id"], revision="hash2",
    )
    assert new["external_ref"] == {"source": "report.docx"}


@pytest.mark.asyncio
async def test_replace_node_revision_unknown_id_raises(db):
    project = await db_module.create_project(db, "rg-12")
    with pytest.raises(ValueError, match="not found"):
        await db_module.replace_node_revision(db, project["id"], "nope-not-real", revision="v2")


@pytest.mark.asyncio
async def test_replace_node_revision_already_superseded_raises(db):
    project = await db_module.create_project(db, "rg-13")
    old = await db_module.create_node(db, project["id"], "code", "f.py", revision="v1")
    new = await db_module.replace_node_revision(db, project["id"], old["id"], revision="v2")
    with pytest.raises(ValueError, match="already superseded"):
        await db_module.replace_node_revision(db, project["id"], old["id"], revision="v3")
    # the still-current row can still be replaced fine.
    newer = await db_module.replace_node_revision(db, project["id"], new["id"], revision="v3")
    assert newer["supersedes_id"] == new["id"]


# ---------------------------------------------------------------------------
# create_edge — validation, idempotency, self-loop rejection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_edge_rejects_unknown_edge_kind(db):
    project = await db_module.create_project(db, "rg-14")
    with pytest.raises(ValueError, match="edge_kind must be one of"):
        await db_module.create_edge(
            db, project["id"], "bogus",
            {"node_type": "code", "identity_key": "a"},
            {"node_type": "claim", "identity_key": "b"},
        )


@pytest.mark.asyncio
async def test_create_edge_rejects_self_loop(db):
    project = await db_module.create_project(db, "rg-15")
    with pytest.raises(ValueError, match="itself"):
        await db_module.create_edge(
            db, project["id"], "cites",
            {"node_type": "claim", "identity_key": "same"},
            {"node_type": "claim", "identity_key": "same"},
        )


@pytest.mark.asyncio
async def test_create_edge_rejects_malformed_ref(db):
    project = await db_module.create_project(db, "rg-16")
    with pytest.raises(ValueError, match="from_ref"):
        await db_module.create_edge(
            db, project["id"], "cites", "not-a-dict",
            {"node_type": "citation", "identity_key": "z"},
        )
    with pytest.raises(ValueError, match="non-empty identity_key"):
        await db_module.create_edge(
            db, project["id"], "cites",
            {"node_type": "claim", "identity_key": ""},
            {"node_type": "citation", "identity_key": "z"},
        )


@pytest.mark.asyncio
async def test_create_edge_repeat_call_is_a_noop(db):
    project = await db_module.create_project(db, "rg-17")
    from_ref = {"node_type": "code", "identity_key": "f.py"}
    to_ref = {"node_type": "claim", "identity_key": "c1"}
    first = await db_module.create_edge(db, project["id"], "supports", from_ref, to_ref)
    second = await db_module.create_edge(db, project["id"], "supports", from_ref, to_ref)
    assert first["id"] == second["id"]
    assert await _count(db, "research_edges") == 1


@pytest.mark.asyncio
async def test_create_edge_different_kind_is_a_new_row(db):
    project = await db_module.create_project(db, "rg-18")
    from_ref = {"node_type": "code", "identity_key": "f.py"}
    to_ref = {"node_type": "claim", "identity_key": "c1"}
    await db_module.create_edge(db, project["id"], "supports", from_ref, to_ref)
    await db_module.create_edge(db, project["id"], "contradicts", from_ref, to_ref)
    assert await _count(db, "research_edges") == 2


@pytest.mark.asyncio
async def test_create_edge_rejects_secret_looking_label(db):
    project = await db_module.create_project(db, "rg-19")
    with pytest.raises(ValueError, match="Refusing to persist"):
        await db_module.create_edge(
            db, project["id"], "cites",
            {"node_type": "claim", "identity_key": "c1"},
            {"node_type": "citation", "identity_key": "z1"},
            label="sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )


# ---------------------------------------------------------------------------
# Unresolved edges — created before either endpoint exists, auto-resolved
# once a matching node is created.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge_created_before_nodes_is_unresolved(db):
    project = await db_module.create_project(db, "rg-20")
    edge = await db_module.create_edge(
        db, project["id"], "supports",
        {"node_type": "code", "identity_key": "f.py"},
        {"node_type": "claim", "identity_key": "c1"},
    )
    assert edge["from_node_id"] is None
    assert edge["to_node_id"] is None
    assert edge["resolved_at"] is None

    unresolved = await db_module.get_unresolved_edges(db, project["id"])
    assert len(unresolved) == 1
    assert unresolved[0]["id"] == edge["id"]


@pytest.mark.asyncio
async def test_unresolved_edge_auto_resolves_when_node_appears(db):
    project = await db_module.create_project(db, "rg-21")
    await db_module.create_edge(
        db, project["id"], "supports",
        {"node_type": "code", "identity_key": "f.py"},
        {"node_type": "claim", "identity_key": "c1"},
    )
    assert len(await db_module.get_unresolved_edges(db, project["id"])) == 1

    # Creating just ONE side partially resolves (still unresolved overall).
    code_node = await db_module.create_node(db, project["id"], "code", "f.py")
    partially = await db_module.get_unresolved_edges(db, project["id"])
    assert len(partially) == 1
    assert partially[0]["from_node_id"] == code_node["id"]
    assert partially[0]["to_node_id"] is None
    assert partially[0]["resolved_at"] is None

    # Creating the OTHER side fully resolves it.
    claim_node = await db_module.create_node(db, project["id"], "claim", "c1")
    assert await db_module.get_unresolved_edges(db, project["id"]) == []
    edges = await db_module.get_edges_for_identity(db, project["id"], "claim", "c1")
    assert edges[0]["to_node_id"] == claim_node["id"]
    assert edges[0]["resolved_at"] is not None


@pytest.mark.asyncio
async def test_get_unresolved_edges_filters_by_edge_kind(db):
    project = await db_module.create_project(db, "rg-22")
    await db_module.create_edge(
        db, project["id"], "supports",
        {"node_type": "code", "identity_key": "f.py"},
        {"node_type": "claim", "identity_key": "c1"},
    )
    await db_module.create_edge(
        db, project["id"], "cites",
        {"node_type": "claim", "identity_key": "c1"},
        {"node_type": "citation", "identity_key": "z1"},
    )
    supports_only = await db_module.get_unresolved_edges(db, project["id"], edge_kind="supports")
    assert len(supports_only) == 1
    assert supports_only[0]["edge_kind"] == "supports"


@pytest.mark.asyncio
async def test_get_edges_for_identity_role_filter(db):
    project = await db_module.create_project(db, "rg-23")
    await db_module.create_edge(
        db, project["id"], "supports",
        {"node_type": "code", "identity_key": "f.py"},
        {"node_type": "claim", "identity_key": "c1"},
    )
    await db_module.create_edge(
        db, project["id"], "cites",
        {"node_type": "claim", "identity_key": "c1"},
        {"node_type": "citation", "identity_key": "z1"},
    )
    both = await db_module.get_edges_for_identity(db, project["id"], "claim", "c1")
    assert len(both) == 2
    as_to = await db_module.get_edges_for_identity(db, project["id"], "claim", "c1", role="to")
    assert len(as_to) == 1
    assert as_to[0]["edge_kind"] == "supports"
    as_from = await db_module.get_edges_for_identity(db, project["id"], "claim", "c1", role="from")
    assert len(as_from) == 1
    assert as_from[0]["edge_kind"] == "cites"
    with pytest.raises(ValueError, match="role must be"):
        await db_module.get_edges_for_identity(db, project["id"], "claim", "c1", role="sideways")


# ---------------------------------------------------------------------------
# claim-to-evidence query.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_claim_evidence_returns_resolved_and_unresolved(db):
    project = await db_module.create_project(db, "rg-24")
    pid = project["id"]
    await db_module.create_node(db, pid, "claim", "c1", title="X causes Y")
    code_node = await db_module.create_node(db, pid, "code", "experiment.py", revision="sha1")

    await db_module.create_edge(
        db, pid, "supports",
        {"node_type": "code", "identity_key": "experiment.py"},
        {"node_type": "claim", "identity_key": "c1"},
    )
    # A second, unresolved piece of evidence -- the citation hasn't been
    # ingested as a node yet.
    await db_module.create_edge(
        db, pid, "contradicts",
        {"node_type": "citation", "identity_key": "zotero:XYZ"},
        {"node_type": "claim", "identity_key": "c1"},
    )

    evidence = await db_module.get_claim_evidence(db, pid, "c1")
    assert len(evidence) == 2
    by_kind = {e["edge_kind"]: e for e in evidence}
    assert by_kind["supports"]["resolved"] is True
    assert by_kind["supports"]["evidence_node"]["id"] == code_node["id"]
    assert by_kind["contradicts"]["resolved"] is False
    assert by_kind["contradicts"]["evidence_node"] is None


@pytest.mark.asyncio
async def test_get_claim_evidence_ignores_unrelated_edge_kinds(db):
    project = await db_module.create_project(db, "rg-25")
    pid = project["id"]
    await db_module.create_node(db, pid, "claim", "c1")
    await db_module.create_node(db, pid, "document", "report.docx")
    # 'documents' isn't a claim-evidence kind -- shouldn't show up.
    await db_module.create_edge(
        db, pid, "documents",
        {"node_type": "claim", "identity_key": "c1"},
        {"node_type": "document", "identity_key": "report.docx"},
    )
    assert await db_module.get_claim_evidence(db, pid, "c1") == []


@pytest.mark.asyncio
async def test_get_claim_evidence_requires_non_empty_identity(db):
    project = await db_module.create_project(db, "rg-26")
    with pytest.raises(ValueError, match="non-empty claim_identity_key"):
        await db_module.get_claim_evidence(db, project["id"], "  ")


# ---------------------------------------------------------------------------
# artifact-to-document lineage query.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_artifact_document_lineage_walks_run_output_document_chain(db):
    project = await db_module.create_project(db, "rg-27")
    pid = project["id"]

    await db_module.create_edge(
        db, pid, "produces",
        {"node_type": "run", "identity_key": "run-1"},
        {"node_type": "output", "identity_key": "sha256:deadbeef"},
    )
    await db_module.create_edge(
        db, pid, "documents",
        {"node_type": "output", "identity_key": "sha256:deadbeef"},
        {"node_type": "document", "identity_key": "report.docx"},
    )
    await db_module.create_node(db, pid, "run", "run-1")
    await db_module.create_node(db, pid, "output", "sha256:deadbeef")
    await db_module.create_node(db, pid, "document", "report.docx")

    lineage = await db_module.get_artifact_document_lineage(db, pid, "run", "run-1")
    node_refs = {(n["node_type"], n["identity_key"]) for n in lineage["nodes"]}
    assert node_refs == {
        ("run", "run-1"), ("output", "sha256:deadbeef"), ("document", "report.docx"),
    }
    edge_pairs = {
        (e["edge_kind"], e["from_identity_key"], e["to_identity_key"])
        for e in lineage["edges"]
    }
    assert edge_pairs == {
        ("produces", "run-1", "sha256:deadbeef"),
        ("documents", "sha256:deadbeef", "report.docx"),
    }


@pytest.mark.asyncio
async def test_get_artifact_document_lineage_diamond_shape(db):
    """One run produces two outputs, both of which document the SAME
    report -- a real DAG, not a single chain; the lineage subgraph must
    surface both branches."""
    project = await db_module.create_project(db, "rg-28")
    pid = project["id"]

    for out_key in ("sha256:aaa", "sha256:bbb"):
        await db_module.create_edge(
            db, pid, "produces",
            {"node_type": "run", "identity_key": "run-1"},
            {"node_type": "output", "identity_key": out_key},
        )
        await db_module.create_edge(
            db, pid, "documents",
            {"node_type": "output", "identity_key": out_key},
            {"node_type": "document", "identity_key": "report.docx"},
        )

    lineage = await db_module.get_artifact_document_lineage(db, pid, "run", "run-1")
    assert len(lineage["edges"]) == 4
    output_keys = {
        n["identity_key"] for n in lineage["nodes"] if n["node_type"] == "output"
    }
    # Nodes were never created for the outputs -- only edges -- so they're
    # legitimately absent from `nodes` while their edges are still surfaced.
    assert output_keys == set()
    edge_targets = {e["to_identity_key"] for e in lineage["edges"] if e["edge_kind"] == "produces"}
    assert edge_targets == {"sha256:aaa", "sha256:bbb"}


@pytest.mark.asyncio
async def test_get_lineage_subgraph_backward_direction(db):
    project = await db_module.create_project(db, "rg-29")
    pid = project["id"]
    await db_module.create_edge(
        db, pid, "produces",
        {"node_type": "run", "identity_key": "run-1"},
        {"node_type": "output", "identity_key": "out-1"},
    )
    await db_module.create_edge(
        db, pid, "documents",
        {"node_type": "output", "identity_key": "out-1"},
        {"node_type": "document", "identity_key": "report.docx"},
    )
    # Starting at the DOCUMENT and walking backward reaches the run.
    lineage = await db_module.get_lineage_subgraph(
        db, pid, "document", "report.docx",
        edge_kinds=("produces", "documents"), direction="backward",
    )
    node_refs = {(n["node_type"], n["identity_key"]) for n in lineage["nodes"]}
    # No nodes were ever created (only edges), so `nodes` is empty, but the
    # edges prove the walk actually reached both hops.
    assert node_refs == set()
    edge_kinds_seen = {e["edge_kind"] for e in lineage["edges"]}
    assert edge_kinds_seen == {"produces", "documents"}


@pytest.mark.asyncio
async def test_get_lineage_subgraph_rejects_bad_direction(db):
    project = await db_module.create_project(db, "rg-30")
    with pytest.raises(ValueError, match="direction must be"):
        await db_module.get_lineage_subgraph(
            db, project["id"], "run", "run-1", direction="sideways",
        )


@pytest.mark.asyncio
async def test_get_lineage_subgraph_respects_max_hops():
    """A long produces->derived_from chain is only walked up to max_hops."""
    import aiosqlite

    from meridian.db.research_graph import _migrate_research_graph

    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        await _migrate_research_graph(conn)
        pid = "rg-31-project"
        # code-0 -derived_from-> code-1 -derived_from-> ... -> code-5
        for i in range(5):
            await db_module.create_edge(
                conn, pid, "derived_from",
                {"node_type": "code", "identity_key": f"code-{i}"},
                {"node_type": "code", "identity_key": f"code-{i + 1}"},
            )
        full = await db_module.get_lineage_subgraph(
            conn, pid, "code", "code-0", edge_kinds=("derived_from",), max_hops=50,
        )
        assert len(full["edges"]) == 5
        limited = await db_module.get_lineage_subgraph(
            conn, pid, "code", "code-0", edge_kinds=("derived_from",), max_hops=2,
        )
        assert len(limited["edges"]) == 2
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Postgres parity (skipped locally unless TEST_DATABASE_URL is set).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_graph_core_round_trip_on_any_backend(anydb):
    """Node create/replace + edge create/resolve + claim evidence, on
    whichever backend `anydb` parametrizes to (SQLite always; Postgres when
    TEST_DATABASE_URL is set)."""
    project = await db_module.create_project(anydb, "rg-anydb")
    pid = project["id"]

    old = await db_module.create_node(anydb, pid, "output", "sha256:aaa", revision="v1")
    new = await db_module.replace_node_revision(anydb, pid, old["id"], revision="v2")
    assert new["supersedes_id"] == old["id"]
    current = await db_module.get_current_node(anydb, pid, "output", "sha256:aaa")
    assert current["id"] == new["id"]

    await db_module.create_edge(
        anydb, pid, "supports",
        {"node_type": "output", "identity_key": "sha256:aaa"},
        {"node_type": "claim", "identity_key": "c1"},
    )
    await db_module.create_node(anydb, pid, "claim", "c1")
    evidence = await db_module.get_claim_evidence(anydb, pid, "c1")
    assert len(evidence) == 1
    assert evidence[0]["resolved"] is True
    assert evidence[0]["evidence_node"]["id"] == new["id"]


# ---------------------------------------------------------------------------
# Extension identity-helper functions (meridian-docs / meridian-outputs) —
# plain, dependency-free shape builders consumed by a caller that has both
# this extension AND core meridian installed.
# ---------------------------------------------------------------------------


def _extensions_root() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extensions")


def test_docs_intel_research_graph_document_identity_helper():
    ext_dir = os.path.join(_extensions_root(), "meridian-docs")
    if ext_dir not in sys.path:
        sys.path.insert(0, ext_dir)
    from meridian_docs import docs_intel  # noqa: PLC0415

    result = docs_intel.research_graph_document_identity(
        "report.docx", content_hash="abc123",
    )
    assert result == {
        "node_type": "document",
        "identity_key": "report.docx",
        "revision": "abc123",
        "external_ref": {"source": "report.docx"},
    }
    with_element = docs_intel.research_graph_document_identity(
        "report.docx", element_id="p42",
    )
    assert with_element["identity_key"] == "report.docx::p42"
    assert with_element["external_ref"]["element_id"] == "p42"
    with pytest.raises(ValueError, match="non-empty source_path"):
        docs_intel.research_graph_document_identity("")


def test_outputs_local_research_graph_output_identity_helper():
    ext_dir = os.path.join(_extensions_root(), "meridian-outputs")
    if ext_dir not in sys.path:
        sys.path.insert(0, ext_dir)
    from meridian_outputs import outputs_local as OL  # noqa: PLC0415

    result = OL.research_graph_output_identity(path="out/foo.csv", sha256="deadbeef")
    assert result == {
        "node_type": "output",
        "identity_key": "sha256:deadbeef",
        "revision": "deadbeef",
        "external_ref": {"path": "out/foo.csv", "sha256": "deadbeef"},
    }
    from_row = OL.research_graph_output_identity(
        row={"canonical_path": "out/bar.csv", "sha256": "cafef00d"},
    )
    assert from_row["identity_key"] == "sha256:cafef00d"
    with pytest.raises(ValueError, match="requires at least one of"):
        OL.research_graph_output_identity()
