"""Tests for meridian_docs.docs_intel.retrofit_plaintext_captions (82b0b1a6).

Bulk-converts existing plain-text Figure/Table captions (hardcoded numbers,
no SEQ field) into real Word SEQ fields, then calls renumber_sequences so
duplicate/incorrect numbers are corrected -- closing the gap where
renumber_sequences (which only ever walked existing SEQ fields) silently
walked straight past plain-text captions, letting duplicate hardcoded
caption numbers survive a full renumbering pass untouched.

All tests are pure Python (stdlib + pytest) -- no mcp, no network. Follows
the same conventions as test_docs_intel_new_primitives.py / test_relocate_table.py:
tests that mutate write a minimal .docx to tmp_path first.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


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


def _texts_by_para_id(path: str) -> dict[str, str]:
    paras = docs_intel.parse_docx(path)
    return {p["para_id"]: p["text"] for p in paras}


# ---------------------------------------------------------------------------
# The real motivating bug: 4 plain-text captions all hardcoded "Figure 42",
# alongside one PRE-EXISTING real SEQ Figure caption ("Figure 1"). Before
# this primitive existed, renumber_sequences alone could never see the 4
# plain-text duplicates (it only walks <w:fldSimple> SEQ fields) so they
# would survive a full renumbering pass untouched.
# ---------------------------------------------------------------------------

_FOUR_DUPLICATE_FIGURES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="C0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref100000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. Baseline architecture diagram.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure 42. First duplicate figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure 42. Second duplicate figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure 42. Third duplicate figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000004">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure 42. Fourth duplicate figure.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_retrofit_converts_duplicate_plaintext_captions_and_renumbers(tmp_path):
    path = _write_docx(tmp_path, _FOUR_DUPLICATE_FIGURES_XML)

    result = docs_intel.retrofit_plaintext_captions(path)

    assert result["status"] == "converted"
    assert result["candidates_found"] == 4
    converted_ids = [c["para_id"] for c in result["conversions"]]
    assert converted_ids == ["P0000001", "P0000002", "P0000003", "P0000004"]
    # Every converted caption carried the SAME hardcoded "42" before renumbering.
    assert all(c["old_cached_number"] == "42" for c in result["conversions"])
    assert all(c["kind"] == "Figure" for c in result["conversions"])
    # Each gets its own distinct cross-reference bookmark.
    bookmarks = [c["ref_bookmark"] for c in result["conversions"]]
    assert len(bookmarks) == len(set(bookmarks)) == 4
    assert result["skipped"] == []

    # renumber_sequences ran automatically and fixed the (now-real) SEQ fields.
    renumber = result["renumber_sequences"]
    assert renumber["status"] == "corrected"
    assert renumber["figure_count"] == 5

    # Confirm the write actually landed and every figure is now sequential
    # and unique -- the exact bug (4x "Figure 42" survives renumbering) is
    # fixed by converting to real SEQ fields first, then renumbering.
    texts = _texts_by_para_id(path)
    assert texts["C0000001"] == "Figure 1. Baseline architecture diagram."
    assert texts["P0000001"] == "Figure 2. First duplicate figure."
    assert texts["P0000002"] == "Figure 3. Second duplicate figure."
    assert texts["P0000003"] == "Figure 4. Third duplicate figure."
    assert texts["P0000004"] == "Figure 5. Fourth duplicate figure."

    numbers = [texts[pid].split()[1].rstrip(".") for pid in
               ("C0000001", "P0000001", "P0000002", "P0000003", "P0000004")]
    assert numbers == ["1", "2", "3", "4", "5"]
    assert len(set(numbers)) == 5  # all unique -- the duplicate is gone


# ---------------------------------------------------------------------------
# No-op cases: nothing to convert.
# ---------------------------------------------------------------------------

def test_retrofit_unchanged_when_no_plaintext_captions(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Just a normal paragraph.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)
    result = docs_intel.retrofit_plaintext_captions(path)
    assert result["status"] == "unchanged"
    assert result["candidates_found"] == 0
    assert result["conversions"] == []
    assert result["renumber_sequences"] is None


def test_retrofit_unchanged_when_captions_already_real_seq_fields(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="C0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:r><w:t xml:space="preserve">. Already a real caption.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)
    result = docs_intel.retrofit_plaintext_captions(path)
    assert result["status"] == "unchanged"
    assert result["candidates_found"] == 0


# ---------------------------------------------------------------------------
# Label-text preservation, including the bare "Figure N" (no description) case.
# ---------------------------------------------------------------------------

def test_retrofit_preserves_descriptive_label_text(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t xml:space="preserve">Figure 7. Loss curve for run 42.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)
    result = docs_intel.retrofit_plaintext_captions(path)
    assert result["status"] == "converted"
    assert result["conversions"][0]["label_text"] == "Loss curve for run 42."

    texts = _texts_by_para_id(path)
    # Only figure in the doc -- renumber_sequences corrects 7 -> 1.
    assert texts["P0000001"] == "Figure 1. Loss curve for run 42."


def test_retrofit_bare_caption_with_no_description(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Figure 9</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)
    result = docs_intel.retrofit_plaintext_captions(path)
    assert result["status"] == "converted"
    assert result["conversions"][0]["label_text"] == ""

    texts = _texts_by_para_id(path)
    # No dangling ". " suffix left behind.
    assert texts["P0000001"] == "Figure 1"


# ---------------------------------------------------------------------------
# Table captions use the same code path.
# ---------------------------------------------------------------------------

def test_retrofit_handles_table_captions(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t xml:space="preserve">Table 7. Hyperparameter sweep results.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)
    result = docs_intel.retrofit_plaintext_captions(path)
    assert result["status"] == "converted"
    assert result["conversions"][0]["kind"] == "Table"
    assert result["renumber_sequences"]["table_count"] == 1

    texts = _texts_by_para_id(path)
    assert texts["P0000001"] == "Table 1. Hyperparameter sweep results."


# ---------------------------------------------------------------------------
# Safety: a candidate paragraph that already owns a bookmark is skipped, not
# silently destroyed.
# ---------------------------------------------------------------------------

def test_retrofit_skips_candidate_with_pre_existing_bookmark(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:bookmarkStart w:id="5" w:name="_CustomAnchor"/>
      <w:r><w:t xml:space="preserve">Figure 3. Has a pre-existing custom bookmark.</w:t></w:r>
      <w:bookmarkEnd w:id="5"/>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)
    result = docs_intel.retrofit_plaintext_captions(path)

    assert result["status"] == "unchanged"
    assert result["candidates_found"] == 0
    assert result["conversions"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["para_id"] == "P0000001"
    assert result["skipped"][0]["kind"] == "Figure"

    # File left completely untouched.
    texts = _texts_by_para_id(path)
    assert texts["P0000001"] == "Figure 3. Has a pre-existing custom bookmark."


# ---------------------------------------------------------------------------
# Error handling, matching sibling primitives' conventions.
# ---------------------------------------------------------------------------

def test_retrofit_missing_file_errors():
    result = docs_intel.retrofit_plaintext_captions("C:/nonexistent/path/doc.docx")
    assert "error" in result
