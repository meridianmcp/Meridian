"""Tests for c84ca127 -- chunk-level BM25 indexing in meridian_docs.docs_intel.

Covers:
  - _build_chunks_from_paras: correct chunk boundaries at H1 headings.
  - _build_chunks_from_paras: nested heading_path (H1 > H2 > H3 stacking).
  - _build_chunks_from_paras: sibling H1 headings each produce independent paths.
  - _build_chunks_from_paras: preamble paragraphs before the first heading.
  - index_docx_chunks + fts5_search_chunks: end-to-end indexing and search.
  - fts5_search_chunks: heading match outranks body-only match (BM25 weights 5:1).
  - Sync triggers: INSERT/DELETE on docx_chunks keeps docx_chunks_fts consistent.
  - Empty document: no chunks produced, no crash.
  - Constants: _CHUNK_WEIGHT_HEADING == 5.0, _CHUNK_WEIGHT_BODY == 1.0.

All tests are pure Python (stdlib + pytest) -- no mcp, no network.
"""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from typing import Any

import pytest

from meridian_docs import docs_intel


# ---------------------------------------------------------------------------
# Minimal .docx builder helpers
# ---------------------------------------------------------------------------

def _make_docx(xml: str) -> bytes:
    """Wrap a word/document.xml string into a minimal .docx ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


# A simple 2-heading document:
#   H1 "Introduction"  + body para
#   H2 "Design"        + body para (no paraId)
_BASIC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="00000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="00000002">
      <w:r><w:t>Meridian coordinates AI sessions.</w:t></w:r>
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


def _basic_docx() -> bytes:
    return _make_docx(_BASIC_XML)


# ---------------------------------------------------------------------------
# Constant checks
# ---------------------------------------------------------------------------

def test_chunk_weight_constants():
    assert docs_intel._CHUNK_WEIGHT_HEADING == 5.0
    assert docs_intel._CHUNK_WEIGHT_BODY == 1.0


# ---------------------------------------------------------------------------
# _build_chunks_from_paras: boundary and heading_path tests
# ---------------------------------------------------------------------------

def test_build_chunks_from_paras_basic_boundaries():
    """_build_chunks_from_paras produces one chunk per heading section."""
    paras = docs_intel.parse_docx(_basic_docx())
    chunks = docs_intel._build_chunks_from_paras(paras)

    assert len(chunks) == 2

    c0 = chunks[0]
    assert c0["chunk_id"] == 0
    assert c0["heading_text"] == "Introduction"
    assert c0["heading_path"] == ["Introduction"]
    assert c0["heading_para_id"] == "00000001"
    assert "Meridian" in c0["body_text"]
    assert "sessions" in c0["body_text"]

    c1 = chunks[1]
    assert c1["chunk_id"] == 1
    assert c1["heading_text"] == "Design"
    assert c1["heading_path"] == ["Introduction", "Design"]
    assert "paragraph with no paraId" in c1["body_text"]
    assert c1["heading_para_id"] == "00000003"


def test_build_chunks_from_paras_nested_heading_path():
    """Heading path correctly reflects multi-level nesting (H1 > H2 > H3)."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="A1">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Alpha</w:t></w:r>
    </w:p>
    <w:p w14:paraId="A2">
      <w:r><w:t>body under Alpha</w:t></w:r>
    </w:p>
    <w:p w14:paraId="B1">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Beta</w:t></w:r>
    </w:p>
    <w:p w14:paraId="B2">
      <w:r><w:t>body under Beta</w:t></w:r>
    </w:p>
    <w:p w14:paraId="G1">
      <w:pPr><w:pStyle w:val="Heading3"/></w:pPr>
      <w:r><w:t>Gamma</w:t></w:r>
    </w:p>
    <w:p w14:paraId="G2">
      <w:r><w:t>body under Gamma</w:t></w:r>
    </w:p>
    <w:p w14:paraId="D1">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Delta</w:t></w:r>
    </w:p>
    <w:p w14:paraId="D2">
      <w:r><w:t>body under Delta</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""
    paras = docs_intel.parse_docx(_make_docx(xml))
    chunks = docs_intel._build_chunks_from_paras(paras)

    assert len(chunks) == 4

    alpha = chunks[0]
    assert alpha["heading_text"] == "Alpha"
    assert alpha["heading_path"] == ["Alpha"]
    assert "body under Alpha" in alpha["body_text"]

    beta = chunks[1]
    assert beta["heading_text"] == "Beta"
    assert beta["heading_path"] == ["Alpha", "Beta"]
    assert "body under Beta" in beta["body_text"]

    gamma = chunks[2]
    assert gamma["heading_text"] == "Gamma"
    assert gamma["heading_path"] == ["Alpha", "Beta", "Gamma"]
    assert "body under Gamma" in gamma["body_text"]

    # Delta is H2 -- Gamma (H3) and Beta (H2) are both popped, Alpha (H1) remains.
    delta = chunks[3]
    assert delta["heading_text"] == "Delta"
    assert delta["heading_path"] == ["Alpha", "Delta"]
    assert "body under Delta" in delta["body_text"]


def test_build_chunks_from_paras_sibling_h1_resets_path():
    """Two sibling H1 headings each produce an independent chunk; the second H1
    does NOT include the first H1 in its heading_path."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter One</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Content of one.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Chapter Two</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Content of two.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    paras = docs_intel.parse_docx(_make_docx(xml))
    chunks = docs_intel._build_chunks_from_paras(paras)

    assert len(chunks) == 2
    assert chunks[0]["heading_path"] == ["Chapter One"]
    assert chunks[1]["heading_path"] == ["Chapter Two"]
    # Second chunk must NOT have Chapter One in its path.
    assert "Chapter One" not in chunks[1]["heading_path"]


def test_build_chunks_from_paras_preamble():
    """Paragraphs before the first heading form a preamble chunk with empty heading_text."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Pre-heading preamble text.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Section One</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Body of section one.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    paras = docs_intel.parse_docx(_make_docx(xml))
    chunks = docs_intel._build_chunks_from_paras(paras)

    assert len(chunks) == 2

    preamble = chunks[0]
    assert preamble["heading_text"] == ""
    assert preamble["heading_path"] == []
    assert "preamble text" in preamble["body_text"]

    section = chunks[1]
    assert section["heading_text"] == "Section One"
    assert section["heading_path"] == ["Section One"]


def test_build_chunks_from_paras_empty_document():
    """An empty document (no paragraphs) yields no chunks without crashing."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body/>
</w:document>
"""
    paras = docs_intel.parse_docx(_make_docx(xml))
    chunks = docs_intel._build_chunks_from_paras(paras)
    assert chunks == []


def test_build_chunks_from_paras_chunk_ids_sequential():
    """chunk_id is strictly sequential from 0."""
    paras = docs_intel.parse_docx(_basic_docx())
    chunks = docs_intel._build_chunks_from_paras(paras)
    for i, c in enumerate(chunks):
        assert c["chunk_id"] == i


# ---------------------------------------------------------------------------
# index_docx_chunks + fts5_search_chunks: end-to-end
# ---------------------------------------------------------------------------

def test_index_docx_chunks_and_search_basic(tmp_path):
    """index_docx_chunks populates docx_chunks; fts5_search_chunks returns hits."""
    db = str(tmp_path / "doc.idx.sqlite")
    summary = docs_intel.index_docx_chunks(_basic_docx(), db)

    assert summary["chunk_count"] == 2
    assert summary["index_db"] == db

    # "sessions" appears in chunk 0's body (Introduction chunk).
    hits = docs_intel.fts5_search_chunks(db, "sessions")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["heading_text"] == "Introduction"
    assert hit["heading_path"] == ["Introduction"]
    assert "bm25_score" in hit
    assert isinstance(hit["bm25_score"], float)
    # BM25 scores from SQLite FTS5 are negative.
    assert hit["bm25_score"] < 0


def test_index_docx_chunks_idempotent(tmp_path):
    """Re-indexing the same document leaves a consistent state (idempotent)."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_basic_docx(), db)
    summary2 = docs_intel.index_docx_chunks(_basic_docx(), db)
    assert summary2["chunk_count"] == 2

    hits = docs_intel.fts5_search_chunks(db, "sessions")
    assert len(hits) == 1


def test_fts5_search_chunks_heading_match_outranks_body_match(tmp_path):
    """A term in a heading ranks above the same term appearing only in body prose.

    Doc: H1 "Design" with body "some text"
         H1 "Intro"  with body "Design concepts"
    Searching "Design" should rank the heading-bearing chunk first.
    """
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Design</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>some text here</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Intro</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Design concepts are discussed here.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_make_docx(xml), db)

    hits = docs_intel.fts5_search_chunks(db, "Design")
    assert len(hits) == 2

    # BM25 scores are negative; lower = more relevant.
    heading_chunk = next(h for h in hits if h["heading_text"] == "Design")
    body_only_chunk = next(h for h in hits if h["heading_text"] == "Intro")

    assert heading_chunk["bm25_score"] < body_only_chunk["bm25_score"], (
        "Heading match should have lower (more relevant) BM25 score than body-only match"
    )


def test_fts5_search_chunks_no_match_returns_empty(tmp_path):
    """A query that matches nothing returns an empty list rather than raising."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_basic_docx(), db)

    hits = docs_intel.fts5_search_chunks(db, "xyzzy_not_in_doc")
    assert hits == []


def test_fts5_search_chunks_missing_db_returns_empty(tmp_path):
    """fts5_search_chunks on a non-existent DB returns [] (graceful degradation)."""
    db = str(tmp_path / "nonexistent.idx.sqlite")
    hits = docs_intel.fts5_search_chunks(db, "anything")
    assert hits == []


def test_fts5_search_chunks_heading_path_in_results(tmp_path):
    """Every search hit carries a correctly decoded heading_path list."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_basic_docx(), db)

    # Search for "paragraph" which is in the Design chunk body.
    hits = docs_intel.fts5_search_chunks(db, "paragraph")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["heading_path"] == ["Introduction", "Design"]
    assert isinstance(hit["heading_path"], list)


# ---------------------------------------------------------------------------
# Sync triggers: INSERT/DELETE on docx_chunks keeps docx_chunks_fts consistent
# ---------------------------------------------------------------------------

def test_sync_trigger_insert_keeps_fts_consistent(tmp_path):
    """AFTER INSERT trigger: a manually inserted chunk is immediately searchable."""
    db = str(tmp_path / "doc.idx.sqlite")
    # Initialize schema (creates tables + triggers).
    docs_intel.index_docx_chunks(_basic_docx(), db)

    conn = sqlite3.connect(db)
    try:
        # Insert a new chunk directly -- the trigger should propagate to FTS.
        conn.execute(
            "INSERT INTO docx_chunks (chunk_id, heading_path_json, heading_text, "
            "body_text, start_para_id, end_para_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (99, json.dumps(["Trigger Test"]), "Trigger Test",
             "special trigger body", "tp1", "tp2"),
        )
        conn.commit()
    finally:
        conn.close()

    hits = docs_intel.fts5_search_chunks(db, "trigger")
    assert any(h["heading_text"] == "Trigger Test" for h in hits)


def test_sync_trigger_delete_removes_from_fts(tmp_path):
    """AFTER DELETE trigger: deleting a chunk removes it from FTS search."""
    db = str(tmp_path / "doc.idx.sqlite")
    docs_intel.index_docx_chunks(_basic_docx(), db)

    # Confirm chunk 0 (Introduction) is currently searchable.
    hits_before = docs_intel.fts5_search_chunks(db, "sessions")
    assert len(hits_before) == 1

    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM docx_chunks WHERE chunk_id = 0")
        conn.commit()
    finally:
        conn.close()

    hits_after = docs_intel.fts5_search_chunks(db, "sessions")
    assert hits_after == []


# ---------------------------------------------------------------------------
# Honest scope difference: parse_docx misses table cell content
# ---------------------------------------------------------------------------

def test_chunk_body_text_is_paragraphs_only_no_table_fabrication(tmp_path):
    """Verify the honest scope: chunks do not fabricate table content.

    parse_docx() collects only <w:p> paragraphs; <w:tbl> siblings are not
    included.  This test confirms that table cell text is absent from chunk
    body_text so the scope difference is transparent.
    """
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>TableCellContent</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p><w:r><w:t>Paragraph after table.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    paras = docs_intel.parse_docx(_make_docx(xml))
    chunks = docs_intel._build_chunks_from_paras(paras)

    # There should be exactly one chunk (Results heading + its body).
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk["heading_text"] == "Results"
    # The table cell text is NOT in the body (parse_docx skips <w:tbl>).
    assert "TableCellContent" not in chunk["body_text"]
    # The paragraph after the table IS present.
    assert "Paragraph after table" in chunk["body_text"]
