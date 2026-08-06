"""conftest.py -- shared fixtures for tools/meridian_fallbacks/tests.

Builds minimal, VALID synthetic .docx files entirely in memory (no network,
no real Word/LibreOffice, no dependency on any real document anywhere on
disk -- including, per this package's own ground rule, the canonical
thesis DOCX, which nothing under this test package ever touches or even
references).
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
).encode("utf-8")

_PACKAGE_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
).encode("utf-8")

_EMPTY_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    "</Relationships>"
).encode("utf-8")


def make_document_xml(paragraphs: "list[str] | None" = None) -> bytes:
    """Build a minimal ``word/document.xml`` with one ``<w:p>`` per string in
    ``paragraphs`` (default: three simple paragraphs). One line, no pretty
    whitespace -- matches how Word/python-docx actually serialize this part,
    and what safe_image_insert's paragraph-splicing regex expects."""
    if paragraphs is None:
        paragraphs = ["Introduction", "Body paragraph.", "Another paragraph."]
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body>"
        "</w:document>"
    )
    return xml.encode("utf-8")


def make_minimal_docx_parts(paragraphs: "list[str] | None" = None) -> dict[str, bytes]:
    """Return a ``{part_name: bytes}`` mapping for a minimal but fully valid
    .docx: satisfies ``safe_ooxml_writer.REQUIRED_PARTS`` and includes an
    (empty) ``word/_rels/document.xml.rels`` so image-insertion tests have a
    real relationships part to extend rather than relying on
    ``safe_image_insert``'s "part missing -> synthesize a minimal one"
    fallback path (that path is covered by its own dedicated test)."""
    return {
        "[Content_Types].xml": _CONTENT_TYPES_XML,
        "_rels/.rels": _PACKAGE_RELS_XML,
        "word/document.xml": make_document_xml(paragraphs),
        "word/_rels/document.xml.rels": _EMPTY_RELS_XML,
    }


def zip_parts(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def minimal_docx_parts() -> dict[str, bytes]:
    return make_minimal_docx_parts()


@pytest.fixture
def docx_path(tmp_path: Path) -> Path:
    """Write a minimal, valid synthetic .docx to a temp file and return its
    path. Each test gets its own ``tmp_path``, so this is never shared
    across tests and never touches anything outside pytest's own temp dir."""
    path = tmp_path / "sample.docx"
    path.write_bytes(zip_parts(make_minimal_docx_parts()))
    return path


@pytest.fixture
def fake_image_bytes() -> bytes:
    """An opaque, non-empty byte string used to exercise image-insertion
    code paths. This package never decodes or validates image CONTENT --
    only routes bytes by file extension -- so a real PNG encoder is not
    needed here, only a real PNG *signature* prefix for readability."""
    return b"\x89PNG\r\n\x1a\n" + b"synthetic-test-image-payload-not-a-real-image"
