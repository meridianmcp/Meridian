"""a2bd8c35 — enforce project-scoped proposal ownership and one-to-many
promotion lineage.

Discovery-brief context: the public creation/listing surface
(add_workspace_proposal / get_workspace_proposals) already gained real
project_id ownership + an explicit workspace-wide opt-in via a8afd8f9 (see
tests/test_a8afd8f9_proposal_project_scope.py) — verified NOT re-duplicated
here, only exercised as a precondition. What a8afd8f9 did NOT add is a way
for a promotion to produce more than ONE sprint item per proposal: this file
covers the remaining piece explicitly called out in this item's own goal —
"one proposal to a root investigation plus implementation children, with
durable proposal_evidence_links for each child" — implemented as
``meridian.db.workspace.promote_workspace_proposal_with_children``.

Explicitly OUT of scope (separate, already-filed items; not duplicated here
per this item's own coordination note):
  * proposal-to-proposal typed lineage relations (ff1843dc) —
    meridian/db/proposal_lineage.py and its own test file are untouched.
  * the ce4883f3 preview/commit depth-based promotion contract
    (meridian/proposal_promotion.py) — untouched; this new function sits
    alongside it as an additional promotion entry point, not a replacement.
  * legacy family_id backfill/reclassification (40786e0d).
  * the deeper cross-project isolation sweep (efea329f) — this file adds
    focused isolation/authorization regression coverage for the NEW function
    only, not a general audit of the whole proposal surface.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.db import workspace as workspace_mod


async def _count(db, table: str, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


async def _children_of(db, parent_id: str) -> list[dict]:
    async with db.execute(
        "SELECT * FROM sprint_items WHERE parent_id = ? ORDER BY title ASC",
        (parent_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [db_module._row_to_dict(r) for r in rows if r is not None]


# ---------------------------------------------------------------------------
# Validation — fail closed BEFORE any write.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_with_children_requires_nonempty_children(db):
    project = await db_module.create_project(db, "a2bd8c35-empty-children")
    prop = await db_module.add_workspace_proposal(
        db, "Needs children", "body", project_id=project["id"],
    )
    with pytest.raises(ValueError, match="at least one"):
        await workspace_mod.promote_workspace_proposal_with_children(
            db, prop["id"], project["id"], children=[],
        )
    # Nothing was written — the proposal is still unpromoted.
    refreshed = (await db_module.get_workspace_proposals(db, status="all"))
    row = next(p for p in refreshed if p["id"] == prop["id"])
    assert row["status"] == "raw"
    assert await _count(db, "sprint_items") == 0


@pytest.mark.asyncio
async def test_promote_with_children_rejects_blank_title(db):
    project = await db_module.create_project(db, "a2bd8c35-blank-title")
    prop = await db_module.add_workspace_proposal(
        db, "Blank child title", "body", project_id=project["id"],
    )
    with pytest.raises(ValueError, match="non-blank 'title'"):
        await workspace_mod.promote_workspace_proposal_with_children(
            db, prop["id"], project["id"],
            children=[{"title": "Real child"}, {"title": "   "}],
        )
    # Fail-closed BEFORE any write: not even the root investigation exists.
    assert await _count(db, "sprint_items") == 0
    refreshed = (await db_module.get_workspace_proposals(db, status="all"))
    row = next(p for p in refreshed if p["id"] == prop["id"])
    assert row["status"] == "raw"


@pytest.mark.asyncio
async def test_promote_with_children_rejects_invalid_owner(db):
    project = await db_module.create_project(db, "a2bd8c35-bad-owner")
    prop = await db_module.add_workspace_proposal(
        db, "Bad owner", "body", project_id=project["id"],
    )
    with pytest.raises(ValueError, match="owner must be"):
        await workspace_mod.promote_workspace_proposal_with_children(
            db, prop["id"], project["id"],
            children=[{"title": "Child", "owner": "robot"}],
        )
    assert await _count(db, "sprint_items") == 0


@pytest.mark.asyncio
async def test_promote_with_children_rejects_non_dict_child(db):
    project = await db_module.create_project(db, "a2bd8c35-non-dict-child")
    prop = await db_module.add_workspace_proposal(
        db, "Non-dict child", "body", project_id=project["id"],
    )
    with pytest.raises(ValueError, match="must be an object"):
        await workspace_mod.promote_workspace_proposal_with_children(
            db, prop["id"], project["id"], children=["just a string"],
        )
    assert await _count(db, "sprint_items") == 0


# ---------------------------------------------------------------------------
# Root + children creation shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_with_children_creates_root_and_children_with_parent_id(db):
    project = await db_module.create_project(db, "a2bd8c35-root-children")
    prop = await db_module.add_workspace_proposal(
        db, "Fan-out investigation", "body", project_id=project["id"],
    )
    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], project["id"],
        children=[{"title": "Implement part A"}, {"title": "Implement part B"}],
    )
    root_id = result["root_sprint_item_id"]
    assert root_id == result["sprint_item_id"]
    assert result["root_sprint_item_title"] == "Fan-out investigation"
    assert len(result["children"]) == 2
    child_titles = {c["title"] for c in result["children"]}
    assert child_titles == {"Implement part A", "Implement part B"}

    # The root investigation itself is a real, promoted sprint item.
    async with db.execute(
        "SELECT * FROM sprint_items WHERE id = ?", (root_id,),
    ) as cur:
        root_row = db_module._row_to_dict(await cur.fetchone())
    assert root_row is not None
    assert root_row["parent_id"] is None

    # Every child is a real sprint_items row with parent_id = the root.
    children_rows = await _children_of(db, root_id)
    assert len(children_rows) == 2
    for row in children_rows:
        assert row["project_id"] == project["id"]
        assert row["status"] == "pending"

    refreshed = (await db_module.get_workspace_proposals(db, status="all"))
    proposal_row = next(p for p in refreshed if p["id"] == prop["id"])
    assert proposal_row["status"] == "promoted"
    assert proposal_row["promoted_to_sprint_item_id"] == root_id


@pytest.mark.asyncio
async def test_promote_with_children_children_inherit_root_version(db):
    project = await db_module.create_project(db, "a2bd8c35-version-inherit")
    prop = await db_module.add_workspace_proposal(
        db, "Versioned investigation", "body", project_id=project["id"],
    )
    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], project["id"],
        children=[{"title": "Child A"}, {"title": "Child B"}],
        sprint_item_version="v2.5",
    )
    root_id = result["root_sprint_item_id"]
    children_rows = await _children_of(db, root_id)
    assert {row["version"] for row in children_rows} == {"v2.5"}


@pytest.mark.asyncio
async def test_promote_with_children_child_touches_resources_serialized(db):
    project = await db_module.create_project(db, "a2bd8c35-child-resources")
    prop = await db_module.add_workspace_proposal(
        db, "Resource-scoped children", "body", project_id=project["id"],
    )
    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], project["id"],
        children=[
            {"title": "Touches a file", "touches_resources": ["file:meridian/example.py"]},
            {"title": "No resources declared"},
        ],
    )
    root_id = result["root_sprint_item_id"]
    children_rows = {row["title"]: row for row in await _children_of(db, root_id)}
    scoped = children_rows["Touches a file"]
    unscoped = children_rows["No resources declared"]
    assert scoped["touches_resources"] is not None
    assert "file:meridian/example.py" in scoped["touches_resources"]
    assert unscoped["touches_resources"] is None


@pytest.mark.asyncio
async def test_promote_with_children_owner_is_recorded_unchained(db):
    """Owner is stored per child, but (unlike add_subtask) children here are
    never chained via depends_on — every child is independently claimable."""
    project = await db_module.create_project(db, "a2bd8c35-owner-unchained")
    prop = await db_module.add_workspace_proposal(
        db, "Owned children", "body", project_id=project["id"],
    )
    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], project["id"],
        children=[
            {"title": "Human step", "owner": "human"},
            {"title": "AI step", "owner": "ai"},
        ],
    )
    root_id = result["root_sprint_item_id"]
    children_rows = {row["title"]: row for row in await _children_of(db, root_id)}
    assert children_rows["Human step"]["owner"] == "human"
    assert children_rows["AI step"]["owner"] == "ai"
    assert children_rows["Human step"]["depends_on"] is None
    assert children_rows["AI step"]["depends_on"] is None


# ---------------------------------------------------------------------------
# Durable proposal_evidence_links for the root AND every child
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_with_children_links_evidence_for_root_and_each_child(db):
    project = await db_module.create_project(db, "a2bd8c35-evidence-links")
    prop = await db_module.add_workspace_proposal(
        db, "Evidence-linked investigation", "body", project_id=project["id"],
    )
    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], project["id"],
        children=[{"title": "Implement part A"}, {"title": "Implement part B"}],
    )
    root_id = result["root_sprint_item_id"]
    child_ids = {c["id"] for c in result["children"]}
    for child in result["children"]:
        assert child["evidence_link"] is not None
        assert child["evidence_link"]["entity_id"] == child["id"]
        assert child["evidence_link"]["entity_type"] == "sprint_item"

    links = await db_module.get_proposal_links(db, project["id"], prop["id"])
    linked_sprint_item_ids = {
        link["entity_id"] for link in links if link["entity_type"] == "sprint_item"
    }
    # The root (linked by promote_workspace_proposal itself) AND every child.
    assert root_id in linked_sprint_item_ids
    assert child_ids.issubset(linked_sprint_item_ids)

    evidence = await db_module.get_proposal_evidence(db, project["id"], prop["id"])
    hydrated_ids = {item["id"] for item in evidence["sprint_items"]}
    assert root_id in hydrated_ids
    assert child_ids.issubset(hydrated_ids)


@pytest.mark.asyncio
async def test_promote_with_children_evidence_link_failure_is_non_fatal(db, monkeypatch):
    """Mirrors promote_workspace_proposal's own best-effort evidence-link
    behavior: a failure linking one child's evidence must never retroactively
    fail a promotion whose sprint items already committed."""
    project = await db_module.create_project(db, "a2bd8c35-evidence-failure")
    prop = await db_module.add_workspace_proposal(
        db, "Evidence link may fail", "body", project_id=project["id"],
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated evidence-link outage")

    # promote_workspace_proposal_with_children lazily re-imports
    # link_proposal_evidence from meridian.db on every call (same pattern
    # promote_workspace_proposal itself uses for its own root-evidence link)
    # -- patching the package-level attribute is what that fresh import sees.
    import meridian.db as db_pkg
    monkeypatch.setattr(db_pkg, "link_proposal_evidence", _boom, raising=False)

    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], project["id"],
        children=[{"title": "Child A"}],
    )
    assert result["children"][0]["evidence_link"] is None
    # The child sprint item itself was NOT rolled back just because linking failed.
    root_id = result["root_sprint_item_id"]
    children_rows = await _children_of(db, root_id)
    assert len(children_rows) == 1


# ---------------------------------------------------------------------------
# Cross-project ownership / isolation for the new function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_with_children_cross_project_without_override_rejected(db):
    home = await db_module.create_project(db, "a2bd8c35-cross-home")
    other = await db_module.create_project(db, "a2bd8c35-cross-other")
    prop = await db_module.add_workspace_proposal(
        db, "Home-scoped investigation", "body", project_id=home["id"],
    )
    with pytest.raises(ValueError, match="scoped to project"):
        await workspace_mod.promote_workspace_proposal_with_children(
            db, prop["id"], other["id"],
            children=[{"title": "Should never be created"}],
        )
    # Nothing was written for either project — root creation itself is
    # rejected by promote_workspace_proposal before this function's own
    # child-creation loop ever runs.
    assert await _count(db, "sprint_items") == 0
    refreshed = (await db_module.get_workspace_proposals(db, status="all"))
    row = next(p for p in refreshed if p["id"] == prop["id"])
    assert row["status"] == "raw"


@pytest.mark.asyncio
async def test_promote_with_children_cross_project_with_override_succeeds(db):
    home = await db_module.create_project(db, "a2bd8c35-transfer-home")
    other = await db_module.create_project(db, "a2bd8c35-transfer-other")
    prop = await db_module.add_workspace_proposal(
        db, "Transferable investigation", "body", project_id=home["id"],
    )
    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], other["id"],
        children=[{"title": "Child in the new project"}],
        allow_project_transfer=True,
        transfer_reason="home project deprecated",
    )
    assert result["project_id"] == other["id"]
    root_id = result["root_sprint_item_id"]
    children_rows = await _children_of(db, root_id)
    assert len(children_rows) == 1
    assert children_rows[0]["project_id"] == other["id"]


@pytest.mark.asyncio
async def test_promote_with_children_ignores_title_tags_item_group_as_authorization(db):
    """The Goal's own explicit requirement: ownership is decided SOLELY by
    the stored project_id column, never by parsing title/tags/item_group
    text — even when that text names a DIFFERENT project outright."""
    home = await db_module.create_project(db, "a2bd8c35-authz-home")
    decoy = await db_module.create_project(db, "a2bd8c35-authz-decoy")
    prop = await db_module.add_workspace_proposal(
        db,
        title=f"[project:{decoy['id']}] Looks like it belongs to decoy",
        body="This body also mentions the decoy project id: " + decoy["id"],
        tags=f"project:{decoy['id']},decoy-owned",
        project_id=home["id"],
    )
    # Promoting into the DECOY project (which the title/tags/body all claim
    # ownership of) must still be rejected as a cross-project mismatch,
    # because the proposal's real, structural owner is `home`, not `decoy`.
    with pytest.raises(ValueError, match="scoped to project"):
        await workspace_mod.promote_workspace_proposal_with_children(
            db, prop["id"], decoy["id"],
            children=[{"title": "Should never be created"}],
        )
    # Promoting into its REAL project_id (home) succeeds regardless of what
    # the free-text fields claim.
    result = await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], home["id"],
        children=[{"title": "Legitimately home-scoped child"}],
    )
    assert result["project_id"] == home["id"]


@pytest.mark.asyncio
async def test_promote_with_children_evidence_isolated_from_other_projects(db):
    """get_proposal_evidence's own project scoping (already covered by its
    dedicated test file) composes correctly with the NEW children: evidence
    hydration scoped to a project that does NOT own this proposal's sprint
    items returns nothing for them."""
    home = await db_module.create_project(db, "a2bd8c35-isolation-home")
    unrelated = await db_module.create_project(db, "a2bd8c35-isolation-unrelated")
    prop = await db_module.add_workspace_proposal(
        db, "Isolated investigation", "body", project_id=home["id"],
    )
    await workspace_mod.promote_workspace_proposal_with_children(
        db, prop["id"], home["id"],
        children=[{"title": "Home child"}],
    )
    evidence_from_unrelated_project = await db_module.get_proposal_evidence(
        db, unrelated["id"], prop["id"],
    )
    assert evidence_from_unrelated_project["sprint_items"] == []
    assert evidence_from_unrelated_project["link_count"] == 0


# ---------------------------------------------------------------------------
# Partial-failure rollback: only the children THIS call inserted, never the
# already-committed root promotion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_with_children_partial_failure_rolls_back_only_children(db, monkeypatch):
    project = await db_module.create_project(db, "a2bd8c35-partial-failure")
    prop = await db_module.add_workspace_proposal(
        db, "Partial failure idea", "body", project_id=project["id"],
    )

    real_new_id = workspace_mod._new_id
    ids: dict[str, str] = {}
    call_count = {"n": 0}

    def fake_new_id():
        call_count["n"] += 1
        if call_count["n"] == 1:
            ids["root"] = real_new_id()
            return ids["root"]
        if call_count["n"] == 2:
            ids["child_a"] = real_new_id()
            return ids["child_a"]
        # 3rd call (child B's id): deliberately collide with child A's own
        # id, forcing a PRIMARY KEY violation on the second child's INSERT.
        return ids["child_a"]

    monkeypatch.setattr(workspace_mod, "_new_id", fake_new_id)

    with pytest.raises(Exception):
        await workspace_mod.promote_workspace_proposal_with_children(
            db, prop["id"], project["id"],
            children=[{"title": "Child A"}, {"title": "Child B"}],
        )

    # The failed batch's children are fully compensated -- none survive.
    assert await _count(db, "sprint_items", "parent_id IS NOT NULL") == 0

    # But the root investigation itself (already committed by
    # promote_workspace_proposal before children were attempted) is untouched.
    refreshed = (await db_module.get_workspace_proposals(db, status="all"))
    root_proposal = next(p for p in refreshed if p["id"] == prop["id"])
    assert root_proposal["status"] == "promoted"
    assert root_proposal["promoted_to_sprint_item_id"] == ids["root"]
    async with db.execute(
        "SELECT COUNT(*) AS n FROM sprint_items WHERE id = ?", (ids["root"],),
    ) as cur:
        row = await cur.fetchone()
    assert int(row["n"] if isinstance(row, dict) else row[0]) == 1
