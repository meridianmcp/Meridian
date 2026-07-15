"""db42acce — tunnel-routed structural doc-store ingest for figures/tables/equations.

Tests that the full path works end-to-end:
  local .docx → parse blocks via document_content_tree → forward to hosted
  ingest_document_structure MCP tool → store in doc-structure store →
  find_similar_figure returns a REAL non-null document_id.

This is the explicit completion bar from the sprint item: after
ingest_document_structure is called with a real docx's blocks, find_similar_figure
must return document_id != None (the bug it fixes).
"""
from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest

from meridian import doc_store
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Synthetic .docx fixture with figures and tables
# ---------------------------------------------------------------------------

_STRUCTURAL_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="0000D001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Experimental Setup</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Parameter</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Temperature</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>25 C</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p>
      <w:fldSimple w:instr="SEQ Table \\* ARABIC">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t>: Experimental conditions summary</w:t></w:r>
    </w:p>
    <w:p>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t>: Photograph of the apparatus</w:t></w:r>
    </w:p>
    <w:p w14:paraId="0000D002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _seed_store_via_env(tmp_path, monkeypatch) -> str:
    sidecar = str(tmp_path / "doc_structure.db")
    monkeypatch.setenv("MERIDIAN_DOC_STORE_URL", sidecar)
    doc_store._reset_doc_store_cache()
    return sidecar


# ---------------------------------------------------------------------------
# Unit: elements_from_docx_content_tree produces figures from SEQ fields
# ---------------------------------------------------------------------------

def test_elements_from_docx_content_tree_extracts_figure_and_table():
    """Confirms the server-side mapper used by ingest_document_structure
    correctly identifies figure and table elements from SEQ-field captions."""
    from docparse.docs_intel import document_content_tree

    data = _zip_docx(_STRUCTURAL_DOCUMENT_XML)
    tree = document_content_tree(data)
    elements = doc_store.elements_from_docx_content_tree(tree)

    kinds = [e["kind"] for e in elements]
    # Heading1, table, figure, Heading2
    assert "heading" in kinds
    assert "figure" in kinds
    assert "table" in kinds

    fig = next(e for e in elements if e["kind"] == "figure")
    assert "apparatus" in (fig.get("ref") or "").lower() or "figure" in (fig.get("ref") or "").lower()


# ---------------------------------------------------------------------------
# Integration: ingest_document_structure MCP tool → find_similar_figure
# ---------------------------------------------------------------------------

def test_ingest_document_structure_enables_find_similar_figure(tmp_path, monkeypatch):
    """THE KEY TEST (db42acce completion bar):

    After calling ingest_document_structure with real structural blocks from a
    local .docx, find_similar_figure must return a REAL non-null document_id.

    Before this fix, find_similar_figure always returned document_id=null after
    a content= ingest because the structural doc-store was never populated.
    """
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "struct-proj")
            pid = proj["id"]
            source = "/local/path/to/chapter.docx"

            # Parse blocks from our synthetic .docx (mirroring what
            # ingest_local_document_structure does on the tunnel-local side).
            from docparse.docs_intel import document_content_tree
            data = _zip_docx(_STRUCTURAL_DOCUMENT_XML)
            tree = document_content_tree(data)
            blocks = tree.get("blocks") or []
            assert blocks, "test fixture must have body blocks"

            # Call the new ingest_document_structure MCP tool (server-side).
            ingest_res = await mh._dispatch_mcp_tool(
                "ingest_document_structure",
                {
                    "project_id": pid,
                    "source": source,
                    "blocks": json.dumps(blocks),
                    "doc_type": "docx",
                    "title": "Chapter 1",
                },
                db, str(tmp_path),
            )
            assert "error" not in ingest_res, f"ingest_document_structure failed: {ingest_res}"
            stored_doc_id = ingest_res["document_id"]
            assert stored_doc_id is not None, "document_id must not be None after structural ingest"
            assert ingest_res["element_count"] > 0, "must have stored at least one element"
            assert ingest_res["source"] == source

            # Now index a figure against the stored document (using the same source).
            fig_res = await mh._dispatch_mcp_tool(
                "index_figure",
                {
                    "project_id": pid,
                    "doc": source,
                    "caption": "Photograph of the apparatus",
                    "semantic_label": "apparatus photo",
                },
                db, str(tmp_path),
            )
            assert "error" not in fig_res, f"index_figure failed: {fig_res}"
            assert fig_res["document_id"] == stored_doc_id, (
                f"index_figure document_id {fig_res['document_id']!r} must match "
                f"the stored doc_id {stored_doc_id!r}"
            )

            # THE COMPLETION BAR: find_similar_figure must return a non-null document_id.
            find_res = await mh._dispatch_mcp_tool(
                "find_similar_figure",
                {
                    "project_id": pid,
                    "doc": source,
                    "description_or_path": "apparatus photograph",
                },
                db, str(tmp_path),
            )
            assert "error" not in find_res, f"find_similar_figure failed: {find_res}"
            assert find_res["document_id"] is not None, (
                "find_similar_figure returned document_id=None — the structural "
                "doc-store was not populated (db42acce root cause bug)"
            )
            assert find_res["document_id"] == stored_doc_id, (
                f"find_similar_figure document_id {find_res['document_id']!r} must "
                f"match the stored doc_id {stored_doc_id!r}"
            )
            assert len(find_res["matches"]) > 0, "must return at least one figure match"
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_ingest_document_structure_upserts_on_same_source(tmp_path, monkeypatch):
    """Re-calling ingest_document_structure with the SAME source upserts
    (stable document_id, refreshed elements) rather than creating a second row."""
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "upsert-proj")
            pid = proj["id"]
            source = "/local/path/upsert_test.docx"

            from docparse.docs_intel import document_content_tree
            data = _zip_docx(_STRUCTURAL_DOCUMENT_XML)
            tree = document_content_tree(data)
            blocks = tree.get("blocks") or []

            first = await mh._dispatch_mcp_tool(
                "ingest_document_structure",
                {"project_id": pid, "source": source, "blocks": json.dumps(blocks)},
                db, str(tmp_path),
            )
            assert "error" not in first
            first_doc_id = first["document_id"]

            second = await mh._dispatch_mcp_tool(
                "ingest_document_structure",
                {"project_id": pid, "source": source, "blocks": json.dumps(blocks)},
                db, str(tmp_path),
            )
            assert "error" not in second
            # Upsert preserves the same document_id.
            assert second["document_id"] == first_doc_id, (
                "upsert must reuse the existing document_id, not create a new one"
            )
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


def test_ingest_document_structure_requires_source_and_blocks(tmp_path, monkeypatch):
    """Validation: missing source or blocks both return a clear error."""
    async def _run():
        from meridian import server as mh

        _seed_store_via_env(tmp_path, monkeypatch)
        db = await db_module.init_db(":memory:")
        try:
            proj = await db_module.create_project(db, "val-proj")
            pid = proj["id"]

            # Missing source
            res = await mh._dispatch_mcp_tool(
                "ingest_document_structure",
                {"project_id": pid, "blocks": "[]"},
                db, str(tmp_path),
            )
            assert "error" in res

            # Missing blocks
            res2 = await mh._dispatch_mcp_tool(
                "ingest_document_structure",
                {"project_id": pid, "source": "test.docx"},
                db, str(tmp_path),
            )
            assert "error" in res2

            # Missing project_id
            res3 = await mh._dispatch_mcp_tool(
                "ingest_document_structure",
                {"source": "test.docx", "blocks": "[]"},
                db, str(tmp_path),
            )
            assert "error" in res3
        finally:
            await db.close()
            await doc_store.close_all_doc_stores()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Unit: local_ingest.ingest_local_document_structure (tunnel-local side)
# ---------------------------------------------------------------------------

def test_ingest_local_document_structure_parses_docx_and_calls_hosted(tmp_path, monkeypatch):
    """Verifies the tunnel-local function with explicit force_hosted=True (f8c7ffdc):
    1. Reads the .docx via document_content_tree.
    2. Calls call_hosted_ingest_structure with the blocks.
    Mocks out the HTTP call to avoid needing a real server.

    Note (f8c7ffdc): force_hosted=True is now REQUIRED to use the hosted path;
    callers that omit index_db_path without force_hosted=True get a DocExtractionError.
    """
    import sys
    import os
    # Add the extensions/meridian-docs to path so we can import meridian_docs
    ext_path = os.path.join(
        os.path.dirname(__file__), "..", "extensions", "meridian-docs"
    )
    sys.path.insert(0, os.path.abspath(ext_path))
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable in this environment")

    # Write a real .docx file.
    docx_path = tmp_path / "test_doc.docx"
    docx_path.write_bytes(_zip_docx(_STRUCTURAL_DOCUMENT_XML))

    # Track calls to call_hosted_ingest_structure.
    calls = []

    def mock_call_hosted_ingest_structure(
        project_id, source, blocks, title=None, doc_type="docx",
        base_url=None, token=None,
    ):
        calls.append({
            "project_id": project_id,
            "source": source,
            "blocks": blocks,
            "doc_type": doc_type,
        })
        # Return a plausible hosted server response.
        return {
            "document_id": "mock-doc-id-12345",
            "source": source,
            "doc_type": doc_type,
            "element_count": len(blocks),
        }

    monkeypatch.setattr(
        local_ingest, "call_hosted_ingest_structure", mock_call_hosted_ingest_structure
    )

    result = local_ingest.ingest_local_document_structure(
        path=str(docx_path),
        project_id="test-project-id",
        title="Test Document",
        force_hosted=True,  # f8c7ffdc: must explicitly opt in to hosted path
    )

    # Verify call was made with the right arguments.
    assert len(calls) == 1, "must call call_hosted_ingest_structure exactly once"
    call = calls[0]
    assert call["project_id"] == "test-project-id"
    assert call["source"] == str(docx_path), "source must default to the file path"
    assert isinstance(call["blocks"], list), "blocks must be a list"
    assert len(call["blocks"]) > 0, "blocks must be non-empty for a non-trivial docx"
    assert call["doc_type"] == "docx"

    # Result has the expected keys.
    assert result["document_id"] == "mock-doc-id-12345"
    assert result["local_path"] == str(docx_path)
    assert result["blocks_forwarded"] == len(call["blocks"])


def test_ingest_local_document_structure_rejects_non_docx(tmp_path):
    """Only .docx files are supported — .pdf and .txt are rejected clearly.
    force_hosted=True is required to reach file-type validation (f8c7ffdc)."""
    import sys, os
    ext_path = os.path.join(
        os.path.dirname(__file__), "..", "extensions", "meridian-docs"
    )
    sys.path.insert(0, os.path.abspath(ext_path))
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable in this environment")

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake pdf content")

    with pytest.raises(local_ingest.UnsupportedDocumentError):
        local_ingest.ingest_local_document_structure(
            str(pdf_path), "proj-id", force_hosted=True
        )

    txt_path = tmp_path / "doc.txt"
    txt_path.write_text("some text")

    with pytest.raises(local_ingest.UnsupportedDocumentError):
        local_ingest.ingest_local_document_structure(
            str(txt_path), "proj-id", force_hosted=True
        )


def test_ingest_local_document_structure_raises_on_missing_file(tmp_path):
    """FileNotFoundError when the path does not exist.
    force_hosted=True is required to reach file-existence validation (f8c7ffdc)."""
    import sys, os
    ext_path = os.path.join(
        os.path.dirname(__file__), "..", "extensions", "meridian-docs"
    )
    sys.path.insert(0, os.path.abspath(ext_path))
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable in this environment")

    with pytest.raises(FileNotFoundError):
        local_ingest.ingest_local_document_structure(
            str(tmp_path / "nonexistent.docx"), "proj-id", force_hosted=True
        )
