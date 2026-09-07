"""Tests for sprint item 81b5491b — SCHEMA: paper_strategy_graph —
argument-layer nodes and rhetorical edges.

Covers:
  * meridian.paper_strategy — the pure closed-vocabulary/validator layer.
  * meridian.db.paper_strategy_graph — the persistence layer
    (create_strategy_node / supersede_strategy_node / approve_strategy_node /
    reject_strategy_node / get_current_strategy_node /
    list_strategy_node_versions / create_strategy_edge /
    get_strategy_edges_for_node), on SQLite via the `db` fixture (full
    init_db path).
  * meridian.models — PaperStrategyNodeCreate / PaperStrategyNode /
    PaperStrategyEdgeCreate / PaperStrategyEdge validation (valid + invalid
    cases).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from meridian import db as db_module
from meridian import models
from meridian import paper_strategy as ps

_SECRET_LOOKING = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


async def _count(db, table: str) -> int:
    async with db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


# ---------------------------------------------------------------------------
# meridian.paper_strategy — pure vocabulary + validators.
# ---------------------------------------------------------------------------


def test_node_types_and_edge_types_are_the_documented_sets():
    assert ps.NODE_TYPES == {
        "thesis", "claim", "counter_claim", "evidence", "warrant",
        "rebuttal", "concession", "motivation", "framing_note",
    }
    assert ps.EDGE_TYPES == {
        "supports", "rebuts", "concedes", "qualifies", "motivates",
        "contrasts", "elaborates", "restates",
    }
    # Every edge kind is documented in EDGE_DIRECTIONALITY -- no silent gaps.
    assert set(ps.EDGE_DIRECTIONALITY) == ps.EDGE_TYPES
    assert ps.NODE_STATUSES == {"draft", "approved", "rejected", "superseded"}


def test_validate_node_type_accepts_all_and_rejects_unknown():
    for nt in ps.NODE_TYPES:
        assert ps.validate_node_type(nt) == nt
        assert ps.validate_node_type(nt.upper()) == nt  # case-insensitive
    with pytest.raises(ValueError, match="node_type must be one of"):
        ps.validate_node_type("bogus")
    with pytest.raises(ValueError, match="node_type must be one of"):
        ps.validate_node_type(None)


def test_validate_edge_kind_accepts_all_and_rejects_unknown():
    for ek in ps.EDGE_TYPES:
        assert ps.validate_edge_kind(ek) == ek
    with pytest.raises(ValueError, match="edge_kind must be one of"):
        ps.validate_edge_kind("bogus-kind")


# ---------------------------------------------------------------------------
# Migration — table + indexes, idempotent; not inline in either base literal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_strategy_graph_migration_creates_tables_and_indexes_idempotently():
    import aiosqlite

    from meridian.db.paper_strategy_graph import _migrate_paper_strategy_graph

    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        await _migrate_paper_strategy_graph(conn)
        await _migrate_paper_strategy_graph(conn)  # re-run must be a no-op

        for table_name in ("paper_strategy_nodes", "paper_strategy_edges"):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ) as cur:
                assert await cur.fetchone() is not None, table_name
        for index_name in (
            "idx_paper_strategy_nodes_family_version",
            "idx_paper_strategy_nodes_project",
            "idx_paper_strategy_edges_unique",
            "idx_paper_strategy_edges_from",
            "idx_paper_strategy_edges_to",
        ):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ) as cur:
                assert await cur.fetchone() is not None, index_name

        async with conn.execute("PRAGMA table_info(paper_strategy_nodes)") as cur:
            node_cols = {r["name"] for r in await cur.fetchall()}
        assert node_cols == {
            "id", "project_id", "family_id", "version", "node_type",
            "document_ref", "statement", "rationale", "status",
            "approved_by", "approved_at", "rejection_reason",
            "supersedes_id", "superseded_by", "created_by", "created_at",
            "updated_at",
        }
        async with conn.execute("PRAGMA table_info(paper_strategy_edges)") as cur:
            edge_cols = {r["name"] for r in await cur.fetchall()}
        assert edge_cols == {
            "id", "project_id", "edge_kind", "from_node_id", "to_node_id",
            "label", "created_by", "created_at",
        }
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_paper_strategy_graph_unique_version_constraint_enforced():
    """A raw duplicate (project_id, family_id, version) insert must fail --
    confirms the unique index is real, not just documentation."""
    import aiosqlite

    from meridian.db.paper_strategy_graph import _migrate_paper_strategy_graph

    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        await _migrate_paper_strategy_graph(conn)
        await conn.execute(
            "INSERT INTO paper_strategy_nodes "
            "(id, project_id, family_id, version, node_type, statement) "
            "VALUES ('n1', 'proj-1', 'fam-1', 1, 'thesis', 'stmt')"
        )
        await conn.commit()
        with pytest.raises(Exception, match="(?i)unique"):
            await conn.execute(
                "INSERT INTO paper_strategy_nodes "
                "(id, project_id, family_id, version, node_type, statement) "
                "VALUES ('n2', 'proj-1', 'fam-1', 1, 'claim', 'stmt2')"
            )
    finally:
        await conn.close()


def test_paper_strategy_graph_not_inline_in_base_literals():
    from meridian.db import CREATE_TABLES
    from meridian.pg_adapter import CREATE_TABLES_CORE

    for name, literal in (
        ("CREATE_TABLES_CORE", CREATE_TABLES_CORE),
        ("CREATE_TABLES", CREATE_TABLES),
    ):
        assert "paper_strategy_nodes" not in literal, name
        assert "paper_strategy_edges" not in literal, name


@pytest.mark.asyncio
async def test_paper_strategy_graph_wired_into_full_init_db(db):
    """Sanity check the migration is actually wired into init_db's startup
    chain (not just directly callable) -- the `db` fixture goes through the
    real init_db path."""
    project = await db_module.create_project(db, "psg-wiring")
    node = await db_module.create_strategy_node(
        db, project["id"], "thesis", "Our approach generalizes across domains.",
    )
    assert node["node_type"] == "thesis"
    assert node["status"] == "draft"
    assert node["family_id"] == node["id"]
    assert node["version"] == 1


# ---------------------------------------------------------------------------
# create_strategy_node — validation, defaults.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_strategy_node_rejects_unknown_node_type(db):
    project = await db_module.create_project(db, "psg-1")
    with pytest.raises(ValueError, match="node_type must be one of"):
        await db_module.create_strategy_node(db, project["id"], "bogus", "x")


@pytest.mark.asyncio
async def test_create_strategy_node_rejects_blank_statement(db):
    project = await db_module.create_project(db, "psg-2")
    with pytest.raises(ValueError, match="non-empty statement"):
        await db_module.create_strategy_node(db, project["id"], "claim", "   ")


@pytest.mark.asyncio
async def test_create_strategy_node_rejects_secret_looking_statement(db):
    project = await db_module.create_project(db, "psg-3")
    with pytest.raises(ValueError, match="Refusing to persist"):
        await db_module.create_strategy_node(
            db, project["id"], "claim", _SECRET_LOOKING,
        )


@pytest.mark.asyncio
async def test_create_strategy_node_rejects_secret_looking_rationale(db):
    project = await db_module.create_project(db, "psg-3b")
    with pytest.raises(ValueError, match="Refusing to persist"):
        await db_module.create_strategy_node(
            db, project["id"], "claim", "a fine claim", rationale=_SECRET_LOOKING,
        )


@pytest.mark.asyncio
async def test_create_strategy_node_all_node_types_accepted(db):
    project = await db_module.create_project(db, "psg-4")
    for i, nt in enumerate(sorted(ps.NODE_TYPES)):
        node = await db_module.create_strategy_node(db, project["id"], nt, f"statement-{i}")
        assert node["node_type"] == nt
        assert node["status"] == "draft"
        assert node["version"] == 1
        assert node["family_id"] == node["id"]


@pytest.mark.asyncio
async def test_create_strategy_node_stores_optional_fields(db):
    project = await db_module.create_project(db, "psg-5")
    node = await db_module.create_strategy_node(
        db, project["id"], "evidence", "Table 3 shows a 12pt improvement.",
        document_ref="results-section::p4",
        rationale="Leads with the strongest number to preempt skepticism.",
        created_by="adam",
    )
    assert node["document_ref"] == "results-section::p4"
    assert node["rationale"] == "Leads with the strongest number to preempt skepticism."
    assert node["created_by"] == "adam"
    assert node["supersedes_id"] is None
    assert node["superseded_by"] is None


# ---------------------------------------------------------------------------
# supersede_strategy_node — atomic version bump.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supersede_strategy_node_creates_next_version_and_retires_old(db):
    project = await db_module.create_project(db, "psg-6")
    v1 = await db_module.create_strategy_node(
        db, project["id"], "claim", "Our method is faster.",
    )
    v2 = await db_module.supersede_strategy_node(
        db, project["id"], v1["id"], statement="Our method is faster on 3 of 4 benchmarks.",
    )
    assert v2["id"] != v1["id"]
    assert v2["family_id"] == v1["family_id"]
    assert v2["version"] == 2
    assert v2["status"] == "draft"
    assert v2["supersedes_id"] == v1["id"]
    # node_type/document_ref/rationale inherited unchanged from v1.
    assert v2["node_type"] == v1["node_type"]

    reread_v1 = await db_module.get_strategy_node(db, project["id"], v1["id"])
    assert reread_v1["status"] == "superseded"
    assert reread_v1["superseded_by"] == v2["id"]

    versions = await db_module.list_strategy_node_versions(db, project["id"], v1["family_id"])
    assert [v["id"] for v in versions] == [v1["id"], v2["id"]]

    current = await db_module.get_current_strategy_node(db, project["id"], v1["family_id"])
    assert current["id"] == v2["id"]


@pytest.mark.asyncio
async def test_supersede_strategy_node_rejects_missing_node(db):
    project = await db_module.create_project(db, "psg-7")
    with pytest.raises(ValueError, match="not found"):
        await db_module.supersede_strategy_node(db, project["id"], "no-such-id", statement="x")


@pytest.mark.asyncio
async def test_supersede_strategy_node_rejects_already_superseded(db):
    project = await db_module.create_project(db, "psg-8")
    v1 = await db_module.create_strategy_node(db, project["id"], "claim", "v1")
    await db_module.supersede_strategy_node(db, project["id"], v1["id"], statement="v2")
    with pytest.raises(ValueError, match="supersede the CURRENT"):
        await db_module.supersede_strategy_node(db, project["id"], v1["id"], statement="v3")


@pytest.mark.asyncio
async def test_supersede_strategy_node_rejects_rejected_node(db):
    project = await db_module.create_project(db, "psg-9")
    v1 = await db_module.create_strategy_node(db, project["id"], "claim", "v1")
    await db_module.reject_strategy_node(db, project["id"], v1["id"], "wrong framing")
    with pytest.raises(ValueError, match="supersede the CURRENT"):
        await db_module.supersede_strategy_node(db, project["id"], v1["id"], statement="v2")


# ---------------------------------------------------------------------------
# approve_strategy_node / reject_strategy_node — the human-approval gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_strategy_node_sets_status_and_approver(db):
    project = await db_module.create_project(db, "psg-10")
    node = await db_module.create_strategy_node(db, project["id"], "thesis", "The thesis.")
    approved = await db_module.approve_strategy_node(db, project["id"], node["id"], "adam")
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "adam"
    assert approved["approved_at"] is not None


@pytest.mark.asyncio
async def test_approve_strategy_node_rejects_blank_approver(db):
    project = await db_module.create_project(db, "psg-11")
    node = await db_module.create_strategy_node(db, project["id"], "thesis", "The thesis.")
    with pytest.raises(ValueError, match="non-empty approved_by"):
        await db_module.approve_strategy_node(db, project["id"], node["id"], "  ")


@pytest.mark.asyncio
async def test_approve_strategy_node_rejects_non_draft(db):
    project = await db_module.create_project(db, "psg-12")
    node = await db_module.create_strategy_node(db, project["id"], "thesis", "The thesis.")
    await db_module.approve_strategy_node(db, project["id"], node["id"], "adam")
    with pytest.raises(ValueError, match="only a 'draft' node can be approved"):
        await db_module.approve_strategy_node(db, project["id"], node["id"], "adam")


@pytest.mark.asyncio
async def test_reject_strategy_node_sets_status_and_reason(db):
    project = await db_module.create_project(db, "psg-13")
    node = await db_module.create_strategy_node(db, project["id"], "counter_claim", "A weak objection.")
    rejected = await db_module.reject_strategy_node(db, project["id"], node["id"], "not worth addressing")
    assert rejected["status"] == "rejected"
    assert rejected["rejection_reason"] == "not worth addressing"


@pytest.mark.asyncio
async def test_reject_strategy_node_rejects_blank_reason(db):
    project = await db_module.create_project(db, "psg-14")
    node = await db_module.create_strategy_node(db, project["id"], "counter_claim", "x")
    with pytest.raises(ValueError, match="non-empty reason"):
        await db_module.reject_strategy_node(db, project["id"], node["id"], "")


@pytest.mark.asyncio
async def test_reject_strategy_node_rejects_non_draft(db):
    project = await db_module.create_project(db, "psg-15")
    node = await db_module.create_strategy_node(db, project["id"], "counter_claim", "x")
    await db_module.reject_strategy_node(db, project["id"], node["id"], "no good")
    with pytest.raises(ValueError, match="only a 'draft' node can be rejected"):
        await db_module.reject_strategy_node(db, project["id"], node["id"], "again")


# ---------------------------------------------------------------------------
# create_strategy_edge / get_strategy_edges_for_node.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_strategy_edge_rejects_unknown_edge_kind(db):
    project = await db_module.create_project(db, "psg-16")
    a = await db_module.create_strategy_node(db, project["id"], "evidence", "a")
    b = await db_module.create_strategy_node(db, project["id"], "claim", "b")
    with pytest.raises(ValueError, match="edge_kind must be one of"):
        await db_module.create_strategy_edge(db, project["id"], "bogus", a["id"], b["id"])


@pytest.mark.asyncio
async def test_create_strategy_edge_rejects_self_loop(db):
    project = await db_module.create_project(db, "psg-17")
    a = await db_module.create_strategy_node(db, project["id"], "claim", "a")
    with pytest.raises(ValueError, match="node to itself"):
        await db_module.create_strategy_edge(db, project["id"], "supports", a["id"], a["id"])


@pytest.mark.asyncio
async def test_create_strategy_edge_rejects_missing_endpoint(db):
    project = await db_module.create_project(db, "psg-18")
    a = await db_module.create_strategy_node(db, project["id"], "claim", "a")
    with pytest.raises(ValueError, match="not found"):
        await db_module.create_strategy_edge(db, project["id"], "supports", a["id"], "no-such-node")


@pytest.mark.asyncio
async def test_create_strategy_edge_rejects_secret_looking_label(db):
    project = await db_module.create_project(db, "psg-18b")
    a = await db_module.create_strategy_node(db, project["id"], "evidence", "a")
    b = await db_module.create_strategy_node(db, project["id"], "claim", "b")
    with pytest.raises(ValueError, match="Refusing to persist"):
        await db_module.create_strategy_edge(
            db, project["id"], "supports", a["id"], b["id"], label=_SECRET_LOOKING,
        )


@pytest.mark.asyncio
async def test_create_strategy_edge_idempotent_on_repeat_call(db):
    project = await db_module.create_project(db, "psg-19")
    a = await db_module.create_strategy_node(db, project["id"], "evidence", "a")
    b = await db_module.create_strategy_node(db, project["id"], "claim", "b")
    e1 = await db_module.create_strategy_edge(db, project["id"], "supports", a["id"], b["id"])
    e2 = await db_module.create_strategy_edge(db, project["id"], "supports", a["id"], b["id"])
    assert e1["id"] == e2["id"]
    assert await _count(db, "paper_strategy_edges") == 1


@pytest.mark.asyncio
async def test_get_strategy_edges_for_node_role_filtering(db):
    project = await db_module.create_project(db, "psg-20")
    thesis = await db_module.create_strategy_node(db, project["id"], "thesis", "t")
    claim = await db_module.create_strategy_node(db, project["id"], "claim", "c")
    evidence = await db_module.create_strategy_node(db, project["id"], "evidence", "e")
    await db_module.create_strategy_edge(db, project["id"], "supports", claim["id"], thesis["id"])
    await db_module.create_strategy_edge(db, project["id"], "supports", evidence["id"], claim["id"])

    from_thesis = await db_module.get_strategy_edges_for_node(db, project["id"], thesis["id"], role="from")
    assert from_thesis == []
    to_thesis = await db_module.get_strategy_edges_for_node(db, project["id"], thesis["id"], role="to")
    assert len(to_thesis) == 1
    assert to_thesis[0]["from_node_id"] == claim["id"]

    both_sides = await db_module.get_strategy_edges_for_node(db, project["id"], claim["id"])
    assert len(both_sides) == 2

    with pytest.raises(ValueError, match="role must be"):
        await db_module.get_strategy_edges_for_node(db, project["id"], claim["id"], role="sideways")


# ---------------------------------------------------------------------------
# meridian.models — Pydantic validation (valid + invalid).
# ---------------------------------------------------------------------------


def test_paper_strategy_node_create_valid():
    m = models.PaperStrategyNodeCreate(
        project_id="proj-1", node_type="thesis", statement="Our thesis.",
    )
    assert m.node_type == "thesis"
    assert m.document_ref is None


def test_paper_strategy_node_create_rejects_unknown_node_type():
    with pytest.raises(ValidationError):
        models.PaperStrategyNodeCreate(
            project_id="proj-1", node_type="bogus", statement="x",
        )


def test_paper_strategy_node_create_rejects_blank_statement():
    with pytest.raises(ValidationError):
        models.PaperStrategyNodeCreate(project_id="proj-1", node_type="claim", statement="")


def test_paper_strategy_node_create_rejects_blank_project_id():
    with pytest.raises(ValidationError):
        models.PaperStrategyNodeCreate(project_id="", node_type="claim", statement="x")


def test_paper_strategy_node_round_trip_shape():
    m = models.PaperStrategyNode(
        id="n1", project_id="proj-1", family_id="n1", version=1,
        node_type="claim", statement="x", status="draft",
        created_at="2026-01-01 00:00:00",
    )
    assert m.status == "draft"
    assert m.superseded_by is None


def test_paper_strategy_node_rejects_unknown_status():
    with pytest.raises(ValidationError):
        models.PaperStrategyNode(
            id="n1", project_id="proj-1", family_id="n1", version=1,
            node_type="claim", statement="x", status="bogus",
            created_at="2026-01-01 00:00:00",
        )


def test_paper_strategy_edge_create_valid():
    m = models.PaperStrategyEdgeCreate(
        project_id="proj-1", edge_kind="supports",
        from_node_id="n1", to_node_id="n2",
    )
    assert m.edge_kind == "supports"


def test_paper_strategy_edge_create_rejects_unknown_edge_kind():
    with pytest.raises(ValidationError):
        models.PaperStrategyEdgeCreate(
            project_id="proj-1", edge_kind="bogus",
            from_node_id="n1", to_node_id="n2",
        )


def test_paper_strategy_edge_create_rejects_blank_node_ids():
    with pytest.raises(ValidationError):
        models.PaperStrategyEdgeCreate(
            project_id="proj-1", edge_kind="supports",
            from_node_id="", to_node_id="n2",
        )


def test_paper_strategy_edge_round_trip_shape():
    m = models.PaperStrategyEdge(
        id="e1", project_id="proj-1", edge_kind="rebuts",
        from_node_id="n1", to_node_id="n2", created_at="2026-01-01 00:00:00",
    )
    assert m.edge_kind == "rebuts"
    assert m.label is None
