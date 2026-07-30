"""273df573 — scoped docx-region claim protection for the meridian-docs (docs)
tunnel slot.

Mirrors test_docx_tunnel_write_lock.py's word-slot coverage, but the new
`check_docs_write_conflict` guard delegates to the RICHER Model-B primitive
(`meridian.db.locks.check_docx_region_write_conflict`, the same gate
`update_paragraph` already enforces) instead of a plain whole-file claim
check: two sessions can hold non-overlapping SCOPED element claims on the
SAME .docx concurrently, and only a whole-file lock or a same-element claim
by another session blocks.

session_id-propagation regression coverage (273df573 fix, added after review):
the guard above was always correctly wired, but `session_id` never actually
reached it for a REAL meridian-docs tunnel call, because none of the 27
mutating `@mcp.tool()` wrappers in `extensions/meridian-docs/meridian_docs/
server.py` declared a `session_id` parameter -- so it never appeared in any
tool's advertised MCP `inputSchema`, and a compliant client had no field to
populate. See the schema-introspection + end-to-end tests near the bottom of
this file.
"""
from __future__ import annotations

import os
import sys

import pytest

from meridian import db as db_module
from meridian.routes import tunnel as tun

# Make meridian_docs importable from the local extensions directory, same
# pattern as tests/test_meridian_docs_bibliography_write.py /
# tests/test_fdbd4296_local_ingest_wrapper.py.
_EXT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-docs")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)


async def _mk_session(db, name: str) -> str:
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


def _docs_mcp_tool_schema(tool_name: str) -> dict:
    """Fetch a meridian-docs @mcp.tool()'s LIVE advertised inputSchema.

    Reads straight off the registered FastMCP Tool object -- the same source
    a real MCP client's `tools/list` is generated from -- so this reflects
    exactly what a compliant caller discovers, not just what the Python
    function signature happens to accept.
    """
    from meridian_docs import server as docs_server  # noqa: PLC0415

    tool = docs_server.mcp._tool_manager._tools[tool_name]
    return tool.parameters


# ---------------------------------------------------------------------------
# Pure target-detection helper — no db, no tunnel.
# ---------------------------------------------------------------------------

def test_docs_write_target_resolves_anchor_for_paragraph_shaped_tools():
    assert tun._docs_write_target(
        "insert_caption",
        {"docx_path": "thesis.docx", "anchor_para_id": "AB12", "kind": "Figure", "label_text": "x"},
    ) == ("thesis.docx", "AB12")
    assert tun._docs_write_target(
        "edit_equation",
        {"docx_path": "paper.docx", "equation_para_id": "EQ01", "new_payload": "E=mc^2"},
    ) == ("paper.docx", "EQ01")
    # Connector-prefixed name accepted (prefix stripped before lookup).
    assert tun._docs_write_target(
        "docs__move_section",
        {"docx_path": "d.docx", "section_id": "SEC1", "destination_anchor_para_id": "X"},
    ) == ("d.docx", "SEC1")


def test_docs_write_target_falls_back_to_none_for_non_paragraph_anchors():
    # relocate_figure keys off an integer figure_index, not a paraId — must
    # NOT be passed through as element_id (incompatible id space).
    assert tun._docs_write_target(
        "relocate_figure",
        {"docx_path": "f.docx", "figure_index": 1, "destination_anchor_para_id": "X"},
    ) == ("f.docx", None)
    assert tun._docs_write_target(
        "relocate_table",
        {"docx_path": "f.docx", "table_index": 0, "destination_anchor_para_id": "X"},
    ) == ("f.docx", None)
    # Whole-document tools have no natural anchor argument at all.
    assert tun._docs_write_target(
        "renumber_sequences", {"docx_path": "f.docx"},
    ) == ("f.docx", None)
    assert tun._docs_write_target(
        "sync_bibliography", {"docx_path": "f.docx", "csl_items": {}},
    ) == ("f.docx", None)


def test_docs_write_target_uses_canonical_path_arg_for_merge():
    assert tun._docs_write_target(
        "merge_docx_draft",
        {"canonical_path": "canon.docx", "draft_path": "draft.docx"},
    ) == ("canon.docx", None)


def test_docs_write_target_none_for_reads_and_unknowns():
    for tool in (
        "document_outline", "parse_document", "get_structure",
        "get_structure_elements", "get_paragraph", "search_paragraphs",
        "search_document", "read_document_snapshot", "check_render_capability",
        "find_image_paragraph", "extract_equations", "get_equations",
        "audit_equation_style", "scan_citation_keys", "format_reference",
        "get_section_content", "find_references_to", "scan_stale_notes",
        "list_internal_notes",
    ):
        assert tun._docs_write_target(tool, {"docx_path": "x.docx"}) is None, tool
    # Sidecar-only index builders never touch the .docx itself.
    assert tun._docs_write_target(
        "index_document", {"path": "x.docx", "index_db_path": "idx.db"}
    ) is None
    assert tun._docs_write_target(
        "index_document_structure", {"path": "x.docx", "index_db_path": "idx.db"}
    ) is None
    assert tun._docs_write_target(
        "index_equations", {"path": "x.docx", "index_db_path": "idx.db"}
    ) is None
    # Unknown tool name.
    assert tun._docs_write_target("some_future_tool", {"docx_path": "x.docx"}) is None
    # A known writer with no identifiable path arg fails open (can't guard it).
    assert tun._docs_write_target("insert_caption", {"anchor_para_id": "A"}) is None
    assert tun._docs_write_target("insert_caption", None) is None


# ---------------------------------------------------------------------------
# check_docs_write_conflict against real docx-region claims.
# ---------------------------------------------------------------------------

async def test_disjoint_scoped_claims_both_proceed(db):
    """Two sessions, two different elements, same file — neither is blocked."""
    sess_a = await _mk_session(db, "docs-a")
    sess_b = await _mk_session(db, "docs-b")
    doc = "shared/paper.docx"

    await db_module.claim_docx_region(db, sess_a, doc, "PARA_A")
    await db_module.claim_docx_region(db, sess_b, doc, "PARA_B")

    verdict_a = await tun.check_docs_write_conflict(
        db, "tenant-1", "insert_caption",
        {"docx_path": doc, "anchor_para_id": "PARA_A", "kind": "Figure", "label_text": "x"},
        session_id=sess_a,
    )
    verdict_b = await tun.check_docs_write_conflict(
        db, "tenant-1", "insert_caption",
        {"docx_path": doc, "anchor_para_id": "PARA_B", "kind": "Figure", "label_text": "y"},
        session_id=sess_b,
    )
    assert verdict_a is None
    assert verdict_b is None


async def test_overlapping_scoped_claim_rejected(db):
    """Second session's call to the SAME element is rejected with a clear message."""
    owner = await _mk_session(db, "docs-owner")
    intruder = await _mk_session(db, "docs-intruder")
    doc = "contested/paper.docx"
    elem = "PARA_SHARED"

    await db_module.claim_docx_region(db, owner, doc, elem)

    verdict = await tun.check_docs_write_conflict(
        db, "tenant-1", "edit_caption",
        {"docx_path": doc, "caption_para_id": elem, "new_label_text": "z"},
        session_id=intruder,
    )
    assert verdict is not None
    assert verdict["blocked"] is True
    assert verdict["reason"] == "element_locked"
    assert verdict["holder"] == owner
    assert elem in verdict["message"]


async def test_whole_file_lock_blocks_all_scoped_docs_writers(db):
    """A whole-file write lock blocks every scoped docs-slot writer on that file."""
    file_owner = await _mk_session(db, "docs-file-owner")
    other = await _mk_session(db, "docs-other")
    doc = "locked/paper.docx"

    claimed = await db_module.claim_file(db, doc, file_owner, mode="write")
    assert claimed["claimed"] is True

    verdict = await tun.check_docs_write_conflict(
        db, "tenant-1", "insert_equation",
        {"docx_path": doc, "anchor_para_id": "ANY", "payload": "x"},
        session_id=other,
    )
    assert verdict is not None
    assert verdict["blocked"] is True
    assert verdict["reason"] == "file_locked"
    assert verdict["holder"] == file_owner


async def test_fail_open_without_db():
    """No db handle -> fail open (the guard never fabricates a block)."""
    verdict = await tun.check_docs_write_conflict(
        None, "tenant-1", "insert_caption",
        {"docx_path": "x.docx", "anchor_para_id": "A", "kind": "Figure", "label_text": "l"},
        session_id="s1",
    )
    assert verdict is None


async def test_fail_open_missing_session_id_when_unclaimed(db):
    """A caller with no session_id on an otherwise-unclaimed file is not blocked —
    a missing coordination signal must not become a denial-of-service on every
    meridian-docs call."""
    verdict = await tun.check_docs_write_conflict(
        db, "tenant-1", "insert_caption",
        {"docx_path": "fresh.docx", "anchor_para_id": "A", "kind": "Figure", "label_text": "l"},
        session_id=None,
    )
    assert verdict is None


async def test_read_only_docs_tool_never_gated(db):
    """A read-only docs-slot tool attempts no claim check at all, even against a
    live whole-file lock on the same document."""
    owner = await _mk_session(db, "docs-ro-owner")
    doc = "readonly-check.docx"
    await db_module.claim_file(db, doc, owner, mode="write")

    verdict = await tun.check_docs_write_conflict(
        db, "tenant-1", "search_document",
        {"docx_path": doc, "query": "term"},
        session_id="reader",
    )
    assert verdict is None


# ---------------------------------------------------------------------------
# End-to-end: call_tunnel_tool raises on a conflicting docs write, and only for
# the docs slot / mutating tools — and the underlying file is provably untouched.
# ---------------------------------------------------------------------------

async def test_call_tunnel_tool_raises_on_docs_write_conflict(db, monkeypatch, tmp_path):
    owner = await _mk_session(db, "docs-e2e-owner")
    doc_path = tmp_path / "shared.docx"
    original_bytes = b"ORIGINAL-DOCX-BYTES"
    doc_path.write_bytes(original_bytes)
    doc = str(doc_path)
    elem = "PARA1"

    await db_module.claim_docx_region(db, owner, doc, elem)

    tenant = "tenant-docs-77"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"docs__edit_caption": "docs"},
    )
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    called = {"relayed": False}

    async def _fake_jsonrpc(*a, **k):
        called["relayed"] = True
        doc_path.write_bytes(b"MUTATED-BY-RELAY")
        return {"result": {"content": []}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    with pytest.raises(RuntimeError, match="claimed by session"):
        await tun.call_tunnel_tool(
            tenant, "docs__edit_caption",
            {"docx_path": doc, "caption_para_id": elem, "new_label_text": "hijacked"},
            db=db, session_id="intruder",
        )

    assert called["relayed"] is False
    assert doc_path.read_bytes() == original_bytes


async def test_call_tunnel_tool_relays_docs_write_when_clear(db, monkeypatch):
    tenant = "tenant-docs-78"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"docs__insert_caption": "docs"},
    )
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    async def _fake_jsonrpc(*a, **k):
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    result = await tun.call_tunnel_tool(
        tenant, "docs__insert_caption",
        {"docx_path": "brand-new.docx", "anchor_para_id": "A", "kind": "Figure", "label_text": "l"},
        db=db, session_id="s1",
    )
    assert result == {"content": [{"type": "text", "text": "ok"}]}


async def test_call_tunnel_tool_whole_file_lock_blocks_docs_write(db, monkeypatch):
    file_owner = await _mk_session(db, "docs-e2e-flock")
    doc = "e2e-locked.docx"
    await db_module.claim_file(db, doc, file_owner, mode="write")

    tenant = "tenant-docs-79"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"docs__insert_image": "docs"},
    )
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    async def _fake_jsonrpc(*a, **k):
        raise AssertionError("must not relay when whole-file-locked")

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    with pytest.raises(RuntimeError, match="whole-file write lock"):
        await tun.call_tunnel_tool(
            tenant, "docs__insert_image",
            {"docx_path": doc, "image_path": "x.png"},
            db=db, session_id="other",
        )


async def test_call_tunnel_tool_read_only_docs_tool_not_gated(db, monkeypatch):
    """A read-only docs tool is relayed even against a live whole-file lock."""
    owner = await _mk_session(db, "docs-e2e-ro-owner")
    doc = "e2e-readonly.docx"
    await db_module.claim_file(db, doc, owner, mode="write")

    tenant = "tenant-docs-80"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"docs__search_document": "docs"},
    )
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    called = {"relayed": False}

    async def _fake_jsonrpc(*a, **k):
        called["relayed"] = True
        return {"result": {"content": [{"type": "text", "text": "[]"}]}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    result = await tun.call_tunnel_tool(
        tenant, "docs__search_document",
        {"docx_path": doc, "query": "term"},
        db=db, session_id="reader",
    )
    assert called["relayed"] is True
    assert result == {"content": [{"type": "text", "text": "[]"}]}


# ---------------------------------------------------------------------------
# Fail-open coverage gaps flagged by review: an in-guard lookup EXCEPTION
# (not just a None db) must still fail open, and db=None must still allow the
# relay through call_tunnel_tool itself, not just through
# check_docs_write_conflict in isolation.
# ---------------------------------------------------------------------------

async def test_check_docs_write_conflict_fails_open_on_lookup_exception(db, monkeypatch):
    """A genuine lookup error inside check_docx_region_write_conflict (e.g. a
    transient DB failure) must fall through the guard's `except Exception:`
    branch and degrade to None (no block) -- distinct from the already-
    covered "db is None" fast path above, which never reaches the try/except
    at all."""
    async def _boom(*a, **k):
        raise RuntimeError("simulated claim-lookup failure")

    monkeypatch.setattr(db_module, "check_docx_region_write_conflict", _boom)

    verdict = await tun.check_docs_write_conflict(
        db, "tenant-1", "insert_caption",
        {"docx_path": "boom.docx", "anchor_para_id": "A", "kind": "Figure", "label_text": "l"},
        session_id="s1",
    )
    assert verdict is None


async def test_call_tunnel_tool_relays_docs_write_when_db_is_none(monkeypatch):
    """End-to-end (through call_tunnel_tool itself, not check_docs_write_
    conflict in isolation): db=None must still let a MUTATING docs-slot
    write relay through to the tunneled server."""
    tenant = "tenant-docs-no-db"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"docs__insert_caption": "docs"},
    )
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    called = {"relayed": False}

    async def _fake_jsonrpc(*a, **k):
        called["relayed"] = True
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    result = await tun.call_tunnel_tool(
        tenant, "docs__insert_caption",
        {"docx_path": "no-db.docx", "anchor_para_id": "A", "kind": "Figure", "label_text": "l"},
        db=None, session_id="s1",
    )
    assert called["relayed"] is True
    assert result == {"content": [{"type": "text", "text": "ok"}]}


# ---------------------------------------------------------------------------
# 273df573 fix verification — session_id now reaches the guard for a REAL
# meridian-docs tunnel call, via the tool's own advertised MCP schema. These
# tests exercise the actual regression: before the fix, EVERY wrapper below
# already passed the "guard is wired correctly" tests above (session_id was
# handed to check_docs_write_conflict directly in those tests) yet the bug
# still shipped, because no real client could ever populate session_id in
# the first place -- the schema never declared it. That's the gap this
# section closes.
# ---------------------------------------------------------------------------

def test_all_docs_write_tools_declare_session_id_in_their_mcp_schema():
    """Every tool in tunnel.py's _DOCS_WRITE_TOOLS map must advertise
    `session_id` in its OWN registered MCP inputSchema -- not merely accept
    it as a Python kwarg on the underlying docs_intel call. `tools/list` (the
    schema a real client discovers and populates) is generated from each
    `@mcp.tool()` wrapper's own declared parameters, so this is the actual
    surface a compliant client sees.

    Against the pre-fix code (extensions/meridian-docs/meridian_docs/
    server.py before this commit), every iteration of this loop fails --
    none of the 27 wrappers declared session_id, so it never appeared in
    inputSchema for any of them.
    """
    assert len(tun._DOCS_WRITE_TOOLS) == 27
    for tool_name in sorted(tun._DOCS_WRITE_TOOLS):
        schema = _docs_mcp_tool_schema(tool_name)
        props = schema.get("properties", {})
        assert "session_id" in props, (
            f"{tool_name}'s MCP inputSchema is missing session_id -- a "
            "compliant client has no way to populate it, so the tunnel-layer "
            "DOCX region-claim guard can never identify this caller."
        )


async def test_e2e_schema_declared_session_id_lets_owner_write_own_claimed_element(db, monkeypatch):
    """273df573 regression -- the actual bug scenario, end to end, through
    the real schema-declared-parameter -> args dict -> handler extraction ->
    guard flow (as close as this test infrastructure gets to a live MCP
    client without spinning up the stdio transport).

    Before the fix: session_id was never part of any meridian-docs tool's
    declared MCP schema, so a real client had no field to populate; meridian/
    mcp/handler.py's `args.get("session_id")` extraction (itself always
    correct) therefore always received nothing for a real call, and
    check_docs_write_conflict could never recognize the calling session as
    the owner of its OWN scoped claim -- it was rejected as if a stranger
    held the element. This is the feature's stated primary use case.

    Steps, mirroring a real compliant client + handler.py exactly:
      1. Read edit_caption's LIVE MCP inputSchema and confirm session_id is
         actually declared there (this alone fails against the pre-fix code).
      2. Build a tools/call arguments dict using only keys that schema
         declares -- what a compliant client would send.
      3. Extract session_id from that dict the same way meridian/mcp/
         handler.py does: `(args.get("session_id") or "").strip() or None`.
      4. Feed the extracted session_id into call_tunnel_tool, exactly as
         handler.py does when relaying a docs-slot tool call.
    """
    owner = await _mk_session(db, "docs-schema-owner")
    doc = "schema-owner-writes-own-claim.docx"
    elem = "PARA_SCHEMA_OWNED"
    await db_module.claim_docx_region(db, owner, doc, elem)

    schema = _docs_mcp_tool_schema("edit_caption")
    assert "session_id" in schema.get("properties", {})

    call_arguments = {
        "docx_path": doc,
        "caption_para_id": elem,
        "new_label_text": "revised by owner",
        "session_id": owner,
    }
    # Sanity: every key we're sending is one the schema actually declares --
    # this is what "a compliant client" means in practice.
    assert set(call_arguments) <= set(schema["properties"])

    tenant = "tenant-docs-schema-owner"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"docs__edit_caption": "docs"},
    )
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    called = {"relayed": False}

    async def _fake_jsonrpc(*a, **k):
        called["relayed"] = True
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    # meridian/mcp/handler.py, verbatim:
    #   session_id=(args.get("session_id") or "").strip() or None
    extracted_session_id = (call_arguments.get("session_id") or "").strip() or None
    assert extracted_session_id == owner

    result = await tun.call_tunnel_tool(
        tenant, "docs__edit_caption", call_arguments,
        db=db, session_id=extracted_session_id,
    )
    assert called["relayed"] is True
    assert result == {"content": [{"type": "text", "text": "ok"}]}


async def test_e2e_missing_session_id_reproduces_the_original_bug(db, monkeypatch):
    """Companion to the regression test above, proving the fix (not some
    incidental change) is what flips the outcome: WITHOUT session_id -- the
    state of every real call before this fix, since no wrapper declared the
    field -- the SAME owner writing to the SAME element they legitimately
    claimed is wrongly rejected as if a stranger held it."""
    owner = await _mk_session(db, "docs-schema-owner-missing")
    doc = "schema-owner-missing-session.docx"
    elem = "PARA_MISSING_SESSION"
    await db_module.claim_docx_region(db, owner, doc, elem)

    tenant = "tenant-docs-schema-owner-missing"
    monkeypatch.setitem(
        tun._tunnel_tool_routes, tenant, {"docs__edit_caption": "docs"},
    )
    monkeypatch.setitem(tun._tunnel_docs_sockets, tenant, object())

    async def _fake_jsonrpc(*a, **k):
        raise AssertionError("must not relay when the caller isn't recognized as the owner")

    monkeypatch.setattr(tun, "_tunnel_jsonrpc", _fake_jsonrpc)

    with pytest.raises(RuntimeError, match="claimed by session"):
        await tun.call_tunnel_tool(
            tenant, "docs__edit_caption",
            {"docx_path": doc, "caption_para_id": elem, "new_label_text": "x"},
            db=db, session_id=None,
        )
