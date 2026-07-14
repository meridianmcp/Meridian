"""f7ee1ba7 — Model B: scoped docx-region claims.

Tests for the element-level parallel-edit protection for .docx files:

(a) A session can claim a specific scoped region of a docx (not just the whole file).
(b) An edit attempt WITHIN the claimed scope (caller is the owner) succeeds.
(c) An edit attempt OUTSIDE the claimed scope (different session owns it) is
    structurally rejected with a clear error.
(d) A whole-file (unscoped) claim still works as before — 73d233e4 regression.
(e) Two sessions can hold NON-OVERLAPPING scoped claims on the SAME file concurrently
    (the actual precision benefit — mirrors how symbol claims work for code files).
"""
from __future__ import annotations

import pytest

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
# (a) A session can claim a specific scoped region — not just the whole file
# ---------------------------------------------------------------------------

async def test_claim_docx_region_succeeds(db):
    """A session can claim a specific element_id in a .docx file."""
    sess = await _mk_session(db, "writer-a")
    doc = "thesis/chapter1.docx"
    para_id = "1A2B3C4D"

    result = await db_module.claim_docx_region(db, sess, doc, para_id)

    assert result["claimed"] is True
    assert result["element_id"] == para_id
    assert result["session_id"] == sess
    assert db_module._normalize_file_path(doc) in result["file_path"]


async def test_claim_docx_region_idempotent(db):
    """Re-claiming the same element by the same session refreshes and succeeds."""
    sess = await _mk_session(db, "writer-idem")
    doc = "report.docx"
    para_id = "AABBCCDD"

    r1 = await db_module.claim_docx_region(db, sess, doc, para_id)
    r2 = await db_module.claim_docx_region(db, sess, doc, para_id)

    assert r1["claimed"] is True
    assert r2["claimed"] is True
    # After idempotent re-claim only ONE active row should exist.
    claims = await db_module.get_docx_region_claims(db, doc)
    active = [c for c in claims if c.get("session_id") == sess and c.get("element_id") == para_id]
    assert len(active) == 1


# ---------------------------------------------------------------------------
# (b) Write within the claimed scope by the owner is allowed
# ---------------------------------------------------------------------------

async def test_check_conflict_owner_allowed(db):
    """The session that owns the element_id is not blocked by its own claim."""
    owner = await _mk_session(db, "owner-b")
    doc = "owned.docx"
    para_id = "11223344"

    await db_module.claim_docx_region(db, owner, doc, para_id)

    conflict = await db_module.check_docx_region_write_conflict(
        db, owner, doc, para_id
    )
    assert conflict is None, f"Owner should be allowed, got conflict: {conflict}"


async def test_check_conflict_no_claims_always_allowed(db):
    """A file with no scoped claims allows any write (unguarded mode)."""
    conflict = await db_module.check_docx_region_write_conflict(
        db, "any-session", "fresh.docx", "DEADBEEF"
    )
    assert conflict is None


# ---------------------------------------------------------------------------
# (c) Write OUTSIDE the claimed scope is structurally rejected
# ---------------------------------------------------------------------------

async def test_check_conflict_other_session_blocked(db):
    """An edit to an element claimed by another session is rejected."""
    owner = await _mk_session(db, "owner-c")
    intruder = await _mk_session(db, "intruder-c")
    doc = "contested.docx"
    para_id = "CAFEBABE"

    await db_module.claim_docx_region(db, owner, doc, para_id)

    conflict = await db_module.check_docx_region_write_conflict(
        db, intruder, doc, para_id
    )
    assert conflict is not None
    assert conflict["blocked"] is True
    assert conflict["reason"] == "element_locked"
    assert conflict["holder"] == owner
    assert para_id in conflict["message"]


async def test_check_conflict_unknown_session_blocked(db):
    """An edit from an unclaimed session to a claimed element is rejected."""
    owner = await _mk_session(db, "owner-unk")
    doc = "unknown-sess.docx"
    para_id = "FEEDFACE"

    await db_module.claim_docx_region(db, owner, doc, para_id)

    # session_id is a session that has NO claim
    conflict = await db_module.check_docx_region_write_conflict(
        db, "totally-unknown-session", doc, para_id
    )
    assert conflict is not None
    assert conflict["blocked"] is True


async def test_check_conflict_no_element_id_scoped_mode_blocked(db):
    """A write with no element_id on a file in scoped mode is blocked
    when the caller has no claim on the file at all."""
    owner = await _mk_session(db, "owner-noel")
    doc = "scoped-mode.docx"
    await db_module.claim_docx_region(db, owner, doc, "EL001")

    # Different session with no element_id provided
    conflict = await db_module.check_docx_region_write_conflict(
        db, "non-owner-session", doc, None
    )
    assert conflict is not None
    assert conflict["blocked"] is True
    assert conflict["reason"] == "scoped_mode"


# ---------------------------------------------------------------------------
# (d) Whole-file (unscoped) claim still blocks — 73d233e4 regression
# ---------------------------------------------------------------------------

async def test_whole_file_lock_blocks_region_claim(db):
    """A whole-file write lock blocks a scoped region claim by another session."""
    file_owner = await _mk_session(db, "file-owner")
    region_claimer = await _mk_session(db, "region-claimer")
    doc = "file-locked.docx"

    claimed = await db_module.claim_file(db, doc, file_owner, mode="write")
    assert claimed["claimed"] is True

    result = await db_module.claim_docx_region(
        db, region_claimer, doc, "PARA001"
    )
    assert result["claimed"] is False
    assert result["reason"] == "file_locked"


async def test_whole_file_lock_blocks_update_paragraph_gate(db):
    """A whole-file write lock held by another session blocks the check gate."""
    file_owner = await _mk_session(db, "fl-owner")
    doc = "write-locked.docx"

    await db_module.claim_file(db, doc, file_owner, mode="write")

    conflict = await db_module.check_docx_region_write_conflict(
        db, "intruder-session", doc, "PARA999"
    )
    assert conflict is not None
    assert conflict["blocked"] is True
    assert conflict["reason"] == "file_locked"


async def test_own_file_lock_does_not_block_self(db):
    """A session that holds the whole-file lock can still write any element."""
    owner = await _mk_session(db, "fl-self")
    doc = "self-locked.docx"

    await db_module.claim_file(db, doc, owner, mode="write")

    conflict = await db_module.check_docx_region_write_conflict(
        db, owner, doc, "PARA-MINE"
    )
    assert conflict is None


# ---------------------------------------------------------------------------
# (e) Two sessions can hold NON-OVERLAPPING scoped claims on the SAME file
# ---------------------------------------------------------------------------

async def test_two_sessions_non_overlapping_elements(db):
    """Two sessions can claim different element_ids on the same file — no conflict."""
    sess_a = await _mk_session(db, "parallel-a")
    sess_b = await _mk_session(db, "parallel-b")
    doc = "shared-thesis.docx"

    ra = await db_module.claim_docx_region(db, sess_a, doc, "EL_INTRO")
    rb = await db_module.claim_docx_region(db, sess_b, doc, "EL_CONCLUSION")

    assert ra["claimed"] is True
    assert rb["claimed"] is True

    # Each session is allowed to write its own element.
    ca = await db_module.check_docx_region_write_conflict(db, sess_a, doc, "EL_INTRO")
    cb = await db_module.check_docx_region_write_conflict(db, sess_b, doc, "EL_CONCLUSION")
    assert ca is None, f"sess_a blocked on its own element: {ca}"
    assert cb is None, f"sess_b blocked on its own element: {cb}"


async def test_two_sessions_overlapping_element_blocked(db):
    """Two sessions CANNOT claim the same element_id."""
    sess_a = await _mk_session(db, "conflict-a")
    sess_b = await _mk_session(db, "conflict-b")
    doc = "conflict.docx"
    shared_elem = "EL_SHARED"

    ra = await db_module.claim_docx_region(db, sess_a, doc, shared_elem)
    rb = await db_module.claim_docx_region(db, sess_b, doc, shared_elem)

    assert ra["claimed"] is True
    assert rb["claimed"] is False
    assert rb["reason"] == "element_conflict"
    assert rb["conflicts"][0]["holder_session_id"] == sess_a


async def test_parallel_writes_cross_block(db):
    """Session A cannot write Session B's element (and vice versa)."""
    sess_a = await _mk_session(db, "cross-a")
    sess_b = await _mk_session(db, "cross-b")
    doc = "cross.docx"

    await db_module.claim_docx_region(db, sess_a, doc, "PARA_A")
    await db_module.claim_docx_region(db, sess_b, doc, "PARA_B")

    # A tries to write B's element.
    conflict_a_on_b = await db_module.check_docx_region_write_conflict(
        db, sess_a, doc, "PARA_B"
    )
    assert conflict_a_on_b is not None
    assert conflict_a_on_b["blocked"] is True
    assert conflict_a_on_b["holder"] == sess_b

    # B tries to write A's element.
    conflict_b_on_a = await db_module.check_docx_region_write_conflict(
        db, sess_b, doc, "PARA_A"
    )
    assert conflict_b_on_a is not None
    assert conflict_b_on_a["blocked"] is True
    assert conflict_b_on_a["holder"] == sess_a


# ---------------------------------------------------------------------------
# get_docx_region_claims — read-only listing
# ---------------------------------------------------------------------------

async def test_get_docx_region_claims_returns_active(db):
    """get_docx_region_claims lists active claims; released ones are absent."""
    sess = await _mk_session(db, "lister")
    doc = "list-me.docx"

    await db_module.claim_docx_region(db, sess, doc, "ELEM1")
    await db_module.claim_docx_region(db, sess, doc, "ELEM2")

    claims = await db_module.get_docx_region_claims(db, doc)
    element_ids = {c["element_id"] for c in claims}
    assert "ELEM1" in element_ids
    assert "ELEM2" in element_ids


async def test_get_docx_region_claims_empty_for_unregistered(db):
    """A file nobody has claimed returns an empty claims list."""
    claims = await db_module.get_docx_region_claims(db, "nobody-claimed.docx")
    assert claims == []


# ---------------------------------------------------------------------------
# release_docx_region_claims
# ---------------------------------------------------------------------------

async def test_release_docx_region_claims_single_element(db):
    """Releasing a single element allows another session to claim it."""
    sess_a = await _mk_session(db, "rel-a")
    sess_b = await _mk_session(db, "rel-b")
    doc = "releasable.docx"
    elem = "RELELEM"

    await db_module.claim_docx_region(db, sess_a, doc, elem)
    # B cannot claim yet.
    rb_before = await db_module.claim_docx_region(db, sess_b, doc, elem)
    assert rb_before["claimed"] is False

    # A releases.
    released = await db_module.release_docx_region_claims(db, sess_a, doc, elem)
    assert released == 1

    # Now B can claim.
    rb_after = await db_module.claim_docx_region(db, sess_b, doc, elem)
    assert rb_after["claimed"] is True


async def test_release_docx_region_claims_all_for_file(db):
    """Releasing all claims on a file lets another session claim any element."""
    sess_a = await _mk_session(db, "rel-all-a")
    sess_b = await _mk_session(db, "rel-all-b")
    doc = "release-all.docx"

    await db_module.claim_docx_region(db, sess_a, doc, "E1")
    await db_module.claim_docx_region(db, sess_a, doc, "E2")
    await db_module.claim_docx_region(db, sess_a, doc, "E3")

    released = await db_module.release_docx_region_claims(db, sess_a, doc)
    assert released == 3

    claims = await db_module.get_docx_region_claims(db, doc)
    assert claims == []

    # B can now claim any element.
    rb = await db_module.claim_docx_region(db, sess_b, doc, "E1")
    assert rb["claimed"] is True


async def test_release_docx_region_claims_session_wide(db):
    """Session-wide release (no file_path) drops claims across all files."""
    sess = await _mk_session(db, "wide-rel")
    await db_module.claim_docx_region(db, sess, "doc1.docx", "E1")
    await db_module.claim_docx_region(db, sess, "doc2.docx", "E2")

    released = await db_module.release_docx_region_claims(db, sess)
    assert released == 2

    c1 = await db_module.get_docx_region_claims(db, "doc1.docx")
    c2 = await db_module.get_docx_region_claims(db, "doc2.docx")
    assert c1 == []
    assert c2 == []


# ---------------------------------------------------------------------------
# Fail-open and edge-case safety
# ---------------------------------------------------------------------------

async def test_check_conflict_fail_open_no_db(db):
    """No db handle -> fail open (never fabricates a block)."""
    conflict = await db_module.check_docx_region_write_conflict(
        None, "s1", "test.docx", "PARA1"
    )
    assert conflict is None


async def test_claim_docx_region_rejects_empty_file_path(db):
    sess = await _mk_session(db, "val-sess")
    result = await db_module.claim_docx_region(db, sess, "", "ELEM1")
    assert result["claimed"] is False
    assert result["reason"] == "invalid"


async def test_claim_docx_region_rejects_empty_element_id(db):
    sess = await _mk_session(db, "val-sess2")
    result = await db_module.claim_docx_region(db, sess, "doc.docx", "")
    assert result["claimed"] is False
    assert result["reason"] == "invalid"
