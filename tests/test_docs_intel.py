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
