"""Tests for meridian_docs.docs_intel.relocate_table (c031622b).

relocate_table moves a bare <w:tbl> with no owning heading to a new location
in the document, atomically -- the sibling primitive to move_section
(6ff24136), but addressing its SOURCE by 0-based body-child position
(table_index) instead of a heading's para_id, since a bare table has no
heading to anchor on.

All tests are pure Python (stdlib + pytest) -- no mcp, no network. Follows
the same conventions as test_docs_intel_new_primitives.py (which covers
move_section / copy_section): tests that mutate write a minimal .docx to
tmp_path first.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


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


def _body_tags_and_ids(path: str) -> list[tuple[str, str | None]]:
    """[(local_tag, w14:paraId or None), ...] in document order, post-write."""
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    return [
        (el.tag.rsplit("}", 1)[-1], el.get(docs_intel._q(_W14, "paraId")))
        for el in body
    ]


# ---------------------------------------------------------------------------
# Successful relocation: preserves content + formatting (w:tblPr/w:tblGrid)
# ---------------------------------------------------------------------------

_SIMPLE_TABLE_DOC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Intro paragraph.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
      <w:tblGrid>
        <w:gridCol w:w="2000"/>
        <w:gridCol w:w="2000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A2</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Middle paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>End paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_relocate_table_moves_and_preserves_formatting(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML)

    result = docs_intel.relocate_table(path, 1, "P0000003", destination_position="after")
    assert result["status"] == "moved"
    assert result["table_index"] == 1
    assert result["row_count"] == 2
    assert result["col_count"] == 2

    tags_and_ids = _body_tags_and_ids(path)
    # Table now sits right after P0000003 (before the trailing sectPr).
    order = [tid for _tag, tid in tags_and_ids if tid]
    assert order == ["P0000001", "P0000002", "P0000003"]
    tbl_pos = next(i for i, (tag, _tid) in enumerate(tags_and_ids) if tag == "tbl")
    assert tags_and_ids[tbl_pos - 1] == ("p", "P0000003")
    assert tags_and_ids[tbl_pos + 1][0] == "sectPr"

    # Formatting + content preserved verbatim.
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    tbl = body[tbl_pos]
    tbl_pr = tbl.find(docs_intel._q(_W, "tblPr"))
    tbl_style = tbl_pr.find(docs_intel._q(_W, "tblStyle"))
    assert tbl_style.get(docs_intel._q(_W, "val")) == "TableGrid"
    grid_cols = tbl.find(docs_intel._q(_W, "tblGrid")).findall(docs_intel._q(_W, "gridCol"))
    assert len(grid_cols) == 2
    cell_texts = [
        "".join(t.text or "" for t in tc.iter(docs_intel._q(_W, "t")))
        for tr in tbl.findall(docs_intel._q(_W, "tr"))
        for tc in tr.findall(docs_intel._q(_W, "tc"))
    ]
    assert cell_texts == ["A1", "B1", "A2", "B2"]


def test_relocate_table_before_anchor(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML, name="before.docx")
    result = docs_intel.relocate_table(path, 1, "P0000001", destination_position="before")
    assert result["status"] == "moved"

    tags_and_ids = _body_tags_and_ids(path)
    assert tags_and_ids[0][0] == "tbl"
    assert tags_and_ids[1] == ("p", "P0000001")


# ---------------------------------------------------------------------------
# Error cases: invalid source selector
# ---------------------------------------------------------------------------

def test_relocate_table_rejects_table_index_out_of_range(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML, name="oor.docx")
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.relocate_table(path, 99, "P0000001")
    assert "error" in result
    assert "out of range" in result["error"]

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_relocate_table_rejects_non_table_index(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML, name="nottbl.docx")
    # body index 0 is a <w:p>, not a <w:tbl>.
    result = docs_intel.relocate_table(path, 0, "P0000003")
    assert "error" in result
    assert "not a <w:tbl>" in result["error"]


def test_relocate_table_rejects_negative_table_index(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML, name="neg.docx")
    result = docs_intel.relocate_table(path, -1, "P0000003")
    assert "error" in result


# ---------------------------------------------------------------------------
# Error cases: invalid destination selector
# ---------------------------------------------------------------------------

def test_relocate_table_rejects_unknown_destination(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML, name="unknown_dest.docx")
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.relocate_table(path, 1, "NOPE")
    assert "error" in result
    assert "not found" in result["error"]

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_relocate_table_rejects_bad_destination_position(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML, name="badpos.docx")
    result = docs_intel.relocate_table(path, 1, "P0000001", destination_position="sideways")
    assert "error" in result


_TABLE_WITH_ADDRESSABLE_CELL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Before table.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr><w:tc><w:p w14:paraId="CELLP0001"><w:r><w:t>Cell text.</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>After table.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_relocate_table_rejects_destination_inside_table_being_moved(tmp_path):
    path = _write_docx(tmp_path, _TABLE_WITH_ADDRESSABLE_CELL_XML, name="inside.docx")
    # CELLP0001 is a paragraph INSIDE the table at index 1 -- anchoring on it
    # resolves to the very table being relocated.
    result = docs_intel.relocate_table(path, 1, "CELLP0001")
    assert "error" in result
    assert "being relocated" in result["error"]


# ---------------------------------------------------------------------------
# Edge case: table at the very start of the document, moved to the very end
# ---------------------------------------------------------------------------

_TABLE_AT_START_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Only cell.</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>First paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Second paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_relocate_table_from_start_to_end_of_document(tmp_path):
    path = _write_docx(tmp_path, _TABLE_AT_START_XML, name="start_to_end.docx")

    result = docs_intel.relocate_table(path, 0, "P0000002", destination_position="after")
    assert result["status"] == "moved"

    tags_and_ids = _body_tags_and_ids(path)
    # Table is now the last real body child, immediately before the trailing sectPr.
    assert tags_and_ids[0] == ("p", "P0000001")
    assert tags_and_ids[1] == ("p", "P0000002")
    assert tags_and_ids[2][0] == "tbl"
    assert tags_and_ids[3][0] == "sectPr"


# ---------------------------------------------------------------------------
# Edge case: table cell holds a relationship reference (image r:embed) --
# must survive the move byte-for-byte (never renamed/reparented).
# ---------------------------------------------------------------------------

_TABLE_WITH_IMAGE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Before table.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="3000"/></w:tblGrid>
      <w:tr>
        <w:tc>
          <w:p>
            <w:r>
              <w:drawing xmlns:r="{_R_NS}">
                <a:blip xmlns:a="{_A_NS}" r:embed="rId7"/>
              </w:drawing>
            </w:r>
          </w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>After table.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_relocate_table_preserves_image_relationship_reference(tmp_path):
    path = _write_docx(tmp_path, _TABLE_WITH_IMAGE_XML, name="image.docx")

    result = docs_intel.relocate_table(path, 1, "P0000002", destination_position="after")
    assert result["status"] == "moved"

    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    blip = next(iter(body.iter(f"{{{_A_NS}}}blip")), None)
    assert blip is not None
    assert blip.get(f"{{{_R_NS}}}embed") == "rId7"


# ---------------------------------------------------------------------------
# 027b7ada-style destination fix: a HEADING anchor + destination_position=
# "after" lands the table after that heading's ENTIRE section, not
# immediately after the heading paragraph itself (same fix move_section /
# copy_section rely on, reusing _locate_section_bounds).
# ---------------------------------------------------------------------------

_TABLE_AND_TWO_SECTIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Table cell.</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Results body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000003">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Sub-results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>Sub-results body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000004">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000004">
      <w:r><w:t>Conclusion body paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_relocate_table_after_heading_anchor_lands_after_whole_section(tmp_path):
    path = _write_docx(tmp_path, _TABLE_AND_TWO_SECTIONS_XML, name="afterheading.docx")

    result = docs_intel.relocate_table(path, 1, "H0000002", destination_position="after")
    assert result["status"] == "moved"

    tags_and_ids = _body_tags_and_ids(path)
    ordered_ids = [tid for _tag, tid in tags_and_ids if tid]
    # Results' own body (P0000002) and its Sub-results subsection (H0000003 +
    # P0000003) must all still precede the relocated table -- it must NOT
    # land immediately after the Results heading, swallowing its body.
    assert ordered_ids.index("P0000002") < ordered_ids.index("H0000004")
    assert ordered_ids.index("P0000003") < ordered_ids.index("H0000004")
    tbl_pos = next(i for i, (tag, _tid) in enumerate(tags_and_ids) if tag == "tbl")
    assert tags_and_ids[tbl_pos - 1] == ("p", "P0000003")
    assert tags_and_ids[tbl_pos + 1] == ("p", "H0000004")


# ---------------------------------------------------------------------------
# e87b8338-style safety check reused via _bookmarks_split_by_range: a
# bookmark that starts BEFORE the table and ends INSIDE one of its cells
# would be torn apart by the move -- must gate BEFORE any write, and the
# override flag must allow it through explicitly.
# ---------------------------------------------------------------------------

_TABLE_SPLIT_BOOKMARK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:bookmarkStart w:id="42" w:name="_Ref999999999"/>
      <w:r><w:t>Before table, bookmark starts here.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:t>Cell text.</w:t></w:r><w:bookmarkEnd w:id="42"/></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>After table.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_relocate_table_gates_before_write_on_bookmark_split(tmp_path):
    path = _write_docx(tmp_path, _TABLE_SPLIT_BOOKMARK_XML, name="split.docx")
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.relocate_table(path, 1, "P0000002", destination_position="after")
    assert "error" in result
    assert result["split_bookmarks"] == ["_Ref999999999"]

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes, "file was mutated despite the gate rejecting the move"


def test_relocate_table_allow_bookmark_split_override(tmp_path):
    path = _write_docx(tmp_path, _TABLE_SPLIT_BOOKMARK_XML, name="split_override.docx")
    result = docs_intel.relocate_table(
        path, 1, "P0000002", destination_position="after", allow_bookmark_split=True,
    )
    assert result["status"] == "moved"


# ---------------------------------------------------------------------------
# Missing file / malformed docx
# ---------------------------------------------------------------------------

def test_relocate_table_missing_file_errors():
    result = docs_intel.relocate_table("C:/nonexistent/path/doc.docx", 0, "P1")
    assert "error" in result
