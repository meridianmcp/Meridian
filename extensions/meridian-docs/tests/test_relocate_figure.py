"""Tests for the native image-plus-caption relocation primitive."""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


_FIGURE_BLOCK_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>Destination</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000002"><w:r><w:drawing><a:blip r:embed="rId8"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP000002"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>2</w:t></w:r></w:fldSimple></w:p>
    <w:p w14:paraId="P0000003"><w:r><w:t>Tail</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def _write_docx(tmp_path, xml: str = _FIGURE_BLOCK_XML) -> str:
    path = tmp_path / "figure.docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/media/image1.png", b"image")
        archive.writestr("word/media/image2.png", b"image")
    path.write_bytes(buf.getvalue())
    return str(path)


def _body_ids(path: str) -> list[str | None]:
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    return [child.get(docs_intel._q(_W14, "paraId")) for child in body]


def test_relocate_figure_moves_image_and_caption_atomically_and_renumbers(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.relocate_figure(
        path, 2, "P0000001", destination_position="before"
    )

    assert result["status"] == "moved"
    assert result["moved_block_count"] == 2
    assert _body_ids(path) == [
        "IMG000002",
        "CAP000002",
        "P0000001",
        "IMG000001",
        "CAP000001",
        "P0000002",
        "P0000003",
        None,
    ]

    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    images = [
        child
        for child in body
        if child.tag == docs_intel._q(_W, "p")
        and child.find(f".//{docs_intel._q(_W, 'drawing')}") is not None
    ]
    embeds = [
        drawing.get(f"{{{_R}}}embed")
        for image in images
        for drawing in image.iter(docs_intel._q(_A, "blip"))
    ]
    assert embeds == ["rId8", "rId7"]

    caption_numbers = {}
    for child in body:
        para_id = child.get(docs_intel._q(_W14, "paraId"))
        field = child.find(docs_intel._q(_W, "fldSimple"))
        if para_id and field is not None:
            caption_numbers[para_id] = next(field.iter(docs_intel._q(_W, "t"))).text
    assert caption_numbers == {"CAP000002": "1", "CAP000001": "2"}
    assert result["renumber_sequences"]["status"] == "corrected"


def test_relocate_figure_rejects_non_figure_caption_without_writing(tmp_path):
    xml = _FIGURE_BLOCK_XML.replace(
        'w:instr=" SEQ Figure \\* ARABIC "',
        'w:instr=" SEQ Table \\* ARABIC "',
        1,
    )
    path = _write_docx(tmp_path, xml)
    before = (tmp_path / "figure.docx").read_bytes()

    result = docs_intel.relocate_figure(path, 1, "P0000002")

    assert "error" in result
    assert "SEQ Figure caption" in result["error"]
    assert (tmp_path / "figure.docx").read_bytes() == before


def test_relocate_figure_rejects_destination_inside_block(tmp_path):
    path = _write_docx(tmp_path)
    before = (tmp_path / "figure.docx").read_bytes()

    result = docs_intel.relocate_figure(path, 1, "CAP000001")

    assert "error" in result
    assert "inside the figure block" in result["error"]
    assert (tmp_path / "figure.docx").read_bytes() == before
