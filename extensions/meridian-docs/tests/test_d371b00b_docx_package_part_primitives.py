"""Tests for the d371b00b DOCX package-part primitives:

* ``remove_docx_package_part`` -- reference-counted, dry-run-capable removal
  of an unreferenced ``word/media/*`` package part + its relationship(s),
  with [Content_Types].xml cleanup.
* ``insert_docx_media_part`` -- collision-free, bijection-checked insertion
  of a brand-new image/media package member.

Fixture/style conventions mirror test_docx_image_insertion_write.py (rels +
content-types + media fixture, CAS-safe rollback pattern via a forced
verification-failure monkeypatch) and test_19be1551_insert_figure_block.py
(autouse render-capability stub so structural tests never depend on
LibreOffice/Word actually being installed on the machine running the suite).

Every fixture is built fresh under ``tmp_path``; no test ever opens, reads,
or writes any file outside its own per-test temp directory -- there is no
"canonical staging DOCX" anywhere in this repository for these tests to
accidentally touch.
"""
from __future__ import annotations

import os
import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """Every test in this file exercises STRUCTURAL/bijection correctness and
    must not depend on -- or be slowed/blocked by -- whichever render
    backends (LibreOffice, Word COM) happen to be installed on the machine
    running the suite. Mirrors test_19be1551_insert_figure_block.py's own
    autouse stub; tests that specifically care about the render gate itself
    override this per-test."""
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

_IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

# 200x100 / 300x150 px PNG headers -- the pixel payload is irrelevant to OOXML
# packaging, only the dimensions (for EMU sizing) and distinct byte identity
# (for "genuinely new / genuinely unchanged" assertions) matter.
_PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    + (200).to_bytes(4, "big") + (100).to_bytes(4, "big") + b"payload-a"
)
_PNG_2 = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    + (300).to_bytes(4, "big") + (150).to_bytes(4, "big") + b"payload-b"
)


# ---------------------------------------------------------------------------
# Base fixture: a document with ONE existing, genuinely-referenced image
# (rId7 -> word/media/image1.png) plus a plain anchor paragraph new inserts
# land at.
# ---------------------------------------------------------------------------

_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Anchor</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''

_RELS_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId7" Type="{_IMAGE_REL_TYPE}" Target="media/image1.png"/>
</Relationships>
'''

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""


def _write_docx(
    path,
    *,
    doc_xml: str = _DOC_XML,
    rels_xml: str = _RELS_XML,
    content_types_xml: str = _CONTENT_TYPES_XML,
    media: "dict[str, bytes] | None" = None,
) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", doc_xml)
        archive.writestr("word/_rels/document.xml.rels", rels_xml)
        archive.writestr("[Content_Types].xml", content_types_xml)
        for name, data in (media or {}).items():
            archive.writestr(name, data)


def _setup(tmp_path, **kwargs):
    docx_path = tmp_path / "report.docx"
    image_path = tmp_path / "chart.png"
    media = {"word/media/image1.png": _PNG}
    media.update(kwargs.pop("media", {}) or {})
    _write_docx(docx_path, media=media, **kwargs)
    image_path.write_bytes(_PNG_2)
    return str(docx_path), str(image_path)


def _read_zip_entries(path) -> "dict[str, bytes]":
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            entries[info.filename] = archive.read(info.filename)
    return entries


# ===========================================================================
# insert_docx_media_part
# ===========================================================================

def test_insert_docx_media_part_creates_new_image_with_bijection_and_extents(tmp_path):
    """New-image regression: a brand-new relationship + media part land,
    frame extents match what was requested, and the explicit post-write
    bijection check (relationship <-> media, exactly 1:1) passes."""
    docx_path, image_path = _setup(tmp_path)

    result = docs_intel.insert_docx_media_part(docx_path, image_path, anchor_para_id="P0000001")

    assert result["status"] == "inserted"
    assert result["image_name"] == "word/media/image2.png"
    assert result["relationship_id"] == "rId8"
    assert result["content_type_action"] == "default_added"
    assert result["width_emu"] > 0
    assert result["height_emu"] > 0
    assert result["render_verified"] is True

    verified = docs_intel._verify_media_part_insertion_write(
        docx_path,
        image_para_id=result["image_para_id"],
        relationship_id=result["relationship_id"],
        media_part_name=result["image_name"],
        expected_media_bytes=_PNG_2,
        expected_width_emu=result["width_emu"],
        expected_height_emu=result["height_emu"],
    )
    assert verified is None

    entries = _read_zip_entries(docx_path)
    assert entries["word/media/image2.png"] == _PNG_2
    assert entries["word/media/image1.png"] == _PNG  # untouched pre-existing media

    rels_root = ET.fromstring(entries["word/_rels/document.xml.rels"])
    matching = [c for c in rels_root if c.get("Id") == "rId8"]
    assert len(matching) == 1
    assert matching[0].get("Target") == "media/image2.png"

    content_types_root = ET.fromstring(entries["[Content_Types].xml"])
    png_default = next(c for c in content_types_root if c.get("Extension") == "png")
    assert png_default.get("ContentType") == "image/png"


def test_insert_docx_media_part_reuses_matching_default_content_type(tmp_path):
    content_types_with_png = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""
    docx_path, image_path = _setup(tmp_path, content_types_xml=content_types_with_png)

    result = docs_intel.insert_docx_media_part(docx_path, image_path, anchor_para_id="P0000001")

    assert result["status"] == "inserted"
    assert result["content_type_action"] == "default_reused"

    entries = _read_zip_entries(docx_path)
    content_types_root = ET.fromstring(entries["[Content_Types].xml"])
    png_defaults = [c for c in content_types_root if c.get("Extension") == "png"]
    assert len(png_defaults) == 1, "must not append a duplicate Default entry"


def test_insert_docx_media_part_uses_override_when_default_content_type_disagrees(tmp_path):
    conflicting_content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="application/octet-stream"/>
</Types>
"""
    docx_path, image_path = _setup(tmp_path, content_types_xml=conflicting_content_types)

    result = docs_intel.insert_docx_media_part(docx_path, image_path, anchor_para_id="P0000001")

    assert result["status"] == "inserted"
    assert result["content_type_action"] == "override_added"

    entries = _read_zip_entries(docx_path)
    content_types_root = ET.fromstring(entries["[Content_Types].xml"])
    png_default = next(c for c in content_types_root if c.get("Extension") == "png")
    # The pre-existing (disagreeing) Default is left completely alone -- it
    # may still be relied on by some other part in the package.
    assert png_default.get("ContentType") == "application/octet-stream"
    override = next(
        c for c in content_types_root
        if c.get("PartName") == f"/{result['image_name']}"
    )
    assert override.get("ContentType") == "image/png"


def test_insert_docx_media_part_relationship_ids_never_collide_with_existing_gaps(tmp_path):
    """Relationship-collision regression: rId2 is deliberately skipped in the
    starting fixture (rId1 + rId3 already used, rId3 an External hyperlink
    relationship) -- the newly generated id must land on rId4 (the true
    next-unused id), never accidentally reuse rId3 or produce a duplicate."""
    rels_with_gap = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="{_IMAGE_REL_TYPE}" Target="media/image1.png"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/" TargetMode="External"/>
</Relationships>
'''
    doc_xml_rid1 = _DOC_XML.replace('r:embed="rId7"', 'r:embed="rId1"')
    docx_path, image_path = _setup(tmp_path, doc_xml=doc_xml_rid1, rels_xml=rels_with_gap)

    result = docs_intel.insert_docx_media_part(docx_path, image_path, anchor_para_id="P0000001")

    assert result["status"] == "inserted"
    assert result["relationship_id"] == "rId4"

    entries = _read_zip_entries(docx_path)
    rels_root = ET.fromstring(entries["word/_rels/document.xml.rels"])
    ids = [c.get("Id") for c in rels_root]
    assert ids.count("rId4") == 1
    assert sorted(ids) == ["rId1", "rId3", "rId4"]


def test_insert_docx_media_part_second_insert_gets_a_distinct_id_and_part(tmp_path):
    docx_path, image_path = _setup(tmp_path)

    first = docs_intel.insert_docx_media_part(docx_path, image_path, anchor_para_id="P0000001")
    assert first["status"] == "inserted"

    second_image_path = os.path.join(os.path.dirname(image_path), "chart2.png")
    with open(second_image_path, "wb") as fh:
        fh.write(_PNG)
    second = docs_intel.insert_docx_media_part(
        docx_path, second_image_path, anchor_para_id=first["image_para_id"], position="after",
    )

    assert second["status"] == "inserted"
    assert second["relationship_id"] != first["relationship_id"]
    assert second["image_name"] != first["image_name"]

    for r, expected in ((first, _PNG_2), (second, _PNG)):
        assert docs_intel._verify_media_part_insertion_write(
            docx_path,
            image_para_id=r["image_para_id"],
            relationship_id=r["relationship_id"],
            media_part_name=r["image_name"],
            expected_media_bytes=expected,
            expected_width_emu=r["width_emu"],
            expected_height_emu=r["height_emu"],
        ) is None


def test_insert_docx_media_part_rollback_on_forced_verification_failure(tmp_path, monkeypatch):
    """Rollback regression: a forced (simulated) post-write verification
    failure -- injected AFTER the real write already succeeded -- must
    restore docx_path byte-for-byte, never leave an orphan media part or a
    dangling relationship behind."""
    docx_path, image_path = _setup(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel, "_verify_media_part_insertion_write",
        lambda *args, **kwargs: {"error": "simulated post-write verification failure"},
    )

    result = docs_intel.insert_docx_media_part(docx_path, image_path, anchor_para_id="P0000001")

    assert "error" in result
    assert result["file_restored"] is True
    assert result["concurrent_write_detected"] is False

    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "docx_path must be restored to its pre-write content on "
            "verification failure -- no orphan media part or dangling "
            "relationship may survive"
        )


def test_insert_docx_media_part_only_mutates_its_own_docx_never_a_sibling(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    sibling_path = tmp_path / "canonical_staging.docx"
    _write_docx(sibling_path, media={"word/media/image1.png": _PNG})
    sibling_before = sibling_path.read_bytes()

    result = docs_intel.insert_docx_media_part(docx_path, image_path, anchor_para_id="P0000001")

    assert result["status"] == "inserted"
    assert sibling_path.read_bytes() == sibling_before
    assert not os.path.exists(str(sibling_path) + ".bak")


def test_insert_docx_media_part_rejects_unsupported_format_without_mutating(tmp_path):
    docx_path, _image_path = _setup(tmp_path)
    bogus_path = os.path.join(os.path.dirname(docx_path), "notes.txt")
    with open(bogus_path, "wb") as fh:
        fh.write(b"not an image")
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.insert_docx_media_part(docx_path, bogus_path, anchor_para_id="P0000001")

    assert "error" in result
    assert "unsupported image format" in result["error"]
    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes


# ===========================================================================
# remove_docx_package_part
# ===========================================================================

def test_remove_docx_package_part_refuses_still_referenced_part(tmp_path):
    """The real-refusal proof required by this sprint item: a part
    genuinely referenced by word/document.xml is refused for BOTH
    dry_run=True and dry_run=False -- never a silent skip -- and the file is
    byte-for-byte untouched either way."""
    docx_path, _image_path = _setup(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    for dry_run in (True, False):
        result = docs_intel.remove_docx_package_part(
            docx_path, "word/media/image1.png", dry_run=dry_run,
        )
        assert "error" in result
        assert result["status"] == "refused_still_referenced"
        assert result["reference_count"] == 1
        assert result["referencing_relationship_ids"] == ["rId7"]

    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "a refused removal (dry_run or real) must never mutate the file"
        )


_ORPHAN_DOC_XML = _DOC_XML  # IMG000001 references only rId7, never rId8

_ORPHAN_RELS_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId7" Type="{_IMAGE_REL_TYPE}" Target="media/image1.png"/>
  <Relationship Id="rId8" Type="{_IMAGE_REL_TYPE}" Target="media/image2.png"/>
</Relationships>
'''

_ORPHAN_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""


def _setup_with_orphan(tmp_path) -> str:
    """word/media/image2.png (rId8) has a relationship but is NEVER
    referenced anywhere in word/document.xml -- a genuinely orphaned media
    part, sharing its 'png' extension with the still-referenced image1.png
    (rId7)."""
    docx_path = tmp_path / "report.docx"
    _write_docx(
        docx_path,
        doc_xml=_ORPHAN_DOC_XML, rels_xml=_ORPHAN_RELS_XML,
        content_types_xml=_ORPHAN_CONTENT_TYPES_XML,
        media={"word/media/image1.png": _PNG, "word/media/image2.png": _PNG_2},
    )
    return str(docx_path)


def test_remove_docx_package_part_dry_run_reports_orphan_without_mutating(tmp_path):
    """Orphan-media regression, dry-run half: reports exactly what would be
    removed for a genuinely unreferenced part without touching the zip."""
    docx_path = _setup_with_orphan(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.remove_docx_package_part(docx_path, "word/media/image2.png", dry_run=True)

    assert result["status"] == "dry_run"
    assert result["reference_count"] == 0
    assert result["would_remove"]["part_name"] == "word/media/image2.png"
    assert result["would_remove"]["relationship_ids"] == ["rId8"]
    # image1.png is still png and still needs the shared Default entry.
    assert result["would_remove"]["content_type_defaults_removed"] == []

    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes, "dry_run must never touch the zip"


def test_remove_docx_package_part_removes_orphan_and_keeps_shared_default_content_type(tmp_path):
    """Orphan-media regression, real-removal half: the orphaned part and its
    relationship are actually removed; the shared 'png' Default is KEPT
    because the still-referenced image1.png needs it."""
    docx_path = _setup_with_orphan(tmp_path)

    result = docs_intel.remove_docx_package_part(docx_path, "word/media/image2.png", dry_run=False)

    assert result["status"] == "removed"
    assert result["relationship_ids_removed"] == ["rId8"]
    assert result["content_type_overrides_removed"] == []
    assert result["content_type_defaults_removed"] == []
    assert result["render_verified"] is True

    entries = _read_zip_entries(docx_path)
    assert "word/media/image2.png" not in entries
    assert entries["word/media/image1.png"] == _PNG  # untouched, still referenced

    rels_root = ET.fromstring(entries["word/_rels/document.xml.rels"])
    ids = {c.get("Id") for c in rels_root}
    assert ids == {"rId7"}

    content_types_root = ET.fromstring(entries["[Content_Types].xml"])
    png_defaults = [c for c in content_types_root if c.get("Extension") == "png"]
    assert len(png_defaults) == 1, "image1.png (still png) still needs the shared Default"

    assert docs_intel._verify_part_removal_write(
        docx_path, removed_part_name="word/media/image2.png",
        removed_relationship_ids=["rId8"],
    ) is None


def test_remove_docx_package_part_removes_default_content_type_when_last_of_its_extension(tmp_path):
    """Content-type cleanup regression: when the removed part was the ONLY
    package member of its extension, the shared Default entry itself is
    dropped too -- not just the part-specific relationship."""
    doc_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Anchor</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''
    rels_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId9" Type="{_IMAGE_REL_TYPE}" Target="media/image9.gif"/>
</Relationships>
'''
    content_types_xml = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="gif" ContentType="image/gif"/>
</Types>
"""
    docx_path = tmp_path / "orphan_only.docx"
    _write_docx(
        docx_path, doc_xml=doc_xml, rels_xml=rels_xml, content_types_xml=content_types_xml,
        media={"word/media/image9.gif": b"unreferenced-gif-bytes"},
    )

    result = docs_intel.remove_docx_package_part(str(docx_path), "word/media/image9.gif", dry_run=False)

    assert result["status"] == "removed"
    assert result["content_type_defaults_removed"] == ["gif"]

    entries = _read_zip_entries(str(docx_path))
    content_types_root = ET.fromstring(entries["[Content_Types].xml"])
    assert not any(c.get("Extension") == "gif" for c in content_types_root)


def test_remove_docx_package_part_refuses_non_media_part(tmp_path):
    docx_path = _setup_with_orphan(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.remove_docx_package_part(docx_path, "word/document.xml", dry_run=False)

    assert "error" in result
    assert "word/media" in result["error"]
    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes


def test_remove_docx_package_part_refuses_missing_part(tmp_path):
    docx_path = _setup_with_orphan(tmp_path)

    result = docs_intel.remove_docx_package_part(
        docx_path, "word/media/does_not_exist.png", dry_run=False,
    )

    assert "error" in result
    assert "not found" in result["error"]


def test_remove_docx_package_part_rollback_on_forced_verification_failure(tmp_path, monkeypatch):
    """Rollback regression (removal side): a forced post-write verification
    failure -- injected AFTER the real removal already succeeded -- must
    restore docx_path byte-for-byte."""
    docx_path = _setup_with_orphan(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel, "_verify_part_removal_write",
        lambda *args, **kwargs: {"error": "simulated post-write verification failure"},
    )

    result = docs_intel.remove_docx_package_part(docx_path, "word/media/image2.png", dry_run=False)

    assert "error" in result
    assert result["file_restored"] is True
    assert result["concurrent_write_detected"] is False

    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "docx_path must be restored to its pre-write content on "
            "verification failure"
        )


# ===========================================================================
# Shared bijection helper -- direct unit coverage
# ===========================================================================

def test_docx_media_bijection_report_detects_orphans_dangling_and_shared():
    rels_root = ET.fromstring(f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdOrphanTarget" Type="{_IMAGE_REL_TYPE}" Target="media/dangling.png"/>
  <Relationship Id="rIdA" Type="{_IMAGE_REL_TYPE}" Target="media/shared.png"/>
  <Relationship Id="rIdB" Type="{_IMAGE_REL_TYPE}" Target="media/shared.png"/>
</Relationships>
''')
    media_part_names = {"word/media/shared.png", "word/media/unreferenced.png"}

    report = docs_intel._docx_media_bijection_report(media_part_names, rels_root)

    assert report["orphaned_media"] == ["word/media/unreferenced.png"]
    assert report["dangling_relationships"] == ["rIdOrphanTarget"]
    assert report["shared_media"] == {"word/media/shared.png": ["rIdA", "rIdB"]}
