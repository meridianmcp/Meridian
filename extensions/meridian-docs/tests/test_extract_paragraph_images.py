"""Tests for extract_paragraph_images (b2e6a7d0) -- extracting EVERY image
embedded in one docx paragraph to real files on disk, with basic metadata,
so a caller can hand the resulting paths straight to an image-capable Read
instead of hand-rolling a "zip -> find blip -> resolve rId via rels -> dump
bytes" script every time a figure needs to actually be looked at.

Blip-completeness is the headline correctness property under test: a prior
hand-rolled audit script in this project had a blip_rid(p) helper that
returned only the FIRST <a:blip> per paragraph, silently dropping
legitimate 2nd/3rd/4th panel images and producing 18 false-positive
findings before an independent cross-check caught it. Several tests here
specifically build a paragraph with MULTIPLE <w:drawing>/<a:blip> elements
to prove this tool never reproduces that bug.

Fixture conventions mirror tests/test_insert_image.py (same synthetic PNG
header trick, same rels/content-types skeleton) and
tests/test_flag_for_review.py (same locate_anchor-anchor-dict conventions).
"""

from __future__ import annotations

import os
import zipfile

from meridian_docs import docs_intel, server


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"

# 200x100 px PNG header; pixel payload content is irrelevant to OOXML
# packaging or to _image_dimensions_px, which only reads the IHDR chunk.
_PNG_200x100 = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    + (200).to_bytes(4, "big") + (100).to_bytes(4, "big") + b"payload-a"
)
# A second, differently-sized PNG so two extracted images are trivially
# distinguishable by both bytes and pixel dimensions.
_PNG_64x32 = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    + (64).to_bytes(4, "big") + (32).to_bytes(4, "big") + b"payload-b"
)


def _drawing_xml(relationship_id: str, cx: int, cy: int) -> str:
    return (
        f'<w:drawing><wp:inline xmlns:wp="{_WP}" distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/>'
        f'<wp:docPr id="1" name="Picture 1"/>'
        f'<a:graphic xmlns:a="{_A}"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic xmlns:pic="{_PIC}"><pic:blipFill>'
        f'<a:blip xmlns:r="{_R}" r:embed="{relationship_id}"/>'
        f"</pic:blipFill></pic:pic></a:graphicData></a:graphic>"
        f"</wp:inline></w:drawing>"
    )


_DOCUMENT_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>No images here.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="F0000001">
      <w:r>{_drawing_xml("rId1", 1828800, 914400)}</w:r>
    </w:p>
    <w:p w14:paraId="M0000001">
      <w:r>{_drawing_xml("rId2", 600000, 300000)}</w:r>
      <w:r>{_drawing_xml("rId3", 600000, 300000)}</w:r>
    </w:p>
    <w:p w14:paraId="B0000001">
      <w:r>{_drawing_xml("rId404", 600000, 300000)}</w:r>
    </w:p>
    <w:p w14:paraId="F0000001b">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:r><w:t xml:space="preserve"> -- a unique caption phrase for anchor lookup</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>a table cell</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_RELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image3.png"/>
</Relationships>
"""
# Note: rId404 (used by paragraph B0000001) is deliberately ABSENT above --
# a dangling relationship reference, exercised by the "bad reference" test.

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""


def _write_docx(tmp_path, name="doc.docx"):
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
        archive.writestr("word/_rels/document.xml.rels", _RELS_XML)
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        archive.writestr("word/media/image1.png", _PNG_200x100)
        archive.writestr("word/media/image2.png", _PNG_200x100)
        archive.writestr("word/media/image3.png", _PNG_64x32)
    return path


# ---------------------------------------------------------------------------
# Single image.
# ---------------------------------------------------------------------------


def test_extract_paragraph_images_single_image(tmp_path):
    path = _write_docx(tmp_path)
    out_dir = str(tmp_path / "out")

    result = docs_intel.extract_paragraph_images(path, "F0000001", out_dir=out_dir)

    assert result["status"] == "extracted"
    assert result["image_count"] == 1
    img = result["images"][0]
    assert img["blip_index"] == 1
    assert img["relationship_id"] == "rId1"
    assert img["media_part"] == "word/media/image1.png"
    assert img["pixel_width"] == 200
    assert img["pixel_height"] == 100
    assert img["file_size_bytes"] == len(_PNG_200x100)
    assert img["displayed_extent_emu"] == {"cx": 1828800, "cy": 914400}
    assert img["displayed_extent_inches"] == {"width": 2.0, "height": 1.0}
    assert os.path.isfile(img["extracted_path"])
    with open(img["extracted_path"], "rb") as fh:
        assert fh.read() == _PNG_200x100


# ---------------------------------------------------------------------------
# Blip-completeness: the headline correctness property.
# ---------------------------------------------------------------------------


def test_extract_paragraph_images_multi_blip_paragraph_extracts_every_image(tmp_path):
    path = _write_docx(tmp_path)
    out_dir = str(tmp_path / "out")

    result = docs_intel.extract_paragraph_images(path, "M0000001", out_dir=out_dir)

    assert result["status"] == "extracted"
    # THE regression this tool exists to prevent: a "first blip only" bug
    # would report image_count == 1 here. Both images must be present.
    assert result["image_count"] == 2
    blip_indices = [img["blip_index"] for img in result["images"]]
    assert blip_indices == [1, 2]
    media_parts = {img["media_part"] for img in result["images"]}
    assert media_parts == {"word/media/image2.png", "word/media/image3.png"}

    # Both files actually landed on disk, distinguishable by content/size.
    paths = [img["extracted_path"] for img in result["images"]]
    assert len(set(paths)) == 2
    for p in paths:
        assert os.path.isfile(p)

    # The second image (image3.png, 64x32) is correctly distinguished from
    # the first (image2.png, 200x100) -- proves per-blip pairing, not just
    # "found two things and both point at image1".
    by_part = {img["media_part"]: img for img in result["images"]}
    assert by_part["word/media/image2.png"]["pixel_width"] == 200
    assert by_part["word/media/image3.png"]["pixel_width"] == 64
    assert by_part["word/media/image3.png"]["pixel_height"] == 32


# ---------------------------------------------------------------------------
# No images / dangling reference handling.
# ---------------------------------------------------------------------------


def test_extract_paragraph_images_no_images_in_paragraph(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.extract_paragraph_images(path, "P0000001")

    assert result["status"] == "no_images"
    assert result["image_count"] == 0
    assert result["images"] == []


def test_extract_paragraph_images_dangling_relationship_reports_per_image_error(tmp_path):
    path = _write_docx(tmp_path)
    out_dir = str(tmp_path / "out")

    result = docs_intel.extract_paragraph_images(path, "B0000001", out_dir=out_dir)

    # The whole call does not abort -- a bad reference is reported as a
    # per-image error entry.
    assert result["status"] == "extracted"
    assert result["image_count"] == 1
    assert "error" in result["images"][0]
    assert "rId404" in result["images"][0]["error"]
    assert "extracted_path" not in result["images"][0]


# ---------------------------------------------------------------------------
# out_dir handling.
# ---------------------------------------------------------------------------


def test_extract_paragraph_images_creates_missing_out_dir(tmp_path):
    path = _write_docx(tmp_path)
    out_dir = str(tmp_path / "nested" / "out")
    assert not os.path.isdir(out_dir)

    result = docs_intel.extract_paragraph_images(path, "F0000001", out_dir=out_dir)

    assert result["status"] == "extracted"
    assert os.path.isdir(out_dir)
    assert result["images"][0]["extracted_path"].startswith(out_dir)


def test_extract_paragraph_images_defaults_to_a_fresh_temp_dir(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.extract_paragraph_images(path, "F0000001")

    assert result["status"] == "extracted"
    assert result["out_dir"]
    assert os.path.isdir(result["out_dir"])
    assert os.path.isfile(result["images"][0]["extracted_path"])


# ---------------------------------------------------------------------------
# Anchor resolution via a locate_anchor-style query, same convention as
# flag_for_review.
# ---------------------------------------------------------------------------


def test_extract_paragraph_images_text_query_anchor(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.extract_paragraph_images(
        path, {"text": "unique caption phrase for anchor lookup"},
    )

    # The caption paragraph itself has no image -- anchor resolution still
    # succeeds; "no_images" (not an error) reports that honestly.
    assert result["status"] == "no_images"
    assert result["anchor_para_id"] == "F0000001b"
    assert result["image_count"] == 0
    assert result["element_type"] == "figure_caption"


def test_extract_paragraph_images_ambiguous_anchor_is_refused(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.extract_paragraph_images(path, {"text": "does not exist anywhere"})

    assert "error" in result
    assert result.get("locate_result", {}).get("status") == "not_found"


def test_extract_paragraph_images_table_target_anchor_is_refused(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.extract_paragraph_images(path, {"text": "a table cell"})

    assert "error" in result
    assert "table" in result["error"]


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------


def test_extract_paragraph_images_unknown_para_id(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.extract_paragraph_images(path, "DOES_NOT_EXIST")

    assert "error" in result
    assert "not found" in result["error"]


def test_extract_paragraph_images_missing_file(tmp_path):
    result = docs_intel.extract_paragraph_images(str(tmp_path / "nope.docx"), "P0000001")
    assert "error" in result


def test_extract_paragraph_images_never_mutates_the_document(tmp_path):
    path = _write_docx(tmp_path)
    with open(path, "rb") as fh:
        before = fh.read()

    docs_intel.extract_paragraph_images(path, "M0000001")

    with open(path, "rb") as fh:
        after = fh.read()
    assert before == after


# ---------------------------------------------------------------------------
# server.py MCP wrapper delegates correctly.
# ---------------------------------------------------------------------------


def test_extract_paragraph_images_server_wrapper_delegates(tmp_path):
    path = _write_docx(tmp_path)
    out_dir = str(tmp_path / "out")

    result = server.extract_paragraph_images(path, "F0000001", out_dir=out_dir)

    assert result["status"] == "extracted"
    assert result["image_count"] == 1
