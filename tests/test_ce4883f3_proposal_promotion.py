"""Tests for ce4883f3 — configurable proposal-to-handoff planner workflow.

Covers the orchestration layer in ``meridian.proposal_promotion``:
``preview_proposal_promotion`` (read-only) and ``commit_proposal_promotion``
(the write path), built on top of the EXISTING, already-tested proposal
lifecycle in ``meridian.db.workspace`` and wave computation in
``meridian.db.sprint_items.get_parallelizable_groups`` — this file targets
the NEW orchestration (depth selection, preview/commit hash freshness,
deviation/HITL routing, handoff scoping), not the underlying primitives
those already have their own coverage (test_workspace_proposals.py,
test_proposals.py, test_a56f0951_promote_proposal_touches_resources.py,
test_handoff_item_selection.py).

Every test passes ``infer_touches_resources=False`` unless explicitly
testing inference, so nothing here depends on this worktree's live git
history (``_infer_touches_resources_from_proposal`` shells out to
``git diff --name-only HEAD~3``).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian import db as db_module
from meridian import proposal_promotion


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _proposal(db, title: str = "Idea", body: str = "body", **kwargs):
    return await db_module.add_workspace_proposal(db, title, body, **kwargs)


# ---------------------------------------------------------------------------
# Depth validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_depth_rejected_with_clear_error(db):
    pid = await _project(db, "unknown-depth")
    proposal = await _proposal(db)
    with pytest.raises(ValueError, match="Unknown promotion depth"):
        await proposal_promotion.preview_proposal_promotion(
            db, proposal["id"], pid, "not_a_real_depth",
        )


# ---------------------------------------------------------------------------
# Preview at every depth for a fresh proposal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", list(proposal_promotion.PROMOTION_DEPTHS))
async def test_preview_every_depth_for_fresh_proposal(db, depth):
    pid = await _project(db, f"preview-depth-{depth}")
    proposal = await _proposal(db, "Fresh idea", "needs work")

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, depth, infer_touches_resources=False,
    )

    assert preview["proposal_id"] == proposal["id"]
    assert preview["project_id"] == pid
    assert preview["depth"] == depth
    assert preview["preview_hash"].startswith("sha256:")
    assert preview["computed_at"]

    if depth == "proposal":
        # rank 0: a fresh (status='raw') proposal trivially satisfies the
        # shallowest depth -- intake/scope already exists the moment the
        # proposal row does. Nothing further to assert at this depth.
        assert preview["already_satisfied"] is True
        return

    assert preview["already_satisfied"] is False
    contract = preview["contract_status"]
    # intake/scope is trivially satisfied the moment the proposal exists.
    assert contract["intake_source_scope"] == "present"
    # tools/tests/evidence/rollback and the deviation block are never
    # established by this module -- always an honest not_applicable.
    assert contract["tools_tests_evidence_rollback"] == "not_applicable"
    assert contract["deviation_block"] == "not_applicable"

    rank = proposal_promotion._DEPTH_RANK[depth]
    if rank >= proposal_promotion._DEPTH_RANK["investigation"]:
        assert contract["investigation_findings"] == "optional_at_commit"
        assert preview["would_create"]["status_transition"] == {
            "from": "raw", "to": "investigating",
        }
    else:
        assert contract["investigation_findings"] == "not_applicable"

    if rank >= proposal_promotion._DEPTH_RANK["pointers"]:
        assert contract["pointers"] == "optional_at_commit"
    else:
        assert contract["pointers"] == "not_applicable"

    if rank >= proposal_promotion._DEPTH_RANK["sprint_items"]:
        assert contract["sprint_items"] == "would_create"
        assert contract["ownership_dependency_frontier"] == "would_create"
        assert contract["wave_gates"] == "present"
        assert preview["would_create"]["sprint_item"]["title"] == "Fresh idea"
        assert preview["wave_preview"] is not None
    else:
        assert contract["sprint_items"] == "not_applicable"
        assert contract["wave_gates"] == "not_applicable"
        assert preview["wave_preview"] is None

    if rank >= proposal_promotion._DEPTH_RANK["executable_handoff"]:
        assert "handoff" in preview["would_create"]


# ---------------------------------------------------------------------------
# already_satisfied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_already_satisfied_for_already_promoted_proposal(db):
    pid = await _project(db, "already-promoted")
    proposal = await _proposal(db, "Promote me", "body")
    await db_module.promote_workspace_proposal(db, proposal["id"], pid)

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )

    assert preview["already_satisfied"] is True
    assert preview["contract_status"] is None
    assert preview["would_create"] is None
    assert preview["wave_preview"] is None
    assert "already" in preview["reason"].lower()

    # Shallower depths are satisfied too.
    shallow = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "investigation", infer_touches_resources=False,
    )
    assert shallow["already_satisfied"] is True

    # The deepest depth is NOT considered satisfied by status alone -- a
    # handoff is a repeatable action, not a one-time state transition.
    deepest = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "executable_handoff", infer_touches_resources=False,
    )
    assert deepest["already_satisfied"] is False


# ---------------------------------------------------------------------------
# Commit at sprint_items depth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_sprint_items_creates_exactly_one_sprint_item(db):
    pid = await _project(db, "commit-sprint-items")
    proposal = await _proposal(db, "Ship the feature", "implementation notes")

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )
    result = await proposal_promotion.commit_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", preview["preview_hash"],
        infer_touches_resources=False,
    )

    assert result["already_satisfied"] is False
    assert result["deviation"] is None
    assert result["hitl_pending"] is False
    sprint_item = result["committed"]["sprint_item"]
    assert sprint_item["reused_existing"] is False
    assert sprint_item["id"]

    async with db.execute("SELECT COUNT(*) AS n FROM sprint_items") as cur:
        row = await cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    assert n == 1

    proposals = await db_module.get_workspace_proposals(db, status="all")
    assert proposals[0]["status"] == "promoted"
    assert proposals[0]["promoted_to_sprint_item_id"] == sprint_item["id"]


# ---------------------------------------------------------------------------
# Commit at executable_handoff depth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_executable_handoff_scopes_pending_batch(db, tmp_path):
    pid = await _project(db, "commit-handoff-scope")
    unrelated = await db_module.add_sprint_item(
        db, pid, "current", "unrelated pending item", force=True,
    )
    proposal = await _proposal(db, "New capability", "do the thing")

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "executable_handoff", infer_touches_resources=False,
    )
    result = await proposal_promotion.commit_proposal_promotion(
        db, proposal["id"], pid, "executable_handoff", preview["preview_hash"],
        infer_touches_resources=False, data_dir=str(tmp_path),
    )

    assert result["deviation"] is None
    assert result["hitl_pending"] is False
    sprint_item_id = result["committed"]["sprint_item"]["id"]
    handoff_info = result["committed"]["handoff"]
    assert handoff_info["path"]

    content = Path(handoff_info["path"]).read_text(encoding="utf-8")
    assert sprint_item_id in content
    assert unrelated["id"] not in content


@pytest.mark.asyncio
async def test_commit_executable_handoff_requires_data_dir(db):
    pid = await _project(db, "handoff-missing-data-dir")
    proposal = await _proposal(db, "Needs data dir", "body")

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "executable_handoff", infer_touches_resources=False,
    )
    with pytest.raises(ValueError, match="data_dir"):
        await proposal_promotion.commit_proposal_promotion(
            db, proposal["id"], pid, "executable_handoff", preview["preview_hash"],
            infer_touches_resources=False,
        )


# ---------------------------------------------------------------------------
# Stale preview_hash rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_preview_hash_rejected_and_nothing_written(db):
    pid = await _project(db, "stale-preview")
    proposal = await _proposal(db, "Stale preview target", "body")

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )

    # Mutate the proposal via a DIFFERENT call in between preview and commit.
    await db_module.advance_workspace_proposal_status(
        db, proposal["id"], "investigating",
    )

    with pytest.raises(proposal_promotion.StalePreviewError):
        await proposal_promotion.commit_proposal_promotion(
            db, proposal["id"], pid, "sprint_items", preview["preview_hash"],
            infer_touches_resources=False,
        )

    async with db.execute("SELECT COUNT(*) AS n FROM sprint_items") as cur:
        row = await cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    assert n == 0, "stale commit must not have created a sprint item"

    proposals = await db_module.get_workspace_proposals(db, status="all")
    assert proposals[0]["status"] == "investigating"  # only the mutating call's effect


# ---------------------------------------------------------------------------
# Idempotent commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_commit_twice_at_same_depth_is_noop_second_time(db):
    pid = await _project(db, "idempotent-commit")
    proposal = await _proposal(db, "Idempotent target", "body")

    preview1 = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )
    result1 = await proposal_promotion.commit_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", preview1["preview_hash"],
        infer_touches_resources=False,
    )
    assert result1["already_satisfied"] is False
    first_item_id = result1["committed"]["sprint_item"]["id"]

    # Fresh preview computed AFTER the first commit -- this is the
    # already_satisfied=true path now.
    preview2 = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )
    assert preview2["already_satisfied"] is True
    result2 = await proposal_promotion.commit_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", preview2["preview_hash"],
        infer_touches_resources=False,
    )
    assert result2["already_satisfied"] is True
    assert result2["committed"] == {}

    async with db.execute("SELECT COUNT(*) AS n FROM sprint_items") as cur:
        row = await cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    assert n == 1, "second commit must not create a duplicate sprint item"

    proposals = await db_module.get_workspace_proposals(db, status="all")
    assert proposals[0]["promoted_to_sprint_item_id"] == first_item_id


# ---------------------------------------------------------------------------
# Genuine race: two concurrent commits at sprint_items depth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_commits_exactly_one_winner_loser_reports_honestly(db):
    pid = await _project(db, "concurrent-commit-race")
    proposal = await _proposal(db, "Race target", "body")

    # Both callers preview from the SAME pre-race state so both hashes are
    # valid against a fresh re-preview at the moment each commit call
    # actually starts (neither has committed yet).
    preview_a = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )
    preview_b = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )
    assert preview_a["preview_hash"] == preview_b["preview_hash"]

    async def _attempt(preview_hash: str):
        return await proposal_promotion.commit_proposal_promotion(
            db, proposal["id"], pid, "sprint_items", preview_hash,
            infer_touches_resources=False,
        )

    results = await asyncio.gather(
        _attempt(preview_a["preview_hash"]), _attempt(preview_b["preview_hash"]),
    )

    succeeded = [r for r in results if r.get("deviation") is None]
    raced = [r for r in results if r.get("deviation") is not None]

    assert len(succeeded) == 1, f"expected exactly 1 real winner, got {results}"
    assert len(raced) == 1
    assert raced[0]["deviation"]["category"] == "race_retry"
    assert raced[0]["hitl_pending"] is False
    assert "error" in raced[0]  # the loser's failure is reported, not swallowed

    async with db.execute("SELECT COUNT(*) AS n FROM sprint_items") as cur:
        row = await cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    assert n == 1, "a lost race must never create a duplicate sprint item"

    # The loser's honest-failure event is durably recorded.
    async with db.execute(
        "SELECT COUNT(*) AS n FROM proposal_events WHERE proposal_id = ? "
        "AND event_type = 'deviation_auto_resolved'",
        (proposal["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert (row["n"] if isinstance(row, dict) else row[0]) == 1


# ---------------------------------------------------------------------------
# Wave / dependency preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wave_preview_reports_conflict_with_existing_item(db):
    pid = await _project(db, "wave-conflict")
    existing = await db_module.add_sprint_item(
        db, pid, "current", "existing item touching hosted.py",
        touches_resources=["file:meridian/hosted.py"], force=True,
    )
    proposal = await _proposal(db, "Also touches hosted.py", "body")

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items",
        touches_resources=["file:meridian/hosted.py"], infer_touches_resources=False,
    )

    wave_preview = preview["wave_preview"]
    assert wave_preview is not None
    assert wave_preview["declared_resources"] == ["file:meridian/hosted.py"]
    conflict_item_ids = {
        item_id
        for conflict in wave_preview["conflicts"]
        for item_id in conflict["conflicting_item_ids"]
    }
    assert existing["id"] in conflict_item_ids


@pytest.mark.asyncio
async def test_wave_preview_no_resources_reports_new_singleton_group(db):
    pid = await _project(db, "wave-no-resources")
    proposal = await _proposal(db, "Nothing declared", "body")

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items",
        touches_resources=[], infer_touches_resources=False,
    )

    assert preview["wave_preview"]["would_join"] == "new_singleton_group"
    assert preview["wave_preview"]["conflicts"] == []


# ---------------------------------------------------------------------------
# Tenant / project isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_preview_and_commit_refused(db):
    pid = await _project(db, "cross-tenant-target")
    proposal = await _proposal(db, "Tenant A idea", "body", tenant_id="tenant-a")

    with pytest.raises(ValueError, match="not found"):
        await proposal_promotion.preview_proposal_promotion(
            db, proposal["id"], pid, "sprint_items",
            tenant_id="tenant-b", infer_touches_resources=False,
        )

    with pytest.raises(ValueError, match="not found"):
        await proposal_promotion.commit_proposal_promotion(
            db, proposal["id"], pid, "sprint_items", "sha256:doesnotmatter",
            tenant_id="tenant-b", infer_touches_resources=False,
        )

    async with db.execute("SELECT COUNT(*) AS n FROM sprint_items") as cur:
        row = await cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    assert n == 0


# ---------------------------------------------------------------------------
# HITL routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_files_hitl_for_destructive_behavior_deviation(db):
    pid = await _project(db, "destructive-deviation")
    proposal = await _proposal(
        db, "Purge stale rows",
        "This proposal will purge and delete old records directly from production.",
    )

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "executable_handoff", infer_touches_resources=False,
    )
    result = await proposal_promotion.commit_proposal_promotion(
        db, proposal["id"], pid, "executable_handoff", preview["preview_hash"],
        infer_touches_resources=False,
    )

    assert result["hitl_pending"] is True
    assert result["hitl_request_id"]
    assert result["deviation"]["category"] == "destructive_behavior"
    # The remaining step (handoff generation) must NOT have executed --
    # committed carries only what happened before the HITL gate (the sprint
    # item), never a "handoff" key.
    assert "handoff" not in result["committed"]

    hitl_row = await db_module.get_hitl_request(db, result["hitl_request_id"])
    assert hitl_row is not None
    assert hitl_row["kind"] == "proposal_deviation"

    # The sprint item itself WAS created (the deviation is detected only
    # after the item exists, per the module's own documented design).
    async with db.execute("SELECT COUNT(*) AS n FROM sprint_items") as cur:
        row = await cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    assert n == 1


@pytest.mark.asyncio
async def test_override_reason_bypasses_hitl_and_proceeds(db):
    pid = await _project(db, "destructive-deviation-override")
    proposal = await _proposal(
        db, "Purge stale rows",
        "This proposal will purge and delete old records.",
    )

    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", infer_touches_resources=False,
    )
    result = await proposal_promotion.commit_proposal_promotion(
        db, proposal["id"], pid, "sprint_items", preview["preview_hash"],
        infer_touches_resources=False,
        override_reason="Reviewed manually -- this only purges a test fixture table.",
    )

    assert result["hitl_pending"] is False
    assert result["deviation"] is None
    assert result["committed"]["sprint_item"]["id"]

    async with db.execute(
        "SELECT COUNT(*) AS n FROM proposal_events WHERE proposal_id = ? "
        "AND event_type = 'deviation_override'",
        (proposal["id"],),
    ) as cur:
        row = await cur.fetchone()
    assert (row["n"] if isinstance(row, dict) else row[0]) == 1
