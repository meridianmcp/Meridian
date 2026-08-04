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
