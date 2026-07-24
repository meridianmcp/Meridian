"""Tests for meridian_docs.docs_intel.insert_tracked_paragraph (7205c8e0).

FEAT: tracked-changes insertion support -- w:ins-wrapped paragraph insert.
Every major word processor (Word, Google Docs) supports inserting new
content under Track Changes so a reviewer can see exactly what an editing
pass added; this module had none at all.

Scope (confirmed, per Adam): insertion only -- NOT deletion tracking
(w:del) or review/accept-reject (a separate, deprioritized concern,
proposal 9b7ecceb). w:ins/w:del are inline within document.xml itself, no
new part/relationship needed -- fits the existing single-part write
invariant, unlike f1185012's header/footer work.

These tests build a throwaway docx, call insert_tracked_paragraph, verify
the resulting XML has correct w:ins structure (id, author, date, wrapped
run+text), and that the new paragraph is immediately findable by its real
w14:paraId via other functions in this module (no reindex needed).
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone

import pytest
import xml.etree.ElementTree as ET

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _make_docx_bytes(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "doc.docx") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml))
    return path


_TWO_PARA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>First body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Second body paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _read_document_root(path: str) -> ET.Element:
    with zipfile.ZipFile(path) as zf:
        return ET.fromstring(zf.read("word/document.xml"))


# ---------------------------------------------------------------------------
# Helper unit tests: _next_revision_id / _build_tracked_insertion_paragraph
# ---------------------------------------------------------------------------


def test_next_revision_id_starts_at_one_when_no_existing_revisions():
    root = ET.fromstring(_TWO_PARA_XML)
    assert docs_intel._next_revision_id(root) == 1


def test_next_revision_id_returns_one_past_max_seen():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:ins w:id="3" w:author="A" w:date="2026-01-01T00:00:00Z"><w:r><w:t>x</w:t></w:r></w:ins>
    </w:p>
    <w:p>
      <w:del w:id="7" w:author="A" w:date="2026-01-01T00:00:00Z"><w:r><w:delText>y</w:delText></w:r></w:del>
    </w:p>
  </w:body>
</w:document>
"""
    root = ET.fromstring(xml)
    assert docs_intel._next_revision_id(root) == 8


def test_build_tracked_insertion_paragraph_structure():
    p = docs_intel._build_tracked_insertion_paragraph("Hello tracked world.", "ABCD1234", 5, "Meridian Agent")

    assert p.tag == _q(_W, "p")
    assert p.get(_q(_W14, "paraId")) == "ABCD1234"

    ins = p.find(_q(_W, "ins"))
    assert ins is not None
    assert ins.get(_q(_W, "id")) == "5"
    assert ins.get(_q(_W, "author")) == "Meridian Agent"

    date_str = ins.get(_q(_W, "date"))
    assert date_str is not None
    assert date_str.endswith("Z")
    assert "+00:00" not in date_str
    # Fully parseable as UTC ISO-8601 once the "Z" is normalized.
    parsed = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60

    # The <w:ins> wraps exactly one <w:r> with the text.
    runs = ins.findall(_q(_W, "r"))
    assert len(runs) == 1
    t = runs[0].find(_q(_W, "t"))
    assert t.text == "Hello tracked world."

    # No content outside the <w:ins> -- the paragraph's ENTIRE content is the
    # tracked insertion.
    assert list(p) == [ins]


# ---------------------------------------------------------------------------
# insert_tracked_paragraph -- end to end
# ---------------------------------------------------------------------------


def test_insert_tracked_paragraph_after_anchor(tmp_path):
    path = _write_docx(tmp_path, _TWO_PARA_XML)

    result = docs_intel.insert_tracked_paragraph(
        path, "A brand-new tracked sentence.", "P0000001", position="after"
    )

    assert result["status"] == "inserted"
    assert result["text"] == "A brand-new tracked sentence."
    assert result["anchor_para_id"] == "P0000001"
    assert result["position"] == "after"
    assert result["author"] == "Meridian Agent"
    assert result["revision_id"] == 1
    assert re.fullmatch(r"[0-9A-F]{8}", result["para_id"])

    root = _read_document_root(path)
    body = root.find(_q(_W, "body"))
    para_ids = [p.get(_q(_W14, "paraId")) for p in body.findall(_q(_W, "p"))]
    assert para_ids == ["H0000001", "P0000001", result["para_id"], "P0000002"]

    inserted_p = body.findall(_q(_W, "p"))[2]
    ins = inserted_p.find(_q(_W, "ins"))
    assert ins is not None
    assert ins.get(_q(_W, "author")) == "Meridian Agent"
    text = "".join(t.text or "" for t in inserted_p.iter(_q(_W, "t")))
    assert text == "A brand-new tracked sentence."


def test_insert_tracked_paragraph_before_anchor(tmp_path):
    path = _write_docx(tmp_path, _TWO_PARA_XML)

    result = docs_intel.insert_tracked_paragraph(
        path, "Inserted before.", "P0000002", position="before", author="Reviewer Jane"
    )
    assert result["author"] == "Reviewer Jane"

    root = _read_document_root(path)
    body = root.find(_q(_W, "body"))
    para_ids = [p.get(_q(_W14, "paraId")) for p in body.findall(_q(_W, "p"))]
    assert para_ids == ["H0000001", "P0000001", result["para_id"], "P0000002"]


def test_insert_tracked_paragraph_uses_fresh_paraid_not_colliding(tmp_path):
    path = _write_docx(tmp_path, _TWO_PARA_XML)
    r1 = docs_intel.insert_tracked_paragraph(path, "First insertion.", "P0000001")
    r2 = docs_intel.insert_tracked_paragraph(path, "Second insertion.", "P0000001")

    assert r1["para_id"] != r2["para_id"]
    assert r1["revision_id"] != r2["revision_id"]
    assert r2["revision_id"] == r1["revision_id"] + 1

    root = _read_document_root(path)
    body = root.find(_q(_W, "body"))
    para_ids = [p.get(_q(_W14, "paraId")) for p in body.findall(_q(_W, "p"))]
    assert len(para_ids) == len(set(para_ids))  # every paraId is unique


def test_insert_tracked_paragraph_findable_immediately_no_reindex(tmp_path):
    """The new paragraph is immediately findable by its real w14:paraId via
    other functions in this module -- no reindex needed."""
    path = _write_docx(tmp_path, _TWO_PARA_XML)
    result = docs_intel.insert_tracked_paragraph(path, "Findable content.", "P0000001")

    # _find_para_by_id resolves the freshly-minted id right away.
    root = _read_document_root(path)
    found = docs_intel._find_para_by_id(root, result["para_id"])
    assert found is not None
    _body, elem, _idx = found
    text = "".join(t.text or "" for t in elem.iter(_q(_W, "t")))
    assert text == "Findable content."

    # parse_docx (no sidecar index at all) also sees it in document order.
    paras = docs_intel.parse_docx(path)
    matching = [p for p in paras if p["para_id"] == result["para_id"]]
    assert len(matching) == 1
    assert matching[0]["text"] == "Findable content."


def test_insert_tracked_paragraph_resolves_synth_id_anchor(tmp_path):
    """anchor_para_id resolution uses the same three-tier scheme as every
    other write primitive -- a heading discovered via document_outline (a
    synth id, no native w14:paraId) works as an anchor too."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Setup</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Setup body paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml, name="synth.docx")
    outline = docs_intel.document_outline(path)
    heading_id = outline["headings"][0]["para_id"]
    assert heading_id.startswith("sp")

    result = docs_intel.insert_tracked_paragraph(path, "Tracked note after heading.", heading_id)
    assert result["status"] == "inserted"

    root = _read_document_root(path)
    body = root.find(_q(_W, "body"))
    texts_in_order = [
        "".join(t.text or "" for t in p.iter(_q(_W, "t"))) for p in body.findall(_q(_W, "p"))
    ]
    assert texts_in_order == ["Setup", "Tracked note after heading.", "Setup body paragraph."]


# ---------------------------------------------------------------------------
# Error handling.
# ---------------------------------------------------------------------------


def test_insert_tracked_paragraph_rejects_empty_text(tmp_path):
    path = _write_docx(tmp_path, _TWO_PARA_XML)
    result = docs_intel.insert_tracked_paragraph(path, "   ", "P0000001")
    assert "error" in result


def test_insert_tracked_paragraph_rejects_invalid_position(tmp_path):
    path = _write_docx(tmp_path, _TWO_PARA_XML)
    result = docs_intel.insert_tracked_paragraph(path, "Text", "P0000001", position="sideways")
    assert "error" in result


def test_insert_tracked_paragraph_rejects_empty_author(tmp_path):
    path = _write_docx(tmp_path, _TWO_PARA_XML)
    result = docs_intel.insert_tracked_paragraph(path, "Text", "P0000001", author="   ")
    assert "error" in result


def test_insert_tracked_paragraph_anchor_not_found(tmp_path):
    path = _write_docx(tmp_path, _TWO_PARA_XML)
    result = docs_intel.insert_tracked_paragraph(path, "Text", "NoSuchId")
    assert "error" in result
    assert "NoSuchId" in result["error"]


def test_insert_tracked_paragraph_missing_file():
    result = docs_intel.insert_tracked_paragraph("/no/such/path.docx", "Text", "P0000001")
    assert "error" in result


def test_insert_tracked_paragraph_invalidates_sidecar_mtime(tmp_path):
    path = _write_docx(tmp_path, _TWO_PARA_XML)
    index_db_path = str(tmp_path / "sidecar.sqlite3")
    docs_intel.index_docx(path, index_db_path)

    conn = docs_intel._connect(index_db_path)
    try:
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key = 'source_mtime'"
        ).fetchone()
        assert row is not None and row[0] is not None
    finally:
        conn.close()

    docs_intel.insert_tracked_paragraph(
        path, "Text", "P0000001", index_db_path=index_db_path
    )

    conn = docs_intel._connect(index_db_path)
    try:
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key = 'source_mtime'"
        ).fetchone()
        assert row is not None and row[0] is None
    finally:
        conn.close()
