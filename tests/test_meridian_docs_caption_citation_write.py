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

1c59cb90 — also exercises insert_cross_reference (REFILED from 7b5bfb00):
  - insert_caption now embeds a "_Ref<digits>" bookmark around the caption's
    "<Kind> <N>" text and returns it as ref_bookmark.
  - insert_cross_reference builds a REF complex field targeting that bookmark,
    resolvable via target_caption_para_id OR bookmark_name.
  - Retrofit path: a pre-1c59cb90 caption (no bookmark) gets one created
    on-demand the first time it's cross-referenced by para_id.
  - Trailing-space handling, and error paths (missing bookmark/target/anchor,
    both-or-neither identifier args, non-caption target).

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


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """016015e1/ddd79188 -- insert_caption now invokes the real
    render-capability gate (render_gate.check_render_capability) AFTER
    structural verification passes. Tests in this file exercise STRUCTURAL
    correctness and must not depend on -- or be slowed/blocked by --
    whichever render backends (LibreOffice, Word COM) happen to be
    installed on the machine running the suite. Stub a successful
    'rendered' result by default, mirroring
    test_19be1551_insert_figure_block.py's fixture of the same name.
    """
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {
            "status": "rendered",
            "backend": "test-stub",
            "detail": {"stub": True},
        },
    )


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

    def test_insert_figure_caption_before_rejected(self, tmp_path):
        """position='before' is invalid for Figure captions and must return an error."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Before anchor",
            position="before",
        )

        # Must return an error, not succeed.
        assert "error" in res, f"expected error for Figure+before, got: {res}"
        assert "figure" in res["error"].lower() or "before" in res["error"].lower()
        # File must be byte-for-byte unchanged on failure.
        assert open(docx, "rb").read() == original, "file was mutated despite error"

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

    def test_insert_table_caption_before_is_valid(self, tmp_path):
        """Regression: Table captions must still support position='before' (unaffected by Figure fix)."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Table",
            label_text="My table above",
            position="before",
        )

        assert "error" not in res, f"unexpected error for Table+before: {res.get('error')}"
        assert res["status"] == "inserted"
        assert res["kind"] == "Table"
        xml = _read_doc_xml(docx)
        assert "SEQ Table" in xml
        assert "My table above" in xml

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

        # Edit via the id parse_docx just resolved (native w14:paraId when
        # present, else the content-derived synth id -- see 71db285b).
        edit = docs_intel.edit_caption(
            docx_path=docx,
            caption_para_id=cap_para["para_id"],
            new_label_text="Edited label",
        )
        assert "error" not in edit, f"edit failed: {edit.get('error')}"
        xml_after_edit = _read_doc_xml(docx)
        assert "Edited label" in xml_after_edit

        # 71db285b -- this paragraph has no native w14:paraId, so its id is
        # the content-derived synth id (heading breadcrumb + normalized TEXT
        # + occurrence). edit_caption just changed that very text, which
        # changes the paragraph's OWN synth id (by design -- the id is a hash
        # of current content, not a positional counter). Re-resolve the
        # caption's CURRENT id post-edit rather than reusing the pre-edit one,
        # exactly as a real caller must after any content-changing write.
        paras_after_edit = docs_intel.parse_docx(docx)
        cap_para_after_edit = next(
            (p for p in paras_after_edit if p.get("style") == "Caption"), None
        )
        assert cap_para_after_edit is not None, "Caption paragraph not found after edit"

        # Remove.
        rem = docs_intel.remove_caption(
            docx_path=docx,
            caption_para_id=cap_para_after_edit["para_id"],
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


# ---------------------------------------------------------------------------
# Synthetic .docx fixtures for image-paragraph detection tests
# ---------------------------------------------------------------------------

# A document with two image paragraphs: one DrawingML (<w:drawing>) and one
# legacy VML (<w:pict>).  Two plain text paragraphs surround them.
_DOC_XML_WITH_IMAGES = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
  <w:body>
    <w:p w14:paraId="IMG00001">
      <w:r><w:t>Introduction paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="IMG00002">
      <w:r>
        <w:drawing>
          <wp:inline><wp:extent cx="1000000" cy="500000"/></wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
    <w:p w14:paraId="IMG00003">
      <w:r><w:t>A text paragraph between images.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="IMG00004">
      <w:r>
        <w:pict>
          <v:shape xmlns:v="urn:schemas-microsoft-com:vml" id="pic1" type="#_x0000_t75"/>
        </w:pict>
      </w:r>
    </w:p>
    <w:p w14:paraId="IMG00005">
      <w:r><w:t>Conclusion paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# A document with no images at all (for the empty-result case).
_DOC_XML_NO_IMAGES = _DOC_XML  # reuse the basic 3-paragraph fixture


def _write_docx_images(path: str) -> None:
    """Write the image-containing synthetic .docx to disk."""
    with open(path, "wb") as fh:
        fh.write(_zip_docx(_DOC_XML_WITH_IMAGES))


# ---------------------------------------------------------------------------
# find_image_paragraph tests
# ---------------------------------------------------------------------------

class TestFindImageParagraph:
    """Tests for docs_intel.find_image_paragraph."""

    def test_finds_drawing_paragraph(self, tmp_path):
        """DrawingML <w:drawing> paragraph is detected and its para_id returned."""
        docx = str(tmp_path / "doc.docx")
        _write_docx_images(docx)

        res = docs_intel.find_image_paragraph(docx)

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["count"] == 2, f"expected 2 image paragraphs, got {res['count']}"
        paras = res["image_paragraphs"]
        assert len(paras) == 2
        # First image is the DrawingML one.
        assert paras[0]["para_id"] == "IMG00002"

    def test_finds_pict_paragraph(self, tmp_path):
        """Legacy VML <w:pict> paragraph is also detected."""
        docx = str(tmp_path / "doc.docx")
        _write_docx_images(docx)

        res = docs_intel.find_image_paragraph(docx)

        assert "error" not in res
        paras = res["image_paragraphs"]
        # Second image is the VML pict one.
        assert paras[1]["para_id"] == "IMG00004"

    def test_no_images_returns_empty_list(self, tmp_path):
        """Document with no images returns count=0 and empty list."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_NO_IMAGES)

        res = docs_intel.find_image_paragraph(docx)

        assert "error" not in res
        assert res["count"] == 0
        assert res["image_paragraphs"] == []

    def test_figure_index_returns_single_entry(self, tmp_path):
        """figure_index=1 returns the first image paragraph's para_id directly."""
        docx = str(tmp_path / "doc.docx")
        _write_docx_images(docx)

        res = docs_intel.find_image_paragraph(docx, figure_index=1)

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["para_id"] == "IMG00002"
        assert res["figure_index"] == 1

    def test_figure_index_selects_second(self, tmp_path):
        """figure_index=2 returns the second image paragraph."""
        docx = str(tmp_path / "doc.docx")
        _write_docx_images(docx)

        res = docs_intel.find_image_paragraph(docx, figure_index=2)

        assert "error" not in res
        assert res["para_id"] == "IMG00004"
        assert res["figure_index"] == 2

    def test_figure_index_out_of_range(self, tmp_path):
        """figure_index beyond the number of images returns an error."""
        docx = str(tmp_path / "doc.docx")
        _write_docx_images(docx)

        res = docs_intel.find_image_paragraph(docx, figure_index=99)

        assert "error" in res
        assert "out of range" in res["error"]

    def test_figure_index_zero_is_invalid(self, tmp_path):
        """figure_index=0 is out-of-range (1-based indexing)."""
        docx = str(tmp_path / "doc.docx")
        _write_docx_images(docx)

        res = docs_intel.find_image_paragraph(docx, figure_index=0)

        assert "error" in res

    def test_missing_file_returns_error(self, tmp_path):
        """Nonexistent file returns {"error": ...}."""
        res = docs_intel.find_image_paragraph(str(tmp_path / "nonexistent.docx"))

        assert "error" in res

    def test_image_para_id_is_usable_as_anchor(self, tmp_path):
        """The returned para_id can be passed directly to insert_caption (anchor after image)."""
        docx = str(tmp_path / "doc.docx")
        _write_docx_images(docx)

        # Find the first image paragraph.
        find_res = docs_intel.find_image_paragraph(docx, figure_index=1)
        assert "error" not in find_res
        anchor_id = find_res["para_id"]

        # Insert a Figure caption using the image's own para_id as anchor.
        cap_res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id=anchor_id,
            kind="Figure",
            label_text="My detected figure",
        )
        assert "error" not in cap_res, f"insert_caption failed: {cap_res.get('error')}"
        assert cap_res["status"] == "inserted"

        # Verify the caption paragraph follows the image paragraph in document order.
        paras = docs_intel.parse_docx(docx)
        img_idx = next(
            (i for i, p in enumerate(paras) if p["para_id"] == anchor_id), None
        )
        cap_idx = next(
            (i for i, p in enumerate(paras) if p.get("style") == "Caption"), None
        )
        assert img_idx is not None, "image paragraph not found after insert"
        assert cap_idx is not None, "Caption paragraph not found after insert"
        assert cap_idx == img_idx + 1, (
            f"Caption (index {cap_idx}) is not immediately after image (index {img_idx})"
        )


# ---------------------------------------------------------------------------
# Figure-before rejection: API surface guard
# ---------------------------------------------------------------------------

class TestFigureCaptionBeforeRejection:
    """Dedicated tests for the Figure+before=error behavior."""

    def test_figure_before_is_rejected(self, tmp_path):
        """position='before' for kind='Figure' must return {error: ...}."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0001",
            kind="Figure",
            label_text="Should never land above image",
            position="before",
        )

        assert "error" in res
        # The error message should mention the constraint clearly.
        assert "before" in res["error"].lower() or "figure" in res["error"].lower()
        # File must be unchanged — no partial write on error.
        assert open(docx, "rb").read() == original

    def test_figure_after_is_still_accepted(self, tmp_path):
        """position='after' (default) for kind='Figure' must still work."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Normal figure caption",
            position="after",
        )

        assert "error" not in res
        assert res["status"] == "inserted"

    def test_figure_default_position_is_after(self, tmp_path):
        """Omitting position for kind='Figure' defaults to 'after' (still valid)."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Default position figure",
        )

        assert "error" not in res
        assert res["status"] == "inserted"

    def test_table_before_is_accepted(self, tmp_path):
        """Regression: Table captions are not affected — position='before' must still work."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Table",
            label_text="Table before anchor",
            position="before",
        )

        assert "error" not in res
        assert res["status"] == "inserted"
        assert res["kind"] == "Table"

    def test_table_after_is_accepted(self, tmp_path):
        """Regression: Table captions with position='after' must still work."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Table",
            label_text="Table after anchor",
            position="after",
        )

        assert "error" not in res
        assert res["status"] == "inserted"


# ---------------------------------------------------------------------------
# Cross-reference: REF-field mechanism (1c59cb90, REFILED from 7b5bfb00)
# ---------------------------------------------------------------------------

class TestInsertCaptionRefBookmark:
    """insert_caption now embeds a _Ref cross-reference bookmark natively."""

    def test_insert_caption_returns_ref_bookmark(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Loss curve",
        )

        assert "error" not in res
        assert res["ref_bookmark"].startswith("_Ref")

        xml = _read_doc_xml(docx)
        assert f'w:name="{res["ref_bookmark"]}"' in xml
        assert "<w:bookmarkStart" in xml
        assert "<w:bookmarkEnd" in xml

    def test_two_captions_get_distinct_bookmarks(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        res1 = docs_intel.insert_caption(
            docx_path=docx, anchor_para_id="AABB0002", kind="Figure", label_text="One",
        )
        res2 = docs_intel.insert_caption(
            docx_path=docx, anchor_para_id="AABB0003", kind="Figure", label_text="Two",
        )
        assert "error" not in res1 and "error" not in res2
        assert res1["ref_bookmark"] != res2["ref_bookmark"]

    def test_ref_bookmark_persisted_in_sidecar(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        db = str(tmp_path / "index.db")
        _write_docx(docx)
        docs_intel._connect(db).close()

        res = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="A figure",
            index_db_path=db,
        )
        assert "error" not in res

        conn = sqlite3.connect(db)
        row = conn.execute("SELECT ref_bookmark FROM docx_figures").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == res["ref_bookmark"]


class TestInsertCrossReference:
    def test_insert_cross_reference_by_target_para_id(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        cap = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Figure",
            label_text="Loss curve",
        )
        assert "error" not in cap

        paras = docs_intel.parse_docx(docx)
        cap_para = next(p for p in paras if p.get("style") == "Caption")

        res = docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="AABB0003",
            target_caption_para_id=cap_para["para_id"],
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["status"] == "inserted"
        assert res["kind"] == "Figure"
        assert res["seq_number"] == "1"
        assert res["display_text"] == "Figure 1"
        assert res["bookmark_name"] == cap["ref_bookmark"]

        xml = _read_doc_xml(docx)
        assert f"REF {cap['ref_bookmark']} \\h" in xml
        assert 'fldCharType="begin"' in xml
        assert 'fldCharType="separate"' in xml
        assert 'fldCharType="end"' in xml
        assert "Figure 1" in xml

    def test_insert_cross_reference_by_bookmark_name(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        cap = docs_intel.insert_caption(
            docx_path=docx,
            anchor_para_id="AABB0002",
            kind="Table",
            label_text="Summary stats",
        )
        assert "error" not in cap

        res = docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="AABB0003",
            bookmark_name=cap["ref_bookmark"],
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["kind"] == "Table"
        assert res["display_text"] == "Table 1"
        xml = _read_doc_xml(docx)
        assert f"REF {cap['ref_bookmark']} \\h" in xml

    def test_retrofits_bookmark_on_pre_existing_caption(self, tmp_path):
        """A caption built without a ref bookmark (pre-1c59cb90 shape) gets one
        created on demand the first time it's cross-referenced by para_id."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)
        xml_before = _read_doc_xml(docx)
        assert "bookmarkStart" not in xml_before

        res = docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="CCDD0003",
            target_caption_para_id="CCDD0002",
        )

        assert "error" not in res, f"unexpected error: {res.get('error')}"
        assert res["bookmark_name"].startswith("_Ref")
        assert res["kind"] == "Figure"
        assert res["seq_number"] == "1"
        assert res["display_text"] == "Figure 1"

        xml_after = _read_doc_xml(docx)
        assert f'w:name="{res["bookmark_name"]}"' in xml_after
        assert f"REF {res['bookmark_name']} \\h" in xml_after

    def test_inserts_separating_space_before_field(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        cap = docs_intel.insert_caption(
            docx_path=docx, anchor_para_id="AABB0002", kind="Figure", label_text="X",
        )

        docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="AABB0003",  # "A second body paragraph." — no trailing space
            bookmark_name=cap["ref_bookmark"],
        )

        xml = _read_doc_xml(docx)
        assert "A second body paragraph." in xml and "Figure 1" in xml
        # A dedicated preserved-space run must precede the field runs.
        assert 'xml:space="preserve">' in xml

    def test_requires_exactly_one_target_identifier(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()

        res_neither = docs_intel.insert_cross_reference(
            docx_path=docx, anchor_para_id="AABB0002",
        )
        assert "error" in res_neither

        res_both = docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="AABB0002",
            target_caption_para_id="AABB0003",
            bookmark_name="_Ref100000000",
        )
        assert "error" in res_both
        assert open(docx, "rb").read() == original

    def test_unknown_anchor_para_id(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)
        original = open(docx, "rb").read()

        res = docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="BOGUS",
            target_caption_para_id="CCDD0002",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_unknown_target_caption_para_id(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)
        original = open(docx, "rb").read()

        res = docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="CCDD0003",
            target_caption_para_id="BOGUS",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_target_para_is_not_a_caption(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)
        original = open(docx, "rb").read()

        res = docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="CCDD0003",
            target_caption_para_id="CCDD0001",  # plain paragraph, not a caption
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_unknown_bookmark_name(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CAPTION)
        original = open(docx, "rb").read()

        res = docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="CCDD0003",
            bookmark_name="_Ref999999999",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_missing_file(self, tmp_path):
        res = docs_intel.insert_cross_reference(
            docx_path=str(tmp_path / "nonexistent.docx"),
            anchor_para_id="AABB0002",
            bookmark_name="_Ref100000000",
        )
        assert "error" in res

    def test_reference_survives_after_reindex_read(self, tmp_path):
        """Round-trip: the REF field's cached display text is parseable back out."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        cap = docs_intel.insert_caption(
            docx_path=docx, anchor_para_id="AABB0002", kind="Figure", label_text="X",
        )
        docs_intel.insert_cross_reference(
            docx_path=docx,
            anchor_para_id="AABB0003",
            bookmark_name=cap["ref_bookmark"],
        )

        paras = docs_intel.parse_docx(docx)
        joined = " ".join(p.get("text", "") for p in paras)
        assert "Figure 1" in joined
