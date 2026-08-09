"""Coverage for the OOXML-Graph DOCX intelligence layer, Phase 1 (618adf32).

Builds a synthetic in-memory .docx (a ZIP with a single word/document.xml) so
the parser + sidecar-SQLite index are tested without any third-party dependency.
"""
from __future__ import annotations

import io
import time
import zipfile

from meridian import docs_intel

_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="00000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="00000002">
      <w:r><w:t>Meridian coordinates </w:t></w:r>
      <w:r><w:t>AI sessions.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="00000003">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Design</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>A paragraph with no paraId.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _synthetic_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _DOCUMENT_XML)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# a62e5b4f / 0d1b0809 — a richer doc with field codes (simple + complex) and a
# table interleaved between paragraphs, to exercise field extraction and the
# full ordered content tree.
# ---------------------------------------------------------------------------
_RICH_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="10000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Contents</w:t></w:r>
    </w:p>
    <w:p w14:paraId="10000002">
      <w:fldSimple w:instr=" TOC \\o &quot;1-3&quot; \\h \\z \\u ">
        <w:r><w:t>1 Intro ....... 3</w:t></w:r>
      </w:fldSimple>
    </w:p>
    <w:p w14:paraId="10000003">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Figures</w:t></w:r>
    </w:p>
    <w:p w14:paraId="10000004">
      <w:r><w:t>Figure </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> SEQ Figure \\* ARABIC </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>1</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t>: a plain caption.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="10000005">
      <w:r><w:t>See page </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> PAGEREF _Ref12345 </w:instrText></w:r>
      <w:r><w:instrText>\\h </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t> for details.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>R1C1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>R1C2</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>R2C1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>R2C2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="10000006">
      <w:r><w:t>Closing paragraph after the table.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _rich_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _RICH_DOCUMENT_XML)
    return buf.getvalue()


def test_parse_docx_extracts_paraids_styles_and_joined_text():
    paras = docs_intel.parse_docx(_synthetic_docx())
    # The fourth paragraph has no w14:paraId — it gets a stable structural-hash id
    # (prefixed "sp") rather than the old fragile positional "p3".
    para_ids = [p["para_id"] for p in paras]
    assert para_ids[:3] == ["00000001", "00000002", "00000003"]
    assert para_ids[3].startswith("sp"), f"expected stable synth id, got {para_ids[3]!r}"
    assert [p["style"] for p in paras] == ["Heading1", None, "Heading2", None]
    # Multiple runs in one paragraph are concatenated.
    assert paras[1]["text"] == "Meridian coordinates AI sessions."


def test_document_outline_headings():
    # 13462df2 — stateless heading outline (no sidecar index).
    out = docs_intel.document_outline(_synthetic_docx())
    assert out["paragraph_count"] == 4
    assert out["heading_count"] == 2
    assert [h["level"] for h in out["headings"]] == [1, 2]
    assert out["headings"][0]["text"] == "Introduction"
    assert out["headings"][0]["para_id"] == "00000001"


def test_get_document_structure_mcp_tool(tmp_path):
    # 13462df2 — exposed as an MCP tool (server-side file path, like ingest_document).
    import asyncio
    from meridian import server as mh
    from meridian import db as db_module

    docx_path = tmp_path / "chapter.docx"
    docx_path.write_bytes(_synthetic_docx())
    db = asyncio.run(db_module.init_db(":memory:"))
    try:
        res = asyncio.run(mh._dispatch_mcp_tool(
            "get_document_structure", {"file_path": str(docx_path)}, db, str(tmp_path)))
        assert res["heading_count"] == 2 and res["paragraph_count"] == 4
        # Missing file -> error dict, never a crash.
        err = asyncio.run(mh._dispatch_mcp_tool(
            "get_document_structure",
            {"file_path": str(tmp_path / "nope.docx")}, db, str(tmp_path)))
        assert "error" in err
        # Missing file_path -> error.
        err2 = asyncio.run(mh._dispatch_mcp_tool(
            "get_document_structure", {}, db, str(tmp_path)))
        assert "error" in err2
    finally:
        asyncio.run(db.close())


def test_get_document_structure_hosted_errors_honestly(tmp_path, monkeypatch):
    # b43bab91 — on hosted Meridian the server can't read a caller's local path,
    # so the tool must fail HONESTLY (explain + point to self-host/tunnel) instead
    # of the doomed read's misleading "file not found". The file physically exists
    # on THIS box, but hosted mode must refuse it regardless.
    import asyncio
    from meridian import server as mh
    from meridian import db as db_module

    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    docx_path = tmp_path / "chapter.docx"
    docx_path.write_bytes(_synthetic_docx())
    db = asyncio.run(db_module.init_db(":memory:"))
    try:
        res = asyncio.run(mh._dispatch_mcp_tool(
            "get_document_structure", {"file_path": str(docx_path)}, db, str(tmp_path)))
        # The honest path is distinguished by hosted=True + actionable guidance
        # (self-host / tunnel), which the old bare "file not found: {fp}" lacked.
        assert res.get("hosted") is True
        assert "error" in res
        low = res["error"].lower()
        assert "self-host" in low or "tunnel" in low
    finally:
        asyncio.run(db.close())


def test_get_latex_structure_hosted_prefers_source_over_path(tmp_path, monkeypatch):
    # b43bab91 — get_latex_structure has the same server-side-file-path problem, but
    # ALSO accepts inline `source` (which works hosted). On hosted: a path-only call
    # fails honestly; an inline-source call still works.
    import asyncio
    from meridian import server as mh
    from meridian import db as db_module

    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    db = asyncio.run(db_module.init_db(":memory:"))
    try:
        err = asyncio.run(mh._dispatch_mcp_tool(
            "get_latex_structure", {"file_path": "/home/user/thesis.tex"}, db, str(tmp_path)))
        assert err.get("hosted") is True and "error" in err
        assert "source" in err["error"].lower()
        # Inline source works even on hosted — the server never touches the FS.
        ok = asyncio.run(mh._dispatch_mcp_tool(
            "get_latex_structure",
            {"source": "\\section{Intro}\nhello world"}, db, str(tmp_path)))
        assert "error" not in ok
        assert "heading_count" in ok
    finally:
        asyncio.run(db.close())


def test_document_structure_endpoint(client, tmp_path):
    """3f596f81 — GET /projects/{id}/document-structure returns the docx outline
    for the Documents panel; failures are returned inline, not as 500s."""
    docx_path = tmp_path / "ch.docx"
    docx_path.write_bytes(_synthetic_docx())
    pid = client.post("/projects", json={"name": "docs-panel"}).json()["id"]
    # Happy path — server-side parse of a real .docx.
    r = client.get(f"/projects/{pid}/document-structure", params={"path": str(docx_path)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paragraph_count"] == 4
    assert body["heading_count"] == 2
    assert [h["level"] for h in body["headings"]] == [1, 2]
    assert body["headings"][0]["text"] == "Introduction"
    # Missing file → inline error, not a 500.
    r2 = client.get(
        f"/projects/{pid}/document-structure",
        params={"path": str(tmp_path / "nope.docx")},
    )
    assert r2.status_code == 200 and "error" in r2.json()
    # Unknown project → 404.
    r3 = client.get(
        "/projects/does-not-exist/document-structure",
        params={"path": str(docx_path)},
    )
    assert r3.status_code == 404


def test_index_and_navigate_by_paraid(tmp_path):
    db = str(tmp_path / "doc.idx.sqlite")
    summary = docs_intel.index_docx(_synthetic_docx(), db)
    assert summary["paragraph_count"] == 4
    assert summary["heading_count"] == 2

    # Targeted lookup by the stable w14:paraId.
    para = docs_intel.get_paragraph(db, "00000002")
    assert para is not None and para["text"] == "Meridian coordinates AI sessions."
    assert docs_intel.get_paragraph(db, "no-such-id") is None

    # Structure outline (headings only, with levels, in document order).
    outline = docs_intel.get_structure(db)
    assert outline == [
        {"para_id": "00000001", "level": 1, "text": "Introduction"},
        {"para_id": "00000003", "level": 2, "text": "Design"},
    ]

    # Text search returns the owning paraId.
    hits = docs_intel.find_paragraphs(db, "AI sessions")
    assert len(hits) == 1 and hits[0]["para_id"] == "00000002"


def test_index_is_idempotent(tmp_path):
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)
    summary = docs_intel.index_docx(_synthetic_docx(), db)  # re-index
    assert summary["paragraph_count"] == 4
    assert len(docs_intel.find_paragraphs(db, "paragraph")) == 1


# ---------------------------------------------------------------------------
# 2426dce9 — staleness detection: a read call auto-refreshes when the source
# .docx's mtime has moved since the last index_docx call, instead of silently
# serving a stale cached paragraph table forever.
# ---------------------------------------------------------------------------


def test_check_staleness_reports_no_source_for_bytes_index(tmp_path):
    # index_docx(bytes, ...) has no file path to track — check_staleness must
    # report stale=False with an explicit "no-source-tracked" reason, not a
    # false positive.
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)
    info = docs_intel.check_staleness(db)
    assert info == {"stale": False, "source_path": None, "reason": "no-source-tracked"}


def test_check_staleness_detects_mtime_change_and_get_structure_auto_refreshes(tmp_path):
    docx_path = tmp_path / "doc.docx"
    docx_path.write_bytes(_synthetic_docx())
    db = str(tmp_path / "doc.idx.sqlite")

    docs_intel.index_docx(str(docx_path), db)
    assert docs_intel.check_staleness(db)["stale"] is False

    # Rewrite the source with new content and force the mtime forward so this
    # assertion is never flaky on filesystems with coarse mtime resolution.
    docx_path.write_bytes(_synthetic_docx())
    new_mtime = docx_path.stat().st_mtime + 5
    import os as _os

    _os.utime(docx_path, (new_mtime, new_mtime))

    stale_info = docs_intel.check_staleness(db)
    assert stale_info["stale"] is True
    assert stale_info["source_path"] == str(docx_path)

    # get_structure must auto-refresh transparently — no manual re-index call.
    docs_intel.get_structure(db)
    assert docs_intel.check_staleness(db)["stale"] is False


def test_get_paragraph_and_find_paragraphs_also_auto_refresh(tmp_path):
    docx_path = tmp_path / "doc.docx"
    docx_path.write_bytes(_synthetic_docx())
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(str(docx_path), db)

    docx_path.write_bytes(_synthetic_docx())
    new_mtime = docx_path.stat().st_mtime + 5
    import os as _os

    _os.utime(docx_path, (new_mtime, new_mtime))

    # Either read entry point should trigger the same auto-refresh.
    docs_intel.get_paragraph(db, "00000001")
    assert docs_intel.check_staleness(db)["stale"] is False


# ---------------------------------------------------------------------------
# a62e5b4f — Word FIELD CODES (simple <w:fldSimple> + complex fldChar/instrText).
# ---------------------------------------------------------------------------


def test_parse_docx_carries_kind_and_empty_fields_on_plain_paragraphs():
    # Purely additive keys: every record now has kind + fields (empty for prose).
    paras = docs_intel.parse_docx(_synthetic_docx())
    assert [p["kind"] for p in paras] == ["heading", "paragraph", "heading", "paragraph"]
    assert all(p["fields"] == [] for p in paras)


def test_parse_docx_extracts_simple_field_toc():
    paras = docs_intel.parse_docx(_rich_docx())
    toc_para = next(p for p in paras if p["para_id"] == "10000002")
    assert len(toc_para["fields"]) == 1
    fld = toc_para["fields"][0]
    assert fld["kind"] == "field"
    assert fld["field_type"] == "TOC"
    # The full instruction string is preserved (quotes/switches intact).
    assert fld["instruction"].startswith("TOC")
    assert '"1-3"' in fld["instruction"]
    # Word regenerates a TOC — it must be flagged for refresh.
    assert fld["needs_refresh"] is True


def test_parse_docx_extracts_complex_field_seq():
    # Complex field: begin / instrText / separate / end wrapped across runs.
    paras = docs_intel.parse_docx(_rich_docx())
    seq_para = next(p for p in paras if p["para_id"] == "10000004")
    assert [f["field_type"] for f in seq_para["fields"]] == ["SEQ"]
    seq = seq_para["fields"][0]
    assert seq["instruction"] == "SEQ Figure \\* ARABIC"
    assert seq["needs_refresh"] is True
    # The visible caption text is still extracted alongside the field.
    assert "a plain caption" in seq_para["text"]


def test_parse_docx_concatenates_multi_run_instruction_pageref():
    # Word splits a long instruction across several <w:instrText> runs; they must
    # be concatenated into one instruction string.
    paras = docs_intel.parse_docx(_rich_docx())
    ref_para = next(p for p in paras if p["para_id"] == "10000005")
    assert len(ref_para["fields"]) == 1
    ref = ref_para["fields"][0]
    assert ref["field_type"] == "PAGEREF"
    assert ref["instruction"] == "PAGEREF _Ref12345 \\h"
    assert ref["needs_refresh"] is True


def test_document_outline_surfaces_fields_without_breaking_headings():
    out = docs_intel.document_outline(_rich_docx())
    # Existing headings-only contract is preserved.
    assert [h["text"] for h in out["headings"]] == ["Contents", "Figures"]
    assert out["heading_count"] == 2
    # New additive keys: fields in document order with owning para_id.
    assert out["field_count"] == 3
    assert [f["field_type"] for f in out["fields"]] == ["TOC", "SEQ", "PAGEREF"]
    assert out["fields"][0]["para_id"] == "10000002"
    assert all(f["needs_refresh"] for f in out["fields"])


# ---------------------------------------------------------------------------
# 0d1b0809 — document_content_tree: full body (paragraphs + tables) in order.
# ---------------------------------------------------------------------------


def test_document_content_tree_preserves_true_document_order():
    tree = docs_intel.document_content_tree(_rich_docx())
    kinds = [b["kind"] for b in tree["blocks"]]
    # heading, TOC-para, heading, SEQ-para, PAGEREF-para, TABLE, closing para.
    assert kinds == [
        "heading",
        "paragraph",
        "heading",
        "paragraph",
        "paragraph",
        "table",
        "paragraph",
    ]
    assert tree["table_count"] == 1
    assert tree["heading_count"] == 2
    # Paragraph count includes headings (they are paragraphs), excludes tables.
    assert tree["paragraph_count"] == 6
    # Fields are counted across the whole body.
    assert tree["field_count"] == 3


def test_document_content_tree_extracts_table_cells_in_order():
    tree = docs_intel.document_content_tree(_rich_docx())
    table = next(b for b in tree["blocks"] if b["kind"] == "table")
    assert table["row_count"] == 2
    assert table["col_count"] == 2
    assert table["rows"] == [["R1C1", "R1C2"], ["R2C1", "R2C2"]]


def test_document_content_tree_nests_by_heading_hierarchy():
    tree = docs_intel.document_content_tree(_rich_docx())
    roots = tree["tree"]
    # Two H1 roots: "Contents" and "Figures".
    assert [r["text"] for r in roots] == ["Contents", "Figures"]
    # The TOC paragraph nests under "Contents".
    contents_children = [c["para_id"] for c in roots[0]["children"]]
    assert contents_children == ["10000002"]
    # The SEQ + PAGEREF paras, the table, and the closing para nest under "Figures".
    figures_child_kinds = [c["kind"] for c in roots[1]["children"]]
    assert figures_child_kinds == ["paragraph", "paragraph", "table", "paragraph"]


def test_document_content_tree_keys_paragraphs_on_paraid():
    tree = docs_intel.document_content_tree(_rich_docx())
    para_ids = [b["para_id"] for b in tree["blocks"] if "para_id" in b]
    assert "10000006" in para_ids  # closing paragraph is addressable
    # A paragraph without w14:paraId gets a stable structural-hash id (prefix "sp").
    out = docs_intel.document_content_tree(_synthetic_docx())
    assert any(b.get("para_id", "").startswith("sp") for b in out["blocks"])


def test_document_content_tree_empty_body():
    empty = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
</w:document>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", empty)
    tree = docs_intel.document_content_tree(buf.getvalue())
    assert tree["blocks"] == [] and tree["tree"] == []
    assert tree["paragraph_count"] == 0 and tree["table_count"] == 0
    assert tree["duplicate_para_ids"] == []


# ---------------------------------------------------------------------------
# 827b6bdc — duplicate native w14:paraId detection. Word's own invariant is
# that w14:paraId is unique per paragraph, but nothing verified that before
# this item; document_content_tree must detect and REPORT a duplicate, never
# silently let two distinct paragraphs collapse onto the same id.
# ---------------------------------------------------------------------------

_DUPLICATE_PARA_ID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="6BDC5378">
      <w:r><w:t>First paragraph, original owner of 6BDC5378.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AAAA9999">
      <w:r><w:t>An unrelated, uniquely-identified paragraph in between.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="6BDC5378">
      <w:r><w:t>Second, unrelated paragraph that WRONGLY shares 6BDC5378.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _duplicate_para_id_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _DUPLICATE_PARA_ID_XML)
    return buf.getvalue()


def test_document_content_tree_reports_duplicate_native_para_id():
    """827b6bdc regression: a real-world malformed/hand-edited/merged .docx
    can carry the SAME native w14:paraId on two distinct paragraphs (the
    reference case this item names: "duplicate 6BDC5378")."""
    tree = docs_intel.document_content_tree(_duplicate_para_id_docx())
    dups = tree["duplicate_para_ids"]
    assert len(dups) == 1
    entry = dups[0]
    assert entry["para_id"] == "6BDC5378"
    assert entry["occurrence_count"] == 2
    # Enough location info to actually find the problem: body-order index +
    # a text snippet from EACH duplicate, not just "duplicate found".
    indices = [o["index"] for o in entry["occurrences"]]
    texts = [o["text"] for o in entry["occurrences"]]
    assert indices == [0, 2]
    assert "First paragraph" in texts[0]
    assert "Second, unrelated paragraph" in texts[1]
    # Both paragraphs are STILL present in blocks (never silently merged or
    # dropped) even though they share a para_id -- surfaced, not collapsed.
    dup_blocks = [b for b in tree["blocks"] if b.get("para_id") == "6BDC5378"]
    assert len(dup_blocks) == 2
    assert dup_blocks[0]["text"] != dup_blocks[1]["text"]


def test_document_content_tree_no_duplicates_reports_empty_list():
    """The overwhelmingly common case (every native id unique) reports zero
    duplicates -- purely additive, every existing caller unaffected."""
    assert docs_intel.document_content_tree(_rich_docx())["duplicate_para_ids"] == []
    assert docs_intel.document_content_tree(_synthetic_docx())["duplicate_para_ids"] == []


def test_duplicate_native_para_id_does_not_disturb_synthesized_ids():
    """A duplicated NATIVE id must not confuse the synthesized-id fallback
    path used for a paragraph that carries no native id at all."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="6BDC5378"><w:r><w:t>Dup one.</w:t></w:r></w:p>
    <w:p><w:r><w:t>No native id at all.</w:t></w:r></w:p>
    <w:p w14:paraId="6BDC5378"><w:r><w:t>Dup two.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    tree = docs_intel.document_content_tree(_make_docx_from_xml(xml))
    assert len(tree["duplicate_para_ids"]) == 1
    synth_block = next(b for b in tree["blocks"] if b["text"] == "No native id at all.")
    assert synth_block["para_id"].startswith("sp")


# ---------------------------------------------------------------------------
# caf5ee34 — stable structural-hash para_id fallback
# ---------------------------------------------------------------------------

def _make_docx_from_xml(xml: str) -> bytes:
    """Wrap raw document XML in a .docx ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_stable_synth_id_survives_insertion_elsewhere():
    """caf5ee34 core: the same logical paragraph keeps its id after an unrelated
    paragraph is inserted at a different position in the document."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )
    base_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter One</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>The target paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""

    # After insertion: an extra paragraph appears BEFORE the target.
    after_insert_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter One</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>An inserted paragraph before the target.</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>The target paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""

    paras_before = docs_intel.parse_docx(_make_docx_from_xml(base_xml))
    paras_after = docs_intel.parse_docx(_make_docx_from_xml(after_insert_xml))

    target_before = next(p for p in paras_before if p["text"] == "The target paragraph.")
    target_after = next(p for p in paras_after if p["text"] == "The target paragraph.")

    assert target_before["para_id"].startswith("sp"), (
        f"expected stable synth id before insertion, got {target_before['para_id']!r}"
    )
    assert target_before["para_id"] == target_after["para_id"], (
        f"stable synth id shifted after insertion: "
        f"{target_before['para_id']!r} -> {target_after['para_id']!r}"
    )


def test_different_paragraphs_get_different_synth_ids():
    """Two paragraphs with different text under the same heading get distinct ids."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Section A</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>First unique paragraph.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Second unique paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    paras = docs_intel.parse_docx(_make_docx_from_xml(xml))
    body_paras = [p for p in paras if p["kind"] == "paragraph"]
    assert len(body_paras) == 2
    assert body_paras[0]["para_id"].startswith("sp")
    assert body_paras[1]["para_id"].startswith("sp")
    assert body_paras[0]["para_id"] != body_paras[1]["para_id"], (
        "two distinct paragraphs should get different synthesized ids"
    )


def test_duplicate_paragraphs_get_distinct_synth_ids():
    """Two paragraphs with identical text at the same structural position
    must not collide — they get different ids via the occurrence counter."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Section</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Duplicate text.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Duplicate text.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    paras = docs_intel.parse_docx(_make_docx_from_xml(xml))
    dup_ids = [p["para_id"] for p in paras if p["text"] == "Duplicate text."]
    assert len(dup_ids) == 2
    assert dup_ids[0] != dup_ids[1], (
        "duplicate paragraphs at the same structural slot must get different synth ids"
    )


def test_native_paraid_unaffected_by_synth_logic():
    """Paragraphs that already have w14:paraId keep their real id unchanged."""
    paras = docs_intel.parse_docx(_synthetic_docx())
    # Three paragraphs have real paraIds from the fixture.
    assert paras[0]["para_id"] == "00000001"
    assert paras[1]["para_id"] == "00000002"
    assert paras[2]["para_id"] == "00000003"
    # Only the fourth (no paraId) gets a synth id.
    assert paras[3]["para_id"].startswith("sp")


def test_synth_id_deterministic_across_calls():
    """The same document parsed twice produces identical synthesized ids (no
    randomness — this would silently break cross-run references)."""
    docx = _synthetic_docx()
    paras_a = docs_intel.parse_docx(docx)
    paras_b = docs_intel.parse_docx(docx)
    ids_a = [p["para_id"] for p in paras_a]
    ids_b = [p["para_id"] for p in paras_b]
    assert ids_a == ids_b, "synthesized ids must be deterministic across runs"


# ---------------------------------------------------------------------------
# e21b2ca7 — durable document-bound synth-id addressing. caf5ee34 (above)
# fixed drift under insertion elsewhere; these cover the residual mutability
# that fix's own follow-up notes flagged: ancestry, content-edit, and
# duplicate-order sensitivity of the sp<hash> fallback.
# ---------------------------------------------------------------------------


def test_synth_id_survives_ancestor_heading_retitle():
    """Core e21b2ca7 fix: a paragraph's synth id must NOT change just because
    an ANCESTOR heading's own text was edited. Only the paragraph's own
    content and its document-order occurrence among identical text feed the
    hash now — the ancestor heading's text no longer does."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )

    def _xml(heading_text: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>{heading_text}</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>The target paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>"""

    before = docs_intel.parse_docx(_make_docx_from_xml(_xml("Chapter One")))
    after = docs_intel.parse_docx(_make_docx_from_xml(_xml("Chapter One (Revised)")))

    target_before = next(p for p in before if p["text"] == "The target paragraph.")
    target_after = next(p for p in after if p["text"] == "The target paragraph.")

    assert target_before["para_id"].startswith("sp")
    assert target_before["para_id"] == target_after["para_id"], (
        "retitling an ancestor heading must not change a descendant "
        f"paragraph's synth id: {target_before['para_id']!r} -> "
        f"{target_after['para_id']!r}"
    )


def test_synth_id_travels_with_content_not_position_on_reorder():
    """e21b2ca7 reorder coverage: two distinct (non-duplicate) paragraphs
    swapping document order each keep THEIR OWN id — ids are keyed by
    content + occurrence, not raw position, so a pure reorder of
    non-duplicate content is fully stable."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )
    original_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p><w:r><w:t>Alpha paragraph.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Beta paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    reordered_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p><w:r><w:t>Beta paragraph.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Alpha paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    before = {
        p["text"]: p["para_id"]
        for p in docs_intel.parse_docx(_make_docx_from_xml(original_xml))
    }
    after = {
        p["text"]: p["para_id"]
        for p in docs_intel.parse_docx(_make_docx_from_xml(reordered_xml))
    }

    assert before["Alpha paragraph."] == after["Alpha paragraph."]
    assert before["Beta paragraph."] == after["Beta paragraph."]


def test_synth_id_own_text_edit_is_a_documented_residual_limitation():
    """e21b2ca7: editing a paragraph's OWN text still changes its own synth
    id — an inherent property of a purely content-derived id with no
    persisted state (there is nothing else here to hash from). Explicitly
    accepted and pinned down by this test (see _build_synth_id_map's own
    docstring) rather than left as a silent, untested gap. Fully closing
    this needs a real persisted identity layer, out of scope for this
    function's stateless, single-parse contract."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )

    def _xml(text: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
  </w:body>
</w:document>"""

    before = docs_intel.parse_docx(_make_docx_from_xml(_xml("Original text.")))[0]
    after = docs_intel.parse_docx(_make_docx_from_xml(_xml("Edited text.")))[0]
    assert before["para_id"] != after["para_id"], (
        "documenting the known limitation: a same-slot paragraph's own text "
        "edit still changes its synth id today"
    )


def test_synth_id_duplicate_order_shift_is_a_documented_residual_limitation():
    """e21b2ca7: inserting a NEW earlier occurrence of duplicated text still
    shifts the occurrence index (and therefore the id) of every LATER
    paragraph sharing that exact text — explicitly documented and tested
    known limitation, not a silent gap. Fully closing this also needs a real
    persisted identity layer (see _build_synth_id_map's docstring)."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )
    before_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p><w:r><w:t>Repeated text.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    after_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p><w:r><w:t>Repeated text.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Repeated text.</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    before_ids = [
        p["para_id"] for p in docs_intel.parse_docx(_make_docx_from_xml(before_xml))
    ]
    after_ids = [
        p["para_id"] for p in docs_intel.parse_docx(_make_docx_from_xml(after_xml))
    ]

    # The FIRST occurrence keeps occurrence-index 0 both times...
    assert before_ids[0] == after_ids[0]
    # ...but a caller who resolves an id against a document that has since
    # gained an EARLIER duplicate must be prepared for later occurrences to
    # shift — this is the known, documented, tested residual gap.
    assert len(after_ids) == 2
    assert after_ids[0] != after_ids[1]


# ---------------------------------------------------------------------------
# 57336a87 — perf micro-benchmark: a few hundred paragraphs must parse well
# under a real-time threshold. We MEASURE, not assume.
# ---------------------------------------------------------------------------


def _repeated_docx(n_paragraphs: int) -> bytes:
    """A realistic-size synthetic docx: n body paragraphs, some headings, some
    fields, plus a couple of tables — the mix a real chapter contains."""
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:body>',
    ]
    for i in range(n_paragraphs):
        pid = f"{i:08d}"
        if i % 25 == 0:
            parts.append(
                f'<w:p w14:paraId="{pid}"><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
                f"<w:r><w:t>Section {i}</w:t></w:r></w:p>"
            )
        elif i % 10 == 0:
            parts.append(
                f'<w:p w14:paraId="{pid}"><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                f"<w:r><w:instrText> SEQ Figure \\* ARABIC </w:instrText></w:r>"
                f'<w:r><w:fldChar w:fldCharType="end"/></w:r>'
                f"<w:r><w:t>Figure caption {i}</w:t></w:r></w:p>"
            )
        else:
            parts.append(
                f'<w:p w14:paraId="{pid}"><w:r><w:t>Body paragraph number {i} with '
                f"some representative prose content.</w:t></w:r></w:p>"
            )
    parts.append("</w:body></w:document>")
    xml = "".join(parts)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_parse_docx_perf_benchmark_under_realtime_threshold():
    """Deterministic, dependency-free parse of a few-hundred-paragraph docx must
    finish well under 1s. Measured, not assumed. Threshold is generous (1.0s) so
    the assertion is stable on slow/loaded CI while still catching an accidental
    quadratic-blowup regression."""
    n = 400
    docx = _repeated_docx(n)

    start = time.perf_counter()
    paras = docs_intel.parse_docx(docx)
    parse_elapsed = time.perf_counter() - start

    assert len(paras) == n
    # Fields were extracted (every 10th para carries a SEQ field, minus overlap
    # with the every-25th headings).
    assert sum(len(p["fields"]) for p in paras) > 0
    assert parse_elapsed < 1.0, f"parse of {n} paragraphs took {parse_elapsed:.3f}s"

    # The full ordered content tree over the same doc is also comfortably fast.
    start = time.perf_counter()
    tree = docs_intel.document_content_tree(docx)
    tree_elapsed = time.perf_counter() - start
    assert tree["paragraph_count"] == n
    assert tree_elapsed < 1.0, (
        f"content-tree of {n} paragraphs took {tree_elapsed:.3f}s"
    )


# ---------------------------------------------------------------------------
# 7a98286b — check_document_structure_issues: read-only structural linter
# consuming document_content_tree's existing output. Verified separately
# against the real staging document (Meridian sprint item evidence); these
# are the synthetic-fixture unit tests for each of the 5 check classes.
# ---------------------------------------------------------------------------

_LINT_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="20000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Untitled Report</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>[&#167;1.5] Later Section</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000003">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>[&#167;1.2] Earlier Section, Out Of Order</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000004">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>[&#167;2.3.4] Deep Subsection Styled As Heading1</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000005">
      <w:r><w:t></w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000006">
      <w:r><w:t>Figure 1: first image, correctly captioned below it.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000007">
      <w:r><w:t>Some non-blank paragraph right before caption two.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000008">
      <w:r><w:t>Figure 2: second image, caption precedes the image slot.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000009">
      <w:r><w:t></w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000010">
      <w:r><w:t>Figure 1: duplicate label reused by mistake.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000011">
      <w:r><w:t>As defined in Section 9.9, the term is used throughout.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="20000012">
      <w:r><w:t>See Section 1.5 for the full derivation.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _lint_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _LINT_DOCUMENT_XML)
    return buf.getvalue()


def _issues_of_type(result, type_name):
    return [i for i in result["issues"] if i["type"] == type_name]


def test_check_document_structure_issues_flags_heading_order_vs_tag():
    result = docs_intel.check_document_structure_issues(_lint_docx())
    order_issues = _issues_of_type(result, "heading_order_vs_tag")
    assert len(order_issues) == 1
    assert order_issues[0]["tag"] == "1.2"
    assert order_issues[0]["previous_tag"] == "1.5"
    assert order_issues[0]["para_id"] == "20000003"


def test_check_document_structure_issues_flags_heading_depth_mismatch():
    result = docs_intel.check_document_structure_issues(_lint_docx())
    depth_issues = _issues_of_type(result, "heading_depth_mismatch")
    assert len(depth_issues) == 1
    assert depth_issues[0]["tag"] == "2.3.4"
    assert depth_issues[0]["style_level"] == 1
    assert depth_issues[0]["implied_depth"] == 3
    assert depth_issues[0]["para_id"] == "20000004"


def test_check_document_structure_issues_flags_caption_before_image():
    result = docs_intel.check_document_structure_issues(_lint_docx())
    cbi_issues = _issues_of_type(result, "caption_before_image")
    assert len(cbi_issues) == 1
    assert cbi_issues[0]["label"] == "Figure 2"
    assert cbi_issues[0]["para_id"] == "20000008"
    # Figure 1 has blank-before/blank-after spacing (routine), not an
    # inversion, so it must NOT be flagged.
    assert all(i["label"] != "Figure 1" for i in cbi_issues)


def test_check_document_structure_issues_flags_duplicate_labels():
    result = docs_intel.check_document_structure_issues(_lint_docx())
    dup_issues = _issues_of_type(result, "duplicate_label")
    assert len(dup_issues) == 1
    assert dup_issues[0]["label"] == "Figure 1"
    assert len(dup_issues[0]["occurrences"]) == 2
    assert {o["para_id"] for o in dup_issues[0]["occurrences"]} == {"20000006", "20000010"}


def test_check_document_structure_issues_flags_dangling_cross_reference():
    result = docs_intel.check_document_structure_issues(_lint_docx())
    xref_issues = _issues_of_type(result, "dangling_cross_reference")
    assert len(xref_issues) == 1
    assert xref_issues[0]["referenced_tag"] == "9.9"
    assert xref_issues[0]["para_id"] == "20000011"
    # A reference to a tag that DOES exist (1.5) must not be flagged.
    assert all(i["referenced_tag"] != "1.5" for i in xref_issues)


def test_check_document_structure_issues_untagged_headings_excluded():
    """The document title (no [§...] tag) must not participate in any check
    -- no crash, no spurious flag, no false 'dangling reference' target."""
    result = docs_intel.check_document_structure_issues(_lint_docx())
    assert all(i.get("tag") != "" for i in result["issues"] if "tag" in i)
    # Sanity: the title's own para_id never appears as a flagged heading.
    tagged_flags = [i for i in result["issues"] if "tag" in i]
    assert all(i["para_id"] != "20000001" for i in tagged_flags)


def test_check_document_structure_issues_multi_tag_heading_all_tags_resolve_refs():
    """A multi-tag heading like '[§5.1.1 + §5.1.2]' must register BOTH tags as
    known section numbers, so a cross-reference to either resolves cleanly."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="30000001">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>[&#167;5.1.1 + &#167;5.1.2] Merged Heading</w:t></w:r>
    </w:p>
    <w:p w14:paraId="30000002">
      <w:r><w:t>See Section 5.1.2 for the secondary metric.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    result = docs_intel.check_document_structure_issues(buf.getvalue())
    assert _issues_of_type(result, "dangling_cross_reference") == []


def test_check_document_structure_issues_clean_document_zero_issues():
    """A well-formed, correctly-ordered document must report zero issues --
    the check must not fire on documents with nothing wrong."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="40000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>[&#167;1] Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="40000002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>[&#167;1.1] Background</w:t></w:r>
    </w:p>
    <w:p w14:paraId="40000003">
      <w:r><w:t>Ordinary body text with no cross-references.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    result = docs_intel.check_document_structure_issues(buf.getvalue())
    assert result == {"issue_count": 0, "issues": []}


def test_check_document_structure_issues_appendix_letter_tag_extracted():
    """Regression: an appendix tag like 'C.5' is the letter as its OWN
    dot-separated component (not fused to the first digit as 'C5') -- an
    earlier version of the extraction regex silently dropped every
    letter-prefixed tag, which meant NEITHER the C.2.5-stranded-mid-chapter
    NOR the C.5-before-C.1 real ordering issues in the actual staging
    document were ever caught. This pins that the extraction actually finds
    appendix tags and orders them correctly against numeric chapter tags."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="50000001">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>[&#167;4.2.2] Chapter Four Subsection</w:t></w:r>
    </w:p>
    <w:p w14:paraId="50000002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>[&#167;C.2.5] Appendix Entry Stranded Here</w:t></w:r>
    </w:p>
    <w:p w14:paraId="50000003">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>[&#167;4.2.3] Chapter Four Continues</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    result = docs_intel.check_document_structure_issues(buf.getvalue())
    order_issues = _issues_of_type(result, "heading_order_vs_tag")
    assert len(order_issues) == 1
    assert order_issues[0]["tag"] == "4.2.3"
    assert order_issues[0]["previous_tag"] == "C.2.5"


# ---------------------------------------------------------------------------
# 4a07e566 — section-type differentiation + w:sectPr page-numbering awareness
# ---------------------------------------------------------------------------

# A synthetic document that exercises all five section types:
#   Abstract -> TOC -> LOF -> main body -> References -> Appendix
_SECTION_TYPED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="60000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Abstract</w:t></w:r>
    </w:p>
    <w:p w14:paraId="60000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Table of Contents</w:t></w:r>
    </w:p>
    <w:p w14:paraId="60000003">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>List of Figures</w:t></w:r>
    </w:p>
    <w:p w14:paraId="60000004">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="60000005">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Background</w:t></w:r>
    </w:p>
    <w:p w14:paraId="60000006">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>References</w:t></w:r>
    </w:p>
    <w:p w14:paraId="60000007">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Appendix A</w:t></w:r>
    </w:p>
    <w:p w14:paraId="60000008">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Supporting Data</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _section_typed_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _SECTION_TYPED_XML)
    return buf.getvalue()


# A synthetic document with w:sectPr elements covering:
#   - a mid-document sectPr (roman numeral front matter, restart at 1)
#   - the final body-level sectPr (arabic, no restart)
_SECTPR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="70000001">
      <w:pPr>
        <w:pStyle w:val="Heading1"/>
        <w:sectPr>
          <w:pgNumType w:fmt="lowerRoman" w:start="1"/>
          <w:type w:val="nextPage"/>
        </w:sectPr>
      </w:pPr>
      <w:r><w:t>Front Matter</w:t></w:r>
    </w:p>
    <w:p w14:paraId="70000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter 1</w:t></w:r>
    </w:p>
    <w:sectPr>
      <w:pgNumType w:fmt="decimal" w:start="1"/>
    </w:sectPr>
  </w:body>
</w:document>
"""


def _sectpr_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _SECTPR_XML)
    return buf.getvalue()


def test_section_type_classification_abstract_toc_lof():
    out = docs_intel.document_outline(_section_typed_docx())
    headings = out["headings"]
    assert headings[0]["text"] == "Abstract"
    assert headings[0]["section_type"] == "abstract"
    assert headings[1]["text"] == "Table of Contents"
    assert headings[1]["section_type"] == "toc"
    assert headings[2]["text"] == "List of Figures"
    assert headings[2]["section_type"] == "lof"


def test_section_type_classification_main_body():
    out = docs_intel.document_outline(_section_typed_docx())
    headings = out["headings"]
    # "Introduction" is the first unclassified level-1 heading -> main
    intro = next(h for h in headings if h["text"] == "Introduction")
    assert intro["section_type"] == "main"
    # "Background" is a sub-heading under Introduction -> inherits main
    bg = next(h for h in headings if h["text"] == "Background")
    assert bg["section_type"] == "main"


def test_section_type_classification_references_and_appendix():
    out = docs_intel.document_outline(_section_typed_docx())
    headings = out["headings"]
    # "References" transitions to back matter -> classified as appendix
    refs = next(h for h in headings if h["text"] == "References")
    assert refs["section_type"] == "appendix"
    # "Appendix A" is an explicit appendix heading
    app = next(h for h in headings if h["text"] == "Appendix A")
    assert app["section_type"] == "appendix"
    # Sub-heading under Appendix A inherits appendix
    sub = next(h for h in headings if h["text"] == "Supporting Data")
    assert sub["section_type"] == "appendix"


def test_section_regions_ordered_distinct():
    out = docs_intel.document_outline(_section_typed_docx())
    # Distinct regions in document order: abstract, toc, lof, main, appendix
    assert out["section_regions"] == ["abstract", "toc", "lof", "main", "appendix"]


def test_document_outline_backward_compatible_plain_doc():
    # The original document still works — section_type is additive, not breaking.
    out = docs_intel.document_outline(_synthetic_docx())
    assert out["heading_count"] == 2
    assert "section_type" in out["headings"][0]
    assert "section_regions" in out
    # Both headings are unclassified level-1/2 with no front-matter markers -> main
    assert all(h["section_type"] == "main" for h in out["headings"])


def test_parse_sectpr_detects_multi_section_page_numbering():
    result = docs_intel.parse_sectpr(_sectpr_docx())
    assert result["section_count"] == 2
    sections = result["sections"]
    # First sectPr: front matter with roman numerals, restart at 1
    s0 = sections[0]
    assert s0["page_num_fmt"] == "lowerRoman"
    assert s0["page_num_start"] == 1
    assert s0["is_continuous"] is False
    assert s0["anchor_para_id"] == "70000001"
    # Second sectPr: body-level, arabic decimal, restart at 1
    s1 = sections[1]
    assert s1["page_num_fmt"] == "decimal"
    assert s1["page_num_start"] == 1
    assert s1["anchor_para_id"] is None  # body-level, no anchor paragraph


def test_parse_sectpr_no_sections_returns_empty():
    # A simple document with no w:sectPr should return section_count=0
    result = docs_intel.parse_sectpr(_synthetic_docx())
    assert result["section_count"] == 0
    assert result["sections"] == []


def test_get_document_section_map_combines_outline_and_sectpr():
    result = docs_intel.get_document_section_map(_sectpr_docx())
    assert "headings" in result
    assert "section_regions" in result
    assert "sectpr" in result
    assert result["sectpr"]["section_count"] == 2
    # Both headings in _sectpr_docx are in "main" (no front-matter pattern)
    assert all(h["section_type"] == "main" for h in result["headings"])


def test_index_docx_structure_stores_section_type(tmp_path):
    db = str(tmp_path / "struct.idx.sqlite")
    # index_docx_structure goes through document_content_tree, not parse_docx,
    # so we need a docx that the content tree can parse (same format).
    docs_intel.index_docx_structure(_section_typed_docx(), db)
    elements = docs_intel.get_local_structure_elements(db)
    headings = elements["headings"]
    assert len(headings) > 0
    assert all("section_type" in h for h in headings)
    abstract_headings = [h for h in headings if h["section_type"] == "abstract"]
    main_headings = [h for h in headings if h["section_type"] == "main"]
    assert len(abstract_headings) >= 1
    assert len(main_headings) >= 1


def test_assign_section_types_empty_input():
    # Edge case: empty heading list returns empty list without crash.
    result = docs_intel._assign_section_types([])
    assert result == []


def test_classify_heading_text_known_patterns():
    assert docs_intel._classify_heading_text("Abstract") == "abstract"
    assert docs_intel._classify_heading_text("ABSTRACT") == "abstract"
    assert docs_intel._classify_heading_text("Table of Contents") == "toc"
    assert docs_intel._classify_heading_text("Contents") == "toc"
    assert docs_intel._classify_heading_text("List of Figures") == "lof"
    assert docs_intel._classify_heading_text("List of Tables") == "lof"
    assert docs_intel._classify_heading_text("Appendix A") == "appendix"
    assert docs_intel._classify_heading_text("Annex B") == "appendix"
    assert docs_intel._classify_heading_text("Introduction") is None
    assert docs_intel._classify_heading_text("Methodology") is None


# ---------------------------------------------------------------------------
# 32d84131 — SQLite FTS5 external-content table + BM25 search
# Decision: meridian-docs uses SQLite FTS5 (not Tantivy) for full-text
# search over paragraph text. FTS5 is built into Python's sqlite3 module
# with zero extra dependencies; Tantivy (used by meridian-outputs) requires
# a compiled Rust extension unsuitable for a stdlib-only uvx extension.
# ---------------------------------------------------------------------------


def test_fts5_search_basic_match(tmp_path):
    """fts5_search_paragraphs returns the correct paragraph on a keyword hit."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)

    hits = docs_intel.fts5_search_paragraphs(db, "sessions")
    assert len(hits) == 1
    assert hits[0]["para_id"] == "00000002"
    assert "bm25_score" in hits[0]
    # BM25 scores are negative in SQLite FTS5 (lower = more relevant).
    assert isinstance(hits[0]["bm25_score"], float)


def test_fts5_search_no_match_returns_empty(tmp_path):
    """fts5_search_paragraphs returns an empty list when no paragraphs match."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)

    hits = docs_intel.fts5_search_paragraphs(db, "xyzzy_nonexistent_token_42")
    assert hits == []


def test_fts5_search_phrase_query(tmp_path):
    """Phrase queries (quoted tokens) match only exact sequences."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)

    # Exact phrase present in the doc.
    hits = docs_intel.fts5_search_paragraphs(db, '"AI sessions"')
    assert len(hits) == 1
    assert hits[0]["para_id"] == "00000002"

    # Reversed order — NOT a match for phrase search.
    hits_rev = docs_intel.fts5_search_paragraphs(db, '"sessions AI"')
    assert hits_rev == []


def test_fts5_search_heading_found(tmp_path):
    """Heading paragraphs are indexed and findable by FTS5."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)

    hits = docs_intel.fts5_search_paragraphs(db, "Introduction")
    assert any(h["para_id"] == "00000001" for h in hits)
    assert any(h["style"] is not None for h in hits)


def test_fts5_search_limit_respected(tmp_path):
    """limit parameter caps the number of results."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)

    # The doc has 4 paragraphs; asking for any token that could match many rows
    # -- use a wildcard so all text rows are candidate hits.
    hits = docs_intel.fts5_search_paragraphs(db, "a*", limit=1)
    assert len(hits) <= 1


def test_fts5_index_rebuilt_on_reindex(tmp_path):
    """After index_docx is called a second time the FTS5 index reflects the new content."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)

    # Verify first index is searchable.
    assert len(docs_intel.fts5_search_paragraphs(db, "sessions")) == 1

    # Re-index (idempotent rebuild).
    docs_intel.index_docx(_synthetic_docx(), db)

    # FTS5 still returns correct results after rebuild.
    hits = docs_intel.fts5_search_paragraphs(db, "sessions")
    assert len(hits) == 1
    assert hits[0]["para_id"] == "00000002"


def test_fts5_search_invalid_query_returns_empty_not_raises(tmp_path):
    """Syntactically invalid FTS5 queries return empty list, not an exception."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx(_synthetic_docx(), db)

    # An unclosed quote is a syntax error in FTS5.
    hits = docs_intel.fts5_search_paragraphs(db, '"unclosed phrase')
    assert isinstance(hits, list)


# ---------------------------------------------------------------------------
# 25743ec6 — chunk-level, heading-aware, weighted BM25 indexing
# ---------------------------------------------------------------------------
#
# Synthetic document used for chunk tests:
#
#   H1: Introduction            (00000001)
#     body: "Meridian..."       (00000002)
#     H2: Design                (00000003)
#       body: "no paraId"       (p3)
#
# Expected chunks:
#   chunk 0: heading="Introduction", path=["Introduction"],
#            body="Meridian coordinates AI sessions."
#   chunk 1: heading="Design",       path=["Introduction", "Design"],
#            body="A paragraph with no paraId."
# ---------------------------------------------------------------------------


def test_build_chunks_correct_boundaries(tmp_path):
    """_build_chunks produces one chunk per heading; body accumulates until the
    next heading of equal or higher level."""
    tree = docs_intel.document_content_tree(_synthetic_docx())
    chunks = docs_intel._build_chunks(tree["blocks"])

    assert len(chunks) == 2

    c0 = chunks[0]
    assert c0["chunk_id"] == 0
    assert c0["heading_text"] == "Introduction"
    assert c0["heading_path"] == ["Introduction"]
    assert "Meridian" in c0["body_text"]
    assert "AI sessions" in c0["body_text"]
    assert c0["heading_para_id"] == "00000001"

    c1 = chunks[1]
    assert c1["chunk_id"] == 1
    assert c1["heading_text"] == "Design"
    # Heading path traces full ancestry: H1 then H2.
    assert c1["heading_path"] == ["Introduction", "Design"]
    assert "paragraph with no paraId" in c1["body_text"]
    assert c1["heading_para_id"] == "00000003"


def test_build_chunks_nested_headings_heading_path():
    """Heading path correctly reflects multi-level nesting."""
    # Build a doc: H1 "A" -> H2 "B" -> H3 "C" (level numbers 1, 2, 3)
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="A0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Alpha</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Beta</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000003">
      <w:pPr><w:pStyle w:val="Heading3"/></w:pPr>
      <w:r><w:t>Gamma</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000004"><w:r><w:t>body under Gamma</w:t></w:r></w:p>
    <w:p w14:paraId="A0000005">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Delta</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A0000006"><w:r><w:t>body under Delta</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    docx = buf.getvalue()

    tree = docs_intel.document_content_tree(docx)
    chunks = docs_intel._build_chunks(tree["blocks"])

    assert len(chunks) == 4  # Alpha, Beta, Gamma, Delta

    alpha = chunks[0]
    assert alpha["heading_text"] == "Alpha"
    assert alpha["heading_path"] == ["Alpha"]
    assert alpha["body_text"] == ""  # no body paragraphs under Alpha before Beta

    beta = chunks[1]
    assert beta["heading_text"] == "Beta"
    assert beta["heading_path"] == ["Alpha", "Beta"]

    gamma = chunks[2]
    assert gamma["heading_text"] == "Gamma"
    assert gamma["heading_path"] == ["Alpha", "Beta", "Gamma"]
    assert "body under Gamma" in gamma["body_text"]

    # Delta is H2 — resets Gamma (H3) and Beta (H2) off the stack;
    # only Alpha (H1) remains as ancestor.
    delta = chunks[3]
    assert delta["heading_text"] == "Delta"
    assert delta["heading_path"] == ["Alpha", "Delta"]
    assert "body under Delta" in delta["body_text"]


def test_build_chunks_equal_level_heading_resets_path():
    """Two sibling H1 headings each produce an independent chunk; the second
    H1 does NOT include the first H1 in its heading_path."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="B0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter One</w:t></w:r>
    </w:p>
    <w:p w14:paraId="B0000002"><w:r><w:t>body one</w:t></w:r></w:p>
    <w:p w14:paraId="B0000003">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter Two</w:t></w:r>
    </w:p>
    <w:p w14:paraId="B0000004"><w:r><w:t>body two</w:t></w:r></w:p>
  </w:body>
</w:document>"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    docx = buf.getvalue()

    tree = docs_intel.document_content_tree(docx)
    chunks = docs_intel._build_chunks(tree["blocks"])

    assert len(chunks) == 2
    assert chunks[0]["heading_path"] == ["Chapter One"]
    assert chunks[1]["heading_path"] == ["Chapter Two"]


def test_index_docx_chunks_and_search_basic(tmp_path):
    """index_docx_chunks populates docx_chunks; fts5_search_chunks returns hits."""
    db = str(tmp_path / "doc.idx.sqlite")
    summary = docs_intel.index_docx_chunks(_synthetic_docx(), db)

    assert summary["chunk_count"] == 2

    # "sessions" appears in chunk 0's body (the Introduction chunk).
    hits = docs_intel.fts5_search_chunks(db, "sessions")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["heading_text"] == "Introduction"
    assert hit["heading_path"] == ["Introduction"]
    assert "bm25_score" in hit
    assert isinstance(hit["bm25_score"], float)


def test_fts5_search_chunks_heading_match_outranks_body_match(tmp_path):
    """A term that appears only in a heading ranks above the same term in body
    text only, when the BM25 heading weight exceeds the body weight."""
    # Doc: H1 "Design" with body "some text", H1 "Intro" with body "Design concepts"
    # Searching "design" should rank the heading-bearing chunk higher.
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="C0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Design</w:t></w:r>
    </w:p>
    <w:p w14:paraId="C0000002"><w:r><w:t>some text here</w:t></w:r></w:p>
    <w:p w14:paraId="C0000003">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="C0000004">
      <w:r><w:t>Design concepts are explained here.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    docx = buf.getvalue()

    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(docx, db)

    hits = docs_intel.fts5_search_chunks(db, "Design")
    assert len(hits) == 2
    # BM25 scores are negative; lower = more relevant. The heading match should
    # score lower (more relevant) than the body-only match.
    heading_chunk = next(h for h in hits if h["heading_text"] == "Design")
    body_chunk = next(h for h in hits if h["heading_text"] == "Introduction")
    assert heading_chunk["bm25_score"] < body_chunk["bm25_score"], (
        f"Heading chunk score {heading_chunk['bm25_score']!r} should be lower "
        f"(more relevant) than body chunk score {body_chunk['bm25_score']!r}"
    )


def test_index_docx_chunks_idempotent(tmp_path):
    """Re-indexing the same document leaves the chunk count unchanged."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_synthetic_docx(), db)
    summary2 = docs_intel.index_docx_chunks(_synthetic_docx(), db)
    assert summary2["chunk_count"] == 2
    hits = docs_intel.fts5_search_chunks(db, "sessions")
    assert len(hits) == 1


def test_fts5_search_chunks_invalid_query_returns_empty(tmp_path):
    """Invalid FTS5 query degrades to empty list, no exception raised."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_synthetic_docx(), db)
    hits = docs_intel.fts5_search_chunks(db, '"unclosed')
    assert isinstance(hits, list)


def test_fts5_search_chunks_no_match_returns_empty(tmp_path):
    """Query with no matching chunks returns empty list."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_synthetic_docx(), db)
    hits = docs_intel.fts5_search_chunks(db, "xyzzy_no_such_token_99")
    assert hits == []


def test_chunk_sync_triggers_after_manual_insert(tmp_path):
    """Sync triggers keep docx_chunks_fts consistent after a direct row insert
    to docx_chunks (mirrors what the AFTER INSERT trigger should handle)."""
    import sqlite3

    db = str(tmp_path / "doc.idx.sqlite")
    # Bootstrap the schema (no chunks yet).
    docs_intel.index_docx_chunks(_synthetic_docx(), db)

    conn = sqlite3.connect(db)
    try:
        # Manually insert a new chunk — the AFTER INSERT trigger should
        # populate docx_chunks_fts automatically.
        import json
        conn.execute(
            "INSERT INTO docx_chunks (chunk_id, heading_path_json, heading_text, "
            "body_text, start_para_id, end_para_id) VALUES (?, ?, ?, ?, ?, ?)",
            (99, json.dumps(["Extra"]), "Extra", "trigger test content",
             "E0000001", "E0000001"),
        )
        conn.commit()
    finally:
        conn.close()

    # The new chunk should be findable via FTS5.
    hits = docs_intel.fts5_search_chunks(db, "trigger")
    assert any(h["chunk_id"] == 99 for h in hits), (
        "AFTER INSERT trigger did not sync new chunk into docx_chunks_fts"
    )


def test_chunk_weight_constants_documented():
    """Module-level weight constants match expected 5:1 ratio."""
    assert docs_intel._CHUNK_WEIGHT_HEADING == 5.0
    assert docs_intel._CHUNK_WEIGHT_BODY == 1.0


def test_fts5_search_chunks_result_shape(tmp_path):
    """Every result dict carries all expected keys."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_synthetic_docx(), db)
    hits = docs_intel.fts5_search_chunks(db, "Introduction")
    assert len(hits) >= 1
    required_keys = {
        "chunk_id", "heading_path", "heading_text",
        "body_text", "start_para_id", "end_para_id", "bm25_score",
    }
    for hit in hits:
        assert required_keys.issubset(hit.keys()), (
            f"Missing keys: {required_keys - hit.keys()}"
        )
        assert isinstance(hit["heading_path"], list)
        assert isinstance(hit["bm25_score"], float)
