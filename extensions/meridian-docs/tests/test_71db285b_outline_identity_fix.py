"""Regression test for 71db285b.

BUG: document_outline (via parse_docx) and move_section/copy_section/
_locate_section_bounds disagreed on paragraph identity for any docx without
real w14:paraId. parse_docx used a naive f"p{index}" position counter, while
move_section/copy_section/_locate_section_bounds resolve ids against
_vendored_content_tree._build_synth_id_map's content-hash "sp<hash>" scheme
(heading breadcrumb + normalized paragraph text + occurrence counter). A
caller who discovered a heading's id via document_outline (the natural,
documented way to discover ids) and then passed that id straight into
move_section/copy_section would get a "not found" style failure on any real
document that lacks a native w14:paraId on every paragraph -- which is most
real Word documents, since Word does not assign w14:paraId to every
paragraph.

FIX: parse_docx now calls _build_synth_id_map(body) once up front and uses
the same three-tier id resolution (native w14:paraId -> synth_map -> f"p{N}")
that _paragraph_node / _find_para_by_id / _locate_section_bounds already use.

These tests build a document with NO native w14:paraId anywhere, discover a
heading's id via document_outline (the code path that was broken), and then
feed that exact id into move_section / copy_section -- proving they now
resolve identically instead of only working after a totally different id
was manually looked up via index_docx_structure.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel
from meridian_docs._vendored_content_tree import _build_synth_id_map


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


# No paragraph anywhere carries a native w14:paraId -- the exact scenario the
# bug report calls out ("any docx without real w14:paraId").
_NO_NATIVE_IDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Intro body paragraph.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Setup</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Setup body paragraph.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Conclusion body paragraph.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_parse_docx_ids_match_synth_id_map_directly():
    """parse_docx's para_id must equal what _build_synth_id_map assigns to the
    same live paragraph element -- not a bare positional f"p{N}"."""
    docx_bytes = _make_docx_bytes(_NO_NATIVE_IDS_XML)
    root = docs_intel.ET.fromstring(
        zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml")
    )
    body = root.find(docs_intel._q(_W, "body"))
    synth_map = _build_synth_id_map(body)
    expected_ids = [synth_map[id(p)] for p in body.findall(docs_intel._q(_W, "p"))]
    assert all(eid.startswith("sp") for eid in expected_ids)

    paras = docs_intel.parse_docx(docx_bytes)
    actual_ids = [p["para_id"] for p in paras]
    assert actual_ids == expected_ids
    # Confirm this is really exercising the fix, not accidentally passing:
    # the old buggy behavior would have produced positional ids instead.
    assert actual_ids != [f"p{i}" for i in range(len(paras))]


def test_document_outline_ids_are_synth_ids_not_positional():
    docx_bytes = _make_docx_bytes(_NO_NATIVE_IDS_XML)
    outline = docs_intel.document_outline(docx_bytes)
    heading_ids = [h["para_id"] for h in outline["headings"]]
    assert len(heading_ids) == 3
    assert all(hid.startswith("sp") for hid in heading_ids)


def test_move_section_accepts_document_outline_discovered_id(tmp_path):
    """The end-to-end regression: discover an id the way a real caller would
    (document_outline), then feed it straight into move_section. Before the
    fix, this id would not resolve against _locate_section_bounds's
    synth-id-based scan and the move would either fail outright or (worse)
    silently target the wrong paragraph."""
    path = _write_docx(tmp_path, _NO_NATIVE_IDS_XML, name="no_native_ids.docx")

    outline_before = docs_intel.document_outline(path)
    by_text = {h["text"]: h["para_id"] for h in outline_before["headings"]}
    setup_id = by_text["Setup"]
    conclusion_id = by_text["Conclusion"]
    assert setup_id.startswith("sp")
    assert conclusion_id.startswith("sp")

    result = docs_intel.move_section(path, setup_id, conclusion_id, destination_position="before")
    assert result["status"] == "moved"
    assert result["moved_block_count"] == 2  # Setup heading + its body paragraph

    outline_after = docs_intel.document_outline(path)
    order = [h["text"] for h in outline_after["headings"]]
    assert order == ["Introduction", "Setup", "Conclusion"]


def test_copy_section_accepts_document_outline_discovered_id(tmp_path):
    path = _write_docx(tmp_path, _NO_NATIVE_IDS_XML, name="no_native_ids_copy.docx")

    outline_before = docs_intel.document_outline(path)
    by_text = {h["text"]: h["para_id"] for h in outline_before["headings"]}
    intro_id = by_text["Introduction"]
    conclusion_id = by_text["Conclusion"]

    result = docs_intel.copy_section(path, intro_id, conclusion_id, destination_position="after")
    assert result["status"] == "copied"

    outline_after = docs_intel.document_outline(path)
    texts = [h["text"] for h in outline_after["headings"]]
    assert texts.count("Introduction") == 2
    assert texts == ["Introduction", "Setup", "Conclusion", "Introduction"]
