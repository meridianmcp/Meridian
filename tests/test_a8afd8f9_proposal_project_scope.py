"""a8afd8f9 — project-scoped proposals as the default; workspace proposals
retained as an explicit opt-in.

Covers the minimal Fix-phase scope agreed in the discovery brief:

  1. add_workspace_proposal(project_id=...) binds scope_type='project' +
     project_id; omitting project_id keeps the unchanged 'workspace' default
     (back-compat for every existing caller, including proposal_promotion.py).
  2. get_workspace_proposals(project_id=...) restricts the listing to that
     project's rows only (excludes workspace-global AND other projects').
  3. promote_workspace_proposal rejects promoting a project-scoped proposal
     into a DIFFERENT project unless allow_project_transfer=True is passed
     with a non-empty transfer_reason (recorded on the 'promoted' event);
     promoting into the SAME project needs no override; a legacy/workspace
     proposal (project_id=None) has nothing to compare against and promotes
     unchanged.
  4. idempotency_key + project_id interaction: a retried call returns the
     ORIGINAL row untouched, even if the retry passes a different project_id.
  5. Migration shape: scope_type/project_id columns exist with the right
     default on a freshly-initialized DB (upgrade path is exercised by every
     other test importing meridian.db, since init_db runs every migration in
     order every time).

Explicitly OUT of scope for this item (see the discovery brief): legacy-row
reclassification/backfill (4eedeef8), and proposal-lineage MCP exposure
(ff1843dc) — neither is touched or asserted against here.
"""
from __future__ import annotations

import pytest

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian import db as db_module
from meridian.mcp.handlers import notes_decisions as nd_mod


_DATA_DIR = "/tmp/meridian-test"


# ---------------------------------------------------------------------------
# Migration shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_proposals_has_scope_columns(db):
    """scope_type defaults to 'workspace'; project_id is nullable."""
    async with db.execute("PRAGMA table_info(workspace_proposals)") as cur:
        cols = {row["name"]: row for row in await cur.fetchall()}
    assert "scope_type" in cols
    assert "project_id" in cols
    # notnull=1 + dflt_value carries the quoted default in SQLite's PRAGMA output.
    assert cols["scope_type"]["notnull"] == 1
    assert "workspace" in str(cols["scope_type"]["dflt_value"])
    assert cols["project_id"]["notnull"] == 0


@pytest.mark.asyncio
async def test_legacy_row_defaults_to_workspace_scope(db):
    """A row inserted with the pre-a8afd8f9 column set (no scope_type/project_id
    passed) still comes back scope_type='workspace', project_id=None — the
    DEFAULT clause applies exactly as it would to a genuinely pre-migration row."""
    prop = await db_module.add_workspace_proposal(db, "Legacy-shaped idea", "body")
    assert prop["scope_type"] == "workspace"
    assert prop.get("project_id") is None


# ---------------------------------------------------------------------------
# add_workspace_proposal(project_id=...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_workspace_proposal_with_project_id_binds_project_scope(db):
    project = await db_module.create_project(db, "a8afd8f9-add-scope")
    prop = await db_module.add_workspace_proposal(
        db, "Cache the parser output", "body", project_id=project["id"],
    )
    assert prop["scope_type"] == "project"
    assert prop["project_id"] == project["id"]


@pytest.mark.asyncio
async def test_add_workspace_proposal_rejects_unknown_project_id(db):
    with pytest.raises(ValueError, match="not found"):
        await db_module.add_workspace_proposal(
            db, "Orphan idea", "body", project_id="no-such-project",
        )
    # Nothing was written for the rejected call.
    async with db.execute(
        "SELECT COUNT(*) AS n FROM workspace_proposals WHERE title = ?",
        ("Orphan idea",),
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"] if isinstance(row, dict) else row[0]) == 0


# ---------------------------------------------------------------------------
# get_workspace_proposals(project_id=...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_proposals_project_filter_excludes_other_scopes(db):
    proj_a = await db_module.create_project(db, "a8afd8f9-list-a")
    proj_b = await db_module.create_project(db, "a8afd8f9-list-b")
    await db_module.add_workspace_proposal(db, "A-scoped", "body", project_id=proj_a["id"])
    await db_module.add_workspace_proposal(db, "B-scoped", "body", project_id=proj_b["id"])
    await db_module.add_workspace_proposal(db, "Workspace-scoped", "body")

    only_a = await db_module.get_workspace_proposals(db, project_id=proj_a["id"])
    titles = {p["title"] for p in only_a}
    assert titles == {"A-scoped"}


@pytest.mark.asyncio
async def test_get_workspace_proposals_without_project_id_is_unchanged(db):
    """Omitting project_id (the default) returns proposals of every scope,
    exactly as before this parameter existed."""
    project = await db_module.create_project(db, "a8afd8f9-list-unfiltered")
    await db_module.add_workspace_proposal(db, "Scoped one", "body", project_id=project["id"])
    await db_module.add_workspace_proposal(db, "Global one", "body")

    everything = await db_module.get_workspace_proposals(db, status="all")
    titles = {p["title"] for p in everything}
    assert {"Scoped one", "Global one"}.issubset(titles)


# ---------------------------------------------------------------------------
# promote_workspace_proposal — project-scope mismatch guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_rejects_cross_project_without_override(db):
    home = await db_module.create_project(db, "a8afd8f9-promote-home")
    other = await db_module.create_project(db, "a8afd8f9-promote-other")
    prop = await db_module.add_workspace_proposal(
        db, "Home idea", "body", project_id=home["id"],
    )
    with pytest.raises(ValueError, match="scoped to project"):
        await db_module.promote_workspace_proposal(db, prop["id"], other["id"])
    # No sprint item was created, and the proposal is still promotable.
    refetched = (await db_module.get_workspace_proposals(db, status="all"))
    row = next(p for p in refetched if p["id"] == prop["id"])
    assert row["status"] == "raw"
    assert row.get("promoted_to_sprint_item_id") is None


@pytest.mark.asyncio
async def test_promote_same_project_needs_no_override(db):
    home = await db_module.create_project(db, "a8afd8f9-promote-same")
    prop = await db_module.add_workspace_proposal(
        db, "Home idea 2", "body", project_id=home["id"],
    )
    result = await db_module.promote_workspace_proposal(db, prop["id"], home["id"])
    assert result["proposal"]["status"] == "promoted"
    assert result["project_id"] == home["id"]


@pytest.mark.asyncio
async def test_promote_cross_project_with_override_succeeds_and_is_audited(db):
    home = await db_module.create_project(db, "a8afd8f9-transfer-home")
    other = await db_module.create_project(db, "a8afd8f9-transfer-other")
    prop = await db_module.add_workspace_proposal(
        db, "Transferable idea", "body", project_id=home["id"],
    )
    result = await db_module.promote_workspace_proposal(
        db, prop["id"], other["id"],
        allow_project_transfer=True,
        transfer_reason="home project was merged into other",
    )
    assert result["proposal"]["status"] == "promoted"
    assert result["project_id"] == other["id"]

    async with db.execute(
        "SELECT payload FROM proposal_events WHERE proposal_id = ? "
        "AND event_type = 'promoted'",
        (prop["id"],),
    ) as cur:
        row = await cur.fetchone()
    payload = row["payload"] if isinstance(row, dict) else row[0]
    assert "project_transfer" in payload
    assert "home project was merged into other" in payload


@pytest.mark.asyncio
async def test_promote_allow_transfer_requires_nonempty_reason(db):
    home = await db_module.create_project(db, "a8afd8f9-transfer-noreason-home")
    other = await db_module.create_project(db, "a8afd8f9-transfer-noreason-other")
    prop = await db_module.add_workspace_proposal(
        db, "No-reason idea", "body", project_id=home["id"],
    )
    with pytest.raises(ValueError, match="transfer_reason"):
        await db_module.promote_workspace_proposal(
            db, prop["id"], other["id"], allow_project_transfer=True,
        )


@pytest.mark.asyncio
async def test_promote_legacy_workspace_proposal_is_unaffected(db):
    """A proposal with no project_id (workspace-scoped, or predating the
    column) promotes into ANY project exactly as it did before this guard
    existed — the mismatch check never fires when there's nothing to compare."""
    target = await db_module.create_project(db, "a8afd8f9-legacy-promote")
    prop = await db_module.add_workspace_proposal(db, "Untethered idea", "body")
    assert prop.get("project_id") is None
    result = await db_module.promote_workspace_proposal(db, prop["id"], target["id"])
    assert result["proposal"]["status"] == "promoted"


# ---------------------------------------------------------------------------
# idempotency_key + project_id interaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_retry_ignores_new_project_id(db):
    """A retried add_workspace_proposal call with the SAME idempotency_key
    returns the ORIGINAL row untouched, even when the retry names a
    different project_id — the original row's scope is never mutated by a
    replay."""
    proj_a = await db_module.create_project(db, "a8afd8f9-idem-a")
    proj_b = await db_module.create_project(db, "a8afd8f9-idem-b")
    first = await db_module.add_workspace_proposal(
        db, "Retried idea", "body",
        project_id=proj_a["id"], idempotency_key="a8afd8f9-retry-1",
    )
    second = await db_module.add_workspace_proposal(
        db, "Retried idea", "body",
        project_id=proj_b["id"], idempotency_key="a8afd8f9-retry-1",
    )
    assert second["id"] == first["id"]
    assert second["project_id"] == proj_a["id"]

    async with db.execute(
        "SELECT COUNT(*) AS n FROM workspace_proposals WHERE title = ?",
        ("Retried idea",),
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"] if isinstance(row, dict) else row[0]) == 1


# ---------------------------------------------------------------------------
# add_proposal MCP handler — explicit scope, never inferred
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_add_proposal_project_scoped(db):
    project = await db_module.create_project(db, "a8afd8f9-handler-project")
    result = await nd_mod.handle_add_proposal(
        {"project_id": project["id"], "title": "Handler idea", "body": "body"},
        db, _DATA_DIR, None, "t-handler-1",
    )
    assert result["scope_type"] == "project"
    assert result["project_id"] == project["id"]


@pytest.mark.asyncio
async def test_handle_add_proposal_explicit_workspace_opt_in(db):
    result = await nd_mod.handle_add_proposal(
        {"title": "Handler workspace idea", "body": "body", "scope": "workspace"},
        db, _DATA_DIR, None, "t-handler-2",
    )
    assert result["scope_type"] == "workspace"
    assert not result.get("project_id")


@pytest.mark.asyncio
async def test_handle_add_proposal_ambiguous_call_is_rejected(db):
    """Neither project_id/project_name nor scope='workspace' -> hard error,
    never a silent guess."""
    result = await nd_mod.handle_add_proposal(
        {"title": "Ambiguous", "body": "body"},
        db, _DATA_DIR, None, "t-handler-3",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_add_proposal_invalid_scope_value_is_rejected(db):
    result = await nd_mod.handle_add_proposal(
        {"title": "Bad scope", "body": "body", "scope": "nonsense"},
        db, _DATA_DIR, None, "t-handler-4",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_promote_proposal_cross_project_needs_override(db):
    home = await db_module.create_project(db, "a8afd8f9-handler-promote-home")
    other = await db_module.create_project(db, "a8afd8f9-handler-promote-other")
    prop = await nd_mod.handle_add_proposal(
        {"project_id": home["id"], "title": "Handler promote idea", "body": "body"},
        db, _DATA_DIR, None, "t-handler-5",
    )
    result = await nd_mod.handle_promote_proposal(
        {"proposal_id": prop["id"], "project_id": other["id"]},
        db, _DATA_DIR, None, "t-handler-5",
    )
    assert "error" in result
    assert "scoped to project" in result["error"]

    overridden = await nd_mod.handle_promote_proposal(
        {
            "proposal_id": prop["id"], "project_id": other["id"],
            "allow_project_transfer": True,
            "transfer_reason": "consolidating into other",
        },
        db, _DATA_DIR, None, "t-handler-5",
    )
    assert "error" not in overridden
    assert overridden["project_id"] == other["id"]
