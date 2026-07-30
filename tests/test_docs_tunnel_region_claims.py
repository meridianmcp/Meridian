"""273df573 — scoped docx-region claim protection for the meridian-docs (docs)
tunnel slot.

Mirrors test_docx_tunnel_write_lock.py's word-slot coverage, but the new
`check_docs_write_conflict` guard delegates to the RICHER Model-B primitive
(`meridian.db.locks.check_docx_region_write_conflict`, the same gate
`update_paragraph` already enforces) instead of a plain whole-file claim
check: two sessions can hold non-overlapping SCOPED element claims on the
SAME .docx concurrently, and only a whole-file lock or a same-element claim
by another session blocks.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.routes import tunnel as tun


async def _mk_session(db, name: str) -> str:
    proj = await db_module.create_project(db, name=f"proj-{name}")
    sess = await db_module.register_session(db, project_id=proj["id"], name=name)
    return sess["id"]


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
