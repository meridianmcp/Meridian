"""fe989980 — wave-scoped DOCX merge manifests and serialized canonical merge gate.

Tests for meridian.db.docx_merge: the coordination layer built ON TOP of the
existing scoped docx-region claim primitives (f7ee1ba7:
claim_docx_region / check_docx_region_write_conflict /
release_docx_region_claims) that lets multiple sessions work a "wave" of
parallel DOCX edits against isolated draft artifacts, then serializes the
actual merge into the ONE canonical file through a single merge owner.

Coverage:
(a) open_merge_manifest — isolation enforcement (draft_path != file_path),
    idempotent registration, manifest reuse across sessions in the same wave.
(b) declare_merge_anchors — reuses claim_docx_region verbatim:
    non-overlapping anchors from different sessions both succeed;
    an overlapping anchor is rejected exactly like a raw claim_docx_region
    conflict would be.
(c) claim_merge_owner / release_merge_owner — single serialized owner.
(d) check_merge_stale_or_overlap — the pre-write gate: not_merge_owner,
    stale_revision, anchor_already_merged (a genuine overlap at merge time),
    manifest_complete / manifest_aborted.
(e) record_merge_result — idempotent for the same session; a concurrent
    write for the same anchor by a different session can only ever have
    one winner (UNIQUE-constraint-backed race safety).
(f) finalize_merge_manifest — verification-gated, idempotent completion;
    fails closed for a non-owner or a missing verification payload.
(g) get_merge_manifest — read-only status view.
"""
from __future__ import annotations

from meridian import db as db_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _mk_session(db, name: str) -> str:
    """Create a minimal project+session, return the session id."""
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


# ---------------------------------------------------------------------------
# (a) open_merge_manifest — isolation + idempotent registration
# ---------------------------------------------------------------------------

async def test_open_merge_manifest_requires_isolated_draft_path(db):
    """draft_path must differ from the canonical file_path — structural guard."""
    sess = await _mk_session(db, "iso-a")
    doc = "thesis/chapter1.docx"

    result = await db_module.open_merge_manifest(
        db, "wave-1", doc, sess, draft_path=doc,
    )
    assert result["opened"] is False
    assert result["reason"] == "not_isolated"


async def test_open_merge_manifest_requires_nonempty_draft_path(db):
    sess = await _mk_session(db, "iso-b")
    result = await db_module.open_merge_manifest(
        db, "wave-1", "doc.docx", sess, draft_path="",
    )
    assert result["opened"] is False
    assert result["reason"] == "invalid"


async def test_open_merge_manifest_succeeds_and_is_idempotent(db):
    """Two calls for the same session refresh the draft row, not duplicate it."""
    sess = await _mk_session(db, "open-a")
    doc = "wave-doc.docx"
    draft = "drafts/wave-doc-sessA.docx"

    r1 = await db_module.open_merge_manifest(db, "wave-9", doc, sess, draft_path=draft)
    assert r1["opened"] is True
    assert r1["status"] == "open"
    manifest_id = r1["manifest_id"]

    r2 = await db_module.open_merge_manifest(
        db, "wave-9", doc, sess, draft_path="drafts/wave-doc-sessA-v2.docx",
    )
    assert r2["opened"] is True
    assert r2["manifest_id"] == manifest_id
    assert r2["draft_path"] == "drafts/wave-doc-sessA-v2.docx"

    status = await db_module.get_merge_manifest(db, "wave-9", doc)
    assert len(status["drafts"]) == 1


async def test_open_merge_manifest_shared_across_sessions_same_wave(db):
    """Multiple sessions registering drafts in the SAME wave share one manifest."""
    sess_a = await _mk_session(db, "shared-a")
    sess_b = await _mk_session(db, "shared-b")
    doc = "shared-wave.docx"

    ra = await db_module.open_merge_manifest(
        db, "wave-shared", doc, sess_a, draft_path="drafts/shared-a.docx",
    )
    rb = await db_module.open_merge_manifest(
        db, "wave-shared", doc, sess_b, draft_path="drafts/shared-b.docx",
    )
    assert ra["manifest_id"] == rb["manifest_id"]

    status = await db_module.get_merge_manifest(db, "wave-shared", doc)
    assert len(status["drafts"]) == 2


async def test_open_merge_manifest_rejects_reopen_after_complete(db):
    sess = await _mk_session(db, "closed-a")
    doc = "closed-wave.docx"
    await db_module.open_merge_manifest(db, "wave-c", doc, sess, draft_path="d1.docx")
    await db_module.claim_merge_owner(db, "wave-c", doc, sess)
    finalize = await db_module.finalize_merge_manifest(
        db, "wave-c", doc, sess, verification={"ok": True},
    )
    assert finalize["finalized"] is True

    reopen = await db_module.open_merge_manifest(
        db, "wave-c", doc, sess, draft_path="d2.docx",
    )
    assert reopen["opened"] is False
    assert reopen["reason"] == "manifest_closed"
    assert reopen["manifest_status"] == "complete"


# ---------------------------------------------------------------------------
# (b) declare_merge_anchors — non-overlap succeeds, overlap is rejected
# ---------------------------------------------------------------------------

async def test_declare_merge_anchors_non_overlapping_both_succeed(db):
    """Two sessions in the same wave can declare DIFFERENT anchors — no conflict."""
    sess_a = await _mk_session(db, "anchor-a")
    sess_b = await _mk_session(db, "anchor-b")
    doc = "non-overlap.docx"

    await db_module.open_merge_manifest(db, "wave-no", doc, sess_a, draft_path="da.docx")
    await db_module.open_merge_manifest(db, "wave-no", doc, sess_b, draft_path="db.docx")

    ra = await db_module.declare_merge_anchors(db, "wave-no", doc, sess_a, ["EL_INTRO"])
    rb = await db_module.declare_merge_anchors(db, "wave-no", doc, sess_b, ["EL_CONCLUSION"])

    assert ra["declared"] is True
    assert ra["claimed_anchors"] == ["EL_INTRO"]
    assert rb["declared"] is True
    assert rb["claimed_anchors"] == ["EL_CONCLUSION"]
    assert ra["conflicts"] == []
    assert rb["conflicts"] == []


async def test_declare_merge_anchors_overlap_rejected(db):
    """Two sessions declaring the SAME anchor: second is rejected (reuses
    claim_docx_region's element_conflict verbatim)."""
    sess_a = await _mk_session(db, "overlap-a")
    sess_b = await _mk_session(db, "overlap-b")
    doc = "overlap.docx"
    shared = "EL_SHARED"

    await db_module.open_merge_manifest(db, "wave-ov", doc, sess_a, draft_path="da.docx")
    await db_module.open_merge_manifest(db, "wave-ov", doc, sess_b, draft_path="db.docx")

    ra = await db_module.declare_merge_anchors(db, "wave-ov", doc, sess_a, [shared])
    rb = await db_module.declare_merge_anchors(db, "wave-ov", doc, sess_b, [shared])

    assert ra["declared"] is True
    assert rb["declared"] is False
    assert rb["claimed_anchors"] == []
    assert rb["conflicts"][0]["element_id"] == shared
    assert rb["conflicts"][0]["reason"] == "element_conflict"
    assert rb["conflicts"][0]["holder_session_id"] == sess_a

    # And the underlying primitive itself is untouched/unweakened — a direct
    # claim_docx_region call sees the exact same conflict.
    direct = await db_module.claim_docx_region(db, sess_b, doc, shared)
    assert direct["claimed"] is False
    assert direct["reason"] == "element_conflict"


async def test_declare_merge_anchors_requires_open_manifest(db):
    sess = await _mk_session(db, "no-manifest")
    result = await db_module.declare_merge_anchors(
        db, "wave-none", "no-manifest.docx", sess, ["E1"],
    )
    assert result["declared"] is False
    assert result["reason"] == "no_manifest"


async def test_declare_merge_anchors_requires_registered_draft(db):
    sess_a = await _mk_session(db, "draft-req-a")
    sess_b = await _mk_session(db, "draft-req-b")
    doc = "draft-required.docx"
    await db_module.open_merge_manifest(db, "wave-dr", doc, sess_a, draft_path="da.docx")

    # sess_b never called open_merge_manifest for this wave/file.
    result = await db_module.declare_merge_anchors(db, "wave-dr", doc, sess_b, ["E1"])
    assert result["declared"] is False
    assert result["reason"] == "no_draft"


# ---------------------------------------------------------------------------
# (c) claim_merge_owner / release_merge_owner — single serialized owner
# ---------------------------------------------------------------------------

async def test_claim_merge_owner_exclusive(db):
    sess_a = await _mk_session(db, "owner-a")
    sess_b = await _mk_session(db, "owner-b")
    doc = "owner.docx"
    await db_module.open_merge_manifest(db, "wave-own", doc, sess_a, draft_path="da.docx")

    ca = await db_module.claim_merge_owner(db, "wave-own", doc, sess_a)
    assert ca["claimed"] is True
    assert ca["status"] == "merging"

    cb = await db_module.claim_merge_owner(db, "wave-own", doc, sess_b)
    assert cb["claimed"] is False
    assert cb["reason"] == "owner_locked"
    assert cb["holder_session_id"] == sess_a


async def test_claim_merge_owner_reclaim_by_same_owner_is_idempotent(db):
    sess = await _mk_session(db, "owner-reclaim")
    doc = "reclaim.docx"
    await db_module.open_merge_manifest(db, "wave-rc", doc, sess, draft_path="d.docx")

    c1 = await db_module.claim_merge_owner(db, "wave-rc", doc, sess)
    c2 = await db_module.claim_merge_owner(db, "wave-rc", doc, sess)
    assert c1["claimed"] is True
    assert c2["claimed"] is True


async def test_release_merge_owner_allows_new_claimant(db):
    sess_a = await _mk_session(db, "rel-owner-a")
    sess_b = await _mk_session(db, "rel-owner-b")
    doc = "rel-owner.docx"
    await db_module.open_merge_manifest(db, "wave-relown", doc, sess_a, draft_path="da.docx")

    await db_module.claim_merge_owner(db, "wave-relown", doc, sess_a)
    released = await db_module.release_merge_owner(db, "wave-relown", doc, sess_a)
    assert released is True

    cb = await db_module.claim_merge_owner(db, "wave-relown", doc, sess_b)
    assert cb["claimed"] is True


async def test_release_merge_owner_noop_for_non_holder(db):
    sess_a = await _mk_session(db, "rel-noop-a")
    sess_b = await _mk_session(db, "rel-noop-b")
    doc = "rel-noop.docx"
    await db_module.open_merge_manifest(db, "wave-noop", doc, sess_a, draft_path="da.docx")
    await db_module.claim_merge_owner(db, "wave-noop", doc, sess_a)

    released = await db_module.release_merge_owner(db, "wave-noop", doc, sess_b)
    assert released is False


async def test_claim_merge_owner_requires_manifest(db):
    sess = await _mk_session(db, "owner-no-manifest")
    result = await db_module.claim_merge_owner(db, "wave-x", "nope.docx", sess)
    assert result["claimed"] is False
    assert result["reason"] == "no_manifest"


# ---------------------------------------------------------------------------
# (d) check_merge_stale_or_overlap — pre-write gate
# ---------------------------------------------------------------------------

async def test_check_merge_stale_or_overlap_not_owner_blocked(db):
    sess_a = await _mk_session(db, "gate-owner")
    sess_b = await _mk_session(db, "gate-nonowner")
    doc = "gate.docx"
    await db_module.open_merge_manifest(db, "wave-gate", doc, sess_a, draft_path="da.docx")
    await db_module.claim_merge_owner(db, "wave-gate", doc, sess_a)

    blocked = await db_module.check_merge_stale_or_overlap(
        db, "wave-gate", doc, sess_b, "EL1",
    )
    assert blocked is not None
    assert blocked["blocked"] is True
    assert blocked["reason"] == "not_merge_owner"


async def test_check_merge_stale_or_overlap_owner_clear_to_proceed(db):
    sess = await _mk_session(db, "gate-clear")
    doc = "gate-clear.docx"
    await db_module.open_merge_manifest(db, "wave-clear", doc, sess, draft_path="d.docx")
    await db_module.claim_merge_owner(db, "wave-clear", doc, sess)

    blocked = await db_module.check_merge_stale_or_overlap(
        db, "wave-clear", doc, sess, "EL1",
    )
    assert blocked is None


async def test_check_merge_stale_or_overlap_stale_revision_blocked(db):
    sess = await _mk_session(db, "gate-stale")
    doc = "gate-stale.docx"
    await db_module.open_merge_manifest(
        db, "wave-stale", doc, sess, draft_path="d.docx", base_revision="rev-1",
    )
    await db_module.claim_merge_owner(db, "wave-stale", doc, sess)

    blocked = await db_module.check_merge_stale_or_overlap(
        db, "wave-stale", doc, sess, "EL1", expected_base_revision="rev-0-outdated",
    )
    assert blocked is not None
    assert blocked["blocked"] is True
    assert blocked["reason"] == "stale_revision"


async def test_check_merge_stale_or_overlap_anchor_already_merged_by_other(db):
    """A genuine overlap at merge time: a DIFFERENT session's draft already
    merged this exact anchor into the canonical file."""
    sess_a = await _mk_session(db, "gate-merged-a")
    sess_b = await _mk_session(db, "gate-merged-b")
    doc = "gate-merged.docx"
    elem = "EL_DUP"

    await db_module.open_merge_manifest(db, "wave-dup", doc, sess_a, draft_path="da.docx")
    await db_module.open_merge_manifest(db, "wave-dup", doc, sess_b, draft_path="db.docx")

    # sess_a merges EL_DUP while holding ownership.
    await db_module.claim_merge_owner(db, "wave-dup", doc, sess_a)
    await db_module.record_merge_result(db, "wave-dup", doc, sess_a, elem)
    await db_module.release_merge_owner(db, "wave-dup", doc, sess_a)

    # Ownership passes to sess_b; it must not be allowed to (re-)merge the
    # same anchor sess_a already merged.
    await db_module.claim_merge_owner(db, "wave-dup", doc, sess_b)
    blocked = await db_module.check_merge_stale_or_overlap(
        db, "wave-dup", doc, sess_b, elem,
    )
    assert blocked is not None
    assert blocked["blocked"] is True
    assert blocked["reason"] == "anchor_already_merged"
    assert blocked["holder_session_id"] == sess_a


async def test_check_merge_stale_or_overlap_manifest_complete_blocks_all(db):
    sess = await _mk_session(db, "gate-complete")
    doc = "gate-complete.docx"
    await db_module.open_merge_manifest(db, "wave-comp", doc, sess, draft_path="d.docx")
    await db_module.claim_merge_owner(db, "wave-comp", doc, sess)
    await db_module.finalize_merge_manifest(
        db, "wave-comp", doc, sess, verification={"tests": "passed"},
    )

    blocked = await db_module.check_merge_stale_or_overlap(
        db, "wave-comp", doc, sess, "EL1",
    )
    assert blocked is not None
    assert blocked["reason"] == "manifest_complete"


# ---------------------------------------------------------------------------
# (e) record_merge_result — idempotent + concurrent-write race safety
# ---------------------------------------------------------------------------

async def test_record_merge_result_idempotent_same_session(db):
    sess = await _mk_session(db, "record-idem")
    doc = "record-idem.docx"
    await db_module.open_merge_manifest(db, "wave-ridem", doc, sess, draft_path="d.docx")
    await db_module.claim_merge_owner(db, "wave-ridem", doc, sess)

    r1 = await db_module.record_merge_result(db, "wave-ridem", doc, sess, "EL1")
    r2 = await db_module.record_merge_result(db, "wave-ridem", doc, sess, "EL1")

    assert r1["recorded"] is True
    assert r1["already_merged"] is False
    assert r2["recorded"] is True
    assert r2["already_merged"] is True


async def test_record_merge_result_concurrent_canonical_write_one_winner(db):
    """Two different sessions racing to record the SAME anchor: only one wins,
    the loser is rejected — never silently double-applied."""
    sess_a = await _mk_session(db, "concurrent-a")
    sess_b = await _mk_session(db, "concurrent-b")
    doc = "concurrent.docx"
    elem = "EL_RACE"

    await db_module.open_merge_manifest(db, "wave-race", doc, sess_a, draft_path="da.docx")
    await db_module.open_merge_manifest(db, "wave-race", doc, sess_b, draft_path="db.docx")

    # sess_a wins ownership and records first.
    await db_module.claim_merge_owner(db, "wave-race", doc, sess_a)
    win = await db_module.record_merge_result(db, "wave-race", doc, sess_a, elem)
    assert win["recorded"] is True
    assert win["already_merged"] is False

    # sess_b (even if it later acquires ownership) cannot record the same
    # anchor out from under sess_a's already-recorded result.
    await db_module.release_merge_owner(db, "wave-race", doc, sess_a)
    await db_module.claim_merge_owner(db, "wave-race", doc, sess_b)
    lose = await db_module.record_merge_result(db, "wave-race", doc, sess_b, elem)
    assert lose["recorded"] is False
    assert lose["reason"] == "anchor_already_merged_by_other"
    assert lose["holder_session_id"] == sess_a

    # Exactly one ledger row exists for this anchor.
    status = await db_module.get_merge_manifest(db, "wave-race", doc)
    matching = [a for a in status["merged_anchors"] if a["element_id"] == elem]
    assert len(matching) == 1
    assert matching[0]["session_id"] == sess_a


async def test_record_merge_result_advances_base_revision(db):
    sess = await _mk_session(db, "revision-advance")
    doc = "revision.docx"
    await db_module.open_merge_manifest(
        db, "wave-rev", doc, sess, draft_path="d.docx", base_revision="rev-0",
    )
    await db_module.claim_merge_owner(db, "wave-rev", doc, sess)

    await db_module.record_merge_result(
        db, "wave-rev", doc, sess, "EL1", canonical_revision_after="rev-1",
    )
    status = await db_module.get_merge_manifest(db, "wave-rev", doc)
    assert status["base_revision"] == "rev-1"


async def test_record_merge_result_requires_manifest(db):
    sess = await _mk_session(db, "record-no-manifest")
    result = await db_module.record_merge_result(db, "wave-none", "nope.docx", sess, "EL1")
    assert result["recorded"] is False
    assert result["reason"] == "no_manifest"


# ---------------------------------------------------------------------------
# (f) finalize_merge_manifest — verification-gated, idempotent completion
# ---------------------------------------------------------------------------

async def test_finalize_requires_truthy_verification(db):
    sess = await _mk_session(db, "final-verify")
    doc = "final-verify.docx"
    await db_module.open_merge_manifest(db, "wave-fv", doc, sess, draft_path="d.docx")
    await db_module.claim_merge_owner(db, "wave-fv", doc, sess)

    result = await db_module.finalize_merge_manifest(
        db, "wave-fv", doc, sess, verification=None,
    )
    assert result["finalized"] is False
    assert result["reason"] == "verification_required"

    result_empty = await db_module.finalize_merge_manifest(
        db, "wave-fv", doc, sess, verification={},
    )
    assert result_empty["finalized"] is False
    assert result_empty["reason"] == "verification_required"


async def test_finalize_requires_merge_owner(db):
    sess_a = await _mk_session(db, "final-owner-a")
    sess_b = await _mk_session(db, "final-owner-b")
    doc = "final-owner.docx"
    await db_module.open_merge_manifest(db, "wave-fo", doc, sess_a, draft_path="da.docx")
    await db_module.claim_merge_owner(db, "wave-fo", doc, sess_a)

    result = await db_module.finalize_merge_manifest(
        db, "wave-fo", doc, sess_b, verification={"ok": True},
    )
    assert result["finalized"] is False
    assert result["reason"] == "not_merge_owner"


async def test_finalize_succeeds_and_releases_ownership(db):
    sess = await _mk_session(db, "final-ok")
    doc = "final-ok.docx"
    await db_module.open_merge_manifest(db, "wave-ok", doc, sess, draft_path="d.docx")
    await db_module.claim_merge_owner(db, "wave-ok", doc, sess)

    result = await db_module.finalize_merge_manifest(
        db, "wave-ok", doc, sess, verification={"tests_passed": True, "count": 12},
    )
    assert result["finalized"] is True
    assert result["already_complete"] is False
    assert result["completed_at"] is not None

    status = await db_module.get_merge_manifest(db, "wave-ok", doc)
    assert status["status"] == "complete"
    assert status["merge_owner_session_id"] is None
    assert status["verification"] == {"tests_passed": True, "count": 12}


async def test_finalize_is_idempotent(db):
    sess = await _mk_session(db, "final-idem")
    doc = "final-idem.docx"
    await db_module.open_merge_manifest(db, "wave-fi", doc, sess, draft_path="d.docx")
    await db_module.claim_merge_owner(db, "wave-fi", doc, sess)

    r1 = await db_module.finalize_merge_manifest(
        db, "wave-fi", doc, sess, verification={"ok": True},
    )
    r2 = await db_module.finalize_merge_manifest(
        db, "wave-fi", doc, sess, verification={"ignored": "should not overwrite"},
    )
    assert r1["already_complete"] is False
    assert r2["already_complete"] is True
    assert r2["verification"] == {"ok": True}


async def test_finalize_requires_manifest(db):
    sess = await _mk_session(db, "final-no-manifest")
    result = await db_module.finalize_merge_manifest(
        db, "wave-none", "nope.docx", sess, verification={"ok": True},
    )
    assert result["finalized"] is False
    assert result["reason"] == "no_manifest"


# ---------------------------------------------------------------------------
# (g) get_merge_manifest — read-only status view
# ---------------------------------------------------------------------------

async def test_get_merge_manifest_returns_none_when_absent(db):
    result = await db_module.get_merge_manifest(db, "wave-absent", "absent.docx")
    assert result is None


async def test_get_merge_manifest_full_lifecycle_view(db):
    sess_a = await _mk_session(db, "view-a")
    sess_b = await _mk_session(db, "view-b")
    doc = "view.docx"

    await db_module.open_merge_manifest(db, "wave-view", doc, sess_a, draft_path="da.docx")
    await db_module.open_merge_manifest(db, "wave-view", doc, sess_b, draft_path="db.docx")
    await db_module.declare_merge_anchors(db, "wave-view", doc, sess_a, ["EL_A"])
    await db_module.declare_merge_anchors(db, "wave-view", doc, sess_b, ["EL_B"])

    await db_module.claim_merge_owner(db, "wave-view", doc, sess_a)
    await db_module.record_merge_result(db, "wave-view", doc, sess_a, "EL_A")

    status = await db_module.get_merge_manifest(db, "wave-view", doc)
    assert status["wave_id"] == "wave-view"
    assert status["status"] == "merging"
    assert len(status["drafts"]) == 2
    drafts_by_session = {d["session_id"]: d for d in status["drafts"]}
    assert drafts_by_session[sess_a]["anchors"] == ["EL_A"]
    assert drafts_by_session[sess_b]["anchors"] == ["EL_B"]
    assert len(status["merged_anchors"]) == 1
    assert status["merged_anchors"][0]["element_id"] == "EL_A"


# ---------------------------------------------------------------------------
# End-to-end: full wave flow — open, declare (non-overlap), own, merge, verify
# ---------------------------------------------------------------------------

async def test_full_wave_merge_flow_end_to_end(db):
    """Two sessions work isolated drafts on non-overlapping anchors, one
    serialized owner merges both into the canonical file, then finalize
    requires verification before the manifest can complete."""
    sess_a = await _mk_session(db, "e2e-a")
    sess_b = await _mk_session(db, "e2e-b")
    doc = "e2e.docx"

    await db_module.open_merge_manifest(
        db, "wave-e2e", doc, sess_a, draft_path="drafts/e2e-a.docx", base_revision="rev-0",
    )
    await db_module.open_merge_manifest(
        db, "wave-e2e", doc, sess_b, draft_path="drafts/e2e-b.docx", base_revision="rev-0",
    )

    da = await db_module.declare_merge_anchors(db, "wave-e2e", doc, sess_a, ["EL_1", "EL_2"])
    db_ = await db_module.declare_merge_anchors(db, "wave-e2e", doc, sess_b, ["EL_3"])
    assert da["declared"] is True
    assert db_["declared"] is True

    # sess_b cannot merge without ownership.
    owner_check = await db_module.claim_merge_owner(db, "wave-e2e", doc, sess_a)
    assert owner_check["claimed"] is True
    blocked = await db_module.check_merge_stale_or_overlap(db, "wave-e2e", doc, sess_b, "EL_3")
    assert blocked["reason"] == "not_merge_owner"

    # Owner (sess_a) merges its own anchors.
    for elem in ("EL_1", "EL_2"):
        gate = await db_module.check_merge_stale_or_overlap(db, "wave-e2e", doc, sess_a, elem)
        assert gate is None
        rec = await db_module.record_merge_result(db, "wave-e2e", doc, sess_a, elem)
        assert rec["recorded"] is True

    # Ownership hands off to sess_b to merge its own anchor.
    await db_module.release_merge_owner(db, "wave-e2e", doc, sess_a)
    await db_module.claim_merge_owner(db, "wave-e2e", doc, sess_b)
    gate_b = await db_module.check_merge_stale_or_overlap(db, "wave-e2e", doc, sess_b, "EL_3")
    assert gate_b is None
    rec_b = await db_module.record_merge_result(db, "wave-e2e", doc, sess_b, "EL_3")
    assert rec_b["recorded"] is True

    # Cannot finalize without verification.
    no_verify = await db_module.finalize_merge_manifest(
        db, "wave-e2e", doc, sess_b, verification=None,
    )
    assert no_verify["finalized"] is False

    final = await db_module.finalize_merge_manifest(
        db, "wave-e2e", doc, sess_b, verification={"all_anchors_merged": 3},
    )
    assert final["finalized"] is True

    status = await db_module.get_merge_manifest(db, "wave-e2e", doc)
    assert status["status"] == "complete"
    assert len(status["merged_anchors"]) == 3

    # Post-completion: no further merges allowed, ever.
    reject = await db_module.check_merge_stale_or_overlap(db, "wave-e2e", doc, sess_a, "EL_4")
    assert reject["reason"] == "manifest_complete"
