"""Contract tests for Word-compatible OOXML package handling.

These tests intentionally target the small public surface of
``meridian_docs.ooxml_integrity`` rather than the implementation in
``docs_intel``.  The contract is deliberately stricter than "the ZIP opens"
because Word's repair dialog is commonly caused by a dangling relationship,
stale content-type override, duplicate relationship id, or lost namespace
binding that a ZIP reader and LibreOffice will both tolerate.

The expected public API is:

* ``validate_docx_package(source, *, include_warnings=True) -> dict`` with an
  ``ok`` boolean and serializable ``issues``/``warnings`` lists;
* ``audit_heading_capitalization(source) -> list[dict]``;
* ``serialize_document_xml_preserving_namespaces(original_xml, root) -> bytes``;
* ``prune_unreferenced_document_media(source) -> bytes``.

No production code is imported indirectly through a private write helper in
this file.  That keeps these tests usable as a release gate for any future
writer, including writers implemented outside ``docs_intel``.
"""
from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import ooxml_integrity


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _q(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _package_parts(*, second_image: bool = True, image_override: bool = False) -> dict[str, bytes]:
    """Return a minimal but relationship-complete DOCX package."""
    overrides = [
        '<ct:Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    ]
    if image_override:
        overrides.append('<ct:Override PartName="/word/media/image1.png" ContentType="image/png"/>')
        if second_image:
            overrides.append('<ct:Override PartName="/word/media/image2.png" ContentType="image/png"/>')

    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:w14="{W14}" xmlns:r="{R}" xmlns:a="{A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Fixture.</w:t></w:r></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:drawing><a:blip r:embed="rIdImage1"/></w:drawing></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''.encode("utf-8")

    document_rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<pr:Relationships xmlns:pr="{PKG_REL}">
  <pr:Relationship Id="rIdImage1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
  {('<pr:Relationship Id="rIdImage2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image2.png"/>' if second_image else '')}
  <pr:Relationship Id="rIdExternal" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.test" TargetMode="External"/>
</pr:Relationships>'''.encode("utf-8")

    parts = {
        "[Content_Types].xml": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<ct:Types xmlns:ct="{CT}">\n'
            f'  <ct:Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            f'  <ct:Default Extension="xml" ContentType="application/xml"/>\n'
            f'  <ct:Default Extension="png" ContentType="image/png"/>\n'
            f'  {"".join(overrides)}\n'
            f'</ct:Types>'
        ).encode("utf-8"),
        "_rels/.rels": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<pr:Relationships xmlns:pr="{PKG_REL}">
  <pr:Relationship Id="rIdOfficeDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</pr:Relationships>'''.encode("utf-8"),
        "word/document.xml": document_xml,
        "word/_rels/document.xml.rels": document_rels,
        "word/media/image1.png": b"\x89PNG\r\n\x1a\nfake-image-1",
    }
    if second_image:
        parts["word/media/image2.png"] = b"\x89PNG\r\n\x1a\nfake-image-2"
    return parts


def _zip_bytes(parts: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return stream.getvalue()


def _issue_codes(report: dict) -> set[str]:
    return {issue["code"] for issue in report.get("issues", [])}


def _issue_parts(report: dict, code: str) -> set[str | None]:
    return {issue.get("part") for issue in report.get("issues", []) if issue["code"] == code}


def test_validate_accepts_relationship_complete_package():
    report = ooxml_integrity.validate_docx_package(_zip_bytes(_package_parts()))

    assert report["ok"] is True, report
    assert report["issues"] == []
    assert report["part_count"] == 6


def test_validate_rejects_malformed_xml_even_when_zip_is_valid():
    parts = _package_parts()
    parts["word/document.xml"] = b"<w:document xmlns:w=\"urn:broken\"><w:body>"

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is False
    assert "xml_parse_error" in _issue_codes(report)
    assert "word/document.xml" in _issue_parts(report, "xml_parse_error")


def test_validate_rejects_dangling_relationship_target():
    parts = _package_parts()
    rels_name = "word/_rels/document.xml.rels"
    parts[rels_name] = parts[rels_name].replace(
        b'Target="media/image2.png"', b'Target="media/missing.png"'
    )

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is False
    assert "dangling_relationship" in _issue_codes(report)
    assert rels_name in _issue_parts(report, "dangling_relationship")


def test_validate_rejects_duplicate_relationship_ids():
    parts = _package_parts()
    rels_name = "word/_rels/document.xml.rels"
    parts[rels_name] = parts[rels_name].replace(b'Id="rIdImage2"', b'Id="rIdImage1"')

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is False
    assert "duplicate_relationship_id" in _issue_codes(report)
    assert rels_name in _issue_parts(report, "duplicate_relationship_id")


def test_validate_rejects_dangling_document_relationship_reference():
    parts = _package_parts()
    parts["word/document.xml"] = parts["word/document.xml"].replace(
        b'r:embed="rIdImage1"', b'r:embed="rIdDoesNotExist"'
    )

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is False
    assert "dangling_relationship_reference" in _issue_codes(report)
    assert "word/document.xml" in _issue_parts(report, "dangling_relationship_reference")


def test_validate_rejects_dangling_reference_when_rels_part_is_missing():
    """PAPER-S9 (2026-08-30) found validate_docx_package returned ok=True on
    an r:id reference whose owning .rels part declares zero relationships:
    the old `if not rel_ids: continue` guard skipped the check entirely
    instead of treating an unresolvable reference as dangling."""
    parts = _package_parts()
    del parts["word/_rels/document.xml.rels"]

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is False
    assert "dangling_relationship_reference" in _issue_codes(report)
    assert "word/document.xml" in _issue_parts(report, "dangling_relationship_reference")


def test_validate_rejects_dangling_reference_when_rels_part_is_empty():
    parts = _package_parts()
    rels_name = "word/_rels/document.xml.rels"
    parts[rels_name] = f'<?xml version="1.0" encoding="UTF-8"?>\n<pr:Relationships xmlns:pr="{PKG_REL}"/>'.encode("utf-8")

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is False
    assert "dangling_relationship_reference" in _issue_codes(report)
    assert "word/document.xml" in _issue_parts(report, "dangling_relationship_reference")


def test_validate_rejects_duplicate_zip_part_name():
    """PAPER-S9 (2026-08-30) found validate_docx_package returned ok=True on
    a ZIP containing word/document.xml written twice with different content
    -- benchmark-preregistration-v0.md's own failure taxonomy names this
    "last-write-wins on read, never healed on write", but nothing checked
    for it before this fix."""
    raw = _zip_bytes(_package_parts())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        parts = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
        archive.writestr("word/document.xml", parts["word/document.xml"] + b"<!-- second copy -->")

    report = ooxml_integrity.validate_docx_package(stream.getvalue())

    assert report["ok"] is False
    assert "duplicate_zip_part" in _issue_codes(report)
    assert "word/document.xml" in _issue_parts(report, "duplicate_zip_part")


def test_validate_rejects_stale_content_type_override():
    parts = _package_parts()
    content_types = "[Content_Types].xml"
    parts[content_types] = parts[content_types].replace(
        b'<ct:Override PartName="/word/document.xml"',
        b'<ct:Override PartName="/word/deleted-document.xml"',
    )

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is False
    assert "dangling_content_type" in _issue_codes(report)
    assert content_types in _issue_parts(report, "dangling_content_type")


def test_validate_reports_duplicate_para_ids_as_warning_not_error():
    parts = _package_parts()
    parts["word/document.xml"] = parts["word/document.xml"].replace(
        b'paraId="P0000002"', b'paraId="P0000001"'
    )

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is True, report
    assert {w["code"] for w in report["warnings"]} == {"duplicate_para_id"}
    without_warnings = ooxml_integrity.validate_docx_package(
        _zip_bytes(parts), include_warnings=False
    )
    assert without_warnings["warnings"] == []


def test_validate_reports_heading_case_drift_as_warning():
    parts = _package_parts(second_image=False)
    parts["word/styles.xml"] = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{W}">
  <w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="Heading 3"/></w:style>
</w:styles>'''.encode("utf-8")
    parts["word/document.xml"] = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}" xmlns:w14="{W14}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:pPr><w:pStyle w:val="Heading3"/></w:pPr><w:r><w:t>AI-assisted tasks and scope</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''.encode("utf-8")

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is True, report
    assert {w["code"] for w in report["warnings"]} == {"heading_case_inconsistency"}
    assert "AI-assisted tasks and scope" in report["warnings"][0]["message"]


def test_validate_reports_hidden_bold_heading_like_case_drift():
    parts = _package_parts(second_image=False)
    parts["word/styles.xml"] = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{W}">
  <w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>'''.encode("utf-8")
    parts["word/document.xml"] = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}" xmlns:w14="{W14}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:pPr><w:pStyle w:val="Normal"/><w:rPr><w:vanish/><w:specVanish/></w:rPr></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>Reporting aggregation families.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''.encode("utf-8")

    report = ooxml_integrity.validate_docx_package(_zip_bytes(parts))

    assert report["ok"] is True, report
    assert {w["code"] for w in report["warnings"]} == {"heading_case_inconsistency"}
    assert report["warnings"][0]["style"] == "hidden-bold-heading-like"


def test_validate_rejects_bad_zip_container():
    report = ooxml_integrity.validate_docx_package(b"not-a-docx")

    assert report["ok"] is False
    assert _issue_codes(report) == {"bad_zip"}
    assert report["part_count"] == 0


def test_serialize_preserves_used_and_unused_namespace_prefixes():
    lxml_etree = pytest.importorskip("lxml.etree")
    custom_uri = "urn:meridian:test:custom"
    unused_uri = "urn:meridian:test:unused"
    original = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:w14="{W14}" xmlns:mc="{MC}"
    xmlns:zzcustom="{custom_uri}" xmlns:unused9="{unused_uri}" mc:Ignorable="w14 unused9">
  <w:body><w:p w14:paraId="P0000001"><zzcustom:extra/></w:p><w:sectPr/></w:body>
</w:document>'''.encode("utf-8")
    root = ET.fromstring(original)
    paragraph = root.find(f".//{_q(W, 'p')}")
    assert paragraph is not None

    result = ooxml_integrity.serialize_document_xml_preserving_namespaces(original, root)
    parsed = lxml_etree.fromstring(result)

    assert dict(parsed.nsmap) == {
        "w": W,
        "w14": W14,
        "mc": MC,
        "zzcustom": custom_uri,
        "unused9": unused_uri,
    }
    assert parsed.get(f"{{{MC}}}Ignorable") == "w14 unused9"
    assert parsed.xpath("name(.//zzcustom:extra)", namespaces={"zzcustom": custom_uri}) == "zzcustom:extra"


def test_serialize_rejects_new_namespace_not_present_in_source():
    original = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}"><w:body><w:sectPr/></w:body></w:document>'''.encode("utf-8")
    root = ET.fromstring(original)
    root.set("{urn:meridian:test:new}flag", "1")

    with pytest.raises(ooxml_integrity.DocxPackageIntegrityError):
        ooxml_integrity.serialize_document_xml_preserving_namespaces(original, root)


def test_serialize_rejects_malformed_source_xml():
    with pytest.raises(ooxml_integrity.DocxPackageIntegrityError):
        ooxml_integrity.serialize_document_xml_preserving_namespaces(
            b"<w:document>", ET.Element(_q(W, "document"))
        )


def test_prune_removes_only_unreferenced_document_media_and_relationship():
    raw = _zip_bytes(_package_parts())

    pruned = ooxml_integrity.prune_unreferenced_document_media(raw)

    with zipfile.ZipFile(io.BytesIO(pruned)) as archive:
        names = set(archive.namelist())
        rels = archive.read("word/_rels/document.xml.rels")
    assert "word/media/image1.png" in names
    assert "word/media/image2.png" not in names
    assert b'rIdImage1' in rels
    assert b'rIdImage2' not in rels
    assert b'rIdExternal' in rels
    assert ooxml_integrity.validate_docx_package(pruned)["ok"] is True


def test_prune_is_byte_identical_when_no_document_media_is_orphaned():
    parts = _package_parts(second_image=False)
    # Remove the otherwise-unreferenced image relationship as well.
    rels_name = "word/_rels/document.xml.rels"
    parts[rels_name] = parts[rels_name].replace(
        b'\n  <pr:Relationship Id="rIdExternal" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.test" TargetMode="External"/>',
        b"",
    )
    raw = _zip_bytes(parts)

    assert ooxml_integrity.prune_unreferenced_document_media(raw) == raw


def test_prune_keeps_shared_media_until_all_references_are_gone():
    parts = _package_parts(second_image=False)
    parts["word/document.xml"] = parts["word/document.xml"].replace(
        b'    <w:sectPr/>',
        b'    <w:p w14:paraId="P0000003"><w:r><w:drawing><a:blip r:link="rIdShared"/></w:drawing></w:r></w:p>\n    <w:sectPr/>',
    )
    parts["word/_rels/document.xml.rels"] = parts["word/_rels/document.xml.rels"].replace(
        b'<pr:Relationship Id="rIdExternal"',
        b'<pr:Relationship Id="rIdShared" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>\n  <pr:Relationship Id="rIdExternal"',
    )
    raw = _zip_bytes(parts)

    pruned = ooxml_integrity.prune_unreferenced_document_media(raw)

    with zipfile.ZipFile(io.BytesIO(pruned)) as archive:
        names = set(archive.namelist())
        rels = archive.read("word/_rels/document.xml.rels")
    assert "word/media/image1.png" in names
    assert b'rIdImage1' in rels
    assert b'rIdShared' in rels


def test_prune_does_not_delete_header_media():
    parts = _package_parts(second_image=False)
    parts.update(
        {
            "word/header1.xml": f'<w:hdr xmlns:w="{W}" xmlns:r="{R}"><w:p><w:r><w:drawing><a:blip xmlns:a="{A}" r:embed="rIdHeaderImage"/></w:drawing></w:r></w:p></w:hdr>'.encode(),
            "word/_rels/header1.xml.rels": f'<pr:Relationships xmlns:pr="{PKG_REL}"><pr:Relationship Id="rIdHeaderImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/header.png"/></pr:Relationships>'.encode(),
            "word/media/header.png": b"header-image",
        }
    )
    raw = _zip_bytes(parts)

    pruned = ooxml_integrity.prune_unreferenced_document_media(raw)

    with zipfile.ZipFile(io.BytesIO(pruned)) as archive:
        names = set(archive.namelist())
    assert "word/media/header.png" in names
