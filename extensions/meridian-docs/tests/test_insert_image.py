"""Regression tests for native centered OOXML image insertion."""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


_DOC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Anchor</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

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
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (200).to_bytes(4, "big") + (100).to_bytes(4, "big") + b"payload"


def _write_docx(path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOC_XML)
        archive.writestr("word/_rels/document.xml.rels", _RELS_XML)
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)


def test_insert_image_always_centers_new_figure_and_packages_media(tmp_path):
    docx_path = tmp_path / "report.docx"
    image_path = tmp_path / "chart.png"
    _write_docx(docx_path)
    image_path.write_bytes(_PNG)

    result = docs_intel.insert_image(
        str(docx_path),
        str(image_path),
        anchor_para_id="P0000001",
        position="after",
    )

    assert result["status"] == "inserted"
    assert result["image_para_id"]
    assert result["image_name"] == "word/media/image1.png"

    with zipfile.ZipFile(docx_path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        body = document.find(f"{{{_W}}}body")
        paragraphs = list(body)
        image_para = paragraphs[1]
        assert image_para.get(f"{{{_W14}}}paraId") == result["image_para_id"]
        jc = image_para.find(f"{{{_W}}}pPr/{{{_W}}}jc")
        assert jc is not None
        assert jc.get(f"{{{_W}}}val") == "center"

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
            child
            for child in content_types
            if child.get("Extension") == "png"
        )
        assert png_default.get("ContentType") == "image/png"


def test_insert_image_rejects_bad_format_without_mutating_document(tmp_path):
    docx_path = tmp_path / "report.docx"
    image_path = tmp_path / "chart.svg"
    _write_docx(docx_path)
    image_path.write_text("<svg/>", encoding="utf-8")
    original = docx_path.read_bytes()

    result = docs_intel.insert_image(str(docx_path), str(image_path))

    assert "unsupported image format" in result["error"]
    assert docx_path.read_bytes() == original
