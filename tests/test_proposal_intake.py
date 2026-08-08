"""Tests for 3f892ea6 — deterministic proposal intake blocks with
provenance-preserving route review and explicit sprint promotion.

Covers:
1. parse_proposal_intake_blocks: exact text/hash/line provenance.
2. Marker routing ([MERIDIAN-DOCS]/[SERENA]/[MERIDIAN-OUTPUTS]/[HUMAN]/[SPRINT]).
3. Unmarked (and unrecognized-marker) blocks stay unresolved, never guessed.
4. Triple-quoted / fenced live-code blocks are excluded from task creation.
5. Duplicate detection (identical block text flagged, canonical preserved).
6. Idempotent re-ingest (unchanged body -> no new rows).
7. Source-hash change detection (edited body -> revision bump + history).
8. No auto-promotion (ingest never creates a sprint item).
9. Explicit promotion + durable backlink (proposal_evidence_links + draft
   promotion fields).
10. Cross-project isolation (drafts from one proposal promoted into two
    different projects land as fully separate, correctly-scoped sprint items).
11. Deterministic ordering across repeated ingests.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian.db.workspace import parse_proposal_intake_blocks


# ---------------------------------------------------------------------------
# Pure-function tests: parse_proposal_intake_blocks (no DB needed)
# ---------------------------------------------------------------------------


def test_parse_blocks_exact_text_hash_and_line_provenance():
    body = "First block line one.\nStill first block.\n\nSecond block."
    blocks = parse_proposal_intake_blocks("prop-1", body)

    assert len(blocks) == 2
    first, second = blocks

    assert first["block_id"] == "b1"
    assert first["text"] == "First block line one.\nStill first block."
    assert first["line_start"] == 1
    assert first["line_end"] == 2
    import hashlib
    assert first["source_hash"] == hashlib.sha256(first["text"].encode("utf-8")).hexdigest()

    assert second["block_id"] == "b2"
    assert second["text"] == "Second block."
    assert second["line_start"] == 4
    assert second["line_end"] == 4
    assert second["source_hash"] == hashlib.sha256(second["text"].encode("utf-8")).hexdigest()

    # intake_key is deterministic given the same (proposal_id, block_id).
    assert first["intake_key"] == parse_proposal_intake_blocks("prop-1", body)[0]["intake_key"]
    # ...but differs across proposals for the identical block position/text.
    assert first["intake_key"] != parse_proposal_intake_blocks("prop-2", body)[0]["intake_key"]


def test_parse_blocks_marker_routing_all_five_markers():
    body = (
        "[MERIDIAN-DOCS] Update the docs.\n\n"
        "[SERENA] Refactor the symbol.\n\n"
        "[MERIDIAN-OUTPUTS] Check the run outputs.\n\n"
        "[HUMAN] Needs a real decision.\n\n"
        "[SPRINT] Ship this as a sprint item."
    )
    blocks = parse_proposal_intake_blocks("prop-routes", body)
    routes = [b["route"] for b in blocks]
    assert routes == [
        "meridian_docs_review",
        "meridian_code_review",
        "meridian_outputs_review",
        "human_decision_review",
        "sprint_item_review",
    ]


def test_parse_blocks_unmarked_and_unrecognized_marker_stay_unresolved():
    body = "Just a plain idea with no marker.\n\n[TODO] Looks like a marker but isn't one."
    blocks = parse_proposal_intake_blocks("prop-unresolved", body)
    assert len(blocks) == 2
    assert blocks[0]["route"] is None
    assert blocks[1]["route"] is None  # [TODO] is not one of the five explicit markers


def test_parse_blocks_candidate_ids_extracted():
    body = "Source proposal: 8d957245-49a5-433c-8644-14f1b6b0074d.\n\nNo ids here."
    blocks = parse_proposal_intake_blocks("prop-cand", body)
    assert blocks[0]["candidate_ids"] == ["8d957245-49a5-433c-8644-14f1b6b0074d"]
    assert blocks[1]["candidate_ids"] == []


def test_parse_blocks_fenced_code_block_excluded_and_is_code_flagged():
    body = (
        "[SPRINT] Ship the fix.\n\n"
        "```python\n"
        "def f():\n"
        "\n"
        "    return 1\n"
        "```\n\n"
        "[HUMAN] Decide the rollout plan."
    )
    blocks = parse_proposal_intake_blocks("prop-code", body)
    assert len(blocks) == 3
    assert blocks[0]["is_code"] is False
    assert blocks[1]["is_code"] is True
    assert blocks[1]["route"] is None
    assert blocks[1]["candidate_ids"] == []
    # The blank line INSIDE the fence must not split it into two blocks.
    assert "def f():" in blocks[1]["text"] and "return 1" in blocks[1]["text"]
    assert blocks[2]["is_code"] is False
    assert blocks[2]["route"] == "human_decision_review"
    # block_id numbering stays sequential/positional across code + non-code.
    assert [b["block_id"] for b in blocks] == ["b1", "b2", "b3"]


def test_parse_blocks_triple_quote_code_block_excluded():
    body = "Some notes.\n\n'''\nlive_code_here = True\n'''\n\nMore notes."
    blocks = parse_proposal_intake_blocks("prop-tq", body)
    assert len(blocks) == 3
    assert blocks[1]["is_code"] is True
    assert "live_code_here = True" in blocks[1]["text"]


# ---------------------------------------------------------------------------
# Integration tests — ingest_proposal_intake / get_proposal_intake_drafts /
# promote_intake_draft (shared db fixture + aiosqlite in-memory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_never_auto_promotes(db):
    """ingest_proposal_intake must never create a sprint item on its own."""
    proj = await db_module.create_project(db, "intake-no-auto-promote")
    proposal = await db_module.add_workspace_proposal(
        db, "Idea bundle", "[SPRINT] Do the thing.\n\n[HUMAN] Decide something."
    )
    before = await db_module.get_sprint_items(db, proj["id"])
    result = await db_module.ingest_proposal_intake(db, proposal["id"])
    after = await db_module.get_sprint_items(db, proj["id"])

    assert len(result["drafts"]) == 2
    assert all(d["status"] == "draft" for d in result["drafts"])
    assert before == after  # no sprint item created anywhere


@pytest.mark.asyncio
async def test_ingest_is_idempotent_on_unchanged_body(db):
    proposal = await db_module.add_workspace_proposal(
        db, "Stable idea", "[SPRINT] First.\n\n[HUMAN] Second."
    )
    first = await db_module.ingest_proposal_intake(db, proposal["id"])
    second = await db_module.ingest_proposal_intake(db, proposal["id"])

    assert first["created"] == ["b1", "b2"]
    assert second["created"] == []
    assert second["unchanged"] == ["b1", "b2"]

    drafts = await db_module.get_proposal_intake_drafts(db, proposal["id"])
    assert len(drafts) == 2  # no duplicate rows from the second ingest
    assert [d["id"] for d in first["drafts"]] == [d["id"] for d in drafts]


@pytest.mark.asyncio
async def test_ingest_detects_source_hash_change_and_preserves_history(db):
    proposal = await db_module.add_workspace_proposal(
        db, "Evolving idea", "[SPRINT] Original wording."
    )
    first = await db_module.ingest_proposal_intake(db, proposal["id"])
    draft_id = first["drafts"][0]["id"]
    assert first["drafts"][0]["revision"] == 1

    # Edit the SAME block position with different text.
    await db.execute(
        "UPDATE workspace_proposals SET body = ? WHERE id = ?",
        ("[SPRINT] Revised wording, still one block.", proposal["id"]),
    )
    await db.commit()

    second = await db_module.ingest_proposal_intake(db, proposal["id"])
    assert second["updated"] == ["b1"]
    assert second["created"] == []

    drafts = await db_module.get_proposal_intake_drafts(db, proposal["id"])
    assert len(drafts) == 1
    updated_draft = drafts[0]
    assert updated_draft["id"] == draft_id  # same row, revised in place
    assert updated_draft["revision"] == 2
    assert "Revised wording" in updated_draft["text"]
    assert len(updated_draft["history"]) == 1
    assert "Original wording" in updated_draft["history"][0]["text"]


@pytest.mark.asyncio
async def test_ingest_flags_duplicate_blocks(db):
    body = "[SPRINT] Repeat me exactly.\n\n[HUMAN] Distinct block.\n\n[SPRINT] Repeat me exactly."
    proposal = await db_module.add_workspace_proposal(db, "Dup idea", body)
    result = await db_module.ingest_proposal_intake(db, proposal["id"])

    drafts = {d["block_id"]: d for d in result["drafts"]}
    assert drafts["b1"]["is_duplicate"] in (0, False)
    assert drafts["b2"]["is_duplicate"] in (0, False)
    assert drafts["b3"]["is_duplicate"] in (1, True)
    assert drafts["b3"]["duplicate_of_block_id"] == "b1"
    assert "b3" in result["duplicates"]
    assert "b1" not in result["duplicates"]


@pytest.mark.asyncio
async def test_ingest_excludes_code_blocks_from_drafts(db):
    body = "[SPRINT] Real work item.\n\n```\nlive code, never a task\n```"
    proposal = await db_module.add_workspace_proposal(db, "Code idea", body)
    result = await db_module.ingest_proposal_intake(db, proposal["id"])

    assert len(result["drafts"]) == 1
    assert result["drafts"][0]["block_id"] == "b1"
    assert result["excluded_code"] == ["b2"]


@pytest.mark.asyncio
async def test_promote_intake_draft_creates_sprint_item_with_backlink(db):
    proj = await db_module.create_project(db, "intake-promote-backlink")
    proposal = await db_module.add_workspace_proposal(
        db, "Promotable idea", "[SPRINT] Ship the deterministic intake pipeline."
    )
    ingested = await db_module.ingest_proposal_intake(db, proposal["id"])
    draft_id = ingested["drafts"][0]["id"]

    result = await db_module.promote_intake_draft(db, draft_id, proj["id"])

    si_id = result["sprint_item_id"]
    item = await db_module.get_sprint_item(db, si_id)
    assert item is not None
    assert item["project_id"] == proj["id"]
    assert item["status"] == "pending"

    # Draft carries the promotion backlink.
    updated_draft = (await db_module.get_proposal_intake_drafts(db, proposal["id"]))[0]
    assert updated_draft["status"] == "promoted"
    assert updated_draft["promoted_to_sprint_item_id"] == si_id
    assert updated_draft["promoted_to_project_id"] == proj["id"]
    assert updated_draft["promoted_at"] is not None

    # Durable evidence link proposal -> sprint_item (get_proposal_evidence).
    evidence = await db_module.get_proposal_evidence(db, proj["id"], proposal["id"])
    assert any(si["id"] == si_id for si in evidence["sprint_items"])


@pytest.mark.asyncio
async def test_promote_intake_draft_twice_raises(db):
    proj = await db_module.create_project(db, "intake-promote-twice")
    proposal = await db_module.add_workspace_proposal(
        db, "Idea", "[SPRINT] Do it once."
    )
    ingested = await db_module.ingest_proposal_intake(db, proposal["id"])
    draft_id = ingested["drafts"][0]["id"]
    await db_module.promote_intake_draft(db, draft_id, proj["id"])

    with pytest.raises(ValueError, match="already promoted"):
        await db_module.promote_intake_draft(db, draft_id, proj["id"])


@pytest.mark.asyncio
async def test_promote_duplicate_draft_raises(db):
    proj = await db_module.create_project(db, "intake-promote-dup")
    body = "[SPRINT] Same text twice.\n\n[HUMAN] filler\n\n[SPRINT] Same text twice."
    proposal = await db_module.add_workspace_proposal(db, "Dup promote idea", body)
    result = await db_module.ingest_proposal_intake(db, proposal["id"])
    dup_draft = next(d for d in result["drafts"] if d["block_id"] == "b3")
    assert dup_draft["is_duplicate"]

    with pytest.raises(ValueError, match="duplicate"):
        await db_module.promote_intake_draft(db, dup_draft["id"], proj["id"])


@pytest.mark.asyncio
async def test_promote_intake_draft_cross_project_isolation(db):
    """Two drafts of ONE proposal promoted into two DIFFERENT projects land as
    fully separate, correctly-scoped sprint items — no leakage either way."""
    proj_a = await db_module.create_project(db, "intake-isolation-a")
    proj_b = await db_module.create_project(db, "intake-isolation-b")
    proposal = await db_module.add_workspace_proposal(
        db, "Two-track idea",
        "[SPRINT] Track A work.\n\n[SPRINT] Track B work.",
    )
    ingested = await db_module.ingest_proposal_intake(db, proposal["id"])
    draft_a = next(d for d in ingested["drafts"] if d["block_id"] == "b1")
    draft_b = next(d for d in ingested["drafts"] if d["block_id"] == "b2")

    result_a = await db_module.promote_intake_draft(db, draft_a["id"], proj_a["id"])
    result_b = await db_module.promote_intake_draft(db, draft_b["id"], proj_b["id"])

    items_a = await db_module.get_sprint_items(db, proj_a["id"])
    items_b = await db_module.get_sprint_items(db, proj_b["id"])

    assert any(it["id"] == result_a["sprint_item_id"] for it in items_a)
    assert not any(it["id"] == result_a["sprint_item_id"] for it in items_b)
    assert any(it["id"] == result_b["sprint_item_id"] for it in items_b)
    assert not any(it["id"] == result_b["sprint_item_id"] for it in items_a)

    evidence_a = await db_module.get_proposal_evidence(db, proj_a["id"], proposal["id"])
    evidence_b = await db_module.get_proposal_evidence(db, proj_b["id"], proposal["id"])
    assert any(si["id"] == result_a["sprint_item_id"] for si in evidence_a["sprint_items"])
    assert not any(si["id"] == result_b["sprint_item_id"] for si in evidence_a["sprint_items"])
    assert any(si["id"] == result_b["sprint_item_id"] for si in evidence_b["sprint_items"])


@pytest.mark.asyncio
async def test_get_proposal_intake_drafts_deterministic_ordering(db):
    body = "\n\n".join(f"[HUMAN] Block number {i}." for i in range(1, 13))
    proposal = await db_module.add_workspace_proposal(db, "Ordering idea", body)
    await db_module.ingest_proposal_intake(db, proposal["id"])

    drafts_1 = await db_module.get_proposal_intake_drafts(db, proposal["id"])
    drafts_2 = await db_module.get_proposal_intake_drafts(db, proposal["id"])

    expected_order = [f"b{i}" for i in range(1, 13)]
    assert [d["block_id"] for d in drafts_1] == expected_order
    assert [d["block_id"] for d in drafts_2] == expected_order


@pytest.mark.asyncio
async def test_ingest_proposal_intake_unknown_proposal_raises(db):
    with pytest.raises(ValueError, match="not found"):
        await db_module.ingest_proposal_intake(db, "does-not-exist")


@pytest.mark.asyncio
async def test_promote_intake_draft_unknown_project_raises(db):
    proposal = await db_module.add_workspace_proposal(
        db, "Idea", "[SPRINT] Something."
    )
    ingested = await db_module.ingest_proposal_intake(db, proposal["id"])
    draft_id = ingested["drafts"][0]["id"]

    with pytest.raises(ValueError, match="not found"):
        await db_module.promote_intake_draft(db, draft_id, "does-not-exist")
