"""679c86f4 -- post-write image-ownership invariant: reject orphan image
paragraphs and duplicate drawing (r:embed) references after DOCX writes.

Two layers of coverage:

* Unit-level tests against :func:`docs_intel._verify_image_ownership`
  directly -- exercising the orphan / composite / duplicate-relationship
  rules in isolation against small synthetic documents, independent of any
  particular writer.
* Integration-level tests through the real write paths this hardening item
  wires the check into (:func:`docs_intel.insert_image`,
  :func:`docs_intel.relocate_figure`, :func:`docs_intel.copy_section`),
  including two disposable regression fixtures reproducing the exact bug
  class this invariant exists to catch: ``copy_section`` deep-copies a
  Figure's image paragraph without minting a fresh relationship id, so the
  original and the copy end up sharing the SAME ``r:embed`` -- one fixture
  named after a mid-document figure ("Figure 5.21", relationship "rId50"),
  one after an appendix figure ("Figure A.4", relationship "rId28"), mirroring
  the two numbering schemes a real long-form thesis document uses. No
  canonical thesis .docx is touched anywhere in this file -- every fixture
  below is a small, disposable, synthetic document built in-memory.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _write_docx(tmp_path, xml: str, name: str = "doc.docx", media: dict[str, bytes] | None = None) -> str:
    path = tmp_path / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        for media_name, data in (media or {"word/media/image1.png": b"image"}).items():
            archive.writestr(media_name, data)
    path.write_bytes(buf.getvalue())
    return str(path)


def _body_ids(path: str) -> list[str | None]:
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    return [child.get(docs_intel._q(_W14, "paraId")) for child in body]


# ---------------------------------------------------------------------------
# Unit-level: _verify_image_ownership against small synthetic documents.
# ---------------------------------------------------------------------------

_SINGLE_ORPHAN_IMAGE_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>Not a caption -- just prose.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_verify_image_ownership_flags_orphan_image_with_no_caption(tmp_path):
    path = _write_docx(tmp_path, _SINGLE_ORPHAN_IMAGE_XML)

    result = docs_intel._verify_image_ownership(path)

    assert result is not None
    assert len(result["orphan_image_paragraphs"]) == 1
    orphan = result["orphan_image_paragraphs"][0]
    assert orphan["para_id"] == "IMG000001"
    assert orphan["composite_size"] == 1
    assert result["duplicate_relationships"] == {}


def test_verify_image_ownership_captioned_image_passes(tmp_path):
    xml = _SINGLE_ORPHAN_IMAGE_XML.replace(
        '<w:p w14:paraId="P0000002"><w:r><w:t>Not a caption -- just prose.</w:t></w:r></w:p>',
        '<w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC ">'
        '<w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>',
    )
    path = _write_docx(tmp_path, xml)

    assert docs_intel._verify_image_ownership(path) is None


def test_verify_image_ownership_skips_orphan_check_when_not_required(tmp_path):
    """insert_image's own contract: a freshly inserted image with no caption
    yet (pending a separate insert_caption call) must not be treated as a
    violation when the caller passes require_immediate_caption=False."""
    path = _write_docx(tmp_path, _SINGLE_ORPHAN_IMAGE_XML)

    result = docs_intel._verify_image_ownership(path, require_immediate_caption=False)

    assert result is None


_COMPOSITE_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId1"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="IMG000002"><w:r><w:drawing><a:blip r:embed="rId2"/></w:drawing></w:r></w:p>
    {{caption}}
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_verify_image_ownership_recognizes_adjacent_composite_with_shared_caption(tmp_path):
    xml = _COMPOSITE_XML.format(
        caption=(
            '<w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC ">'
            '<w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>'
        )
    )
    path = _write_docx(
        tmp_path, xml,
        media={"word/media/image1.png": b"image", "word/media/image2.png": b"image"},
    )

    assert docs_intel._verify_image_ownership(path) is None


def test_verify_image_ownership_flags_uncaptioned_composite_as_two_orphans(tmp_path):
    xml = _COMPOSITE_XML.format(caption="")
    path = _write_docx(
        tmp_path, xml,
        media={"word/media/image1.png": b"image", "word/media/image2.png": b"image"},
    )

    result = docs_intel._verify_image_ownership(path)

    assert result is not None
    assert len(result["orphan_image_paragraphs"]) == 2
    for orphan in result["orphan_image_paragraphs"]:
        assert orphan["composite_size"] == 2
    ids = {o["para_id"] for o in result["orphan_image_paragraphs"]}
    assert ids == {"IMG000001", "IMG000002"}


_TWO_INDEPENDENT_BLOCKS_SHARED_RID_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId9"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>
    <w:p w14:paraId="P0000001"><w:r><w:t>Unrelated paragraph in between.</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000002"><w:r><w:drawing><a:blip r:embed="rId9"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP000002"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>2</w:t></w:r></w:fldSimple></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_verify_image_ownership_flags_relationship_shared_across_independent_blocks(tmp_path):
    path = _write_docx(tmp_path, _TWO_INDEPENDENT_BLOCKS_SHARED_RID_XML)

    result = docs_intel._verify_image_ownership(path)

    assert result is not None
    assert result["orphan_image_paragraphs"] == []
    assert "rId9" in result["duplicate_relationships"]
    assert len(result["duplicate_relationships"]["rId9"]) == 2


def test_verify_image_ownership_allow_relationship_reuse_suppresses_the_flag(tmp_path):
    path = _write_docx(tmp_path, _TWO_INDEPENDENT_BLOCKS_SHARED_RID_XML)

    result = docs_intel._verify_image_ownership(path, allow_relationship_reuse=True)

    assert result is None


# ---------------------------------------------------------------------------
# Integration: insert_image keeps working uncaptioned (its documented
# two-step insert_image -> insert_caption composition is not a violation).
# ---------------------------------------------------------------------------

_ANCHOR_ONLY_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Anchor</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''

_ANCHOR_RELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""

_ANCHOR_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (200).to_bytes(4, "big") + (100).to_bytes(4, "big") + b"payload"


def test_insert_image_still_succeeds_without_a_caption(tmp_path):
    docx_path = tmp_path / "report.docx"
    image_path = tmp_path / "chart.png"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _ANCHOR_ONLY_XML)
        archive.writestr("word/_rels/document.xml.rels", _ANCHOR_RELS_XML)
        archive.writestr("[Content_Types].xml", _ANCHOR_CONTENT_TYPES_XML)
    docx_path.write_bytes(buf.getvalue())
    image_path.write_bytes(_PNG)

    result = docs_intel.insert_image(
        str(docx_path), str(image_path), anchor_para_id="P0000001", position="after"
    )

    assert result["status"] == "inserted"
    assert docs_intel._verify_image_ownership(
        str(docx_path), require_immediate_caption=False
    ) is None


# ---------------------------------------------------------------------------
# Integration: relocate_figure remains a no-op under the new invariant --
# it moves the SAME live image+caption pair as one atomic unit, so it can
# never itself introduce an orphan or a relationship duplicate.
# ---------------------------------------------------------------------------

_RELOCATE_FIGURE_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>Destination</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_relocate_figure_still_passes_the_ownership_invariant(tmp_path):
    path = _write_docx(tmp_path, _RELOCATE_FIGURE_XML)

    result = docs_intel.relocate_figure(path, 1, "P0000002", destination_position="after")

    assert result["status"] == "moved"
    assert docs_intel._verify_image_ownership(path) is None


# ---------------------------------------------------------------------------
# Integration: copy_section regression fixtures -- rId50 reuse at Figure
# 5.21, and rId28 reuse in Figure A.4. copy_section's deep-copy pass mints a
# fresh w14:paraId and renames every bookmark for the copied range, but never
# rewrites a drawing's r:embed attribute -- so copying a section that
# contains a Figure duplicates the RELATIONSHIP REFERENCE without duplicating
# the underlying media part, leaving two independent figure blocks pointing
# at the same image. This must be rejected fail-closed by default.
# ---------------------------------------------------------------------------

_FIGURE_5_21_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="H0000005"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chapter 5: Results</w:t></w:r></w:p>
    <w:p w14:paraId="P0005001"><w:r><w:t>Discussion of the primary model's convergence behavior.</w:t></w:r></w:p>
    <w:p w14:paraId="IMG050021"><w:r><w:drawing><a:blip r:embed="rId50"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP050021"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>21</w:t></w:r></w:fldSimple><w:r><w:t xml:space="preserve">. Convergence plot for the primary model.</w:t></w:r></w:p>
    <w:p w14:paraId="H0000006"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chapter 6: Conclusion</w:t></w:r></w:p>
    <w:p w14:paraId="P0006001"><w:r><w:t>Closing remarks.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_copy_section_rejects_rid50_reuse_at_figure_5_21_and_rolls_back(tmp_path):
    path = _write_docx(tmp_path, _FIGURE_5_21_XML, name="thesis_fixture.docx")
    original_bytes = (tmp_path / "thesis_fixture.docx").read_bytes()
    _raw0, root0 = docs_intel._load_docx_xml_stdlib(path)
    body0 = root0.find(docs_intel._q(_W, "body"))
    baseline_counts = docs_intel._structural_counts([body0])
    baseline_counts["image_count"] = docs_intel._docx_media_count(_raw0)

    result = docs_intel.copy_section(
        path, "H0000005", "H0000006", destination_position="after"
    )

    # Fail closed: an error, not a false "copied" success payload.
    assert "error" in result
    assert result.get("status") != "copied"

    # Relationship ownership: rId50 is the smoking gun, and it is reported,
    # not silently swallowed.
    assert "rId50" in result["duplicate_relationships"]
    assert len(result["duplicate_relationships"]["rId50"]) == 2
    assert result["orphan_image_paragraphs"] == []

    # Rollback: no concurrent writer touched the file, so the restore must
    # succeed and leave the file byte-identical to before the rejected copy.
    assert result["file_restored"] is True
    assert result["concurrent_write_detected"] is False
    with open(path, "rb") as fh:
        restored_bytes = fh.read()
    assert restored_bytes == original_bytes

    # Counts: the restored document's structural counts match the pre-write
    # baseline exactly -- the rejected write left no partial trace.
    _raw1, root1 = docs_intel._load_docx_xml_stdlib(path)
    body1 = root1.find(docs_intel._q(_W, "body"))
    restored_counts = docs_intel._structural_counts([body1])
    restored_counts["image_count"] = docs_intel._docx_media_count(_raw1)
    assert restored_counts == baseline_counts

    # Caption adjacency: the (untouched) original figure is still intact.
    ids = _body_ids(path)
    img_idx = ids.index("IMG050021")
    assert ids[img_idx + 1] == "CAP050021"


def test_copy_section_allow_relationship_reuse_bypasses_the_rejection(tmp_path):
    path = _write_docx(tmp_path, _FIGURE_5_21_XML, name="thesis_fixture.docx")

    result = docs_intel.copy_section(
        path, "H0000005", "H0000006", destination_position="after",
        allow_relationship_reuse=True,
    )

    assert result["status"] == "copied"
    # Two independent Figure captions now exist (original + copy), both
    # still pointing at rId50 -- exactly what the caller explicitly declared
    # intentional.
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    assert docs_intel._count_seq_captions(root, "Figure") == 2
    body = root.find(docs_intel._q(_W, "body"))
    embeds = [
        blip.get(docs_intel._q(_R, "embed"))
        for p in body.iter(docs_intel._q(_W, "p"))
        for blip in p.iter(docs_intel._q(_A, "blip"))
    ]
    assert embeds.count("rId50") == 2


_FIGURE_A4_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="H0000A01"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Appendix A: Supplementary Figures</w:t></w:r></w:p>
    <w:p w14:paraId="P000A001"><w:r><w:t>Additional diagnostic plots follow.</w:t></w:r></w:p>
    <w:p w14:paraId="IMG0A0004"><w:r><w:drawing><a:blip r:embed="rId28"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP0A0004"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>4</w:t></w:r></w:fldSimple><w:r><w:t xml:space="preserve">. Residuals for the appendix sensitivity run.</w:t></w:r></w:p>
    <w:p w14:paraId="H0000A02"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Appendix B: Raw Data</w:t></w:r></w:p>
    <w:p w14:paraId="P000A002"><w:r><w:t>See attached tables.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def test_copy_section_rejects_rid28_reuse_in_figure_a4_and_rolls_back(tmp_path):
    path = _write_docx(tmp_path, _FIGURE_A4_XML, name="thesis_appendix_fixture.docx")
    original_bytes = (tmp_path / "thesis_appendix_fixture.docx").read_bytes()

    result = docs_intel.copy_section(
        path, "H0000A01", "H0000A02", destination_position="after"
    )

    assert "error" in result
    assert result.get("status") != "copied"
    assert "rId28" in result["duplicate_relationships"]
    assert len(result["duplicate_relationships"]["rId28"]) == 2
    assert result["file_restored"] is True
    assert result["concurrent_write_detected"] is False

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes

    # Caption adjacency on the untouched original: still intact.
    ids = _body_ids(path)
    img_idx = ids.index("IMG0A0004")
    assert ids[img_idx + 1] == "CAP0A0004"
