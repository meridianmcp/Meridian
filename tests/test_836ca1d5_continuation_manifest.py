"""836ca1d5 — durable continuation manifest shared by generate_handoff(mode='delta')
and (eventually) start_session(mode='continue').

Covers:
  - build_continuation_manifest's own shape and version-scoping.
  - the durable revision ledger: idempotent across unchanged calls, bumps on a
    real board change (reusing board_snapshot.py's monotonic-counter machinery,
    the SAME bucket start_wave_run/resume_wave already write to).
  - generate_handoff(mode='delta') embeds a <continuation_manifest> JSON tag
    whose revision_hash agrees with an independent build_continuation_manifest
    call against the same live board.
  - generate_handoff(mode='full') does NOT embed the tag (delta-only per this
    item's scope; not yet wired into other modes).
"""
from __future__ import annotations

import json
import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module

_MANIFEST_RE = re.compile(
    r"<continuation_manifest>(.*?)</continuation_manifest>", re.DOTALL
)


def _extract_manifest(content: str) -> dict | None:
    m = _MANIFEST_RE.search(content)
    if not m:
        return None
    return json.loads(m.group(1))


@pytest.mark.asyncio
async def test_manifest_shape_and_scope(db, tmp_path):
    p = await db_module.create_project(db, "cm-shape")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    other_version_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "other version item"
    )
    target = await db_module.add_sprint_item(db, p["id"], "v2", "target item")
    session = await db_module.register_session(
        db, p["id"], "cm-sess", sprint_version="v2"
    )

    manifest = await handoff_module.build_continuation_manifest(
        db, p["id"], session_id=session["id"], source="test",
    )

    assert manifest["schema_version"] == handoff_module._CONTINUATION_MANIFEST_SCHEMA_VERSION
    assert manifest["project_id"] == p["id"]
    assert manifest["session_id"] == session["id"]
    assert manifest["sprint_version"] == "v2"
    assert manifest["source"] == "test"
    assert manifest["revision_hash"].startswith("sha256:")
    assert isinstance(manifest["revision_counter"], int)
    assert manifest["pending_count"] == 1
    assert target["id"] in manifest["pending_item_ids"]
    assert other_version_item["id"] not in manifest["pending_item_ids"]


@pytest.mark.asyncio
async def test_manifest_unscoped_sees_every_version(db, tmp_path):
    p = await db_module.create_project(db, "cm-unscoped")
    it1 = await db_module.add_sprint_item(db, p["id"], "v1", "v1 item")
    it2 = await db_module.add_sprint_item(db, p["id"], "v2", "v2 item")

    manifest = await handoff_module.build_continuation_manifest(db, p["id"])

    assert manifest["sprint_version"] is None
    assert set(manifest["pending_item_ids"]) == {it1["id"], it2["id"]}


@pytest.mark.asyncio
async def test_revision_counter_stable_across_unchanged_calls(db, tmp_path):
    p = await db_module.create_project(db, "cm-stable")
    await db_module.add_sprint_item(db, p["id"], "v1", "an item")

    first = await handoff_module.build_continuation_manifest(db, p["id"])
    second = await handoff_module.build_continuation_manifest(db, p["id"])

    assert first["revision_hash"] == second["revision_hash"]
    assert first["revision_counter"] == second["revision_counter"]


@pytest.mark.asyncio
async def test_revision_counter_bumps_on_real_board_change(db, tmp_path):
    p = await db_module.create_project(db, "cm-bump")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "an item")

    before = await handoff_module.build_continuation_manifest(db, p["id"])
    await db_module.claim_sprint_item(db, p["id"], item["id"], "someone")
    after = await handoff_module.build_continuation_manifest(db, p["id"])

    assert after["revision_hash"] != before["revision_hash"]
    assert after["revision_counter"] > before["revision_counter"]


@pytest.mark.asyncio
async def test_record_revision_false_does_not_advance_ledger(db, tmp_path):
    p = await db_module.create_project(db, "cm-peek")
    await db_module.add_sprint_item(db, p["id"], "v1", "an item")

    recorded = await handoff_module.build_continuation_manifest(db, p["id"])
    assert recorded["revision_counter"] == 1

    # A second item changes the live board, but record_revision=False must
    # only PEEK the ledger, not advance it -- the counter returned is the
    # latest already-recorded one (still 1), not a freshly-computed one.
    await db_module.add_sprint_item(db, p["id"], "v1", "a second item", force=True)
    peeked = await handoff_module.build_continuation_manifest(
        db, p["id"], record_revision=False,
    )
    assert peeked["revision_counter"] == 1

    latest = await db_module.get_latest_board_snapshot_revision(db, p["id"], version=None)
    assert latest["revision_counter"] == 1


@pytest.mark.asyncio
async def test_delta_handoff_embeds_continuation_manifest_matching_live_board(db, tmp_path):
    p = await db_module.create_project(db, "cm-delta-embed")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await db_module.add_sprint_item(db, p["id"], "v1", "delta manifest item")
    session = await db_module.register_session(db, p["id"], "cm-delta-sess")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=session["id"],
    )

    manifest = _extract_manifest(content)
    assert manifest is not None, "delta handoff must embed a <continuation_manifest> tag"
    assert manifest["project_id"] == p["id"]
    assert manifest["session_id"] == session["id"]
    assert manifest["source"] == "generate_handoff:delta"

    # Independently rebuilding the manifest against the (unchanged) live board
    # must agree byte-for-byte on the revision hash -- this is the whole
    # point: a receiving session can trust it as a staleness signal.
    fresh = await handoff_module.build_continuation_manifest(
        db, p["id"], session_id=session["id"],
    )
    assert manifest["revision_hash"] == fresh["revision_hash"]


@pytest.mark.asyncio
async def test_full_handoff_does_not_embed_continuation_manifest(db, tmp_path):
    """Scoped to delta only per this item -- full mode is unaffected."""
    p = await db_module.create_project(db, "cm-full-noembed")
    await db_module.add_sprint_item(db, p["id"], "v1", "an item")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
    )

    assert _extract_manifest(content) is None


# ---------------------------------------------------------------------------
# 07229675 — DOCS-R2-C completion-integrity: build_continuation_manifest must
# not advertise a hard-blocked (blocker_kind in ('superseded',
# 'systemic_invalidated_run')) item as claimable. Confirmed gap: before this,
# pending_item_ids was filtered solely on status in ('pending', 'todo'),
# discarding blocker_kind entirely -- a resuming session reading only that
# field would be handed a dead-end id claim_sprint_item deterministically
# refuses (see meridian.db.sprint_items.claim_sprint_item's own SUPERSEDED /
# SYSTEMIC_INVALIDATED_RUN hard gate). These tests cover the fix: exclusion
# from pending_item_ids/pending_count, surfacing via hard_blocked_pending_ids
# instead of silent drop, blocker_summary passthrough, HITL-gate presence,
# and -- critically -- that none of this disturbs the pre-existing
# revision_hash/revision_counter byte-stability contract documented in
# meridian/db/board_snapshot.py (blocker_kind is deliberately NOT one of the
# four tracked fields the hash is sensitive to).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_excludes_hard_blocked_from_pending_and_lists_it_separately(db, tmp_path):
    p = await db_module.create_project(db, "cm-hard-blocked")
    normal = await db_module.add_sprint_item(db, p["id"], "v1", "normal item")
    superseded = await db_module.add_sprint_item(
        db, p["id"], "v1", "superseded item", blocker_kind="superseded", force=True,
    )
    invalidated = await db_module.add_sprint_item(
        db, p["id"], "v1", "invalidated item",
        blocker_kind="systemic_invalidated_run", force=True,
    )

    manifest = await handoff_module.build_continuation_manifest(db, p["id"])

    assert normal["id"] in manifest["pending_item_ids"]
    assert superseded["id"] not in manifest["pending_item_ids"]
    assert invalidated["id"] not in manifest["pending_item_ids"]
    # Only the genuinely claimable item counts toward pending_count.
    assert manifest["pending_count"] == 1

    hard_blocked_by_id = {e["id"]: e["blocker_kind"] for e in manifest["hard_blocked_pending_ids"]}
    assert hard_blocked_by_id == {
        superseded["id"]: "superseded",
        invalidated["id"]: "systemic_invalidated_run",
    }


@pytest.mark.asyncio
async def test_manifest_no_hard_blocked_items_yields_empty_list(db, tmp_path):
    """Common-case regression: a board with no hard-blocked items sees zero
    observable change from the 07229675 fields."""
    p = await db_module.create_project(db, "cm-no-hard-blocked")
    await db_module.add_sprint_item(db, p["id"], "v1", "an item")

    manifest = await handoff_module.build_continuation_manifest(db, p["id"])

    assert manifest["hard_blocked_pending_ids"] == []
    assert manifest["hitl_gated_item_ids"] == []


@pytest.mark.asyncio
async def test_manifest_blocker_summary_is_passed_through_from_snapshot(db, tmp_path):
    p = await db_module.create_project(db, "cm-blocker-summary")
    await db_module.add_sprint_item(db, p["id"], "v1", "an item")

    manifest = await handoff_module.build_continuation_manifest(db, p["id"])
    snapshot = await db_module.build_board_snapshot(db, p["id"], version=None)

    assert manifest["blocker_summary"] == snapshot["blocker_summary"]
    assert manifest["blocker_summary"] is not None
    assert "eligible_item_ids" in manifest["blocker_summary"]


@pytest.mark.asyncio
async def test_manifest_hitl_gated_item_ids_reflects_blocking_proposal_gate(db, tmp_path):
    p = await db_module.create_project(db, "cm-hitl-gate")
    gated = await db_module.add_sprint_item(db, p["id"], "v1", "gated item")
    ungated = await db_module.add_sprint_item(db, p["id"], "v1", "ungated item")
    await db_module.create_proposal_gate(
        db, p["id"], "product_scope", "Is this scope acceptable?",
        [gated["id"]], "ambiguous product-scope decision needs a human call",
    )

    manifest = await handoff_module.build_continuation_manifest(db, p["id"])

    assert manifest["hitl_gated_item_ids"] == [gated["id"]]
    assert ungated["id"] not in manifest["hitl_gated_item_ids"]
    # Both items are still genuinely claimable -- a HITL gate is advisory
    # visibility here, not an exclusion from pending_item_ids.
    assert gated["id"] in manifest["pending_item_ids"]
    assert ungated["id"] in manifest["pending_item_ids"]


@pytest.mark.asyncio
async def test_manifest_hitl_gate_allowed_state_is_not_reported_as_gated(db, tmp_path):
    """An 'allowed' gate no longer restricts anything (effective_state) --
    must not appear in hitl_gated_item_ids."""
    p = await db_module.create_project(db, "cm-hitl-allowed")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "resolved-gate item")
    gate = await db_module.create_proposal_gate(
        db, p["id"], "destructive_ops", "Confirmed safe to proceed?",
        [item["id"]], "reviewed and approved",
    )
    await db_module.resolve_proposal_gate(
        db, p["id"], gate["id"], "allowed", "approved by maintainer", actor="human",
    )

    manifest = await handoff_module.build_continuation_manifest(db, p["id"])

    assert manifest["hitl_gated_item_ids"] == []


@pytest.mark.asyncio
async def test_manifest_revision_hash_unaffected_by_blocker_kind_or_gate_changes(db, tmp_path):
    """Byte-stability contract (board_snapshot.py): blocker_kind is
    deliberately NOT one of the four tracked fields the revision hash is
    sensitive to, and blocker_summary is documented as never folded into it
    either. Confirms the 07229675 additive fields don't change that contract
    -- the hash must be identical before/after a blocker_kind-only edit, even
    though pending_item_ids/hard_blocked_pending_ids DO change (they are
    freshly recomputed every call, not gated behind the hash)."""
    p = await db_module.create_project(db, "cm-hash-stability")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "an item")

    before = await handoff_module.build_continuation_manifest(db, p["id"])
    assert item["id"] in before["pending_item_ids"]
    assert before["hard_blocked_pending_ids"] == []

    await db_module.patch_sprint_item(db, p["id"], item["id"], blocker_kind="superseded")

    after = await handoff_module.build_continuation_manifest(db, p["id"])
    assert after["revision_hash"] == before["revision_hash"]
    assert after["revision_counter"] == before["revision_counter"]
    # But the freshly-recomputed additive fields DO reflect the new state.
    assert item["id"] not in after["pending_item_ids"]
    assert after["hard_blocked_pending_ids"] == [
        {"id": item["id"], "blocker_kind": "superseded"}
    ]


@pytest.mark.asyncio
async def test_manifest_new_fields_do_not_change_shape_of_existing_fields(db, tmp_path):
    """Schema-version regression guard: adding fields is documented as
    non-breaking (_CONTINUATION_MANIFEST_SCHEMA_VERSION's own docstring) --
    confirm the pre-existing fields keep their exact pre-07229675 shape."""
    p = await db_module.create_project(db, "cm-shape-unchanged")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "an item")

    manifest = await handoff_module.build_continuation_manifest(db, p["id"])

    assert manifest["schema_version"] == handoff_module._CONTINUATION_MANIFEST_SCHEMA_VERSION
    assert isinstance(manifest["pending_item_ids"], list)
    assert isinstance(manifest["pending_count"], int)
    assert manifest["pending_item_ids"] == [item["id"]]
    # New fields present and additively typed.
    assert isinstance(manifest["hard_blocked_pending_ids"], list)
    assert isinstance(manifest["hitl_gated_item_ids"], list)
    assert "blocker_summary" in manifest
