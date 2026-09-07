"""6507e83a (C84-W3, category 3) — whole-document cross-process DOCX lease.

``claim_docx_region`` (f7ee1ba7) hard-requires a non-empty ``element_id`` —
there was no whole-document analog: a durable, cross-process, TTL-bound
lease over an ENTIRE .docx. The closest existing primitive,
``_docx_promotion_lock`` (docs_intel.py / doc_store.py), is a process-local
``threading.RLock`` that cannot protect a SECOND process racing the same
destination file.

This file tests the new primitives in ``meridian/db/locks.py``:

* ``acquire_docx_document_lease`` / ``get_docx_document_lease`` /
  ``release_docx_document_lease`` — acquire/read/release round-trip, and
  every conflict rule (blocked by a whole-file lock, blocked by ANY other
  session's claim — lease or scoped element).
* The lease composing correctly with the PRE-EXISTING Model B scoped-claim
  system: ``claim_docx_region`` rejects a new element claim while another
  session holds the lease, and ``check_docx_region_write_conflict`` blocks
  every OTHER session's write (any element_id, including none) while
  leaving the lease holder's own writes unaffected.
* MCP tool wiring (schema registration + dispatch) for
  ``acquire_docx_document_lease`` / ``get_docx_document_lease`` /
  ``release_docx_document_lease`` / ``find_orphaned_docx_staged_files``.

No new migration is needed on either DB backend: the lease reuses the
EXISTING ``file_docx_region_claims`` table (present on both SQLite —
``meridian/db/locks.py:_migrate_docx_region_claims`` — and Postgres —
``meridian/pg_adapter.py:_migrate_pg_file_docx_region_claims``) with a
reserved sentinel ``element_id`` rather than a new table.
"""
from __future__ import annotations

from meridian import db as db_module
import meridian.server as srv
from meridian.mcp_tools import _MCP_TOOLS_LIST, _TOOL_CATEGORY, _TOOL_ROLE_RELEVANCE, _TOOL_WORKFLOW_TIER

# pytest.ini sets asyncio_mode = auto — `async def test_...` functions below
# are collected as asyncio tests automatically, no explicit marker needed
# (matching tests/test_docx_scoped_region_claims.py's existing convention).
# The plain `def test_...` functions further down (schema/classification
# checks) are synchronous by design and must NOT carry an asyncio mark.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _mk_session(db, name: str) -> str:
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


# ---------------------------------------------------------------------------
# Acquire / release round-trip
# ---------------------------------------------------------------------------

async def test_acquire_lease_succeeds_on_a_clear_file(db):
    sess = await _mk_session(db, "lease-a")
    doc = "thesis/whole.docx"

    result = await db_module.acquire_docx_document_lease(db, sess, doc)

    assert result["leased"] is True
    assert result["session_id"] == sess
    assert db_module._normalize_file_path(doc) in result["file_path"]


async def test_acquire_lease_is_idempotent_for_the_same_session(db):
    sess = await _mk_session(db, "lease-idem")
    doc = "report.docx"

    r1 = await db_module.acquire_docx_document_lease(db, sess, doc)
    r2 = await db_module.acquire_docx_document_lease(db, sess, doc)

    assert r1["leased"] is True
    assert r2["leased"] is True
    claims = await db_module.get_docx_region_claims(db, doc)
    active = [
        c for c in claims
        if c.get("session_id") == sess
        and c.get("element_id") == db_module.DOCX_WHOLE_DOCUMENT_ELEMENT
    ]
    assert len(active) == 1, "re-acquiring must refresh, not duplicate, the lease row"


async def test_acquire_lease_requires_session_id_and_file_path(db):
    r1 = await db_module.acquire_docx_document_lease(db, "", "doc.docx")
    assert r1["leased"] is False
    assert r1["reason"] == "invalid"

    r2 = await db_module.acquire_docx_document_lease(db, "some-session", "")
    assert r2["leased"] is False
    assert r2["reason"] == "invalid"


async def test_get_lease_returns_none_when_unleased(db):
    assert await db_module.get_docx_document_lease(db, "never-leased.docx") is None


async def test_get_lease_returns_the_current_holder(db):
    sess = await _mk_session(db, "lease-get")
    doc = "held.docx"
    await db_module.acquire_docx_document_lease(db, sess, doc)

    lease = await db_module.get_docx_document_lease(db, doc)

    assert lease is not None
    assert lease["session_id"] == sess
    assert lease["element_id"] == db_module.DOCX_WHOLE_DOCUMENT_ELEMENT


async def test_release_lease_returns_zero_when_not_held(db):
    sess = await _mk_session(db, "lease-noop-release")
    released = await db_module.release_docx_document_lease(db, sess, "untouched.docx")
    assert released == 0


async def test_release_lease_then_reacquire_by_a_different_session(db):
    a = await _mk_session(db, "lease-rel-a")
    b = await _mk_session(db, "lease-rel-b")
    doc = "handoff.docx"

    await db_module.acquire_docx_document_lease(db, a, doc)
    released = await db_module.release_docx_document_lease(db, a, doc)
    assert released == 1
    assert await db_module.get_docx_document_lease(db, doc) is None

    r2 = await db_module.acquire_docx_document_lease(db, b, doc)
    assert r2["leased"] is True
    assert r2["session_id"] == b


# ---------------------------------------------------------------------------
# Conflict rules on acquisition
# ---------------------------------------------------------------------------

async def test_acquire_lease_blocked_by_another_sessions_whole_file_lock(db):
    locker = await _mk_session(db, "lease-vs-filelock-holder")
    leaser = await _mk_session(db, "lease-vs-filelock-leaser")
    doc = "locked.docx"

    claimed = await db_module.claim_file(db, doc, locker)
    assert claimed["claimed"] is True  # sanity: the whole-file lock actually landed

    result = await db_module.acquire_docx_document_lease(db, leaser, doc)

    assert result["leased"] is False
    assert result["reason"] == "file_locked"
    assert result["holder_session_id"] == locker


async def test_acquire_lease_not_blocked_by_the_lease_holders_own_whole_file_lock(db):
    """A session that holds BOTH the whole-file lock and requests the lease
    on the same file is not blocked by its own lock (mirrors claim_docx_region's
    identical own-lock exemption)."""
    sess = await _mk_session(db, "lease-self-filelock")
    doc = "self.docx"
    await db_module.claim_file(db, doc, sess)

    result = await db_module.acquire_docx_document_lease(db, sess, doc)

    assert result["leased"] is True


async def test_acquire_lease_blocked_by_an_existing_element_claim(db):
    owner = await _mk_session(db, "lease-vs-element-owner")
    leaser = await _mk_session(db, "lease-vs-element-leaser")
    doc = "partially-claimed.docx"
    await db_module.claim_docx_region(db, owner, doc, "PARA1")

    result = await db_module.acquire_docx_document_lease(db, leaser, doc)

    assert result["leased"] is False
    assert result["reason"] == "region_claims_active"
    assert result["holder_session_id"] == owner
    assert "PARA1" in result["conflicting_elements"]


async def test_acquire_lease_blocked_by_another_sessions_existing_lease(db):
    a = await _mk_session(db, "lease-vs-lease-a")
    b = await _mk_session(db, "lease-vs-lease-b")
    doc = "double-leased.docx"
    await db_module.acquire_docx_document_lease(db, a, doc)

    result = await db_module.acquire_docx_document_lease(db, b, doc)

    assert result["leased"] is False
    assert result["reason"] == "region_claims_active"
    assert result["holder_session_id"] == a


# ---------------------------------------------------------------------------
# Composition with claim_docx_region (element-scoped claims)
# ---------------------------------------------------------------------------

async def test_claim_docx_region_rejects_the_reserved_sentinel_element_id(db):
    sess = await _mk_session(db, "lease-sentinel-guard")
    result = await db_module.claim_docx_region(
        db, sess, "any.docx", db_module.DOCX_WHOLE_DOCUMENT_ELEMENT,
    )
    assert result["claimed"] is False
    assert result["reason"] == "invalid"


async def test_claim_docx_region_blocked_by_another_sessions_lease(db):
    leaser = await _mk_session(db, "lease-blocks-region-a")
    claimer = await _mk_session(db, "lease-blocks-region-b")
    doc = "leased-whole.docx"
    await db_module.acquire_docx_document_lease(db, leaser, doc)

    result = await db_module.claim_docx_region(db, claimer, doc, "SOME_PARA")

    assert result["claimed"] is False
    assert result["reason"] == "document_leased"
    assert result["holder_session_id"] == leaser


async def test_lease_holder_itself_can_still_claim_a_region(db):
    """Holding your own whole-document lease should never block your OWN
    subsequent element-scoped claim on the same file."""
    sess = await _mk_session(db, "lease-self-region")
    doc = "own-lease.docx"
    await db_module.acquire_docx_document_lease(db, sess, doc)

    result = await db_module.claim_docx_region(db, sess, doc, "SOME_PARA")

    assert result["claimed"] is True


# ---------------------------------------------------------------------------
# Composition with check_docx_region_write_conflict (the write-time gate
# BOTH update_paragraph's handler and the meridian-docs tunnel relay
# (meridian/routes/tunnel.py:check_docs_write_conflict) already call).
# ---------------------------------------------------------------------------

async def test_write_conflict_blocks_other_sessions_scoped_write_while_leased(db):
    leaser = await _mk_session(db, "lease-write-block-a")
    writer = await _mk_session(db, "lease-write-block-b")
    doc = "guarded.docx"
    await db_module.acquire_docx_document_lease(db, leaser, doc)

    conflict = await db_module.check_docx_region_write_conflict(
        db, writer, doc, "ANY_PARA_ID",
    )

    assert conflict is not None
    assert conflict["blocked"] is True
    assert conflict["reason"] == "document_leased"
    assert conflict["holder"] == leaser


async def test_write_conflict_blocks_other_sessions_unscoped_write_while_leased(db):
    """No element_id at all (a whole-file-shaped write, e.g. relocate_figure /
    relocate_table's real MCP arguments) is blocked identically."""
    leaser = await _mk_session(db, "lease-write-block-noelem-a")
    writer = await _mk_session(db, "lease-write-block-noelem-b")
    doc = "guarded-noelem.docx"
    await db_module.acquire_docx_document_lease(db, leaser, doc)

    conflict = await db_module.check_docx_region_write_conflict(
        db, writer, doc, None,
    )

    assert conflict is not None
    assert conflict["blocked"] is True
    assert conflict["reason"] == "document_leased"


async def test_write_conflict_allows_the_lease_holders_own_writes(db):
    sess = await _mk_session(db, "lease-write-self")
    doc = "own-writes.docx"
    await db_module.acquire_docx_document_lease(db, sess, doc)

    conflict_scoped = await db_module.check_docx_region_write_conflict(
        db, sess, doc, "ANY_PARA_ID",
    )
    conflict_unscoped = await db_module.check_docx_region_write_conflict(
        db, sess, doc, None,
    )

    assert conflict_scoped is None
    assert conflict_unscoped is None


async def test_write_conflict_clears_after_lease_release(db):
    leaser = await _mk_session(db, "lease-write-clear-a")
    writer = await _mk_session(db, "lease-write-clear-b")
    doc = "released.docx"
    await db_module.acquire_docx_document_lease(db, leaser, doc)

    blocked = await db_module.check_docx_region_write_conflict(db, writer, doc, "P1")
    assert blocked is not None

    await db_module.release_docx_document_lease(db, leaser, doc)

    clear = await db_module.check_docx_region_write_conflict(db, writer, doc, "P1")
    assert clear is None


# ---------------------------------------------------------------------------
# Genuine two-session (simulated two-process) concurrent-write scenario —
# the item's explicit "concurrency tests" ask.
# ---------------------------------------------------------------------------

async def test_two_session_concurrent_write_scenario(db):
    """Session A (simulating a bulk canonical-merge promotion) leases the
    whole document; Session B's concurrent attempt to write ANY paragraph is
    rejected BEFORE any filesystem write would happen; once A finishes and
    releases, B's write is allowed through cleanly."""
    a = await _mk_session(db, "concurrent-a")
    b = await _mk_session(db, "concurrent-b")
    doc = "concurrent-merge-target.docx"

    lease = await db_module.acquire_docx_document_lease(db, a, doc)
    assert lease["leased"] is True

    # B tries several different elements — every one is blocked while A holds
    # the lease, not just a specific "reserved" element_id.
    for elem in ("PARA_1", "PARA_2", None):
        conflict = await db_module.check_docx_region_write_conflict(db, b, doc, elem)
        assert conflict is not None and conflict["blocked"] is True

    # A finishes its bulk rewrite and releases the lease.
    released = await db_module.release_docx_document_lease(db, a, doc)
    assert released == 1

    # B's write now proceeds unblocked.
    for elem in ("PARA_1", "PARA_2", None):
        conflict = await db_module.check_docx_region_write_conflict(db, b, doc, elem)
        assert conflict is None


# ---------------------------------------------------------------------------
# MCP tool schema + dispatch wiring
# ---------------------------------------------------------------------------

_NEW_LEASE_TOOLS = (
    "acquire_docx_document_lease",
    "get_docx_document_lease",
    "release_docx_document_lease",
)


def test_new_lease_tools_registered_in_schema():
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    for tool_name in _NEW_LEASE_TOOLS + ("find_orphaned_docx_staged_files",):
        assert tool_name in names, f"{tool_name} missing from _MCP_TOOLS_LIST"


def test_new_lease_tools_classified_consistently_with_claim_docx_region():
    """The new lease tools should sit in the SAME category/role/tier bucket
    as the pre-existing claim_docx_region, since they are the same kind of
    executor-facing, maintenance-tier file-locking primitive."""
    for tool_name in _NEW_LEASE_TOOLS:
        assert _TOOL_CATEGORY.get(tool_name) == _TOOL_CATEGORY.get("claim_docx_region")
        assert _TOOL_ROLE_RELEVANCE.get(tool_name) == _TOOL_ROLE_RELEVANCE.get("claim_docx_region")
        assert _TOOL_WORKFLOW_TIER.get(tool_name) == _TOOL_WORKFLOW_TIER.get("claim_docx_region")


def test_maintenance_only_tools_get_the_baked_in_prefix():
    tool_map = {t["name"]: t for t in _MCP_TOOLS_LIST}
    for tool_name in _NEW_LEASE_TOOLS + ("find_orphaned_docx_staged_files",):
        tool = tool_map[tool_name]
        assert tool["workflow_tier"] == "maintenance-only"
        assert tool["description"].startswith("[MAINTENANCE] ")


async def test_acquire_get_release_lease_mcp_dispatch_roundtrip(db):
    a = await _mk_session(db, "mcp-lease-a")
    b = await _mk_session(db, "mcp-lease-b")
    doc = "mcp-dispatch.docx"

    acquired = await srv._dispatch_mcp_tool(
        "acquire_docx_document_lease", {"session_id": a, "file_path": doc}, db, "/tmp",
    )
    assert acquired["leased"] is True

    conflict = await srv._dispatch_mcp_tool(
        "acquire_docx_document_lease", {"session_id": b, "file_path": doc}, db, "/tmp",
    )
    assert conflict["leased"] is False

    got = await srv._dispatch_mcp_tool(
        "get_docx_document_lease", {"file_path": doc}, db, "/tmp",
    )
    assert got["lease"]["session_id"] == a

    released = await srv._dispatch_mcp_tool(
        "release_docx_document_lease", {"session_id": a, "file_path": doc}, db, "/tmp",
    )
    assert released["released"] == 1

    got_after = await srv._dispatch_mcp_tool(
        "get_docx_document_lease", {"file_path": doc}, db, "/tmp",
    )
    assert got_after["lease"] is None


async def test_find_orphaned_docx_staged_files_mcp_dispatch(db, tmp_path):
    stage = tmp_path / ".meridian-docx-stage-abc123.tmp"
    stage.write_bytes(b"partial")

    result = await srv._dispatch_mcp_tool(
        "find_orphaned_docx_staged_files", {"directory": str(tmp_path)}, db, "/tmp",
    )

    assert result["directory"] == str(tmp_path)
    assert len(result["staged_files"]) == 1
    assert result["staged_files"][0]["path"].endswith(".tmp")
