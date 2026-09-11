"""Tests for sprint item ff1843dc — API to expose proposal successor
creation, typed relations, lineage queries, and handoff rendering.

This item builds the MCP-facing API layer on top of the ALREADY-EXISTING
typed-relation storage in ``meridian.db.proposal_lineage`` (5a744f81/
6cdc5df3), which had validation/cycle/tenant-scoping tests of its own in
``tests/test_proposal_lineage.py`` already. This file covers the NEW
surface added on top of that:

  * ``meridian.db.proposal_lineage.get_proposal_descendants`` — forward BFS
    walk (successors of successors), bounded + truncation-aware.
  * ``meridian.db.proposal_lineage.create_proposal_successor`` — one call
    that atomically composes a new ``workspace_proposals`` row with a
    ``proposal_lineage`` edge back to its predecessor.
  * ``meridian.db.proposal_lineage.compare_proposal_versions`` — structural
    diff (title/body/tags/status/scope_type/project_id/family_id, body
    similarity + unified diff) between two proposals.
  * The four new MCP tools: ``create_proposal_successor``,
    ``link_proposal_lineage``, ``get_proposal_lineage``,
    ``compare_proposal_versions`` (schema + dispatch wiring in
    ``meridian.mcp_tools`` / ``meridian.mcp.handler`` /
    ``meridian.mcp.handlers.notes_decisions``).
  * ``meridian.proposal_promotion``'s new ``lineage`` field on
    ``preview_proposal_promotion`` (both the already_satisfied AND the
    normal/would_create branches).
  * ``meridian.handoff.build_proposal_lineage_for_handoff`` and its wiring
    into ``generate_handoff`` (via ``meridian.mcp.handler``'s
    ``_handle_task_tools`` dispatch).

Three real bugs were found and fixed while writing this coverage (see the
"REGRESSION" tests below for each):

  1. ``create_proposal_successor`` stamped the NEW proposal's tenant_id with
     ``effective_tenant_id`` (caller scope OR predecessor tenant) instead of
     the predecessor's OWN stored tenant_id. For a legacy/pre-isolation
     predecessor (tenant_id=None) called under an authenticated tenant
     scope, this made the new proposal's tenant_id disagree with the
     predecessor's, which then made the immediately-following
     ``link_proposal_lineage`` call raise "must not cross tenant/workspace
     boundaries" — even though ``create_proposal_successor``'s OWN upfront
     validation explicitly permits exactly this case. Net effect: the
     function unconditionally failed for this input, AND left an orphan
     proposal row (created, but never linked) behind — directly
     contradicting its own docstring's "never leaves an orphan proposal
     behind" claim.
  2. A secret-shaped ``label`` was only validated inside the
     ``link_proposal_lineage`` call, which runs AFTER
     ``add_workspace_proposal`` has already committed the new proposal —
     so a rejected call for this reason ALSO left an orphan proposal behind.
  3. ``preview_proposal_promotion``'s normal (not-yet-satisfied) return path
     — the far more common of its two branches — never attached the new
     ``lineage`` field at all (only the ``already_satisfied`` early-return
     branch did).

See the module docstrings in ``meridian/db/proposal_lineage.py`` and
``meridian/proposal_promotion.py`` for the fixed implementations.
"""
from __future__ import annotations

import json as _json

import pytest

import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import proposal_promotion
from meridian.db.proposal_lineage import VALID_RELATION_TYPES
from meridian.mcp import handler as mcp_handler
from meridian.mcp.handlers import notes_decisions as nd_mod
from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _READ_ONLY_TOOLS,
    _TOOL_CATEGORY,
    _TOOL_EXAMPLES,
)

_DATA_DIR = "/tmp/meridian-test"

_NEW_TOOLS = (
    "create_proposal_successor",
    "link_proposal_lineage",
    "get_proposal_lineage",
    "compare_proposal_versions",
)


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _proposal(db, title: str = "Idea", body: str = "body", **kwargs):
    return await db_module.add_workspace_proposal(db, title, body, **kwargs)


async def _count(db, table: str) -> int:
    async with db.execute(f"SELECT COUNT(*) AS n FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


# ---------------------------------------------------------------------------
# get_proposal_descendants — forward BFS complement to get_proposal_ancestors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposal_descendants_empty_when_no_successors(db):
    root = await _proposal(db, "Root")
    out = await db_module.get_proposal_descendants(db, root["id"])
    assert out == []


@pytest.mark.asyncio
async def test_get_proposal_descendants_single_level(db):
    root = await _proposal(db, "Root")
    succ = await _proposal(db, "Successor")
    await db_module.link_proposal_lineage(db, succ["id"], root["id"], "supersedes")
    out = await db_module.get_proposal_descendants(db, root["id"])
    assert len(out) == 1
    assert out[0]["from_proposal_id"] == succ["id"]
    assert out[0]["to_proposal_id"] == root["id"]


@pytest.mark.asyncio
async def test_get_proposal_descendants_multi_level_chain(db):
    """root <- v2 <- v3 <- v4: every edge in the chain is a descendant of
    root, regardless of hop distance."""
    root = await _proposal(db, "v1")
    v2 = await _proposal(db, "v2")
    v3 = await _proposal(db, "v3")
    v4 = await _proposal(db, "v4")
    await db_module.link_proposal_lineage(db, v2["id"], root["id"], "supersedes")
    await db_module.link_proposal_lineage(db, v3["id"], v2["id"], "supersedes")
    await db_module.link_proposal_lineage(db, v4["id"], v3["id"], "supersedes")

    out = await db_module.get_proposal_descendants(db, root["id"])
    assert len(out) == 3
    froms = {r["from_proposal_id"] for r in out}
    assert froms == {v2["id"], v3["id"], v4["id"]}
    # Nearest-first: the direct successor's edge (root<-v2) must be the
    # first one encountered (level 1 of the BFS).
    assert out[0]["from_proposal_id"] == v2["id"]


@pytest.mark.asyncio
async def test_get_proposal_descendants_branching_tree(db):
    """root has TWO independent successors (a fork) — both must appear,
    order irrelevant between siblings but grouped in the same BFS level."""
    root = await _proposal(db, "root")
    fork_a = await _proposal(db, "fork-a")
    fork_b = await _proposal(db, "fork-b")
    await db_module.link_proposal_lineage(db, fork_a["id"], root["id"], "forks")
    await db_module.link_proposal_lineage(db, fork_b["id"], root["id"], "forks")

    out = await db_module.get_proposal_descendants(db, root["id"])
    froms = {r["from_proposal_id"] for r in out}
    assert froms == {fork_a["id"], fork_b["id"]}


@pytest.mark.asyncio
async def test_get_proposal_descendants_respects_max_items_cap(db):
    root = await _proposal(db, "root")
    prev = root
    for i in range(5):
        nxt = await _proposal(db, f"v{i + 2}")
        await db_module.link_proposal_lineage(db, nxt["id"], prev["id"], "supersedes")
        prev = nxt

    out = await db_module.get_proposal_descendants(db, root["id"], max_items=2)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_get_proposal_descendants_tenant_isolated(db):
    root = await _proposal(db, "root", tenant_id="tenant-a")
    succ = await _proposal(db, "succ", tenant_id="tenant-a")
    await db_module.link_proposal_lineage(
        db, succ["id"], root["id"], "supersedes", tenant_id="tenant-a",
    )
    same = await db_module.get_proposal_descendants(db, root["id"], tenant_id="tenant-a")
    assert len(same) == 1
    other = await db_module.get_proposal_descendants(db, root["id"], tenant_id="tenant-b")
    assert other == []


@pytest.mark.asyncio
async def test_get_proposal_descendants_never_exceeds_max_items_even_at_exact_boundary(db):
    """A generous sanity check on the internal 'ask for one extra' pattern
    used by the MCP handler / handoff wrapper: requesting max_items=N when
    exactly N+1 real descendants exist returns exactly N+1 (the function
    itself does not silently under- or over-count)."""
    root = await _proposal(db, "root")
    prev = root
    ids = []
    for i in range(4):
        nxt = await _proposal(db, f"v{i + 2}")
        await db_module.link_proposal_lineage(db, nxt["id"], prev["id"], "supersedes")
        ids.append(nxt["id"])
        prev = nxt

    out = await db_module.get_proposal_descendants(db, root["id"], max_items=3 + 1)
    assert len(out) == 4  # exactly 4 real descendants, request cap 4 -> all returned
    out_capped = await db_module.get_proposal_descendants(db, root["id"], max_items=3)
    assert len(out_capped) == 3


# ---------------------------------------------------------------------------
# create_proposal_successor — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_successor_returns_composed_shape(db):
    root = await _proposal(db, "Root idea", "root body")
    result = await db_module.create_proposal_successor(
        db, root["id"], "Root idea v2", "revised body", "supersedes",
    )
    assert result["predecessor_id"] == root["id"]
    assert result["proposal"]["title"] == "Root idea v2"
    assert result["proposal"]["id"] != root["id"]
    assert result["lineage"]["from_proposal_id"] == result["proposal"]["id"]
    assert result["lineage"]["to_proposal_id"] == root["id"]
    assert result["lineage"]["relation_type"] == "supersedes"


@pytest.mark.asyncio
async def test_create_proposal_successor_inherits_project_scope(db):
    pid = await _project(db, "successor-inherits-project")
    root = await _proposal(db, "Root", "body", project_id=pid)
    result = await db_module.create_proposal_successor(
        db, root["id"], "Root v2", "body", "refines",
    )
    assert result["proposal"]["project_id"] == pid
    assert result["proposal"]["scope_type"] == "project"


@pytest.mark.asyncio
async def test_create_proposal_successor_workspace_global_stays_workspace_global(db):
    root = await _proposal(db, "Root", "body")  # no project_id -> workspace-global
    result = await db_module.create_proposal_successor(
        db, root["id"], "Root v2", "body", "refines",
    )
    assert not result["proposal"].get("project_id")
    assert result["proposal"]["scope_type"] == "workspace"


@pytest.mark.asyncio
async def test_create_proposal_successor_inherits_family_id(db):
    root = await _proposal(db, "Root", "body", family_id="fam-123")
    result = await db_module.create_proposal_successor(
        db, root["id"], "Root v2", "body", "refines",
    )
    assert result["proposal"]["family_id"] == "fam-123"


@pytest.mark.asyncio
async def test_create_proposal_successor_label_stored_on_edge_not_proposal(db):
    root = await _proposal(db, "Root", "body")
    result = await db_module.create_proposal_successor(
        db, root["id"], "Root v2", "body", "duplicates", label="marked as dup",
    )
    assert result["lineage"]["label"] == "marked as dup"
    assert "label" not in result["proposal"] or result["proposal"].get("label") is None


@pytest.mark.asyncio
async def test_create_proposal_successor_all_relation_types_accepted(db):
    root = await _proposal(db, "Root", "body")
    for rel in VALID_RELATION_TYPES:
        result = await db_module.create_proposal_successor(
            db, root["id"], f"Root via {rel}", "body", rel,
        )
        assert result["lineage"]["relation_type"] == rel


@pytest.mark.asyncio
async def test_create_proposal_successor_chain_visible_via_ancestors_and_descendants(db):
    root = await _proposal(db, "v1")
    r2 = await db_module.create_proposal_successor(db, root["id"], "v2", "body", "supersedes")
    v2_id = r2["proposal"]["id"]
    r3 = await db_module.create_proposal_successor(db, v2_id, "v3", "body", "supersedes")
    v3_id = r3["proposal"]["id"]

    ancestors = await db_module.get_proposal_ancestors(db, v3_id)
    assert [a["to_proposal_id"] for a in ancestors] == [v2_id, root["id"]]

    descendants = await db_module.get_proposal_descendants(db, root["id"])
    froms = {d["from_proposal_id"] for d in descendants}
    assert froms == {v2_id, v3_id}


# ---------------------------------------------------------------------------
# create_proposal_successor — validation, fail-before-write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_successor_invalid_relation_type_raises_and_creates_nothing(db):
    root = await _proposal(db, "Root")
    before = await _count(db, "workspace_proposals")
    with pytest.raises(ValueError, match="relation_type must be one of"):
        await db_module.create_proposal_successor(db, root["id"], "v2", "body", "bogus")
    assert await _count(db, "workspace_proposals") == before


@pytest.mark.asyncio
async def test_create_proposal_successor_unknown_predecessor_raises_and_creates_nothing(db):
    before = await _count(db, "workspace_proposals")
    with pytest.raises(ValueError, match="does not exist"):
        await db_module.create_proposal_successor(
            db, "nonexistent-predecessor", "v2", "body", "supersedes",
        )
    assert await _count(db, "workspace_proposals") == before


@pytest.mark.asyncio
async def test_create_proposal_successor_explicit_cross_tenant_mismatch_raises_and_creates_nothing(db):
    root = await _proposal(db, "Root", "body", tenant_id="tenant-a")
    before = await _count(db, "workspace_proposals")
    with pytest.raises(ValueError, match="different tenant"):
        await db_module.create_proposal_successor(
            db, root["id"], "v2", "body", "supersedes", tenant_id="tenant-b",
        )
    assert await _count(db, "workspace_proposals") == before


@pytest.mark.asyncio
async def test_create_proposal_successor_idempotency_key_returns_same_proposal(db):
    root = await _proposal(db, "Root")
    first = await db_module.create_proposal_successor(
        db, root["id"], "v2", "body", "supersedes", idempotency_key="retry-key-1",
    )
    second = await db_module.create_proposal_successor(
        db, root["id"], "v2", "body", "supersedes", idempotency_key="retry-key-1",
    )
    assert first["proposal"]["id"] == second["proposal"]["id"]
    assert first["lineage"]["id"] == second["lineage"]["id"]
    # Exactly one new proposal + one new lineage edge, not two.
    assert await _count(db, "proposal_lineage") == 1


# ---------------------------------------------------------------------------
# REGRESSION 1 — legacy predecessor (tenant_id=None) + explicit caller scope
# must succeed, not raise a spurious cross-tenant error, and must not orphan
# a proposal on the way to succeeding.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_successor_legacy_predecessor_with_authenticated_caller_scope(db):
    """A pre-isolation predecessor (tenant_id=None) being operated on by an
    authenticated hosted caller (tenant_id='tenant-a') is exactly the
    permitted case create_proposal_successor's OWN upfront validation
    documents (mirrors link_proposal_lineage's 'NULL matches everything'
    rule) — it must not fail, and the resulting proposal + edge must both
    actually exist (no orphan)."""
    root = await _proposal(db, "Legacy root")  # tenant_id=None
    before = await _count(db, "workspace_proposals")

    result = await db_module.create_proposal_successor(
        db, root["id"], "Legacy root v2", "body", "supersedes",
        tenant_id="tenant-a",
    )

    assert result["proposal"]["id"] is not None
    assert result["lineage"]["from_proposal_id"] == result["proposal"]["id"]
    assert result["lineage"]["to_proposal_id"] == root["id"]
    # Exactly one new proposal was created (the successor) — not zero (which
    # would mean the call silently failed) and the count must reflect a
    # real, linked row rather than an orphan.
    assert await _count(db, "workspace_proposals") == before + 1
    links = await db_module.get_proposal_lineage_links(db, root["id"], tenant_id="tenant-a")
    assert len(links) == 1
    assert links[0]["from_proposal_id"] == result["proposal"]["id"]


@pytest.mark.asyncio
async def test_create_proposal_successor_new_proposal_tenant_matches_predecessor_exactly(db):
    """The new proposal's stored tenant_id must equal the PREDECESSOR's own
    stored tenant_id (None here), not the caller-asserted scope — this is
    what keeps link_proposal_lineage's from_tenant==to_tenant invariant
    satisfied unconditionally."""
    root = await _proposal(db, "Legacy root 2")  # tenant_id=None
    result = await db_module.create_proposal_successor(
        db, root["id"], "v2", "body", "supersedes", tenant_id="tenant-z",
    )
    assert result["proposal"]["tenant_id"] is None


# ---------------------------------------------------------------------------
# REGRESSION 2 — a secret-shaped label must not orphan a proposal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_proposal_successor_secret_in_label_raises_and_creates_nothing(db):
    root = await _proposal(db, "Root")
    before = await _count(db, "workspace_proposals")
    fake_aws_key = "AKIA" + "0123456789ABCDEF"  # matches the aws-access-key-id pattern
    with pytest.raises(ValueError, match="Refusing to persist"):
        await db_module.create_proposal_successor(
            db, root["id"], "v2", "body", "supersedes", label=fake_aws_key,
        )
    assert await _count(db, "workspace_proposals") == before
    assert await _count(db, "proposal_lineage") == 0


# ---------------------------------------------------------------------------
# compare_proposal_versions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_proposal_versions_reports_changed_fields(db):
    root = await _proposal(db, "Old title", "Old body line one\nOld body line two")
    result = await db_module.create_proposal_successor(
        db, root["id"], "New title", "Old body line one\nNew body line two", "refines",
    )
    succ_id = result["proposal"]["id"]

    diff = await db_module.compare_proposal_versions(db, succ_id, root["id"])
    assert diff["from"]["id"] == succ_id
    assert diff["to"]["id"] == root["id"]
    assert diff["diff"]["title"]["changed"] is True
    assert diff["diff"]["title"]["a"] == "New title"
    assert diff["diff"]["title"]["b"] == "Old title"
    assert diff["diff"]["body"]["changed"] is True
    assert 0.0 <= diff["diff"]["body"]["similarity"] <= 1.0
    assert diff["diff"]["body"]["similarity"] > 0.5  # mostly-similar bodies
    assert any("New body line two" in line for line in diff["diff"]["body"]["unified_diff"])


@pytest.mark.asyncio
async def test_compare_proposal_versions_identical_fields_not_changed(db):
    root = await _proposal(db, "Same title", "Same body")
    result = await db_module.create_proposal_successor(
        db, root["id"], "Same title", "Same body", "duplicates",
    )
    succ_id = result["proposal"]["id"]
    diff = await db_module.compare_proposal_versions(db, succ_id, root["id"])
    assert diff["diff"]["title"]["changed"] is False
    assert diff["diff"]["body"]["changed"] is False
    assert diff["diff"]["body"]["similarity"] == 1.0


@pytest.mark.asyncio
async def test_compare_proposal_versions_adjacent_true_for_linked_pair(db):
    root = await _proposal(db, "Root")
    result = await db_module.create_proposal_successor(db, root["id"], "v2", "body", "supersedes")
    succ_id = result["proposal"]["id"]
    diff = await db_module.compare_proposal_versions(db, succ_id, root["id"])
    assert diff["adjacent"] is True
    assert len(diff["direct_relations"]) == 1


@pytest.mark.asyncio
async def test_compare_proposal_versions_adjacent_false_for_unrelated_pair(db):
    a = await _proposal(db, "A")
    b = await _proposal(db, "B")
    diff = await db_module.compare_proposal_versions(db, a["id"], b["id"])
    assert diff["adjacent"] is False
    assert diff["direct_relations"] == []


@pytest.mark.asyncio
async def test_compare_proposal_versions_unknown_id_raises(db):
    root = await _proposal(db, "Root")
    with pytest.raises(ValueError, match="not found"):
        await db_module.compare_proposal_versions(db, "does-not-exist", root["id"])


@pytest.mark.asyncio
async def test_compare_proposal_versions_json_roundtrip(db):
    """The whole response must survive JSON serialization unchanged — the
    real requirement for 'queryable via MCP/HTTP', matching the convention
    test_proposal_evidence_linkage.py already exercises for
    build_proposal_evidence_for_handoff."""
    root = await _proposal(db, "Root")
    result = await db_module.create_proposal_successor(db, root["id"], "v2", "body", "supersedes")
    diff = await db_module.compare_proposal_versions(db, result["proposal"]["id"], root["id"])
    reloaded = _json.loads(_json.dumps(diff))
    assert reloaded == diff


# ---------------------------------------------------------------------------
# MCP handlers — meridian.mcp.handlers.notes_decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_create_proposal_successor_happy_path(db):
    root = await _proposal(db, "Root")  # legacy/self-host row: tenant_id=None
    result = await nd_mod.handle_create_proposal_successor(
        {
            "proposal_id": root["id"], "title": "Root v2", "body": "body",
            "relation_type": "supersedes",
        },
        db, _DATA_DIR, None, "t-1",
    )
    assert "error" not in result, result
    assert result["proposal"]["title"] == "Root v2"
    # The new PROPOSAL inherits the predecessor's own stored tenant_id
    # (None here) verbatim — see REGRESSION 1 above for why this must be
    # predecessor_tenant, not the caller's _mcp_tenant_id. The lineage EDGE
    # itself, however, is scoped to the caller's authenticated tenant.
    assert result["proposal"]["tenant_id"] is None
    assert result["lineage"]["tenant_id"] == "t-1"


@pytest.mark.asyncio
async def test_handle_create_proposal_successor_authenticated_tenant_predecessor(db):
    """When the predecessor ALREADY belongs to the caller's own tenant, the
    successor is created within that same tenant end to end (proposal AND
    edge), matching the common hosted/multi-tenant case."""
    root = await _proposal(db, "Root", tenant_id="t-4")
    result = await nd_mod.handle_create_proposal_successor(
        {
            "proposal_id": root["id"], "title": "Root v2", "body": "body",
            "relation_type": "supersedes",
        },
        db, _DATA_DIR, None, "t-4",
    )
    assert "error" not in result, result
    assert result["proposal"]["tenant_id"] == "t-4"
    assert result["lineage"]["tenant_id"] == "t-4"


@pytest.mark.asyncio
async def test_handle_create_proposal_successor_unknown_predecessor_returns_error(db):
    result = await nd_mod.handle_create_proposal_successor(
        {
            "proposal_id": "nope", "title": "v2", "body": "body",
            "relation_type": "supersedes",
        },
        db, _DATA_DIR, None, "t-1",
    )
    assert "error" in result
    assert "does not exist" in result["error"]


@pytest.mark.asyncio
async def test_handle_link_proposal_lineage_happy_path(db):
    a = await _proposal(db, "A", tenant_id="t-2")
    b = await _proposal(db, "B", tenant_id="t-2")
    result = await nd_mod.handle_link_proposal_lineage(
        {"from_proposal_id": b["id"], "to_proposal_id": a["id"], "relation_type": "duplicates"},
        db, _DATA_DIR, None, "t-2",
    )
    assert "error" not in result, result
    assert result["relation_type"] == "duplicates"


@pytest.mark.asyncio
async def test_handle_link_proposal_lineage_cycle_returns_error(db):
    a = await _proposal(db, "A")
    b = await _proposal(db, "B")
    await db_module.link_proposal_lineage(db, b["id"], a["id"], "supersedes")
    result = await nd_mod.handle_link_proposal_lineage(
        {"from_proposal_id": a["id"], "to_proposal_id": b["id"], "relation_type": "supersedes"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "cycle" in result["error"]


@pytest.mark.asyncio
async def test_handle_get_proposal_lineage_aggregates_all_shapes(db):
    root = await _proposal(db, "Root", tenant_id="t-3")
    succ = await nd_mod.handle_create_proposal_successor(
        {"proposal_id": root["id"], "title": "v2", "body": "body", "relation_type": "supersedes"},
        db, _DATA_DIR, None, "t-3",
    )
    result = await nd_mod.handle_get_proposal_lineage(
        {"proposal_id": root["id"]}, db, _DATA_DIR, None, "t-3",
    )
    assert result["proposal_id"] == root["id"]
    assert len(result["links"]) == 1
    assert result["ancestors"] == []
    assert len(result["successors"]) == 1
    assert result["successors"][0]["from_proposal_id"] == succ["proposal"]["id"]
    assert len(result["descendants"]) == 1
    assert result["descendants_truncated"] is False


@pytest.mark.asyncio
async def test_handle_get_proposal_lineage_unknown_proposal_returns_empty_not_error(db):
    """Read-only aggregation over a nonexistent id degrades gracefully to
    all-empty rather than raising — matches every other lineage read
    function's behavior (no existence check, unlike the write paths)."""
    result = await nd_mod.handle_get_proposal_lineage(
        {"proposal_id": "totally-made-up"}, db, _DATA_DIR, None, None,
    )
    assert result == {
        "proposal_id": "totally-made-up",
        "links": [],
        "ancestors": [],
        "successors": [],
        "descendants": [],
        "descendants_truncated": False,
    }


@pytest.mark.asyncio
async def test_handle_get_proposal_lineage_truncation_marker(db):
    root = await _proposal(db, "root")
    for i in range(3):
        await nd_mod.handle_create_proposal_successor(
            {"proposal_id": root["id"], "title": f"dup-{i}", "body": "b", "relation_type": "duplicates"},
            db, _DATA_DIR, None, None,
        )
    result = await nd_mod.handle_get_proposal_lineage(
        {"proposal_id": root["id"], "max_items": 2}, db, _DATA_DIR, None, None,
    )
    assert len(result["descendants"]) == 2
    assert result["descendants_truncated"] is True


@pytest.mark.asyncio
async def test_handle_get_proposal_lineage_max_items_clamped_to_schema_bounds(db):
    """max_items outside the documented [1, 1000] schema range is clamped,
    not passed straight through — defends the handler's own contract
    against a caller that bypasses JSON-schema validation (e.g. a direct
    Python call, or a non-conformant client)."""
    root = await _proposal(db, "root")
    # A caller-supplied max_items of 0 must not silently disable the cap
    # (nor crash) — clamped up to at least 1.
    result = await nd_mod.handle_get_proposal_lineage(
        {"proposal_id": root["id"], "max_items": 0}, db, _DATA_DIR, None, None,
    )
    assert result["descendants"] == []
    assert result["descendants_truncated"] is False

    # An oversized max_items must not be handed unclamped to a query whose
    # only other bound is the (very generous) hop-count safety cap.
    result2 = await nd_mod.handle_get_proposal_lineage(
        {"proposal_id": root["id"], "max_items": 10_000_000},
        db, _DATA_DIR, None, None,
    )
    assert result2["descendants"] == []


@pytest.mark.asyncio
async def test_handle_compare_proposal_versions_happy_path(db):
    root = await _proposal(db, "Root")
    succ = await db_module.create_proposal_successor(db, root["id"], "v2", "b2", "supersedes")
    result = await nd_mod.handle_compare_proposal_versions(
        {"from_proposal_id": succ["proposal"]["id"], "to_proposal_id": root["id"]},
        db, _DATA_DIR, None, None,
    )
    assert "error" not in result, result
    assert result["adjacent"] is True


@pytest.mark.asyncio
async def test_handle_compare_proposal_versions_unknown_id_returns_error(db):
    result = await nd_mod.handle_compare_proposal_versions(
        {"from_proposal_id": "nope", "to_proposal_id": "also-nope"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# MCP registration — schema shape, dispatch wiring, category/read-only tags.
# Architectural rule under test: project_id must NEVER be in a new tool's
# inputSchema.required (the project_name-as-project_id-alternative
# convention this codebase enforces globally in
# test_every_project_id_tool_schema_advertises_project_name).
# ---------------------------------------------------------------------------


def test_all_four_new_tools_registered_in_mcp_tools_list():
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    for name in _NEW_TOOLS:
        assert name in names, f"{name} missing from _MCP_TOOLS_LIST"


def test_none_of_the_new_tools_require_project_id():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    for name in _NEW_TOOLS:
        schema = by_name[name]["inputSchema"]
        required = schema.get("required") or []
        assert "project_id" not in required, f"{name} lists project_id as required"
        # None of these tools even take project_id (they operate on proposal
        # ids whose project scope is already fixed) — so they must also
        # never be swept up by the "every project_id tool must also
        # advertise project_name" contract.
        assert "project_id" not in schema.get("properties", {}), (
            f"{name} unexpectedly declares project_id"
        )


def test_new_tool_relation_type_enums_match_db_layer_valid_relation_types():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    for name in ("create_proposal_successor", "link_proposal_lineage"):
        enum = by_name[name]["inputSchema"]["properties"]["relation_type"]["enum"]
        assert set(enum) == set(VALID_RELATION_TYPES)


def test_new_tools_have_examples():
    for name in _NEW_TOOLS:
        assert name in _TOOL_EXAMPLES, f"{name} missing a _TOOL_EXAMPLES entry"


def test_new_tools_categorized_as_workspace():
    for name in _NEW_TOOLS:
        assert _TOOL_CATEGORY.get(name) == "workspace"


def test_read_only_tools_flagged_correctly():
    assert "get_proposal_lineage" in _READ_ONLY_TOOLS
    assert "compare_proposal_versions" in _READ_ONLY_TOOLS
    # The two write tools must NOT be flagged read-only.
    assert "create_proposal_successor" not in _READ_ONLY_TOOLS
    assert "link_proposal_lineage" not in _READ_ONLY_TOOLS


def test_new_tools_required_fields_match_handler_arg_access():
    """Sanity cross-check: every arg the handler accesses via args[...]
    (hard-required, would KeyError otherwise) is actually listed in the
    schema's required array."""
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    expected_required = {
        "create_proposal_successor": {"proposal_id", "title", "body", "relation_type"},
        "link_proposal_lineage": {"from_proposal_id", "to_proposal_id", "relation_type"},
        "get_proposal_lineage": {"proposal_id"},
        "compare_proposal_versions": {"from_proposal_id", "to_proposal_id"},
    }
    for name, expected in expected_required.items():
        got = set(by_name[name]["inputSchema"].get("required") or [])
        assert got == expected, f"{name}: required={got}, expected {expected}"


@pytest.mark.asyncio
async def test_end_to_end_dispatch_via_mcp_handler(db):
    """Full pipeline: mcp.handler._dispatch_mcp_tool routes each new tool
    name to its handler and back, exactly as a real MCP client call would."""
    root = await _proposal(db, "E2E Root")
    created = await mcp_handler._dispatch_mcp_tool(
        "create_proposal_successor",
        {"proposal_id": root["id"], "title": "E2E v2", "body": "b", "relation_type": "supersedes"},
        db, _DATA_DIR,
    )
    assert "error" not in created, created
    succ_id = created["proposal"]["id"]

    lineage = await mcp_handler._dispatch_mcp_tool(
        "get_proposal_lineage", {"proposal_id": root["id"]}, db, _DATA_DIR,
    )
    assert lineage["successors"][0]["from_proposal_id"] == succ_id

    compared = await mcp_handler._dispatch_mcp_tool(
        "compare_proposal_versions",
        {"from_proposal_id": succ_id, "to_proposal_id": root["id"]},
        db, _DATA_DIR,
    )
    assert compared["adjacent"] is True

    other = await _proposal(db, "E2E Other")
    linked = await mcp_handler._dispatch_mcp_tool(
        "link_proposal_lineage",
        {"from_proposal_id": other["id"], "to_proposal_id": root["id"], "relation_type": "duplicates"},
        db, _DATA_DIR,
    )
    assert "error" not in linked, linked


# ---------------------------------------------------------------------------
# REGRESSION 3 — preview_proposal_promotion must attach "lineage" on BOTH
# branches (already_satisfied, and the normal/would_create path).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_proposal_promotion_normal_branch_has_lineage_field(db):
    """depth='investigation' on a fresh 'raw' proposal takes the NORMAL
    (not-yet-satisfied) branch — this is the far more common real-world
    call shape, and the one the original patch forgot to attach lineage to."""
    pid = await _project(db, "preview-lineage-normal")
    root = await _proposal(db, "Root")
    succ = await db_module.create_proposal_successor(db, root["id"], "v2", "b", "supersedes")

    preview = await proposal_promotion.preview_proposal_promotion(
        db, root["id"], pid, "investigation",
    )
    assert preview["already_satisfied"] is False
    assert "lineage" in preview
    assert preview["lineage"] is not None
    assert preview["lineage"]["successor_count"] == 1
    assert preview["lineage"]["ancestor_count"] == 0
    assert preview["lineage"]["immediate_predecessor_id"] is None

    # And from the successor's own point of view: one ancestor.
    preview_succ = await proposal_promotion.preview_proposal_promotion(
        db, succ["proposal"]["id"], pid, "investigation",
    )
    assert preview_succ["lineage"]["ancestor_count"] == 1
    assert preview_succ["lineage"]["immediate_predecessor_id"] == root["id"]


@pytest.mark.asyncio
async def test_preview_proposal_promotion_already_satisfied_branch_has_lineage_field(db):
    pid = await _project(db, "preview-lineage-satisfied")
    root = await _proposal(db, "Root")  # status='raw' already satisfies depth='proposal'
    preview = await proposal_promotion.preview_proposal_promotion(
        db, root["id"], pid, "proposal",
    )
    assert preview["already_satisfied"] is True
    assert "lineage" in preview
    assert preview["lineage"]["successor_count"] == 0


@pytest.mark.asyncio
async def test_preview_proposal_promotion_lineage_excluded_from_hash(db):
    """A lineage edge created between two previews of the SAME
    content-identical proposal state must not change preview_hash — lineage
    is informational context, not part of the promotion contract the hash
    guards (this is what makes commit_proposal_promotion's freshness check
    immune to a concurrent, unrelated create_proposal_successor call)."""
    pid = await _project(db, "preview-lineage-hash-stable")
    root = await _proposal(db, "Root")

    preview_before = await proposal_promotion.preview_proposal_promotion(
        db, root["id"], pid, "investigation",
    )
    await db_module.create_proposal_successor(db, root["id"], "v2", "b", "supersedes")
    preview_after = await proposal_promotion.preview_proposal_promotion(
        db, root["id"], pid, "investigation",
    )

    assert preview_before["preview_hash"] == preview_after["preview_hash"]
    assert preview_before["lineage"]["successor_count"] == 0
    assert preview_after["lineage"]["successor_count"] == 1


@pytest.mark.asyncio
async def test_preview_proposal_promotion_lineage_is_best_effort(db, monkeypatch):
    pid = await _project(db, "preview-lineage-guarded")
    root = await _proposal(db, "Root")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated lineage lookup failure")

    monkeypatch.setattr(db_module, "get_proposal_ancestors", _boom)
    preview = await proposal_promotion.preview_proposal_promotion(
        db, root["id"], pid, "investigation",
    )
    assert preview["lineage"] is None
    assert "would_create" in preview  # the rest of the preview still computed


# ---------------------------------------------------------------------------
# handoff.build_proposal_lineage_for_handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_proposal_lineage_for_handoff_hydrates_ancestors_and_successors(db):
    pid = await _project(db, "handoff-lineage-hydrated")
    root = await _proposal(db, "Root")
    item = await db_module.add_sprint_item(db, pid, "v1", "Handoff-linked item", force=True)
    await db_module.link_proposal_evidence(db, pid, root["id"], "sprint_item", item["id"])
    succ = await db_module.create_proposal_successor(db, root["id"], "v2", "b", "supersedes")

    result = await handoff_module.build_proposal_lineage_for_handoff(db, pid)
    assert result is not None
    assert len(result) == 1
    entry = result[0]
    assert entry["proposal_id"] == root["id"]
    assert entry["ancestors"] == []
    assert len(entry["successors"]) == 1
    assert entry["successors"][0]["from_proposal_id"] == succ["proposal"]["id"]
    assert entry["descendant_count"] == 1
    assert entry["descendants_truncated"] is False

    # Must survive a JSON round trip unchanged (real handoff consumption path).
    reloaded = _json.loads(_json.dumps(result))
    assert reloaded == result


@pytest.mark.asyncio
async def test_build_proposal_lineage_for_handoff_empty_when_no_evidence_linked_proposals(db):
    """Mirrors build_proposal_evidence_for_handoff's own scope-resolution
    rule verbatim (by design, per this function's own docstring): a
    proposal with REAL lineage relations but NO proposal_evidence_links
    entry in this project is simply not among the candidate ids considered
    — this is not a bug, it keeps proposal_evidence and proposal_lineage
    describing the exact same proposal set for one handoff."""
    pid = await _project(db, "handoff-lineage-no-evidence")
    root = await _proposal(db, "Root")
    await db_module.create_proposal_successor(db, root["id"], "v2", "b", "supersedes")
    result = await handoff_module.build_proposal_lineage_for_handoff(db, pid)
    assert result == []


@pytest.mark.asyncio
async def test_build_proposal_lineage_for_handoff_descendant_truncation_marker(db):
    pid = await _project(db, "handoff-lineage-truncated")
    root = await _proposal(db, "Root")
    item = await db_module.add_sprint_item(db, pid, "v1", "item", force=True)
    await db_module.link_proposal_evidence(db, pid, root["id"], "sprint_item", item["id"])
    for i in range(3):
        await db_module.create_proposal_successor(
            db, root["id"], f"dup-{i}", "b", "duplicates",
        )

    result = await handoff_module.build_proposal_lineage_for_handoff(db, pid, max_items=2)
    entry = result[0]
    assert entry["descendant_count"] == 2
    assert entry["descendants_truncated"] is True


@pytest.mark.asyncio
async def test_build_proposal_lineage_for_handoff_item_ids_scoping(db):
    pid = await _project(db, "handoff-lineage-item-scoped")
    item_a = await db_module.add_sprint_item(db, pid, "v1", "item A", force=True)
    item_b = await db_module.add_sprint_item(db, pid, "v1", "item B", force=True)
    prop_a = await _proposal(db, "Prop A")
    prop_b = await _proposal(db, "Prop B")
    await db_module.link_proposal_evidence(db, pid, prop_a["id"], "sprint_item", item_a["id"])
    await db_module.link_proposal_evidence(db, pid, prop_b["id"], "sprint_item", item_b["id"])
    await db_module.create_proposal_successor(db, prop_a["id"], "A v2", "b", "supersedes")
    await db_module.create_proposal_successor(db, prop_b["id"], "B v2", "b", "supersedes")

    scoped = await handoff_module.build_proposal_lineage_for_handoff(
        db, pid, item_ids=[item_a["id"]],
    )
    assert [e["proposal_id"] for e in scoped] == [prop_a["id"]]


@pytest.mark.asyncio
async def test_build_proposal_lineage_for_handoff_never_raises(db, monkeypatch):
    pid = await _project(db, "handoff-lineage-guarded")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(db_module, "get_proposal_ids_for_project", _boom)
    result = await handoff_module.build_proposal_lineage_for_handoff(db, pid)
    assert result is None


# ---------------------------------------------------------------------------
# End-to-end: generate_handoff (MCP) carries proposal_lineage alongside the
# pre-existing proposal_evidence field.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_mcp_includes_proposal_lineage_field(db, tmp_path):
    pid = await _project(db, "generate-handoff-lineage")
    item = await db_module.add_sprint_item(db, pid, "v1", "linked item", force=True)
    root = await _proposal(db, "Root")
    await db_module.link_proposal_evidence(db, pid, root["id"], "sprint_item", item["id"])
    succ = await db_module.create_proposal_successor(db, root["id"], "v2", "b", "supersedes")

    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": pid, "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert "error" not in result, result
    assert "proposal_lineage" in result
    lineage = result["proposal_lineage"]
    assert lineage is not None
    entry = next(e for e in lineage if e["proposal_id"] == root["id"])
    assert entry["successors"][0]["from_proposal_id"] == succ["proposal"]["id"]
    # Sibling field describing the SAME proposal set.
    evidence_ids = {b["proposal_id"] for b in result["proposal_evidence"]}
    lineage_ids = {e["proposal_id"] for e in lineage}
    assert lineage_ids == evidence_ids


@pytest.mark.asyncio
async def test_generate_handoff_mcp_unscoped_proposal_lineage_empty_when_no_proposals(db, tmp_path):
    pid = await _project(db, "generate-handoff-lineage-empty")
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": pid, "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert "error" not in result, result
    assert result["proposal_lineage"] == []


# ---------------------------------------------------------------------------
# HTTP route wiring — meridian/routes/handoff.py. Mirrors
# test_proposal_evidence_linkage.py's own
# test_http_handoff_endpoint_includes_proposal_evidence_field /
# test_http_planner_handoff_includes_proposal_evidence_field for the sibling
# field, using the same session-scoped ``client`` fixture.
# ---------------------------------------------------------------------------


def test_http_handoff_endpoint_includes_proposal_lineage_field(client):
    pid = client.post("/projects", json={"name": "http-proposal-lineage"}).json()["id"]
    r = client.post(f"/projects/{pid}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert "proposal_lineage" in body
    assert body["proposal_lineage"] == []


def test_http_planner_handoff_includes_proposal_lineage_field(client):
    pid = client.post("/projects", json={"name": "http-proposal-lineage-planner"}).json()["id"]
    r = client.get(f"/projects/{pid}/handoff/planner")
    assert r.status_code == 200
    body = r.json()
    assert "proposal_lineage" in body
    assert body["proposal_lineage"] == []
