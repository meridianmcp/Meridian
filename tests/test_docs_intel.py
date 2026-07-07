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
    assert [p["para_id"] for p in paras] == ["00000001", "00000002", "00000003", "p3"]
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
    # A paragraph without w14:paraId gets a synthesized p{index} fallback.
    out = docs_intel.document_content_tree(_synthetic_docx())
    assert any(b.get("para_id", "").startswith("p") for b in out["blocks"])


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
