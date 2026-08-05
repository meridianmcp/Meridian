"""efa6cb53 -- prove safe brand-new DOCX image relationship and media
insertion, end to end, on a DISPOSABLE document.

The sprint item this file exists for: the image-insertion write path is not
production-safe until a disposable DOCX (never the canonical/staging
document a caller actually cares about) receives a GENUINELY NEW media part
and relationship, that write passes ZIP/XML/relationship/media structural
verification, and the resulting document reports a legal render/readback
status through the render_gate three-state contract (mirrors Word/COM
readback "when available" -- see render_gate.py's own module docstring for
why this module never assumes a render backend is installed).

Every fixture here is built fresh under ``tmp_path`` and no test ever opens,
reads, or writes any file outside of that per-test temp directory -- there
is no "canonical staging DOCX" anywhere in this repository for these tests
to accidentally touch, and the isolation test below asserts that directly by
placing a second, untouched sibling document alongside the one under test.

Render backends are always dependency-injected fakes (mirrors
test_docx_render_gate.py's own stated rationale: "none of these tests depend
on LibreOffice / Word actually being installed on whatever machine runs the
suite") -- this file proves the CONTRACT integrates correctly with a
freshly-written insert_image() output, without ever spawning a real Word/
LibreOffice process during the test run.
"""
from __future__ import annotations

import os
import zipfile
import xml.etree.ElementTree as ET

from meridian_docs import docs_intel, render_gate


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
_PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    + (200).to_bytes(4, "big") + (100).to_bytes(4, "big") + b"payload"
)

# A second, distinguishable PNG payload for the "two genuinely separate
# media parts" test below.
_PNG_2 = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    + (300).to_bytes(4, "big") + (150).to_bytes(4, "big") + b"a-different-payload"
)


def _write_docx(path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOC_XML)
        archive.writestr("word/_rels/document.xml.rels", _RELS_XML)
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)


def _setup(tmp_path):
    docx_path = tmp_path / "report.docx"
    image_path = tmp_path / "chart.png"
    _write_docx(docx_path)
    image_path.write_bytes(_PNG)
    return str(docx_path), str(image_path)


def _read_zip_entries(path) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            entries[info.filename] = archive.read(info.filename)
    return entries


def _write_zip_entries(path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


# ---------------------------------------------------------------------------
# 1. A genuinely new media part + relationship, fully verified fresh from disk
# ---------------------------------------------------------------------------

def test_insert_image_into_disposable_docx_creates_genuinely_new_relationship_and_media(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    image_bytes = (tmp_path / "chart.png").read_bytes()

    result = docs_intel.insert_image(docx_path, image_path, anchor_para_id="P0000001")

    assert result["status"] == "inserted"
    assert result["image_para_id"]
    assert result["image_name"] == "word/media/image1.png"

    # The independent post-write verifier (efa6cb53) -- re-reads FRESH FROM
    # DISK and confirms the paragraph, relationship, media part, its bytes,
    # and its content-type declaration all genuinely landed.
    verified = docs_intel._verify_image_insertion_write(
        docx_path,
        image_para_id=result["image_para_id"],
        expected_image_bytes=image_bytes,
    )
    assert verified is None

    with zipfile.ZipFile(docx_path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        body = document.find(f"{{{_W}}}body")
        image_para = next(
            p for p in body if p.get(f"{{{_W14}}}paraId") == result["image_para_id"]
        )
        blip = image_para.find(f".//{{{_A}}}blip")
        relationship_id = blip.get(f"{{{_R}}}embed")
        assert relationship_id == "rId1"
        assert archive.read(result["image_name"]) == _PNG

        rels = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
        relationship = next(child for child in rels if child.get("Id") == relationship_id)
        assert relationship.get("Target") == "media/image1.png"

        content_types = ET.fromstring(archive.read("[Content_Types].xml"))
        png_default = next(
            child for child in content_types if child.get("Extension") == "png"
        )
        assert png_default.get("ContentType") == "image/png"


def test_insert_image_second_insert_gets_a_distinct_relationship_and_media_part(tmp_path):
    """Proves 'genuinely new' holds up on a second insert too -- the second
    image must not reuse the first's relationship id or media part name, and
    both must independently verify against their own original bytes."""
    docx_path, image_path = _setup(tmp_path)
    first_bytes = (tmp_path / "chart.png").read_bytes()

    first = docs_intel.insert_image(docx_path, image_path, anchor_para_id="P0000001")
    assert first["status"] == "inserted"

    second_image_path = tmp_path / "chart2.png"
    second_image_path.write_bytes(_PNG_2)

    second = docs_intel.insert_image(
        docx_path, str(second_image_path),
        anchor_para_id=first["image_para_id"], position="after",
    )
    assert second["status"] == "inserted"
    assert second["image_name"] != first["image_name"]
    assert second["image_para_id"] != first["image_para_id"]

    with zipfile.ZipFile(docx_path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        body = document.find(f"{{{_W}}}body")
        first_para = next(p for p in body if p.get(f"{{{_W14}}}paraId") == first["image_para_id"])
        second_para = next(p for p in body if p.get(f"{{{_W14}}}paraId") == second["image_para_id"])
        first_rid = first_para.find(f".//{{{_A}}}blip").get(f"{{{_R}}}embed")
        second_rid = second_para.find(f".//{{{_A}}}blip").get(f"{{{_R}}}embed")
        assert first_rid != second_rid
        assert archive.read(first["image_name"]) == _PNG
        assert archive.read(second["image_name"]) == _PNG_2

    assert docs_intel._verify_image_insertion_write(
        docx_path, image_para_id=first["image_para_id"], expected_image_bytes=first_bytes,
    ) is None
    assert docs_intel._verify_image_insertion_write(
        docx_path, image_para_id=second["image_para_id"], expected_image_bytes=_PNG_2,
    ) is None


# ---------------------------------------------------------------------------
# 2. The verifier itself must catch real tampering, not just rubber-stamp
# ---------------------------------------------------------------------------

def test_verify_image_insertion_write_detects_tampering_after_a_genuine_write(tmp_path):
    docx_path, image_path = _setup(tmp_path)
    image_bytes = (tmp_path / "chart.png").read_bytes()

    result = docs_intel.insert_image(docx_path, image_path, anchor_para_id="P0000001")
    assert result["status"] == "inserted"
    para_id = result["image_para_id"]

    wrong_bytes = docs_intel._verify_image_insertion_write(
        docx_path, image_para_id=para_id, expected_image_bytes=b"not the real image bytes",
    )
    assert wrong_bytes is not None
    assert "does not match the image bytes" in wrong_bytes["error"]

    wrong_para = docs_intel._verify_image_insertion_write(
        docx_path, image_para_id="not-a-real-para-id", expected_image_bytes=image_bytes,
    )
    assert wrong_para is not None and "not found" in wrong_para["error"]

    entries = _read_zip_entries(docx_path)

    # -- the relationship the image depends on is missing entirely --
    rels_root = ET.fromstring(entries["word/_rels/document.xml.rels"])
    for child in list(rels_root):
        if child.get("Id") == "rId1":
            rels_root.remove(child)
    entries_missing_rel = dict(entries)
    entries_missing_rel["word/_rels/document.xml.rels"] = ET.tostring(
        rels_root, encoding="utf-8", xml_declaration=True
    )
    missing_rel_path = tmp_path / "missing_rel.docx"
    _write_zip_entries(missing_rel_path, entries_missing_rel)
    missing_rel = docs_intel._verify_image_insertion_write(
        str(missing_rel_path), image_para_id=para_id, expected_image_bytes=image_bytes,
    )
    assert missing_rel is not None
    assert "relationship" in missing_rel["error"].lower() and "rId1" in missing_rel["error"]

    # -- the relationship survives but the media part it targets is gone --
    entries_missing_media = dict(entries)
    del entries_missing_media["word/media/image1.png"]
    missing_media_path = tmp_path / "missing_media.docx"
    _write_zip_entries(missing_media_path, entries_missing_media)
    missing_media = docs_intel._verify_image_insertion_write(
        str(missing_media_path), image_para_id=para_id, expected_image_bytes=image_bytes,
    )
    assert missing_media is not None
    assert "not actually present in the ZIP package" in missing_media["error"]

    # -- the media part is present but its bytes were corrupted/replaced --
    entries_corrupt = dict(entries)
    entries_corrupt["word/media/image1.png"] = b"corrupted-bytes-not-the-real-image"
    corrupt_path = tmp_path / "corrupt_media.docx"
    _write_zip_entries(corrupt_path, entries_corrupt)
    corrupt = docs_intel._verify_image_insertion_write(
        str(corrupt_path), image_para_id=para_id, expected_image_bytes=image_bytes,
    )
    assert corrupt is not None and "corrupted or mismatched" in corrupt["error"]

    # -- [Content_Types].xml no longer declares a type for the extension --
    content_types_root = ET.fromstring(entries["[Content_Types].xml"])
    for child in list(content_types_root):
        if child.get("Extension", "").lower() == "png":
            content_types_root.remove(child)
    entries_no_ct = dict(entries)
    entries_no_ct["[Content_Types].xml"] = ET.tostring(
        content_types_root, encoding="utf-8", xml_declaration=True
    )
    no_ct_path = tmp_path / "no_content_type.docx"
    _write_zip_entries(no_ct_path, entries_no_ct)
    no_ct = docs_intel._verify_image_insertion_write(
        str(no_ct_path), image_para_id=para_id, expected_image_bytes=image_bytes,
    )
    assert no_ct is not None
    assert "Content_Types" in no_ct["error"] and "content type" in no_ct["error"]


# ---------------------------------------------------------------------------
# 3. Fail-closed: a verification failure must restore, never leave an orphan
# ---------------------------------------------------------------------------

def test_insert_image_verification_failure_restores_disposable_docx_and_fails_closed(tmp_path, monkeypatch):
    """Mirrors the established
    test_insert_figure_block_verification_failure_restores_original_docx
    pattern: force the verification helper to report failure regardless of
    the (real, successful) write that just happened, then confirm the
    disposable document is restored byte-for-byte -- no orphan media part
    and no dangling relationship survive a verification failure."""
    docx_path, image_path = _setup(tmp_path)
    with open(docx_path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(
        docs_intel, "_verify_image_insertion_write",
        lambda *args, **kwargs: {"error": "simulated post-write verification failure"},
    )

    result = docs_intel.insert_image(docx_path, image_path, anchor_para_id="P0000001")

    assert "error" in result
    assert result["file_restored"] is True
    assert result["concurrent_write_detected"] is False
    assert result["image_para_id"]
    assert result["docx_path"] == docx_path

    with open(docx_path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "docx_path must be restored to its pre-write content on "
            "verification failure -- never left with an orphan media part "
            "and a dangling relationship"
        )


# ---------------------------------------------------------------------------
# 4. Isolation: never touches any document other than the one passed in
# ---------------------------------------------------------------------------

def test_insert_image_only_mutates_its_own_docx_path_never_a_sibling_document(tmp_path):
    """Regression guard for the sprint item's 'do not modify the canonical
    staging DOCX' requirement: insert_image (and its backup/restore
    machinery) must only ever touch the exact docx_path it was given, never
    any other document sitting alongside it -- including on a verification
    failure, where a careless restore implementation could plausibly reach
    for the wrong file."""
    docx_path, image_path = _setup(tmp_path)

    canonical_path = tmp_path / "canonical_staging.docx"
    _write_docx(canonical_path)
    canonical_before = canonical_path.read_bytes()

    result = docs_intel.insert_image(docx_path, image_path, anchor_para_id="P0000001")

    assert result["status"] == "inserted"
    assert canonical_path.read_bytes() == canonical_before
    assert not os.path.exists(str(canonical_path) + ".bak")


# ---------------------------------------------------------------------------
# 5. Render/readback gate integration (render_gate's three-state contract)
# ---------------------------------------------------------------------------

def test_freshly_inserted_image_docx_reports_rendered_via_injected_word_com_style_backend(tmp_path):
    """Simulates a Word/COM (or LibreOffice) readback succeeding for the
    freshly-written disposable document -- the "opens or renders/readbacks
    successfully through Word/COM when available" half of the sprint item's
    gate. The backend is injected (never a real Word/soffice process) so
    this test is deterministic on any machine, matching
    test_docx_render_gate.py's own stated rationale."""
    docx_path, image_path = _setup(tmp_path)
    result = docs_intel.insert_image(docx_path, image_path, anchor_para_id="P0000001")
    assert result["status"] == "inserted"

    seen_paths: list[str] = []

    def _fake_render(path: str):
        seen_paths.append(path)
        return {"converted_via": "word-com", "output_filename": "render_probe.pdf"}

    backend = render_gate.RenderBackend(
        name="fake-word-com", unavailable_reason=lambda: None, render=_fake_render,
    )

    readback = render_gate.check_render_capability(docx_path, backends=[backend])

    assert readback["status"] == render_gate.RENDERED
    assert readback["backend"] == "fake-word-com"
    assert seen_paths == [docx_path]


def test_freshly_inserted_image_docx_reports_unavailable_not_rendered_when_no_backend_present(tmp_path):
    """The other legal state for an environment with no render backend
    installed -- never silently reported as 'rendered', and never a crash."""
    docx_path, image_path = _setup(tmp_path)
    result = docs_intel.insert_image(docx_path, image_path, anchor_para_id="P0000001")
    assert result["status"] == "inserted"

    readback = render_gate.check_render_capability(docx_path, backends=[])

    assert readback["status"] == render_gate.UNAVAILABLE_WITH_REASON
    assert readback["status"] != render_gate.RENDERED
    assert readback["status"] != render_gate.FAILED
