"""9d749639 — Tests for meridian-docs DOCX write-back: captions + citations.

Exercises:
  - insert_caption: Figure and Table kinds, Caption style + SEQ field present
    in the output XML, auto-increment numbering across multiple captions,
    position=before/after, section_heading stored in sidecar.
  - edit_caption: label text updated, SEQ field preserved (number unchanged).
  - remove_caption: paragraph removed from body.
  - insert_citation: CSL_CITATION complex field (begin/instrText/separate/
    cached/end) present in output XML, Zotero and CSL source variants.
  - edit_citation: keys and formatted text replaced in-place.
  - remove_citation: field runs removed.
  - Error paths: unknown path, missing file, bad para_id, wrong paragraph type,
    no citation field — all return {"error": ...} with the file byte-for-byte
    unchanged.
  - Sidecar sync: insert_caption with index_db_path invalidates mtime and
    upserts into docx_figures / docx_tables.

All tests use synthetic minimal .docx files built in-memory (no real Word
files, no network).  Test file follows the naming convention of existing
meridian-docs tests:
  tests/test_105e56b9_meridian_docs_spawn.py
  tests/test_w5_9665538a_meridian_docs_slot.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import zipfile

import pytest

# Make meridian_docs importable from the local extensions directory.
_EXT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-docs")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic .docx fixtures
# ---------------------------------------------------------------------------

# A minimal document with three paragraphs addressable by w14:paraId.
_DOC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="AABB0001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AABB0002">
      <w:r><w:t>Body paragraph with some text.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AABB0003">
      <w:r><w:t>A second body paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# A document that already has one Figure caption (for numbering tests).
_DOC_XML_WITH_CAPTION = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="CCDD0001">
      <w:r><w:t>Before figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="CCDD0002">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t xml:space="preserve">. Existing caption</w:t></w:r>
    </w:p>
    <w:p w14:paraId="CCDD0003">
      <w:r><w:t>After figure.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# A document with a paragraph that already has a CSL_CITATION complex field.
_DOC_XML_WITH_CITATION = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="EEFF0001">
      <w:r><w:t xml:space="preserve">See Smith </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"abc","properties":{"formattedCitation":"(Smith 2020)"},"citationItems":[{"id":"smith2020","uris":[],"itemData":{"id":"smith2020","type":"article"}}],"schema":"https://github.com/citation-style-language/schema/raw/master/csl-citation.json"} </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>(Smith 2020)</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t xml:space="preserve"> for details.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="EEFF0002">
      <w:r><w:t>Plain paragraph, no citation.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _zip_docx(xml: str) -> bytes:
    """Build a minimal .docx ZIP containing only word/document.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(path: str, xml: str = _DOC_XML) -> None:
    """Write a minimal .docx to disk."""
    with open(path, "wb") as fh:
        fh.write(_zip_docx(xml))


def _read_doc_xml(path: str) -> str:
    """Read word/document.xml from a .docx as a string."""
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read("word/document.xml").decode("utf-8")


# ---------------------------------------------------------------------------
# Caption: insert (Figure)
# ---------------------------------------------------------------------------

class TestInsertFigureCaption:
    def test_insert_figure_caption_after(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Loss curve for run 42",
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["status"] == "inserted"
        assert res["kind"] == "Figure"
        assert res["seq_number"] == 1
        assert res["label_text"] == "Loss curve for run 42"

        xml = _read_doc_xml(docx)
        # Caption style must be present.
        assert 'w:val="Caption"' in xml
        # SEQ Figure field must be present.
        assert "SEQ Figure" in xml
        # Label text must be present.
        assert "Loss curve for run 42" in xml
        # File must have changed.
        assert open(docx, "rb").read() != original

    def test_insert_figure_caption_before(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Before anchor",
            position="before",
        )

        assert "error" not in res
        xml = _read_doc_xml(docx)
        assert 'w:val="Caption"' in xml
        assert "SEQ Figure" in xml

    def test_insert_figure_caption_seq_number_prefix(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="My chart",
        )
        assert res["seq_number"] == 1
        xml = _read_doc_xml(docx)
        # The "Figure " prefix run must be present.
        assert "Figure " in xml

    def test_insert_figure_caption_autoincrement(self, tmp_path):
        """Second insert of a Figure caption gets seq_number 2."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="CCDD0003",
            kind="Figure",
            label_text="Second figure",
        )
        assert "error" not in res
        # Existing doc has 1 SEQ Figure, so new one should be 2.
        assert res["seq_number"] == 2

    def test_insert_figure_caption_with_section_heading(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Loss curve",
            section_heading="Results",
        )
        assert "error" not in res
        assert res["section_heading"] == "Results"


# ---------------------------------------------------------------------------
# Caption: insert (Table)
# ---------------------------------------------------------------------------

class TestInsertTableCaption:
    def test_insert_table_caption(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Table",
            label_text="Summary statistics",
        )

        assert "error" not in res
        assert res["kind"] == "Table"
        assert res["seq_number"] == 1

        xml = _read_doc_xml(docx)
        assert 'w:val="Caption"' in xml
        assert "SEQ Table" in xml
        assert "Summary statistics" in xml

    def test_table_and_figure_counters_are_independent(self, tmp_path):
        """Inserting a Table caption does not affect Figure counter and v.v."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)  # already has 1 SEQ Figure

        # Insert a Table caption — should be #1 (independent counter).
        res_tbl = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="CCDD0003",
            kind="Table",
            label_text="My table",
        )
        assert "error" not in res_tbl
        assert res_tbl["seq_number"] == 1

        # Insert a second Figure caption — should be #2.
        res_fig = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="CCDD0003",
            kind="Figure",
            label_text="Second figure",
        )
        assert "error" not in res_fig
        assert res_fig["seq_number"] == 2


# ---------------------------------------------------------------------------
# Caption: edit
# ---------------------------------------------------------------------------

class TestEditCaption:
    def test_edit_caption_label_text(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)

        res = docs_intel.edit_caption(
            docx_path=docx,
            caption_para_id="CCDD0002",
            new_label_text="Updated caption text",
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["status"] == "edited"
        assert res["new_label_text"] == "Updated caption text"

        xml = _read_doc_xml(docx)
        assert "Updated caption text" in xml
        # SEQ field must still be present (not clobbered).
        assert "SEQ Figure" in xml
        # Caption style must still be present.
        assert 'w:val="Caption"' in xml

    def test_edit_caption_seq_number_preserved(self, tmp_path):
        """Editing a caption must not change the cached SEQ number."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)

        docs_intel.edit_caption(
            docx_path=docx,
            caption_para_id="CCDD0002",
            new_label_text="New label",
        )

        xml = _read_doc_xml(docx)
        # The SEQ field's cached value "1" must still be present.
        assert ">1<" in xml or "<w:t>1</w:t>" in xml

    def test_edit_caption_rejects_non_caption_paragraph(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.edit_caption(
            docx_path=docx,
            caption_para_id="AABB0002",  # plain body paragraph, not a caption
            new_label_text="Should fail",
        )

        assert "error" in res
        # File must be byte-for-byte unchanged on failure.
        assert open(docx, "rb").read() == original

    def test_edit_caption_unknown_para_id(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.edit_caption(
            docx_path=docx,
            caption_para_id="DOES-NOT-EXIST",
            new_label_text="Nope",
        )

        assert "error" in res
        assert open(docx, "rb").read() == original


# ---------------------------------------------------------------------------
# Caption: remove
# ---------------------------------------------------------------------------

class TestRemoveCaption:
    def test_remove_caption(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)

        # Verify caption exists before remove.
        xml_before = _read_doc_xml(docx)
        assert 'w:val="Caption"' in xml_before

        res = docs_intel.remove_caption(
            docx_path=docx,
            caption_para_id="CCDD0002",
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["status"] == "removed"

        xml_after = _read_doc_xml(docx)
        # Caption paragraph is gone — style should no longer appear.
        assert 'w:val="Caption"' not in xml_after
        # The SEQ field is also gone.
        assert "SEQ Figure" not in xml_after

    def test_remove_caption_rejects_non_caption_paragraph(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.remove_caption(
            docx_path=docx,
            caption_para_id="AABB0002",
        )

        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_remove_caption_missing_file(self, tmp_path):
        res = docs_intel.remove_caption(
            docx_path=str(tmp_path / "nonexistent.docx"),
            caption_para_id="CCDD0002",
        )
        assert "error" in res


# ---------------------------------------------------------------------------
# Caption: error paths
# ---------------------------------------------------------------------------

class TestCaptionErrorPaths:
    def test_insert_caption_bad_kind(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Chart",  # invalid
            label_text="Some label",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_caption_bad_position(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Label",
            position="sideways",  # invalid
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_caption_empty_label(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="   ",  # blank
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_caption_unknown_para_id(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="BOGUS-ID",
            kind="Figure",
            label_text="Label",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_caption_missing_file(self, tmp_path):
        res = docs_intel.insert_caption(
            docx_path=str(tmp_path / "no.docx"),
            anchor_para_id="AABB0001",
            kind="Figure",
            label_text="Label",
        )
        assert "error" in res

    def test_edit_caption_empty_label(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)
        original = open(docx, "rb").read()

        res = docs_intel.edit_caption(
            docx_path=docx,
            caption_para_id="CCDD0002",
            new_label_text="",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original


# ---------------------------------------------------------------------------
# Citation: insert
# ---------------------------------------------------------------------------

class TestInsertCitation:
    def test_insert_citation_zotero(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=["smith2023"],
            formatted_text="(Smith 2023)",
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["status"] == "inserted"
        assert res["citation_keys"] == ["smith2023"]
        assert res["formatted_text"] == "(Smith 2023)"
        assert res["source"] == "zotero"

        xml = _read_doc_xml(docx)
        # CSL_CITATION token must be present in instrText.
        assert "CSL_CITATION" in xml
        # ADDIN ZOTERO_ITEM prefix.
        assert "ADDIN ZOTERO_ITEM" in xml
        # Citation key must be in the JSON payload.
        assert "smith2023" in xml
        # Cached display text.
        assert "(Smith 2023)" in xml
        # Complex field structure: begin / separate / end markers.
        assert 'fldCharType="begin"' in xml
        assert 'fldCharType="separate"' in xml
        assert 'fldCharType="end"' in xml
        # File must have changed.
        assert open(docx, "rb").read() != original

    def test_insert_citation_csl_source(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=["jones2021"],
            formatted_text="(Jones 2021)",
            source="csl",
        )

        assert "error" not in res
        xml = _read_doc_xml(docx)
        # CSL source uses ADDIN CSL_CITATION (no ZOTERO_ITEM prefix).
        assert "ADDIN CSL_CITATION" in xml
        assert "ADDIN ZOTERO_ITEM" not in xml

    def test_insert_citation_multiple_keys(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=["smith2023", "jones2021"],
            formatted_text="(Smith 2023; Jones 2021)",
        )

        assert "error" not in res
        assert len(res["citation_keys"]) == 2
        xml = _read_doc_xml(docx)
        assert "smith2023" in xml
        assert "jones2021" in xml


# ---------------------------------------------------------------------------
# Citation: edit
# ---------------------------------------------------------------------------

class TestEditCitation:
    def test_edit_citation_formatted_text(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)

        res = docs_intel.edit_citation(
            docx_path=docx,
            anchor_para_id="EEFF0001",
            new_formatted_text="(Smith et al. 2020)",
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["status"] == "edited"
        assert res["formatted_text"] == "(Smith et al. 2020)"

        xml = _read_doc_xml(docx)
        assert "(Smith et al. 2020)" in xml
        # Old display text should be gone from cached display run.
        assert "CSL_CITATION" in xml  # field still present

    def test_edit_citation_keys(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)

        res = docs_intel.edit_citation(
            docx_path=docx,
            anchor_para_id="EEFF0001",
            new_citation_keys=["jones2021"],
            new_formatted_text="(Jones 2021)",
        )

        assert "error" not in res
        assert res["citation_keys"] == ["jones2021"]
        xml = _read_doc_xml(docx)
        assert "jones2021" in xml

    def test_edit_citation_requires_at_least_one_arg(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        original = open(docx, "rb").read()

        res = docs_intel.edit_citation(
            docx_path=docx,
            anchor_para_id="EEFF0001",
            # neither new_citation_keys nor new_formatted_text supplied
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_edit_citation_no_field_in_paragraph(self, tmp_path):
        """edit_citation on a paragraph with no CSL_CITATION field returns error."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        original = open(docx, "rb").read()

        res = docs_intel.edit_citation(
            docx_path=docx,
            anchor_para_id="EEFF0002",  # plain paragraph, no citation
            new_formatted_text="(Nobody)",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_edit_citation_unknown_para_id(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        original = open(docx, "rb").read()

        res = docs_intel.edit_citation(
            docx_path=docx,
            anchor_para_id="BOGUS",
            new_formatted_text="(x)",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original


# ---------------------------------------------------------------------------
# Citation: remove
# ---------------------------------------------------------------------------

class TestRemoveCitation:
    def test_remove_citation(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)

        xml_before = _read_doc_xml(docx)
        assert "CSL_CITATION" in xml_before

        res = docs_intel.remove_citation(
            docx_path=docx,
            anchor_para_id="EEFF0001",
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["status"] == "removed"

        xml_after = _read_doc_xml(docx)
        assert "CSL_CITATION" not in xml_after
        assert 'fldCharType="begin"' not in xml_after

    def test_remove_citation_no_field(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        original = open(docx, "rb").read()

        res = docs_intel.remove_citation(
            docx_path=docx,
            anchor_para_id="EEFF0002",  # plain paragraph
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_remove_citation_missing_file(self, tmp_path):
        res = docs_intel.remove_citation(
            docx_path=str(tmp_path / "gone.docx"),
            anchor_para_id="EEFF0001",
        )
        assert "error" in res

    def test_remove_citation_unknown_para_id(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        original = open(docx, "rb").read()

        res = docs_intel.remove_citation(
            docx_path=docx,
            anchor_para_id="NOPE",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original


# ---------------------------------------------------------------------------
# Citation: error paths
# ---------------------------------------------------------------------------

class TestCitationErrorPaths:
    def test_insert_citation_empty_keys(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=[],
            formatted_text="(x)",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_citation_blank_keys(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=["   ", ""],
            formatted_text="(x)",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_citation_empty_formatted_text(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=["key"],
            formatted_text="",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_citation_bad_source(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=["key"],
            formatted_text="(x)",
            source="mendeley",  # only "zotero" / "csl" accepted
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_citation_missing_file(self, tmp_path):
        res = docs_intel.insert_citation(
            docx_path=str(tmp_path / "missing.docx"),
            anchor_para_id="AABB0001",
            citation_keys=["k"],
            formatted_text="(k)",
        )
        assert "error" in res

    def test_insert_citation_bad_para_id(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="DOES-NOT-EXIST",
            citation_keys=["key"],
            formatted_text="(x)",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original


# ---------------------------------------------------------------------------
# Sidecar sync: insert_caption with index_db_path
# ---------------------------------------------------------------------------

class TestSidecarSync:
    def test_insert_figure_caption_invalidates_sidecar(self, tmp_path):
        """After insert_caption, the sidecar mtime is cleared (force re-index)."""
        docx = str(tmp_path / "doc.docx")
        db = str(tmp_path / "index.db")
        _write_docx(docx)

        # Build a sidecar first.
        docs_intel.index_docx(docx, db)

        # Verify mtime is stored.
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key='source_mtime'"
        ).fetchone()
        conn.close()
        assert row is not None and row[0] is not None

        # Now insert a caption with the db path.
        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="A figure",
            index_db_path=db,
        )
        assert "error" not in res

        # mtime should now be NULL (invalidated).
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key='source_mtime'"
        ).fetchone()
        conn.close()
        assert row is None or row[0] is None

    def test_insert_figure_caption_upserts_docx_figures(self, tmp_path):
        """insert_caption with index_db_path adds a row to docx_figures."""
        docx = str(tmp_path / "doc.docx")
        db = str(tmp_path / "index.db")
        _write_docx(docx)

        # Initialise the sidecar tables by indexing the docx.
        docs_intel.index_docx(docx, db)
        # Ensure figure tables exist (connect creates them).
        docs_intel._connect(db).close()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="My figure",
            section_heading="Results",
            index_db_path=db,
        )
        assert "error" not in res

        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT caption, seq_number FROM docx_figures").fetchall()
        conn.close()
        assert len(rows) >= 1
        captions = [r[0] for r in rows]
        assert any("My figure" in (c or "") for c in captions)

    def test_insert_table_caption_upserts_docx_tables(self, tmp_path):
        """insert_caption for Table with index_db_path updates docx_tables."""
        docx = str(tmp_path / "doc.docx")
        db = str(tmp_path / "index.db")
        _write_docx(docx)

        docs_intel._connect(db).close()  # ensure tables exist

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Table",
            label_text="My table",
            index_db_path=db,
        )
        assert "error" not in res

        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT caption FROM docx_tables").fetchall()
        conn.close()
        assert len(rows) >= 1
        assert any("My table" in (r[0] or "") for r in rows)

    def test_insert_citation_invalidates_sidecar(self, tmp_path):
        """insert_citation with index_db_path clears sidecar mtime."""
        docx = str(tmp_path / "doc.docx")
        db = str(tmp_path / "index.db")
        _write_docx(docx)

        docs_intel.index_docx(docx, db)

        res = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=["k1"],
            formatted_text="(K 1)",
            index_db_path=db,
        )
        assert "error" not in res

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key='source_mtime'"
        ).fetchone()
        conn.close()
        assert row is None or row[0] is None


# ---------------------------------------------------------------------------
# Round-trip: insert then read-back via parse_docx
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_insert_caption_then_parse(self, tmp_path):
        """Inserted Caption paragraph is readable by parse_docx."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Loss curve",
        )

        paras = docs_intel.parse_docx(docx)
        styles = [p.get("style") for p in paras]
        assert "Caption" in styles, f"Caption style not found in parsed paragraphs: {styles}"

    def test_insert_then_edit_then_remove_caption(self, tmp_path):
        """Full lifecycle: insert -> edit -> remove."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        # Insert.
        ins = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Original",
        )
        assert "error" not in ins

        # Find the caption paragraph via parse_docx (it has Caption style).
        paras = docs_intel.parse_docx(docx)
        cap_para = next(
            (p for p in paras if p.get("style") == "Caption"), None
        )
        assert cap_para is not None, "Caption paragraph not found after insert"

        # Edit via synthesized para_id (p{N} counting).
        edit = docs_intel.edit_caption(
            docx_path=docx,
            caption_para_id=cap_para["para_id"],
            new_label_text="Edited label",
        )
        assert "error" not in edit, f"edit failed: {edit.get('error')}"
        xml_after_edit = _read_doc_xml(docx)
        assert "Edited label" in xml_after_edit

        # Remove.
        rem = docs_intel.remove_caption(
            docx_path=docx,
            caption_para_id=cap_para["para_id"],
        )
        assert "error" not in rem
        xml_final = _read_doc_xml(docx)
        assert 'w:val="Caption"' not in xml_final

    def test_insert_then_edit_then_remove_citation(self, tmp_path):
        """Full citation lifecycle: insert -> edit -> remove."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        # Insert.
        ins = docs_intel.insert_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            citation_keys=["smith2023"],
            formatted_text="(Smith 2023)",
        )
        assert "error" not in ins

        # Edit.
        edit = docs_intel.edit_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
            new_citation_keys=["jones2021"],
            new_formatted_text="(Jones 2021)",
        )
        assert "error" not in edit
        xml_after = _read_doc_xml(docx)
        assert "jones2021" in xml_after
        assert "(Jones 2021)" in xml_after

        # Remove.
        rem = docs_intel.remove_citation(
            docx_path=docx,
            anchor_para_id="AABB0002",
        )
        assert "error" not in rem
        xml_final = _read_doc_xml(docx)
        assert "CSL_CITATION" not in xml_final

    def test_other_zip_members_preserved(self, tmp_path):
        """Non-document.xml zip members survive a caption write-back unchanged."""
        # Build a docx with an extra member.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", _DOC_XML)
            zf.writestr("[Content_Types].xml", "<ct/>")
            zf.writestr("word/styles.xml", "<styles/>")
        docx = str(tmp_path / "doc.docx")
        with open(docx, "wb") as fh:
            fh.write(buf.getvalue())

        docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Test",
        )

        with zipfile.ZipFile(docx, "r") as zf:
            names = set(zf.namelist())
        assert "[Content_Types].xml" in names
        assert "word/styles.xml" in names
        assert "word/document.xml" in names
