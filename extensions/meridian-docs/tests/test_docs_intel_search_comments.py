"""Tests for document-wide XML search, highlighting, and Word comments."""

from __future__ import annotations

import hashlib
import io
import zipfile
import xml.etree.ElementTree as ET

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Methods</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>The robust search method evaluates every paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="F0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr=" SEQ Figure \\* ARABIC ">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t xml:space="preserve"> — robust search result</w:t></w:r>
    </w:p>
    <w:p w14:paraId="T0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Table </w:t></w:r>
      <w:fldSimple w:instr=" SEQ Table \\* ARABIC ">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t xml:space="preserve"> — robust search table</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>robust search cell</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_HEADER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:r><w:t>robust search header</w:t></w:r></w:p>
</w:hdr>
"""


def _write_docx(tmp_path, name="doc.docx"):
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
        archive.writestr("word/header1.xml", _HEADER_XML)
    return path


def _read_xml(path, member):
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read(member))


def test_search_document_xml_covers_parts_and_structural_filters(tmp_path):
    path = _write_docx(tmp_path)

    all_results = docs_intel.search_document_xml(path, "robust search", limit=50)
    types = {result["element_type"] for result in all_results}
    assert {"paragraph", "figure_caption", "table_caption", "table", "header"} <= types
    assert all_results[0]["bm25_score"] >= all_results[-1]["bm25_score"]

    captions = docs_intel.search_document_xml(
        path, "robust search", element_types=["caption"], limit=10
    )
    assert {result["element_type"] for result in captions} == {
        "figure_caption",
        "table_caption",
    }

    headings = docs_intel.search_document_xml(
        path, "methods", element_types=["heading"], limit=5
    )
    assert headings and headings[0]["element_id"] == "H0000001"
    assert headings[0]["section_path"] == ["Methods"]


def test_highlight_document_matches_writes_native_highlight(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.highlight_document_matches(
        path, "robust search", element_types=["figure_caption"], limit=10
    )

    assert result["status"] == "highlighted"
    assert result["matched_runs"] >= 1
    root = _read_xml(path, "word/document.xml")
    highlighted = root.findall(
        ".//{%s}highlight" % _W
    )
    assert any(node.get("{%s}val" % _W) == "yellow" for node in highlighted)


def test_insert_highlighted_note_comment_mode_creates_valid_word_parts(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.insert_highlighted_note(
        path,
        "Review this method.",
        "P0000001",
        mode="comment",
        author="Adam",
        initials="AD",
    )

    assert result["status"] == "inserted"
    assert result["mode"] == "comment"
    assert result["comment_id"] == 0

    document = _read_xml(path, "word/document.xml")
    comments = _read_xml(path, "word/comments.xml")
    rels = _read_xml(path, "word/_rels/document.xml.rels")
    content_types = _read_xml(path, "[Content_Types].xml")

    assert document.find(".//{%s}commentRangeStart" % _W).get("{%s}id" % _W) == "0"
    assert document.find(".//{%s}commentRangeEnd" % _W).get("{%s}id" % _W) == "0"
    assert document.find(".//{%s}commentReference" % _W).get("{%s}id" % _W) == "0"
    assert comments.find(".//{%s}comment" % _W).get("{%s}author" % _W) == "Adam"
    assert rels.find(
        ".//{%s}Relationship" % _REL
    ).get("Type", "").endswith("/comments")
    assert content_types.find(
        ".//{%s}Override" % _CT
    ).get("PartName") == "/word/comments.xml"

def test_read_document_snapshot_works_while_path_is_open(tmp_path):
    path = _write_docx(tmp_path)
    lock_path = tmp_path / "~$doc.docx"
    lock_path.write_bytes(b"Word lock hint")

    with open(path, "rb") as document_handle:
        result = docs_intel.read_document_snapshot(path)

    assert document_handle.closed
    assert result["status"] == "read_only"
    assert result["word_lock_hint"] is True
    assert result["paragraph_count"] >= 4
    assert "word/document.xml" in result["xml_parts"]
    assert not (tmp_path / "doc.docx.bak").exists()


def test_read_document_snapshot_includes_fingerprint_and_limitations(tmp_path):
    path = _write_docx(tmp_path)
    with open(path, "rb") as handle:
        expected_sha256 = hashlib.sha256(handle.read()).hexdigest()

    result = docs_intel.read_document_snapshot(path)

    assert result["source_sha256"] == expected_sha256
    assert len(result["source_sha256"]) == 64  # hex-encoded SHA-256

    limitations = result["limitations"]
    assert isinstance(limitations, list)
    assert len(limitations) >= 2
    assert any("last SAVED state" in item for item in limitations)
    assert any("word_lock_hint" in item for item in limitations)


def test_read_document_snapshot_error_path_has_no_fingerprint(tmp_path):
    missing_path = str(tmp_path / "does_not_exist.docx")

    result = docs_intel.read_document_snapshot(missing_path)

    assert "error" in result
    assert "source_sha256" not in result
    assert "limitations" not in result


def test_search_document_xml_attaches_quoted_text_and_unique_word_search_locator(tmp_path):
    path = _write_docx(tmp_path)

    results = docs_intel.search_document_xml(
        path, "evaluates every paragraph", limit=5
    )

    assert results
    top = results[0]
    assert top["element_id"] == "P0000001"
    assert "…" not in top["quoted_text"]
    assert top["quoted_text"] in "The robust search method evaluates every paragraph."
    locator = top["word_search_locator"]
    assert locator["find_text"] == top["quoted_text"]
    assert locator["part"] == "word/document.xml"
    assert locator["element_id"] == top["element_id"]
    assert locator["unique_in_part"] is True
    assert locator["occurrence_count_in_part"] == 1


_DOCUMENT_XML_REPEATED_PHRASE = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="R0000001">
      <w:r><w:t>Please review this clause carefully.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="R0000002">
      <w:r><w:t>Please review this clause carefully.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_search_document_xml_word_search_locator_flags_non_unique_matches(tmp_path):
    path = str(tmp_path / "repeated.docx")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML_REPEATED_PHRASE)

    results = docs_intel.search_document_xml(path, "clause carefully", limit=10)

    assert len(results) == 2
    element_ids = set()
    for result in results:
        assert result["quoted_text"] == "Please review this clause carefully."
        locator = result["word_search_locator"]
        assert locator["find_text"] == "Please review this clause carefully."
        assert locator["part"] == "word/document.xml"
        assert locator["unique_in_part"] is False
        assert locator["occurrence_count_in_part"] == 2
        element_ids.add(result["element_id"])
    assert element_ids == {"R0000001", "R0000002"}
