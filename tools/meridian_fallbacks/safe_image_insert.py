"""safe_image_insert.py -- insert an image into a .docx as an inline
``<w:drawing>`` run, WITHOUT a full ``word/document.xml`` re-parse/
re-serialize (see ``safe_ooxml_writer.py``'s module docstring for exactly
why that round trip is avoided in this package).

Part of ``tools/meridian_fallbacks`` -- see ``capability_manifest.json``.

Approach
--------
Every OOXML part this module touches is edited via a small, targeted
BYTE-LEVEL splice (find a stable anchor -- a paragraph boundary, a closing
``</Relationships>`` tag, a closing ``</Types>`` tag -- and insert new text
immediately before it) rather than a parse-mutate-reserialize round trip.
This means every byte of the document this module does NOT touch is passed
through completely unchanged, including namespace declarations and
formatting this module has no model of. The trade-off (documented in
``capability_manifest.json``): this is not a general document.xml editor,
and it assumes ``word/document.xml`` is written on one logical line per
part the way python-docx/Word actually produce it (no literal
``</w:p>``-shaped text inside a text run -- a vanishingly unlikely
collision in practice, but not schema-impossible).

Two layers are exposed:

* :func:`compute_image_insert_parts` -- a PURE function: takes the current
  ``{part_name: bytes}`` mapping and image bytes, returns a NEW mapping
  with the image, relationship, content-type default, and drawing run
  added. Never touches disk. This is what ``transactional_merge.py`` calls
  as part of a larger multi-operation transaction.
* :func:`insert_image` -- the disk-touching convenience wrapper: reads the
  target .docx via a :class:`~safe_ooxml_writer.SafeOoxmlWriter`, calls
  :func:`compute_image_insert_parts`, and commits the result with that same
  writer's validate-then-atomic-replace discipline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safe_ooxml_writer import SafeOoxmlWriter, WriteResult

EMU_PER_INCH = 914400

_CONTENT_TYPE_BY_EXT: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "emf": "image/x-emf",
    "wmf": "image/x-wmf",
}

_IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

_MINIMAL_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    "</Relationships>"
).encode("utf-8")

# `<w:p\b` deliberately does not match `<w:pPr` / `<w:pStyle` etc: a regex
# `\b` is a transition between a "word" char and a non-word char, and both
# 'p' and the following 'P'/'S' are word characters, so no boundary exists
# there. It only matches the literal element name `w:p` followed by
# whitespace, `>`, or `/`.
_PARA_RE = re.compile(
    r"<w:p\b[^>]*/>"  # self-closing paragraph: <w:p .../>
    r"|"
    r"<w:p\b[^>]*>.*?</w:p>",  # paragraph with content, non-greedy to its own close
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_REL_ID_RE = re.compile(r'Id="rId(\d+)"')
_MEDIA_NAME_RE = re.compile(r"^word/media/image(\d+)\.")
_DOC_PR_ID_RE = re.compile(r'docPr\s+id="(\d+)"')


class ImageInsertError(Exception):
    """Raised for any image-insertion precondition or splice failure."""


@dataclass
class ImageInsertResult:
    """Pure result of :func:`compute_image_insert_parts` -- an updated parts
    mapping plus bookkeeping about what was inserted and where."""

    parts: dict[str, bytes]
    relationship_id: str
    media_part: str
    paragraph_index: int
    inserted_new_paragraph: bool


@dataclass
class ImageInsertWriteResult:
    """Outcome of the disk-touching :func:`insert_image`: the underlying
    :class:`~safe_ooxml_writer.WriteResult` plus the same insertion
    bookkeeping :class:`ImageInsertResult` carries."""

    write_result: WriteResult
    relationship_id: str
    media_part: str
    paragraph_index: int
    inserted_new_paragraph: bool


def _xml_escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
    )


def _strip_tags(xml_fragment: str) -> str:
    return _TAG_RE.sub("", xml_fragment)


def _next_rel_id(rels_bytes: bytes) -> str:
    text = rels_bytes.decode("utf-8")
    max_n = max((int(m.group(1)) for m in _REL_ID_RE.finditer(text)), default=0)
    return f"rId{max_n + 1}"


def _add_relationship(rels_bytes: bytes, *, target: str, rel_type: str) -> tuple[str, bytes]:
    rid = _next_rel_id(rels_bytes)
    text = rels_bytes.decode("utf-8")
    try:
        idx = text.rindex("</Relationships>")
    except ValueError as exc:
        raise ImageInsertError(
            "word/_rels/document.xml.rels has no </Relationships> closing tag"
        ) from exc
    entry = f'<Relationship Id="{rid}" Type="{rel_type}" Target="{_xml_escape_attr(target)}"/>'
    new_text = text[:idx] + entry + text[idx:]
    return rid, new_text.encode("utf-8")


def _next_media_filename(parts: dict[str, bytes], ext: str) -> str:
    max_n = 0
    for name in parts:
        m = _MEDIA_NAME_RE.match(name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"image{max_n + 1}.{ext}"


def _ensure_default_content_type(ct_bytes: bytes, *, extension: str, content_type: str) -> bytes:
    text = ct_bytes.decode("utf-8")
    if re.search(rf'<Default\s+Extension="{re.escape(extension)}"', text, re.IGNORECASE):
        return ct_bytes  # already declared -- idempotent, no change
    try:
        idx = text.rindex("</Types>")
    except ValueError as exc:
        raise ImageInsertError("[Content_Types].xml has no </Types> closing tag") from exc
    entry = f'<Default Extension="{extension}" ContentType="{content_type}"/>'
    new_text = text[:idx] + entry + text[idx:]
    return new_text.encode("utf-8")


def _next_doc_pr_id(document_xml: bytes) -> int:
    text = document_xml.decode("utf-8")
    max_n = max((int(m.group(1)) for m in _DOC_PR_ID_RE.finditer(text)), default=0)
    return max_n + 1


def build_drawing_xml(
    rel_id: str,
    *,
    width_emu: int,
    height_emu: int,
    name: str,
    doc_pr_id: int,
) -> str:
    """Build a minimal, valid inline ``<w:drawing>`` OOXML snippet embedding
    the image referenced by relationship ``rel_id``, sized ``width_emu`` x
    ``height_emu`` English Metric Units (914400 EMU == 1 inch)."""
    safe_name = _xml_escape_attr(name)
    return (
        "<w:drawing>"
        f'<wp:inline distT="0" distB="0" distL="0" distR="0" '
        f'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        f'<wp:extent cx="{width_emu}" cy="{height_emu}"/>'
        f'<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{doc_pr_id}" name="{safe_name}"/>'
        f"<wp:cNvGraphicFramePr>"
        f'<a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/>'
        f"</wp:cNvGraphicFramePr>"
        f'<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f"<pic:nvPicPr>"
        f'<pic:cNvPr id="{doc_pr_id}" name="{safe_name}"/>'
        f"<pic:cNvPicPr/>"
        f"</pic:nvPicPr>"
        f"<pic:blipFill>"
        f'<a:blip r:embed="{rel_id}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
        f"<a:stretch><a:fillRect/></a:stretch>"
        f"</pic:blipFill>"
        f"<pic:spPr>"
        f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{width_emu}" cy="{height_emu}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f"</pic:spPr>"
        f"</pic:pic>"
        f"</a:graphicData>"
        f"</a:graphic>"
        f"</wp:inline>"
        "</w:drawing>"
    )


def _insert_run_into_document(
    document_xml: bytes,
    *,
    run_xml: str,
    anchor_text: str | None,
    paragraph_index: int | None,
) -> tuple[bytes, dict[str, Any]]:
    text = document_xml.decode("utf-8")
    matches = list(_PARA_RE.finditer(text))

    target = None
    target_index: int | None = None

    if anchor_text is not None:
        for i, m in enumerate(matches):
            if anchor_text in _strip_tags(m.group(0)):
                target, target_index = m, i
                break
        if target is None:
            raise ImageInsertError(f"anchor_text {anchor_text!r} was not found in any paragraph")
    elif paragraph_index is not None:
        if not (0 <= paragraph_index < len(matches)):
            raise ImageInsertError(
                f"paragraph_index {paragraph_index} out of range (document has {len(matches)} paragraphs)"
            )
        target, target_index = matches[paragraph_index], paragraph_index

    if target is not None:
        block = target.group(0)
        if block.endswith("/>"):
            attrs = block[len("<w:p") : -len("/>")]
            new_block = f"<w:p{attrs}>{run_xml}</w:p>"
        else:
            new_block = block[: -len("</w:p>")] + run_xml + "</w:p>"
        new_text = text[: target.start()] + new_block + text[target.end() :]
        return new_text.encode("utf-8"), {
            "paragraph_index": target_index,
            "inserted_new_paragraph": False,
        }

    # No anchor given: append a brand-new paragraph at the end of the body.
    try:
        body_close = text.rindex("</w:body>")
    except ValueError as exc:
        raise ImageInsertError("word/document.xml has no </w:body> closing tag") from exc
    new_para = f"<w:p>{run_xml}</w:p>"
    new_text = text[:body_close] + new_para + text[body_close:]
    return new_text.encode("utf-8"), {
        "paragraph_index": len(matches),
        "inserted_new_paragraph": True,
    }


def compute_image_insert_parts(
    parts: dict[str, bytes],
    image_bytes: bytes,
    *,
    image_ext: str,
    anchor_text: str | None = None,
    paragraph_index: int | None = None,
    width_emu: int = EMU_PER_INCH,
    height_emu: int = EMU_PER_INCH,
    drawing_name: str = "Picture",
) -> ImageInsertResult:
    """Pure function: return a NEW ``{part_name: bytes}`` mapping with
    ``image_bytes`` added as inline media, referenced by a new inline
    ``<w:drawing>`` run inserted at ``anchor_text`` (first paragraph whose
    plain text contains it), ``paragraph_index`` (0-based), or -- if
    neither is given -- appended as a new paragraph at the end of the body.

    Never mutates ``parts`` or touches disk. Raises :class:`ImageInsertError`
    for any missing required part, unsupported extension, or unresolved
    anchor.
    """
    ext = image_ext.lstrip(".").lower()
    if ext not in _CONTENT_TYPE_BY_EXT:
        raise ImageInsertError(
            f"unsupported image extension {image_ext!r}; supported: {sorted(_CONTENT_TYPE_BY_EXT)}"
        )
    if not image_bytes:
        raise ImageInsertError("image_bytes must be non-empty")

    for required in ("[Content_Types].xml", "word/document.xml"):
        if required not in parts:
            raise ImageInsertError(f"missing required part: {required}")

    new_parts = dict(parts)

    media_name = _next_media_filename(new_parts, ext)
    media_part = f"word/media/{media_name}"
    new_parts[media_part] = image_bytes

    rels_bytes = new_parts.get("word/_rels/document.xml.rels", _MINIMAL_RELS_XML)
    rel_id, new_rels_bytes = _add_relationship(
        rels_bytes, target=f"media/{media_name}", rel_type=_IMAGE_REL_TYPE
    )
    new_parts["word/_rels/document.xml.rels"] = new_rels_bytes

    new_parts["[Content_Types].xml"] = _ensure_default_content_type(
        new_parts["[Content_Types].xml"],
        extension=ext,
        content_type=_CONTENT_TYPE_BY_EXT[ext],
    )

    doc_xml = new_parts["word/document.xml"]
    drawing_xml = build_drawing_xml(
        rel_id,
        width_emu=width_emu,
        height_emu=height_emu,
        name=drawing_name,
        doc_pr_id=_next_doc_pr_id(doc_xml),
    )
    new_doc_xml, insertion_meta = _insert_run_into_document(
        doc_xml,
        run_xml=f"<w:r>{drawing_xml}</w:r>",
        anchor_text=anchor_text,
        paragraph_index=paragraph_index,
    )
    new_parts["word/document.xml"] = new_doc_xml

    return ImageInsertResult(
        parts=new_parts,
        relationship_id=rel_id,
        media_part=media_part,
        paragraph_index=insertion_meta["paragraph_index"],
        inserted_new_paragraph=insertion_meta["inserted_new_paragraph"],
    )


def insert_image(
    docx_path: str | Path,
    image_path: str | Path | None = None,
    *,
    image_bytes: bytes | None = None,
    image_ext: str | None = None,
    anchor_text: str | None = None,
    paragraph_index: int | None = None,
    width_emu: int = EMU_PER_INCH,
    height_emu: int = EMU_PER_INCH,
    drawing_name: str = "Picture",
    writer: SafeOoxmlWriter | None = None,
) -> ImageInsertWriteResult:
    """Insert an image into the .docx at ``docx_path`` and commit the result
    via :class:`~safe_ooxml_writer.SafeOoxmlWriter` (validate + backup +
    atomic replace).

    Supply either ``image_path`` (read from disk, extension inferred from
    its suffix unless ``image_ext`` is given) or both ``image_bytes`` and
    ``image_ext`` directly.
    """
    if image_path is None and image_bytes is None:
        raise ImageInsertError("either image_path or image_bytes must be supplied")

    if image_path is not None:
        p = Path(image_path)
        data = p.read_bytes()
        ext = image_ext or p.suffix
    else:
        data = image_bytes  # type: ignore[assignment]
        ext = image_ext
        if not ext:
            raise ImageInsertError("image_ext is required when image_bytes is supplied directly")

    writer = writer or SafeOoxmlWriter(docx_path)
    parts = writer.read_parts()
    result = compute_image_insert_parts(
        parts,
        data,
        image_ext=ext,
        anchor_text=anchor_text,
        paragraph_index=paragraph_index,
        width_emu=width_emu,
        height_emu=height_emu,
        drawing_name=drawing_name,
    )
    write_result = writer.write_parts(result.parts)
    return ImageInsertWriteResult(
        write_result=write_result,
        relationship_id=result.relationship_id,
        media_part=result.media_part,
        paragraph_index=result.paragraph_index,
        inserted_new_paragraph=result.inserted_new_paragraph,
    )
