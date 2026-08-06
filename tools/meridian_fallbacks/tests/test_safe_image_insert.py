"""Tests for tools/meridian_fallbacks/safe_image_insert.py."""
from __future__ import annotations

import pytest

from tools.meridian_fallbacks.safe_image_insert import (
    ImageInsertError,
    compute_image_insert_parts,
    insert_image,
)
from tools.meridian_fallbacks.safe_ooxml_writer import SafeOoxmlWriter, read_parts_from_bytes

from .conftest import make_document_xml, make_minimal_docx_parts


def _doc_xml_text(parts: dict[str, bytes]) -> str:
    return parts["word/document.xml"].decode("utf-8")


# ---------------------------------------------------------------------------
# compute_image_insert_parts -- pure function behavior
# ---------------------------------------------------------------------------


def test_appends_new_paragraph_when_no_anchor_given(minimal_docx_parts, fake_image_bytes):
    result = compute_image_insert_parts(minimal_docx_parts, fake_image_bytes, image_ext="png")

    assert result.inserted_new_paragraph is True
    assert result.media_part == "word/media/image1.png"
    assert result.parts["word/media/image1.png"] == fake_image_bytes
    assert "<w:drawing>" in _doc_xml_text(result.parts)
    # Purity: the original input mapping must be untouched.
    assert "word/media/image1.png" not in minimal_docx_parts


def test_content_types_gets_default_extension_entry(minimal_docx_parts, fake_image_bytes):
    result = compute_image_insert_parts(minimal_docx_parts, fake_image_bytes, image_ext="png")
    ct = result.parts["[Content_Types].xml"].decode("utf-8")
    assert 'Extension="png"' in ct
    assert "image/png" in ct


def test_relationship_added_with_image_type(minimal_docx_parts, fake_image_bytes):
    result = compute_image_insert_parts(minimal_docx_parts, fake_image_bytes, image_ext="png")
    rels = result.parts["word/_rels/document.xml.rels"].decode("utf-8")
    assert f'Id="{result.relationship_id}"' in rels
    assert "relationships/image" in rels
    assert "media/image1.png" in rels


def test_anchor_text_targets_correct_paragraph(fake_image_bytes):
    parts = make_minimal_docx_parts(["Alpha", "Beta anchor here", "Gamma"])
    result = compute_image_insert_parts(
        parts, fake_image_bytes, image_ext="png", anchor_text="anchor"
    )
    assert result.paragraph_index == 1
    assert result.inserted_new_paragraph is False

    text = _doc_xml_text(result.parts)
    # The drawing landed inside the Beta paragraph specifically.
    beta_start = text.index("Beta anchor here")
    beta_para_end = text.index("</w:p>", beta_start)
    assert "<w:drawing>" in text[beta_start:beta_para_end]
    # And nowhere near Alpha/Gamma's own paragraphs.
    alpha_start = text.index("Alpha")
    alpha_para_end = text.index("</w:p>", alpha_start)
    assert "<w:drawing>" not in text[alpha_start:alpha_para_end]


def test_paragraph_index_targets_correct_paragraph(fake_image_bytes):
    parts = make_minimal_docx_parts(["First", "Second", "Third"])
    result = compute_image_insert_parts(
        parts, fake_image_bytes, image_ext="png", paragraph_index=2
    )
    assert result.paragraph_index == 2
    text = _doc_xml_text(result.parts)
    third_start = text.index("Third")
    third_para_end = text.index("</w:p>", third_start)
    assert "<w:drawing>" in text[third_start:third_para_end]


def test_paragraph_index_out_of_range_raises(minimal_docx_parts, fake_image_bytes):
    with pytest.raises(ImageInsertError, match="out of range"):
        compute_image_insert_parts(
            minimal_docx_parts, fake_image_bytes, image_ext="png", paragraph_index=99
        )


def test_anchor_text_not_found_raises(minimal_docx_parts, fake_image_bytes):
    with pytest.raises(ImageInsertError, match="not found"):
        compute_image_insert_parts(
            minimal_docx_parts, fake_image_bytes, image_ext="png", anchor_text="nope-not-here"
        )


def test_unsupported_extension_raises(minimal_docx_parts, fake_image_bytes):
    with pytest.raises(ImageInsertError, match="unsupported image extension"):
        compute_image_insert_parts(minimal_docx_parts, fake_image_bytes, image_ext="xyz")


def test_empty_image_bytes_raises(minimal_docx_parts):
    with pytest.raises(ImageInsertError, match="non-empty"):
        compute_image_insert_parts(minimal_docx_parts, b"", image_ext="png")


def test_missing_required_part_raises(fake_image_bytes):
    with pytest.raises(ImageInsertError, match="missing required part"):
        compute_image_insert_parts({}, fake_image_bytes, image_ext="png")


def test_self_closing_paragraph_gets_opened(fake_image_bytes):
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p w:rsidR="00AB1234"/><w:p><w:r><w:t>Second</w:t></w:r></w:p>'
        "<w:sectPr/></w:body></w:document>"
    ).encode("utf-8")
    parts = make_minimal_docx_parts()
    parts["word/document.xml"] = doc_xml

    result = compute_image_insert_parts(
        parts, fake_image_bytes, image_ext="png", paragraph_index=0
    )
    text = _doc_xml_text(result.parts)
    assert "<w:p/>" not in text
    assert 'w:rsidR="00AB1234"' in text  # original attribute preserved
    first_para_end = text.index("</w:p>")
    assert "<w:drawing>" in text[:first_para_end]


def test_missing_rels_part_is_synthesized(fake_image_bytes):
    parts = make_minimal_docx_parts()
    del parts["word/_rels/document.xml.rels"]
    result = compute_image_insert_parts(parts, fake_image_bytes, image_ext="png")
    assert "word/_rels/document.xml.rels" in result.parts
    assert result.relationship_id == "rId1"


def test_content_type_insertion_is_idempotent(fake_image_bytes):
    parts = make_minimal_docx_parts()
    first = compute_image_insert_parts(parts, fake_image_bytes, image_ext="png")
    second = compute_image_insert_parts(first.parts, fake_image_bytes, image_ext="png")
    ct = second.parts["[Content_Types].xml"].decode("utf-8")
    assert ct.count('Extension="png"') == 1


def test_media_filename_and_rel_id_increment_across_inserts(fake_image_bytes):
    parts = make_minimal_docx_parts()
    first = compute_image_insert_parts(parts, fake_image_bytes, image_ext="png")
    assert first.media_part == "word/media/image1.png"
    assert first.relationship_id == "rId1"

    second = compute_image_insert_parts(first.parts, fake_image_bytes, image_ext="jpeg")
    assert second.media_part == "word/media/image2.jpeg"
    assert second.relationship_id == "rId2"
    # Both media parts survive in the final mapping.
    assert "word/media/image1.png" in second.parts
    assert "word/media/image2.jpeg" in second.parts


# ---------------------------------------------------------------------------
# insert_image -- disk-touching convenience wrapper
# ---------------------------------------------------------------------------


def test_insert_image_disk_roundtrip(docx_path, fake_image_bytes):
    result = insert_image(docx_path, image_bytes=fake_image_bytes, image_ext="png")

    assert result.write_result.validation.valid is True
    on_disk = read_parts_from_bytes(docx_path.read_bytes())
    assert on_disk[result.media_part] == fake_image_bytes
    assert b"<w:drawing>" in on_disk["word/document.xml"]


def test_insert_image_requires_a_source(docx_path):
    with pytest.raises(ImageInsertError, match="either image_path or image_bytes"):
        insert_image(docx_path)


def test_insert_image_bytes_requires_ext(docx_path, fake_image_bytes):
    with pytest.raises(ImageInsertError, match="image_ext is required"):
        insert_image(docx_path, image_bytes=fake_image_bytes)


def test_insert_image_from_path_infers_extension(tmp_path, docx_path, fake_image_bytes):
    image_path = tmp_path / "picture.png"
    image_path.write_bytes(fake_image_bytes)

    result = insert_image(docx_path, image_path=image_path)

    on_disk = read_parts_from_bytes(docx_path.read_bytes())
    assert on_disk[result.media_part] == fake_image_bytes
    assert result.media_part.endswith(".png")


def test_insert_image_uses_supplied_writer(docx_path, fake_image_bytes):
    writer = SafeOoxmlWriter(docx_path)
    result = insert_image(docx_path, image_bytes=fake_image_bytes, image_ext="gif", writer=writer)
    assert result.write_result.path == str(docx_path)
