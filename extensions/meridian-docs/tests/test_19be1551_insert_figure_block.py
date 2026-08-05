"""Tests for insert_figure_block: atomic centered image + adjacent SEQ Figure
caption insertion in a single document-load-mutate-save transaction.

Fixture/style conventions mirror test_insert_image.py (image packaging /
relationship checks) and test_relocate_figure.py (image+caption body-order
assertions, sidecar wiring).
"""
from __future__ import annotations

import sqlite3
import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel, server


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """ddd79188 -- insert_figure_block now invokes the real render-capability
    gate (render_gate.check_render_capability) AFTER structural verification
    passes. Every test in this file exercises STRUCTURAL correctness and
    must not depend on -- or be slowed/blocked by -- whichever render
    backends (LibreOffice, Word COM) happen to be installed on the machine
    running the suite. Stub a successful 'rendered' result by default so
    every existing assertion here keeps testing exactly what it tested
    before this gate existed. Tests that specifically exercise the render
    gate's own rendered/unavailable-with-reason/failed contract override
    this stub explicitly (see the "render-capability gate" section below).
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


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

_NS = f'xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}"'

_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Anchor</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''

_LEFT_ALIGNED_ANCHOR_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:pPr><w:jc w:val="left"/></w:pPr>
      <w:r><w:t>Anchor</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''

_NO_BODY_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}></w:document>
'''

_TABLE_ANCHOR_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p w14:paraId="CELL0001"><w:r><w:t>Cell text</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
'''

_VERIFY_BRANCHES_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="NODRAW01"><w:r><w:t>No image here</w:t></w:r></w:p>
    <w:p w14:paraId="LEFTIMG01">
      <w:pPr><w:jc w:val="left"/></w:pPr>
      <w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r>
    </w:p>
    <w:p w14:paraId="TBLIMG01">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      <w:r><w:drawing><a:blip r:embed="rId2"/></w:drawing></w:r>
    </w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:p w14:paraId="NOSEQIMG01">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      <w:r><w:drawing><a:blip r:embed="rId3"/></w:drawing></w:r>
    </w:p>
    <w:p w14:paraId="PLAINNEXT01"><w:r><w:t>no seq field here</w:t></w:r></w:p>
    <w:p w14:paraId="LASTIMG01">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      <w:r><w:drawing><a:blip r:embed="rId4"/></w:drawing></w:r>
    </w:p>
  </w:body>
</w:document>
'''

_EXISTING_FIGURE_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      <w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r>
    </w:p>
    <w:p w14:paraId="CAP000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:r><w:t xml:space="preserve">. Existing figure</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>Destination</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''

_RELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""

# 200 x 100 px PNG header; the pixel payload is irrelevant to OOXML packaging.
_PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    + (200).to_bytes(4, "big") + (100).to_bytes(4, "big") + b"payload"
)


def _write_docx(path, xml: str = _DOC_XML) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/_rels/document.xml.rels", _RELS_XML)
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)


def _setup(tmp_path, xml: str = _DOC_XML):
    docx_path = tmp_path / "report.docx"
    image_path = tmp_path / "chart.png"
    _write_docx(docx_path, xml)
    image_path.write_bytes(_PNG)
    return str(docx_path), str(image_path)


def _body_children(docx_path: str) -> list[ET.Element]:
    with zipfile.ZipFile(docx_path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
    return list(document.find(f"{{{_W}}}body"))


# ---------------------------------------------------------------------------
# Centered XML + relationship integrity
# ---------------------------------------------------------------------------

def test_insert_figure_block_centers_image_and_inserts_adjacent_caption(tmp_path):
    docx_path, image_path = _setup(tmp_path)

    result = docs_intel.insert_figure_block(
        docx_path,
        image_path,
        label_text="Loss curve for run 42",
        anchor_para_id="P0000001",
        position="after",
    )

    assert result["status"] == "inserted"
    assert result["image_para_id"]
    assert result["image_name"] == "word/media/image1.png"
    assert result["kind"] == "Figure"
    assert result["seq_number"] == 1
    assert result["label_text"] == "Loss curve for run 42"
    assert result["ref_bookmark"]

    children = _body_children(docx_path)
    # [Anchor, Image, Caption, sectPr]
    assert len(children) == 4
    image_para, caption_para = children[1], children[2]

    assert image_para.get(f"{{{_W14}}}paraId") == result["image_para_id"]
    jc = image_para.find(f"{{{_W}}}pPr/{{{_W}}}jc")
    assert jc is not None
    assert jc.get(f"{{{_W}}}val") == "center"

    # Caption immediately follows the image with nothing in between.
    fld = caption_para.find(f"{{{_W}}}fldSimple")
    assert fld is not None
    assert "SEQ Figure" in fld.get(f"{{{_W}}}instr", "")
    seq_text = fld.find(f".//{{{_W}}}t").text
    assert seq_text == "1"
    caption_text = "".join(t.text or "" for t in caption_para.iter(f"{{{_W}}}t"))
    assert caption_text == "Figure 1. Loss curve for run 42"


def test_insert_figure_block_relationship_and_media_integrity(tmp_path):
    docx_path, image_path = _setup(tmp_path)

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Loss curve", anchor_para_id="P0000001",
    )

    with zipfile.ZipFile(docx_path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        body = document.find(f"{{{_W}}}body")
        image_para = next(
            p for p in body
            if p.get(f"{{{_W14}}}paraId") == result["image_para_id"]
        )
        blip = image_para.find(f".//{{{_A}}}blip")
        assert blip is not None
        relationship_id = blip.get(f"{{{_R}}}embed")
        assert relationship_id == "rId1"
        assert archive.read(result["image_name"]) == _PNG

        rels = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        relationship = next(
            child for child in rels if child.get("Id") == relationship_id
        )
        assert relationship.get("Target") == "media/image1.png"

        content_types = ET.fromstring(archive.read("[Content_Types].xml"))
        png_default = next(
            child for child in content_types if child.get("Extension") == "png"
        )
        assert png_default.get("ContentType") == "image/png"


def test_insert_figure_block_increments_seq_number_over_existing_captions(tmp_path):
    docx_path, image_path = _setup(tmp_path, _EXISTING_FIGURE_DOC_XML)

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Second figure", anchor_para_id="P0000002",
    )

    assert result["status"] == "inserted"
    assert result["seq_number"] == 2


# ---------------------------------------------------------------------------
# Left-aligned source normalization
# ---------------------------------------------------------------------------

def test_insert_figure_block_centers_image_even_when_anchor_is_left_aligned(tmp_path):
    docx_path, image_path = _setup(tmp_path, _LEFT_ALIGNED_ANCHOR_DOC_XML)

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
        position="after",
    )

    assert result["status"] == "inserted"
    children = _body_children(docx_path)
    anchor_para, image_para = children[0], children[1]

    anchor_jc = anchor_para.find(f"{{{_W}}}pPr/{{{_W}}}jc")
    assert anchor_jc is not None and anchor_jc.get(f"{{{_W}}}val") == "left"

    image_jc = image_para.find(f"{{{_W}}}pPr/{{{_W}}}jc")
    assert image_jc is not None
    assert image_jc.get(f"{{{_W}}}val") == "center", (
        "the new image paragraph's own alignment must never inherit the "
        "left-aligned anchor paragraph's alignment"
    )


# ---------------------------------------------------------------------------
# Validation failures leave the document untouched
# ---------------------------------------------------------------------------

def test_insert_figure_block_rejects_bad_format_without_mutating_document(tmp_path):
    docx_path = tmp_path / "report.docx"
    image_path = tmp_path / "chart.svg"
    _write_docx(docx_path)
    image_path.write_text("<svg/>", encoding="utf-8")
    original = docx_path.read_bytes()

    result = docs_intel.insert_figure_block(
        str(docx_path), str(image_path), label_text="Chart",
    )

    assert "unsupported image format" in result["error"]
    assert docx_path.read_bytes() == original


def test_insert_figure_block_rejects_empty_label_text_without_mutating_document(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    original = (tmp_path / "report.docx").read_bytes()

    result = docs_intel.insert_figure_block(docx_path, image_path, label_text="   ")

    assert "label_text must be a non-empty string" in result["error"]
    assert (tmp_path / "report.docx").read_bytes() == original


def test_insert_figure_block_rejects_unknown_anchor_without_mutating_document(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    original = (tmp_path / "report.docx").read_bytes()

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="does-not-exist",
    )

    assert "not found" in result["error"]
    assert (tmp_path / "report.docx").read_bytes() == original


# ---------------------------------------------------------------------------
# Failed-write rollback
# ---------------------------------------------------------------------------

def test_insert_figure_block_verification_failure_restores_original_docx(tmp_path, monkeypatch):
    """A post-write verification failure must restore docx_path from the
    backup _save_docx_with_image already wrote, and report failure -- not a
    false success. Mirrors the established
    test_merge_draft_into_canonical_verification_failure_restores_canonical
    pattern (test_fe989980_merge_draft.py): force the verification helper to
    report a failure regardless of the (real, successful) write that just
    happened, then confirm the file is byte-for-byte restored -- so no
    orphan image, no orphan caption, and no corrupted document remain."""
    docx_path, image_path = _setup(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel, "_verify_figure_block_write",
        lambda *args, **kwargs: {"error": "simulated post-write verification failure"},
    )

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
    )

    assert "error" in result
    assert result["file_restored"] is True

    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "docx_path must be restored to its pre-write content on "
            "verification failure -- not left with an orphan image and no "
            "caption, nor a corrupted document"
        )


def test_verify_figure_block_write_detects_real_mismatches(tmp_path):
    """Unit-level check of the verification helper itself: a genuinely
    correct write passes, and a caller asking for the wrong expected values
    against that SAME on-disk write is correctly rejected."""
    docx_path, image_path = _setup(tmp_path)

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
    )
    assert result["status"] == "inserted"

    ok = docs_intel._verify_figure_block_write(
        docx_path,
        image_para_id=result["image_para_id"],
        expected_seq_number=result["seq_number"],
        expected_label_text=result["label_text"],
    )
    assert ok is None

    wrong_seq = docs_intel._verify_figure_block_write(
        docx_path,
        image_para_id=result["image_para_id"],
        expected_seq_number=999,
        expected_label_text=result["label_text"],
    )
    assert wrong_seq is not None and "SEQ number mismatch" in wrong_seq["error"]

    wrong_label = docs_intel._verify_figure_block_write(
        docx_path,
        image_para_id=result["image_para_id"],
        expected_seq_number=result["seq_number"],
        expected_label_text="not the actual label",
    )
    assert wrong_label is not None and "label text mismatch" in wrong_label["error"]

    wrong_para = docs_intel._verify_figure_block_write(
        docx_path,
        image_para_id="not-a-real-para-id",
        expected_seq_number=result["seq_number"],
        expected_label_text=result["label_text"],
    )
    assert wrong_para is not None and "not found" in wrong_para["error"]


# ---------------------------------------------------------------------------
# Immediate reindex
# ---------------------------------------------------------------------------

def test_insert_figure_block_immediate_reindex_upserts_sidecar_without_manual_reindex(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    index_db_path = str(tmp_path / "index.db")
    docs_intel.index_docx(docx_path, index_db_path)

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Loss curve for run 42",
        anchor_para_id="P0000001", section_heading="Results",
        index_db_path=index_db_path,
    )
    assert result["status"] == "inserted"

    conn = sqlite3.connect(index_db_path)
    try:
        row = conn.execute(
            "SELECT caption, seq_number, section, ref_bookmark FROM docx_figures "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    caption, seq_number, section, ref_bookmark = row
    assert caption == "Figure 1. Loss curve for run 42"
    assert seq_number == "1"
    assert section == "Results"
    assert ref_bookmark == result["ref_bookmark"]

    meta_conn = sqlite3.connect(index_db_path)
    try:
        stale = meta_conn.execute(
            "SELECT value FROM docx_index_meta WHERE key = 'source_mtime'"
        ).fetchone()
    finally:
        meta_conn.close()
    assert stale is not None and stale[0] is None, (
        "source_mtime must be invalidated so the next read auto-reindexes"
    )


# ---------------------------------------------------------------------------
# Remaining validation / anchor-resolution branches
# ---------------------------------------------------------------------------

def test_insert_figure_block_rejects_invalid_docx_path_type(tmp_path):
    result = docs_intel.insert_figure_block("", "chart.png", label_text="Chart")
    assert "docx_path must be a non-empty string" in result["error"]


def test_insert_figure_block_rejects_invalid_image_path_type(tmp_path):
    docx_path, _image_path = _setup(tmp_path)
    result = docs_intel.insert_figure_block(docx_path, "", label_text="Chart")
    assert "image_path must be a non-empty string" in result["error"]


def test_insert_figure_block_rejects_invalid_position(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", position="sideways",
    )
    assert "position must be before or after" in result["error"]


def test_insert_figure_block_rejects_non_positive_width_and_height(tmp_path):
    docx_path, image_path = _setup(tmp_path)

    width_result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", width_inches=0,
    )
    assert "width_inches must be greater than zero" in width_result["error"]

    height_result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", height_inches=-1,
    )
    assert "height_inches must be greater than zero" in height_result["error"]


def test_insert_figure_block_rejects_invalid_style_policy_without_mutating(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    original = (tmp_path / "report.docx").read_bytes()

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart",
        style_policy={"caption_centered": "not-a-bool"},
    )

    assert "error" in result
    assert (tmp_path / "report.docx").read_bytes() == original


def test_insert_figure_block_rejects_unreadable_image_path(tmp_path):
    docx_path, _image_path = _setup(tmp_path)
    directory_as_image = tmp_path / "not-a-file.png"
    directory_as_image.mkdir()

    result = docs_intel.insert_figure_block(
        docx_path, str(directory_as_image), label_text="Chart",
    )

    assert "could not read image" in result["error"]


def test_insert_figure_block_rejects_empty_image_file(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    (tmp_path / "chart.png").write_bytes(b"")

    result = docs_intel.insert_figure_block(docx_path, image_path, label_text="Chart")

    assert "image file is empty" in result["error"]


def test_insert_figure_block_rejects_missing_docx_file(tmp_path):
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(_PNG)

    result = docs_intel.insert_figure_block(
        str(tmp_path / "does-not-exist.docx"), str(image_path), label_text="Chart",
    )

    assert "no such file" in result["error"]


def test_insert_figure_block_rejects_document_with_no_body(tmp_path):
    docx_path = tmp_path / "report.docx"
    image_path = tmp_path / "chart.png"
    _write_docx(docx_path, _NO_BODY_DOC_XML)
    image_path.write_bytes(_PNG)

    result = docs_intel.insert_figure_block(
        str(docx_path), str(image_path), label_text="Chart",
    )

    assert "document has no body" in result["error"]


def test_insert_figure_block_appends_before_trailing_sectpr_when_no_anchor(tmp_path):
    docx_path, image_path = _setup(tmp_path)

    result = docs_intel.insert_figure_block(docx_path, image_path, label_text="Chart")

    assert result["status"] == "inserted"
    children = _body_children(docx_path)
    tags = [c.tag for c in children]
    assert tags[-1] == f"{{{_W}}}sectPr"
    assert children[-3].get(f"{{{_W14}}}paraId") == result["image_para_id"]


def test_insert_figure_block_reports_write_failure(tmp_path, monkeypatch):
    docx_path, image_path = _setup(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(docs_intel, "_save_docx_with_image", _boom)

    result = docs_intel.insert_figure_block(docx_path, image_path, label_text="Chart")

    assert "could not write" in result["error"]


def test_insert_figure_block_rejects_table_cell_anchor(tmp_path):
    docx_path, image_path = _setup(tmp_path, _TABLE_ANCHOR_DOC_XML)
    original = (tmp_path / "report.docx").read_bytes()

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="CELL0001",
    )

    assert "table-cell paragraphs cannot anchor image insertion" in result["error"]
    assert (tmp_path / "report.docx").read_bytes() == original


# ---------------------------------------------------------------------------
# _verify_figure_block_write internal-branch coverage
# ---------------------------------------------------------------------------

def test_verify_figure_block_write_reports_reread_failure_for_missing_file(tmp_path):
    result = docs_intel._verify_figure_block_write(
        str(tmp_path / "does-not-exist.docx"),
        image_para_id="IMG1", expected_seq_number=1, expected_label_text="x",
    )
    assert result is not None and "could not re-read" in result["error"]


def test_verify_figure_block_write_reports_reread_failure_for_invalid_docx(tmp_path):
    bad_path = tmp_path / "not-a-docx.docx"
    bad_path.write_bytes(b"not a zip at all")

    result = docs_intel._verify_figure_block_write(
        str(bad_path), image_para_id="IMG1", expected_seq_number=1, expected_label_text="x",
    )
    assert result is not None and "could not re-read" in result["error"]


def test_verify_figure_block_write_reports_missing_body(tmp_path):
    docx_path = tmp_path / "nobody.docx"
    _write_docx(docx_path, _NO_BODY_DOC_XML)

    result = docs_intel._verify_figure_block_write(
        str(docx_path), image_para_id="IMG1", expected_seq_number=1, expected_label_text="x",
    )
    assert result is not None and "<w:body>" in result["error"]


def test_verify_figure_block_write_covers_every_internal_mismatch_branch(tmp_path):
    docx_path = tmp_path / "branches.docx"
    _write_docx(docx_path, _VERIFY_BRANCHES_DOC_XML)
    path = str(docx_path)

    missing_image = docs_intel._verify_figure_block_write(
        path, image_para_id="NOPE", expected_seq_number=1, expected_label_text="x",
    )
    assert missing_image is not None and "not found" in missing_image["error"]

    no_drawing = docs_intel._verify_figure_block_write(
        path, image_para_id="NODRAW01", expected_seq_number=1, expected_label_text="x",
    )
    assert no_drawing is not None and "no longer contains a" in no_drawing["error"]

    not_centered = docs_intel._verify_figure_block_write(
        path, image_para_id="LEFTIMG01", expected_seq_number=1, expected_label_text="x",
    )
    assert not_centered is not None and "not centered" in not_centered["error"]

    next_not_paragraph = docs_intel._verify_figure_block_write(
        path, image_para_id="TBLIMG01", expected_seq_number=1, expected_label_text="x",
    )
    assert next_not_paragraph is not None and "is not a paragraph" in next_not_paragraph["error"]

    no_seq_field = docs_intel._verify_figure_block_write(
        path, image_para_id="NOSEQIMG01", expected_seq_number=1, expected_label_text="x",
    )
    assert (
        no_seq_field is not None
        and "does not contain a SEQ Figure field" in no_seq_field["error"]
    )

    caption_missing = docs_intel._verify_figure_block_write(
        path, image_para_id="LASTIMG01", expected_seq_number=1, expected_label_text="x",
    )
    assert caption_missing is not None and "the caption is missing" in caption_missing["error"]


# ---------------------------------------------------------------------------
# Render-capability gate (ddd79188): rendered / unavailable-with-reason /
# failed, invoked AFTER structural verification passes -- closes the gap
# between structural re-parse and real Word/COM render verification.
# ---------------------------------------------------------------------------

def test_insert_figure_block_rendered_status_reports_render_evidence(tmp_path, monkeypatch):
    docx_path, image_path = _setup(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda path, **kwargs: {
            "status": "rendered", "backend": "libreoffice-soffice",
            "detail": {"converted_via": "soffice", "output_filename": "out.pdf"},
        },
    )

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
    )

    assert result["status"] == "inserted"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True
    assert result["render_backend"] == "libreoffice-soffice"
    assert result["render_detail"]["converted_via"] == "soffice"


def test_insert_figure_block_render_failed_restores_and_errors(tmp_path, monkeypatch):
    """A structurally-valid write whose render attempt genuinely FAILS must
    be restored from the pre-write backup and reported as an error -- never
    silently accepted just because the structural re-parse passed."""
    docx_path, image_path = _setup(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda path, **kwargs: {
            "status": "failed", "reason": "soffice crashed", "backend": "libreoffice-soffice",
        },
    )

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
    )

    assert "error" in result
    assert result["render_status"] == "failed"
    assert result["file_restored"] is True
    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "a real render failure must restore docx_path to its pre-write "
            "content, exactly like a structural verification failure"
        )


def test_insert_figure_block_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    """No render backend in this environment must NOT be silently treated as
    verified -- by default this fails closed (restore + error) for
    canonical/production promotion, exactly like a real render failure."""
    docx_path, image_path = _setup(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda path, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
    )

    assert "error" in result
    assert result["render_status"] == "unavailable-with-reason"
    assert result["file_restored"] is True
    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes


def test_insert_figure_block_render_unavailable_degrades_with_audited_override(tmp_path, monkeypatch):
    """allow_degraded_render=True + a non-empty degraded_render_reason is the
    ONLY way to accept a write with no render verification -- and it must
    never be reported as verified even when the write is accepted."""
    docx_path, image_path = _setup(tmp_path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda path, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "inserted"
    assert result["render_status"] == "unavailable-with-reason"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True
    assert result["degraded_render_reason"] == "CI sandbox has no LibreOffice/Word installed"


def test_insert_figure_block_allow_degraded_render_requires_non_empty_reason(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    original = (tmp_path / "report.docx").read_bytes()

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
        allow_degraded_render=True,
    )

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    assert (tmp_path / "report.docx").read_bytes() == original


def test_insert_figure_block_structural_reparse_alone_never_yields_verified_render(tmp_path, monkeypatch):
    """The critical invariant this whole item exists for: a write that
    passes REAL structural verification (_verify_figure_block_write is NOT
    stubbed -- this is a genuine write + re-parse) must still fail closed
    when the render backend cannot confirm the document actually renders.
    Structural correctness and render verification are two separate,
    independently-enforced checks; passing one must never be reported as
    satisfying the other."""
    docx_path, image_path = _setup(tmp_path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda path, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
    )

    # The write's STRUCTURE was genuinely fine -- yet the overall call still
    # reports failure, because structural correctness alone is not enough
    # to be reported "verified" / "inserted".
    assert "error" in result
    assert result["render_status"] == "unavailable-with-reason"
    assert result.get("status") != "inserted"


def test_insert_figure_block_server_wrapper_threads_degraded_render_params(tmp_path, monkeypatch):
    docx_path, image_path = _setup(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda path, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = server.insert_figure_block(
        docx_path, image_path, label_text="Chart", anchor_para_id="P0000001",
        allow_degraded_render=True, degraded_render_reason="no backend in test env",
    )

    assert result["status"] == "inserted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True
