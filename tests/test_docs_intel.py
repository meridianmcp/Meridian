"""Coverage for the OOXML-Graph DOCX intelligence layer, Phase 1 (618adf32).

Builds a synthetic in-memory .docx (a ZIP with a single word/document.xml) so
the parser + sidecar-SQLite index are tested without any third-party dependency.
"""
from __future__ import annotations

import io
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
