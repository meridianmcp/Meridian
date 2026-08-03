"""5988a5bb -- close the cross-writer clobber window in the post-write
verify/restore path shared by move_section / copy_section / relocate_figure /
relocate_table / merge_draft_into_canonical / insert_figure_block
(extensions/meridian-docs's independent, duplicate implementation of the
same pattern fixed in meridian/doc_store.py's update_paragraph -- see that
fix's commit for the canonical writeup of the two findings this mirrors).
insert_figure_block (sprint 19be1551) landed on origin/dev concurrently with
this sprint item and carries the identical vulnerable pattern -- discovered
and fixed here too after rebasing onto origin/dev picked it up.

Finding 1 (HIGH): every one of these six mutators' post-write
verify-and-restore-on-failure ran OUTSIDE _docx_promotion_lock and did a
BLIND restore-from-backup on any mismatch. Because claim_docx_region allows
two sessions to hold claims on different, non-overlapping paragraphs of the
SAME .docx file concurrently, and every mutator does a whole-document
read-modify-write, a failed verification could restore a backup that was
actually a DIFFERENT writer's already-promoted, legitimate write --
silently destroying it with no error on either side. Fixed by widening
_docx_promotion_lock to a reentrant threading.RLock held across the full
stage+promote -> verify -> conditional-restore sequence, plus a
compare-and-swap check (_safe_restore_after_verification_failure) comparing
dest's CURRENT on-disk bytes against what THIS writer itself promoted
(_atomic_write_docx_bytes's new promoted_sha256 fingerprint) before ever
restoring.

Finding 2 (MEDIUM): _restore_docx_backup wrote straight into dest via
shutil.copy2 instead of the stage-to-temp + fsync + os.replace pattern every
other write in this module uses. Fixed to route through that same atomic
pattern.

Tests below exercise the REAL, unmocked _verify_docx_write against genuine
mismatches (never a faked return value) -- mirroring
tests/test_5988a5bb_update_paragraph_envelope.py's approach:

* One "same-process real detection" test (move_section) proving the widened
  lock + compare-and-swap gate does NOT break the legitimate self-correction
  path: a genuine verification failure with no concurrent writer still
  restores from backup exactly as before.
* Six "genuine concurrent write" tests, one per fixed call site, each
  wrapping (never replacing the return value of) the real _verify_docx_write
  to inject an independently-written, already-promoted payload from a
  simulated different writer immediately before delegating to the genuine
  detector -- so the resulting mismatch is real. Each asserts the file is
  left EXACTLY as that other writer left it (file_restored=False,
  concurrent_write_detected=True), never silently clobbered by this
  writer's own backup.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _make_docx_bytes(xml: str, media: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
        for name, data in (media or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "doc.docx", media: dict[str, bytes] | None = None) -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml, media))
    return path


def _read_document_xml(path: str) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read("word/document.xml").decode("utf-8")


def _overwrite_docx_document_xml(path: str, new_document_xml: str) -> None:
    """Simulate a DIFFERENT writer's already-promoted write landing on
    ``path`` -- rewrites word/document.xml in place (preserving any other
    zip members, e.g. media), entirely independent of this module's own
    write-transaction machinery. A real concurrent writer (a different
    process) would not go through THIS process's _docx_promotion_lock
    either, so bypassing it here is the accurate simulation, not a shortcut.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == "word/document.xml":
                    data = new_document_xml.encode("utf-8")
                dst.writestr(info, data)
    with open(path, "wb") as fh:
        fh.write(out.getvalue())


_TWO_SECTION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Intro body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Results body paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_SIMPLE_TABLE_DOC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Intro paragraph.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
      <w:tblGrid>
        <w:gridCol w:w="2000"/>
        <w:gridCol w:w="2000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Middle paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>End paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

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


# ---------------------------------------------------------------------------
# Same-process real-detection test: a genuine (unmocked) verification
# failure with NO concurrent writer must still restore from backup exactly
# as before the widened lock / compare-and-swap gate.
# ---------------------------------------------------------------------------

def test_move_section_real_verification_failure_restores_safely_when_no_concurrent_write(tmp_path, monkeypatch):
    """Exercises the REAL, unmocked _verify_docx_write against a genuine
    content-hash mismatch (never a faked return value): _hash_elements is
    mocked so its FIRST call (the pre-cut fingerprint of the range about to
    move) returns a value that can never match what actually lands on disk,
    forcing the real post-promotion re-hash (the only other caller of
    _hash_elements in this flow) to genuinely mismatch. The write itself
    (staging, structural-manifest gate, promotion) still genuinely succeeds
    -- this proves the fix's compare-and-swap gate does not regress the
    ordinary safe-restore path when nobody else touched the file."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    real_hash_elements = docs_intel._hash_elements
    call_count = {"n": 0}

    def _fake_hash_elements(elements):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "0" * 64
        return real_hash_elements(elements)

    monkeypatch.setattr(docs_intel, "_hash_elements", _fake_hash_elements)

    result = docs_intel.move_section(path, "H0000002", "H0000001", destination_position="before")

    assert "error" in result
    assert result["content_hash_mismatch"] is not None
    assert result["concurrent_write_detected"] is False
    assert result["file_restored"] is True

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "a safe restore (no concurrent writer) must return the file to "
            "its pre-write state, exactly as before this fix"
        )


# ---------------------------------------------------------------------------
# Genuine concurrent write -- one test per fixed call site. Each wraps (never
# fakes the return value of) the real _verify_docx_write to inject an
# independently-promoted payload from a simulated different writer, so the
# resulting mismatch this writer observes is real, and the resulting refusal
# to restore is a genuine consequence of the compare-and-swap fingerprint
# check, not a fabricated branch.
# ---------------------------------------------------------------------------

def test_move_section_verification_failure_with_concurrent_write_leaves_file_untouched(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)

    real_verify = docs_intel._verify_docx_write

    def _verify_with_injected_concurrent_write(docx_path, **kwargs):
        _overwrite_docx_document_xml(
            docx_path,
            _TWO_SECTION_XML.replace(
                "Results body paragraph.",
                "a concurrent writer's own payload, landed after ours",
            ),
        )
        return real_verify(docx_path, **kwargs)

    monkeypatch.setattr(docs_intel, "_verify_docx_write", _verify_with_injected_concurrent_write)

    result = docs_intel.move_section(path, "H0000002", "H0000001", destination_position="before")

    assert "error" in result
    assert result["file_restored"] is False
    assert result["concurrent_write_detected"] is True

    xml = _read_document_xml(path)
    assert "a concurrent writer's own payload, landed after ours" in xml


def test_copy_section_verification_failure_with_concurrent_write_leaves_file_untouched(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)

    real_verify = docs_intel._verify_docx_write

    def _verify_with_injected_concurrent_write(docx_path, **kwargs):
        _overwrite_docx_document_xml(
            docx_path,
            _TWO_SECTION_XML.replace(
                "Results body paragraph.",
                "a concurrent writer's own payload, landed after ours",
            ),
        )
        return real_verify(docx_path, **kwargs)

    monkeypatch.setattr(docs_intel, "_verify_docx_write", _verify_with_injected_concurrent_write)

    result = docs_intel.copy_section(path, "H0000002", "H0000001", destination_position="before")

    assert "error" in result
    assert result["file_restored"] is False
    assert result["concurrent_write_detected"] is True

    xml = _read_document_xml(path)
    assert "a concurrent writer's own payload, landed after ours" in xml


def test_relocate_table_verification_failure_with_concurrent_write_leaves_file_untouched(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _SIMPLE_TABLE_DOC_XML, name="table.docx")

    real_verify = docs_intel._verify_docx_write

    def _verify_with_injected_concurrent_write(docx_path, **kwargs):
        _overwrite_docx_document_xml(
            docx_path,
            _SIMPLE_TABLE_DOC_XML.replace(
                "Middle paragraph.",
                "a concurrent writer's own payload, landed after ours",
            ),
        )
        return real_verify(docx_path, **kwargs)

    monkeypatch.setattr(docs_intel, "_verify_docx_write", _verify_with_injected_concurrent_write)

    result = docs_intel.relocate_table(path, 1, "P0000003", destination_position="after")

    assert "error" in result
    assert result["file_restored"] is False
    assert result["concurrent_write_detected"] is True

    xml = _read_document_xml(path)
    assert "a concurrent writer's own payload, landed after ours" in xml


def test_relocate_figure_verification_failure_with_concurrent_write_leaves_file_untouched(tmp_path, monkeypatch):
    path = _write_docx(
        tmp_path, _FIGURE_BLOCK_XML, name="figure.docx",
        media={"word/media/image1.png": b"image", "word/media/image2.png": b"image"},
    )

    real_verify = docs_intel._verify_docx_write

    def _verify_with_injected_concurrent_write(docx_path, **kwargs):
        _overwrite_docx_document_xml(
            docx_path,
            _FIGURE_BLOCK_XML.replace(
                "Tail",
                "a concurrent writer's own payload, landed after ours",
            ),
        )
        return real_verify(docx_path, **kwargs)

    monkeypatch.setattr(docs_intel, "_verify_docx_write", _verify_with_injected_concurrent_write)

    result = docs_intel.relocate_figure(path, 2, "P0000001", destination_position="before")

    assert "error" in result
    assert result["file_restored"] is False
    assert result["concurrent_write_detected"] is True

    xml = _read_document_xml(path)
    assert "a concurrent writer's own payload, landed after ours" in xml


_FIGURE_ANCHOR_DOC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Anchor</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''

_FIGURE_RELS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""

_FIGURE_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""

_FIGURE_PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    + (200).to_bytes(4, "big") + (100).to_bytes(4, "big") + b"payload"
)


def test_insert_figure_block_verification_failure_with_concurrent_write_leaves_file_untouched(tmp_path, monkeypatch):
    docx_path = str(tmp_path / "report.docx")
    image_path = str(tmp_path / "chart.png")
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _FIGURE_ANCHOR_DOC_XML)
        archive.writestr("word/_rels/document.xml.rels", _FIGURE_RELS_XML)
        archive.writestr("[Content_Types].xml", _FIGURE_CONTENT_TYPES_XML)
    with open(image_path, "wb") as fh:
        fh.write(_FIGURE_PNG)

    real_verify = docs_intel._verify_figure_block_write

    def _verify_with_injected_concurrent_write(docx_path_arg, **kwargs):
        # Simulate a DIFFERENT writer's promotion landing between OUR
        # promotion (already complete by the time this runs) and OUR verify.
        _overwrite_docx_document_xml(
            docx_path_arg,
            _FIGURE_ANCHOR_DOC_XML.replace(
                "Anchor",
                "a concurrent writer's own payload, landed after ours",
            ),
        )
        return real_verify(docx_path_arg, **kwargs)

    monkeypatch.setattr(docs_intel, "_verify_figure_block_write", _verify_with_injected_concurrent_write)

    result = docs_intel.insert_figure_block(
        docx_path, image_path, label_text="Loss curve", anchor_para_id="P0000001",
    )

    assert "error" in result
    assert result["file_restored"] is False
    assert result["concurrent_write_detected"] is True

    xml = _read_document_xml(docx_path)
    assert "a concurrent writer's own payload, landed after ours" in xml


def test_merge_draft_into_canonical_verification_failure_with_concurrent_write_leaves_canonical_untouched(tmp_path, monkeypatch):
    canonical = _write_docx(tmp_path, _TWO_SECTION_XML, "canonical.docx")
    draft = str(tmp_path / "draft.docx")

    move_result = docs_intel.move_section(
        canonical, "H0000002", "H0000001", destination_position="before",
        draft_output_path=draft, wave_run_id="wave-1",
    )
    assert move_result["status"] == "moved"

    real_verify = docs_intel._verify_docx_write

    def _verify_with_injected_concurrent_write(docx_path, **kwargs):
        # Simulate a DIFFERENT writer's promotion landing between OUR
        # merge's promotion (already complete by the time this runs) and
        # OUR verify.
        _overwrite_docx_document_xml(
            docx_path,
            _TWO_SECTION_XML.replace(
                "Intro body paragraph.",
                "a concurrent writer's own payload, landed after our merge",
            ),
        )
        return real_verify(docx_path, **kwargs)

    monkeypatch.setattr(docs_intel, "_verify_docx_write", _verify_with_injected_concurrent_write)

    result = docs_intel.merge_draft_into_canonical(canonical, draft)

    assert result["merged"] is False
    assert "error" in result
    assert result["file_restored"] is False
    assert result["concurrent_write_detected"] is True

    xml = _read_document_xml(canonical)
    assert "a concurrent writer's own payload, landed after our merge" in xml
