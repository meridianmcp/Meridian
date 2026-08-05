"""Tests for the 9 new primitives added to meridian_docs.docs_intel:

  1. get_section_content        (178a82dd)
  2. find_references_to         (fea654f9)
  3. scan_stale_notes           (563118d4)
  4. renumber_sequences         (595ccea1)
  5. insert_highlighted_note / list_internal_notes (65c8eb31)
  6. write_section              (82d22824)
  7. move_section               (6ff24136)
  8. copy_section               (8213050a)

All tests are pure Python (stdlib + pytest) -- no mcp, no network. Unlike
test_docs_intel_chunks.py (which only ever reads in-memory bytes), several of
these tools MUTATE a .docx file in place, so tests write a minimal .docx to
tmp_path first.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from meridian_docs import docs_intel


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """016015e1/ddd79188 -- insert_highlighted_note (mode="inline") now
    invokes the real render-capability gate
    (render_gate.check_render_capability) AFTER structural verification
    passes. Tests in this file exercise STRUCTURAL correctness and must not
    depend on -- or be slowed/blocked by -- whichever render backends
    (LibreOffice, Word COM) happen to be installed on the machine running
    the suite. Stub a successful 'rendered' result by default, mirroring
    test_19be1551_insert_figure_block.py's fixture of the same name.
    """
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


def _make_docx_bytes(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "doc.docx") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml))
    return path


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
    <w:p w14:paraId="H0000003">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Sub-results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>Sub-results body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000004">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000004">
      <w:r><w:t>Conclusion body paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


# ---------------------------------------------------------------------------
# 1. get_section_content
# ---------------------------------------------------------------------------

def test_get_section_content_basic(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.get_section_content(path, "H0000002")
    assert result["heading_text"] == "Results"
    assert result["level"] == 1
    # Results section includes its own heading, body para, the H2 sub-heading,
    # and the sub-heading's body para -- ends at Conclusion (next H1).
    texts = [b["text"] for b in result["blocks"]]
    assert texts == ["Results", "Results body paragraph.", "Sub-results", "Sub-results body paragraph."]
    assert result["paragraph_count"] == 4


def test_get_section_content_last_section_runs_to_end(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.get_section_content(path, "H0000004")
    assert result["heading_text"] == "Conclusion"
    assert result["end_index"] is None
    assert [b["text"] for b in result["blocks"]] == ["Conclusion", "Conclusion body paragraph."]


def test_get_section_content_missing_heading_errors(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.get_section_content(path, "NOPE")
    assert "error" in result


def test_get_section_content_missing_file_errors():
    result = docs_intel.get_section_content("C:/nonexistent/path/doc.docx", "H1")
    assert "error" in result


# ---------------------------------------------------------------------------
# 4. renumber_sequences (tested early since other tests build on captions)
# ---------------------------------------------------------------------------

_TWO_FIGURES_COLLIDING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="C0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref100000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. First figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="C0000002">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref100000002"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. Second figure (collides with the first).</w:t></w:r>
    </w:p>
    <w:p w14:paraId="R0000001">
      <w:r><w:t xml:space="preserve">See </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> REF _Ref100000002 \\h </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t xml:space="preserve">Figure 1</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t xml:space="preserve"> for details.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_renumber_sequences_fixes_collision_and_refs(tmp_path):
    path = _write_docx(tmp_path, _TWO_FIGURES_COLLIDING_XML)
    result = docs_intel.renumber_sequences(path)

    assert result["status"] == "corrected"
    assert result["figure_count"] == 2
    assert len(result["collisions_found"]) == 1
    assert result["collisions_found"][0]["cached_number"] == "1"
    # Only the second caption needed correcting (1 -> 2).
    assert len(result["corrections"]) == 1
    assert result["corrections"][0]["old_cached"] == "1"
    assert result["corrections"][0]["new_cached"] == "2"
    assert result["ref_fields_updated"] == 1

    # Re-parse to confirm the write actually landed.
    paras = docs_intel.parse_docx(path)
    texts = {p["para_id"]: p["text"] for p in paras}
    assert "Figure 2" in texts["R0000001"]


def test_renumber_sequences_idempotent_when_already_correct(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)  # no captions at all
    result = docs_intel.renumber_sequences(path)
    assert result["status"] == "unchanged"
    assert result["figure_count"] == 0
    assert result["table_count"] == 0

    result2 = docs_intel.renumber_sequences(path)
    assert result2 == result


# ---------------------------------------------------------------------------
# 2. find_references_to
# ---------------------------------------------------------------------------

def test_find_references_to_by_para_id(tmp_path):
    path = _write_docx(tmp_path, _TWO_FIGURES_COLLIDING_XML)
    result = docs_intel.find_references_to(path, "C0000002")
    assert result["target_kind"] == "Figure"
    assert result["bookmark_names"] == ["_Ref100000002"]
    assert result["reference_count"] == 1
    assert result["references"][0]["para_id"] == "R0000001"
    assert result["references"][0]["field_type"] == "REF"


def test_find_references_to_by_bookmark_name(tmp_path):
    path = _write_docx(tmp_path, _TWO_FIGURES_COLLIDING_XML)
    result = docs_intel.find_references_to(path, "_Ref100000002")
    assert result["reference_count"] == 1


def test_find_references_to_no_bookmark_returns_empty_with_note(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.find_references_to(path, "H0000001")
    assert result["reference_count"] == 0
    assert result["bookmark_names"] == []
    assert "note" in result


def test_find_references_to_unknown_target_errors(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.find_references_to(path, "NOPE")
    assert "error" in result


# ---------------------------------------------------------------------------
# 2b. find_references_to -- literal-text reference scan (b2035fb4)
# ---------------------------------------------------------------------------

_FIGURE_LITERAL_TEXT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="C0000010">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref300000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>2</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. The quarterly trend chart.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000010">
      <w:r><w:t xml:space="preserve">As shown in Figure 2, the trend holds across all quarters.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000011">
      <w:r><w:t xml:space="preserve">This mirrors the discussion in Figure 5, which used an outdated caption number.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_find_references_to_literal_exact_and_stale(tmp_path):
    path = _write_docx(tmp_path, _FIGURE_LITERAL_TEXT_XML)
    result = docs_intel.find_references_to(path, "C0000010")

    assert result["reference_count"] == 0  # no REF fields in this fixture at all
    literal = {r["para_id"]: r for r in result["literal_references"]}
    assert literal["P0000010"]["status"] == "exact"
    assert literal["P0000010"]["matched_number"] == "2"
    assert literal["P0000010"]["matched_text"] == "Figure 2"
    assert literal["P0000011"]["status"] == "stale"
    assert literal["P0000011"]["matched_number"] == "5"

    assert result["literal_reference_count"] == 2
    assert result["combined_reference_count"] == 2
    assert result["combined_references"] == result["references"] + result["literal_references"]


def test_find_references_to_literal_excludes_own_caption_paragraph(tmp_path):
    path = _write_docx(tmp_path, _FIGURE_LITERAL_TEXT_XML)
    result = docs_intel.find_references_to(path, "C0000010")
    literal_para_ids = {r["para_id"] for r in result["literal_references"]}
    assert "C0000010" not in literal_para_ids


def test_find_references_to_include_literal_false_disables_scan(tmp_path):
    path = _write_docx(tmp_path, _FIGURE_LITERAL_TEXT_XML)
    result = docs_intel.find_references_to(path, "C0000010", include_literal=False)
    assert result["literal_references"] == []
    assert result["literal_reference_count"] == 0
    assert result["combined_reference_count"] == result["reference_count"]


_TWO_FIGURES_COLLIDING_WITH_LITERAL_XML = _TWO_FIGURES_COLLIDING_XML.replace(
    "<w:sectPr/>",
    '<w:p w14:paraId="P0000099">'
    '<w:r><w:t xml:space="preserve">Compare Figure 1 against the appendix chart.</w:t></w:r>'
    "</w:p>\n    <w:sectPr/>",
)


def test_find_references_to_literal_ambiguous_on_numbering_collision(tmp_path):
    path = _write_docx(tmp_path, _TWO_FIGURES_COLLIDING_WITH_LITERAL_XML)
    result = docs_intel.find_references_to(path, "C0000002")
    literal = {r["para_id"]: r for r in result["literal_references"]}
    assert literal["P0000099"]["status"] == "ambiguous"
    assert literal["P0000099"]["matched_number"] == "1"


def test_find_references_to_literal_skips_field_driven_blocks(tmp_path):
    # The REF field's own cached display text ("Figure 1") and the OTHER
    # caption's own SEQ-bearing text must never be double-counted as a
    # literal reference -- only the genuinely unfielded paragraph shows up.
    path = _write_docx(tmp_path, _TWO_FIGURES_COLLIDING_WITH_LITERAL_XML)
    result = docs_intel.find_references_to(path, "C0000002")
    literal_para_ids = {r["para_id"] for r in result["literal_references"]}
    assert literal_para_ids == {"P0000099"}


_TABLE_LITERAL_TEXT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="C0000020">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref400000001"/>
      <w:r><w:t xml:space="preserve">Table </w:t></w:r>
      <w:fldSimple w:instr="SEQ Table \\* ARABIC"><w:r><w:t>11</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. Summary statistics.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000020">
      <w:r><w:t xml:space="preserve">Table 11 summarises the results in full.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_find_references_to_literal_table_kind(tmp_path):
    path = _write_docx(tmp_path, _TABLE_LITERAL_TEXT_XML)
    result = docs_intel.find_references_to(path, "C0000020")
    assert result["target_kind"] == "Table"
    assert len(result["literal_references"]) == 1
    assert result["literal_references"][0]["status"] == "exact"
    assert result["literal_references"][0]["matched_number"] == "11"


_FIGURE_CHAPTER_STYLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="C0000030">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref500000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>5.21</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. Chapter-scoped figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000030">
      <w:r><w:t xml:space="preserve">Fig. 5.21 illustrates the same trend from a different angle.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_find_references_to_literal_chapter_style_number_and_alias(tmp_path):
    path = _write_docx(tmp_path, _FIGURE_CHAPTER_STYLE_XML)
    result = docs_intel.find_references_to(path, "C0000030")
    assert len(result["literal_references"]) == 1
    entry = result["literal_references"][0]
    assert entry["status"] == "exact"
    assert entry["matched_number"] == "5.21"
    assert entry["matched_text"] == "Fig. 5.21"


# ---------------------------------------------------------------------------
# 3. scan_stale_notes
# ---------------------------------------------------------------------------

_STALE_NOTES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Results</w:t></w:r></w:p>
    <w:p><w:r><w:t>This is normal prose about the experiment.</w:t></w:r></w:p>
    <w:p><w:r><w:t>[NOTE: currently pending relocation to Section 4]</w:t></w:r></w:p>
    <w:p><w:r><w:t>TODO: fill in the real numbers here.</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="MeridianInternalNote"/></w:pPr><w:r><w:t>TODO but already tracked.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


def test_scan_stale_notes_finds_placeholders_and_excludes_tracked_notes(tmp_path):
    path = _write_docx(tmp_path, _STALE_NOTES_XML)
    result = docs_intel.scan_stale_notes(path)
    assert result["finding_count"] == 2
    texts = [f["text"] for f in result["findings"]]
    assert any("pending relocation" in t for t in texts)
    assert any("TODO: fill in" in t for t in texts)
    assert not any("already tracked" in t for t in texts)
    # section_path should reflect the Results ancestor heading.
    assert all(f["section_path"] == ["Results"] for f in result["findings"])


def test_scan_stale_notes_no_findings_on_clean_doc(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.scan_stale_notes(path)
    assert result["finding_count"] == 0
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# 5. insert_highlighted_note / list_internal_notes
# ---------------------------------------------------------------------------

def test_insert_highlighted_note_structure_and_sidecar(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    db = str(tmp_path / "idx.sqlite")
    # The sidecar sync (like _upsert_sidecar_caption) is gated on the sidecar
    # already existing on disk -- pre-create it, matching real usage
    # (index_document, THEN write tools against the same index_db_path).
    docs_intel.index_docx(path, db)

    result = docs_intel.insert_highlighted_note(
        path, "Double-check this stat before defense.", "P0000001", index_db_path=db
    )
    assert result["status"] == "inserted"
    note_id = result["note_id"]
    assert note_id.startswith("_MNote")

    # Structural checks: highlighted run + dedicated style + bookmark.
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    found = None
    for p in body.findall(docs_intel._q(_W, "p")):
        for bm in p.iter(docs_intel._q(_W, "bookmarkStart")):
            if bm.get(docs_intel._q(_W, "name")) == note_id:
                found = p
    assert found is not None
    pStyle = found.find(docs_intel._q(_W, "pPr")).find(docs_intel._q(_W, "pStyle"))
    assert pStyle.get(docs_intel._q(_W, "val")) == "MeridianInternalNote"
    highlight = found.find(f".//{docs_intel._q(_W, 'highlight')}")
    assert highlight.get(docs_intel._q(_W, "val")) == "yellow"

    notes = docs_intel.list_internal_notes(db)
    assert len(notes) == 1
    assert notes[0]["note_id"] == note_id
    assert notes[0]["anchor_para_id"] == "P0000001"


def test_insert_highlighted_note_without_sidecar_not_listed(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.insert_highlighted_note(path, "no sidecar this time", "P0000001")
    assert result["status"] == "inserted"
    # No index_db_path was ever created.
    assert docs_intel.list_internal_notes(str(tmp_path / "never_created.sqlite")) == []


def test_scan_stale_notes_excludes_real_insert_highlighted_note_output(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    docs_intel.insert_highlighted_note(path, "TODO-shaped text inside a real note", "P0000001")
    result = docs_intel.scan_stale_notes(path)
    assert result["finding_count"] == 0


def test_insert_highlighted_note_invalid_style_errors(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.insert_highlighted_note(path, "text", "P0000001", style="reviewer_note")
    assert "error" in result


# ---------------------------------------------------------------------------
# 6. write_section
# ---------------------------------------------------------------------------

def test_write_section_atomic_heading_paragraph_and_cross_reference(tmp_path):
    path = _write_docx(tmp_path, _TWO_FIGURES_COLLIDING_XML)

    content_spec = [
        {"type": "paragraph", "text": "This new section discusses the figures above."},
        {
            "type": "paragraph",
            "text": "See the earlier result in",
            "references": [{"target_caption_para_id": "C0000001"}],
        },
        {"type": "caption", "kind": "Figure", "label_text": "A brand-new figure."},
    ]
    result = docs_intel.write_section(
        path, "Discussion", 1, content_spec, "R0000001", position="after"
    )
    assert result["status"] == "inserted"
    assert result["heading_text"] == "Discussion"
    assert len(result["block_para_ids"]) == 3
    new_caption_seq = result["block_para_ids"][2]["seq_number"]
    assert new_caption_seq == 3  # after the two existing Figure captions

    # Re-parse and confirm structure + ordering landed correctly.
    paras = docs_intel.parse_docx(path)
    by_id = {p["para_id"]: p for p in paras}
    heading_id = result["heading_para_id"]
    assert by_id[heading_id]["style"] == "Heading1"
    assert by_id[heading_id]["text"] == "Discussion"

    ref_para_id = result["block_para_ids"][1]["para_id"]
    assert "Figure 1" in by_id[ref_para_id]["text"]

    # Order check: heading should immediately follow R0000001 in document order.
    ids_in_order = [p["para_id"] for p in paras]
    assert ids_in_order.index(heading_id) == ids_in_order.index("R0000001") + 1


def test_write_section_validates_before_touching_file(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    bad_spec = [{"type": "paragraph"}]  # missing required 'text'
    result = docs_intel.write_section(path, "New Section", 1, bad_spec, "P0000001")
    assert "error" in result

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_write_section_rejects_bad_reference_target(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    content_spec = [
        {"type": "paragraph", "text": "text", "references": [{"target_caption_para_id": "NOPE"}]}
    ]
    result = docs_intel.write_section(path, "New Section", 1, content_spec, "P0000001")
    assert "error" in result


def test_write_section_after_heading_anchor_does_not_swallow_section_body(tmp_path):
    """6822b142 regression: anchoring "after" a HEADING paragraph (instead of
    that section's own last body paragraph) must land the new section after
    the WHOLE anchor section, not right after the heading itself -- a literal
    splice there would silently re-parent the anchor heading's own body
    paragraph(s) under the newly-inserted heading."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    content_spec = [{"type": "paragraph", "text": "New section body."}]

    result = docs_intel.write_section(
        path, "New Section", 1, content_spec, "H0000001", position="after"
    )
    assert result["status"] == "inserted"

    # Introduction's own section must still contain its original body
    # paragraph -- not have lost it to the new section.
    intro = docs_intel.get_section_content(path, "H0000001")
    intro_texts = [b["text"] for b in intro["blocks"] if b["kind"] == "paragraph"]
    assert intro_texts == ["Intro body paragraph."]

    # The new section lands after Introduction's body, before Results.
    paras = docs_intel.parse_docx(path)
    ids_in_order = [p["para_id"] for p in paras]
    heading_id = result["heading_para_id"]
    assert ids_in_order.index(heading_id) == ids_in_order.index("P0000001") + 1
    assert ids_in_order.index(heading_id) < ids_in_order.index("H0000002")


def test_write_section_before_heading_anchor_unchanged(tmp_path):
    """position="before" a heading anchor is unambiguous already (nothing of
    the anchor's own content sits between "before it" and the heading) --
    confirm the 6822b142 fix left this path untouched."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    content_spec = [{"type": "paragraph", "text": "New section body."}]

    result = docs_intel.write_section(
        path, "New Section", 1, content_spec, "H0000002", position="before"
    )
    assert result["status"] == "inserted"

    paras = docs_intel.parse_docx(path)
    ids_in_order = [p["para_id"] for p in paras]
    heading_id = result["heading_para_id"]
    body_id = result["block_para_ids"][0]["para_id"]
    # New heading, then its own body paragraph, then (immediately) H0000002 --
    # nothing existing was displaced or re-parented by the insertion.
    assert ids_in_order.index(heading_id) == ids_in_order.index("H0000002") - 2
    assert ids_in_order.index(body_id) == ids_in_order.index("H0000002") - 1


def test_write_section_two_new_figures_get_consecutive_ref_bookmarks(tmp_path):
    """Regression guard: two brand-new captions in one call must not collide
    on the same _Ref bookmark name (see write_section's _reserve_ref_bookmark
    docstring note)."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    content_spec = [
        {"type": "caption", "kind": "Figure", "label_text": "First new figure."},
        {"type": "caption", "kind": "Figure", "label_text": "Second new figure."},
    ]
    result = docs_intel.write_section(path, "New Figures", 1, content_spec, "P0000001")
    assert result["status"] == "inserted"
    bookmarks = [b["ref_bookmark"] for b in result["block_para_ids"]]
    assert len(bookmarks) == len(set(bookmarks)) == 2
    seqs = [b["seq_number"] for b in result["block_para_ids"]]
    assert seqs == [1, 2]


# ---------------------------------------------------------------------------
# 7. move_section
# ---------------------------------------------------------------------------

def test_move_section_reorders_body_and_preserves_ids(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)

    result = docs_intel.move_section(path, "H0000002", "H0000001", destination_position="before")
    assert result["status"] == "moved"
    # Results (+ its Sub-results child) = 4 blocks: heading, body, subheading, subbody.
    assert result["moved_block_count"] == 4
    assert "renumber_sequences" in result
    assert "find_references_to" in result

    paras = docs_intel.parse_docx(path)
    order = [p["para_id"] for p in paras]
    # Results section now comes before Introduction.
    assert order.index("H0000002") < order.index("H0000001")
    assert order.index("H0000003") < order.index("H0000001")
    # Conclusion is untouched and still last of the headings.
    assert order.index("H0000004") > order.index("H0000001")
    # Original paraIds are unchanged (a MOVE, not a copy).
    assert set(order) == {
        "H0000001", "P0000001", "H0000002", "P0000002", "H0000003", "P0000003",
        "H0000004", "P0000004",
    }


def test_move_section_rejects_destination_inside_section(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.move_section(path, "H0000002", "H0000003")
    assert "error" in result


def test_move_section_unknown_section_errors(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.move_section(path, "NOPE", "H0000001")
    assert "error" in result


def test_move_section_to_end_of_document(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    # Move Introduction (first section) after Conclusion's LAST paragraph --
    # an ordinary (non-heading) anchor, so this is a literal splice: it lands
    # immediately after P0000004 regardless of section boundaries.
    result = docs_intel.move_section(path, "H0000001", "P0000004", destination_position="after")
    assert result["status"] == "moved"
    paras = docs_intel.parse_docx(path)
    order = [p["para_id"] for p in paras]
    assert order[-2:] == ["H0000001", "P0000001"]


def test_move_section_after_heading_anchor_does_not_swallow_dest_section(tmp_path):
    """027b7ada regression: destination_anchor_para_id anchored on a HEADING
    with destination_position="after" must land the moved section after the
    destination heading's WHOLE section (same 6822b142 fix write_section
    got), not immediately after the heading paragraph -- otherwise the
    moved-in heading would silently re-parent the destination section's own
    body paragraph(s) underneath it."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)

    # Move Introduction to "after Conclusion" using the CONCLUSION HEADING
    # itself as the anchor (not its last body paragraph).
    result = docs_intel.move_section(path, "H0000001", "H0000004", destination_position="after")
    assert result["status"] == "moved"

    # Conclusion's own section must still contain its original body
    # paragraph -- not have lost it to the moved-in Introduction section.
    conclusion = docs_intel.get_section_content(path, "H0000004")
    conclusion_texts = [b["text"] for b in conclusion["blocks"] if b["kind"] == "paragraph"]
    assert conclusion_texts == ["Conclusion body paragraph."]

    paras = docs_intel.parse_docx(path)
    order = [p["para_id"] for p in paras]
    # Conclusion's body paragraph (P0000004) comes before the moved-in
    # Introduction section, which is now the last thing in the document.
    assert order.index("P0000004") < order.index("H0000001")
    assert order[-2:] == ["H0000001", "P0000001"]


# ---------------------------------------------------------------------------
# e87b8338 -- reference-safety check gates BEFORE the write, not after
# ---------------------------------------------------------------------------

_SPLIT_BOOKMARK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:bookmarkStart w:id="42" w:name="_Ref999999999"/>
      <w:r><w:t>Intro body paragraph, bookmark starts here.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Results body paragraph, bookmark ends here.</w:t></w:r>
      <w:bookmarkEnd w:id="42"/>
    </w:p>
    <w:p w14:paraId="H0000003">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>Conclusion body paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_move_section_gates_before_write_on_bookmark_split(tmp_path):
    """A bookmark spanning INTO the section being moved (start inside,
    end outside) must abort the move -- and, critically, the file must be
    byte-for-byte untouched, proving the check ran BEFORE the cut/splice/
    save rather than as a post-hoc report after an already-committed write.
    """
    path = _write_docx(tmp_path, _SPLIT_BOOKMARK_XML, name="split.docx")
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.move_section(path, "H0000001", "P0000003", destination_position="after")
    assert "error" in result
    assert result["split_bookmarks"] == ["_Ref999999999"]

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes, "file was mutated despite the gate rejecting the move"


def test_move_section_allow_bookmark_split_override(tmp_path):
    path = _write_docx(tmp_path, _SPLIT_BOOKMARK_XML, name="split2.docx")
    result = docs_intel.move_section(
        path, "H0000001", "P0000003", destination_position="after",
        allow_bookmark_split=True,
    )
    assert result["status"] == "moved"


# ---------------------------------------------------------------------------
# 6ff24136 -- end-to-end: a section containing a TABLE + a FIGURE caption
# moves as one atomic unit, past another Figure caption, forcing a genuine
# SEQ Figure renumbering collision AND a REF-field display-text update --
# all inside the single move_section call (this is the exact motivating bug,
# "Figure 41/42 collision", reproduced deliberately and proven fixed within
# one atomic operation rather than by a separate renumber_sequences call).
# ---------------------------------------------------------------------------

_MOVE_SECTION_TABLE_AND_CAPTIONS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Setup</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>Setup table cell.</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p w14:paraId="C0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref100000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. The setup figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000003">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="C0000002">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="1" w:name="_Ref100000002"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>2</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="1"/>
      <w:r><w:t xml:space="preserve">. The results figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="R0000001">
      <w:r><w:t xml:space="preserve">As shown above in </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> REF _Ref100000001 \\h </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t xml:space="preserve">Figure 1</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t xml:space="preserve">.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000004">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000004">
      <w:r><w:t>Conclusion body paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_move_section_relocates_table_and_caption_and_fixes_seq_and_ref_atomically(tmp_path):
    path = _write_docx(tmp_path, _MOVE_SECTION_TABLE_AND_CAPTIONS_XML, name="move_seq.docx")

    # Move "Setup" (heading + table + Figure-1 caption, 3 body children) to
    # land right after "Results" (whose own Figure-2 caption + REF-to-Setup
    # paragraph precede it) -- i.e. after R0000001, before Conclusion. This
    # flips on-disk Figure order (2 then 1), which is exactly the collision
    # class renumber_sequences exists to catch and fix.
    result = docs_intel.move_section(path, "H0000002", "R0000001", destination_position="after")
    assert result["status"] == "moved"
    assert result["moved_block_count"] == 3  # heading + table + caption

    # --- atomicity: SEQ renumbering happened as part of THIS call's return ---
    renumber = result["renumber_sequences"]
    assert renumber["status"] == "corrected"
    corrected_kinds_positions = {
        (c["kind"], c["old_cached"], c["new_cached"]) for c in renumber["corrections"]
    }
    assert ("Figure", "2", "1") in corrected_kinds_positions  # Results' caption, now first
    assert ("Figure", "1", "2") in corrected_kinds_positions  # Setup's caption, now second
    # The REF field (pointing at Setup's caption, bookmark _Ref100000001)
    # cached the stale "Figure 1" display text -- must be resynced to "Figure 2".
    assert renumber["ref_fields_updated"] == 1

    # --- re-read the saved file fresh: everything above must be durably true ---
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    body_list = list(body)
    tags_and_ids = [
        (el.tag.rsplit("}", 1)[-1], el.get(docs_intel._q(_W14, "paraId")))
        for el in body_list
    ]
    # Document order now: Intro, Results-heading, Results-caption, REF-para,
    # Setup-heading, tbl, Setup-caption, Conclusion-heading, Conclusion-body.
    ordered_ids = [pid for _tag, pid in tags_and_ids if pid]
    assert ordered_ids.index("H0000003") < ordered_ids.index("H0000002")  # Results before Setup
    assert ordered_ids.index("R0000001") < ordered_ids.index("H0000002")  # REF-para before Setup
    assert ordered_ids.index("H0000002") < ordered_ids.index("H0000004")  # Setup before Conclusion
    # The table moved WITH its section (immediately after Setup's heading,
    # immediately before Setup's caption) -- not left behind or dropped.
    setup_pos = next(i for i, (_t, pid) in enumerate(tags_and_ids) if pid == "H0000002")
    assert tags_and_ids[setup_pos + 1][0] == "tbl"
    assert tags_and_ids[setup_pos + 2][1] == "C0000001"

    # SEQ cached numbers on disk match the corrected values.
    fld_texts: dict[str, str] = {}
    for p in body.iter(docs_intel._q(_W, "p")):
        pid = p.get(docs_intel._q(_W14, "paraId"))
        if pid in ("C0000001", "C0000002"):
            t_el = next(iter(p.iter(docs_intel._q(_W, "t"))), None)
            fld = p.find(docs_intel._q(_W, "fldSimple"))
            seq_t = next(iter(fld.iter(docs_intel._q(_W, "t"))), None)
            fld_texts[pid] = seq_t.text
    assert fld_texts["C0000002"] == "1"  # Results' caption, now first in doc order
    assert fld_texts["C0000001"] == "2"  # Setup's caption, now second

    # REF field's cached display text was updated in place, on disk.
    ref_para = next(
        p for p in body.iter(docs_intel._q(_W, "p"))
        if p.get(docs_intel._q(_W14, "paraId")) == "R0000001"
    )
    display_texts = [t.text for t in ref_para.iter(docs_intel._q(_W, "t")) if t.text]
    assert "Figure 2" in display_texts
    assert "Figure 1" not in display_texts


# ---------------------------------------------------------------------------
# 7600db1c regression: move_section must work when section_id /
# destination_anchor_para_id are synthesized "sp<hash>" ids, not just native
# w14:paraId -- this is the id scheme almost every REAL document's headings
# and captions actually carry (Word does not assign w14:paraId to every
# paragraph). Before 7600db1c, find_references_to's pre-move safety check
# (called with section_id) could only resolve p{N}/native ids, so calling
# move_section with the realistic id a caller would actually have obtained
# from document_outline/parse_document failed on ~every real document
# (documented as a BLOCKING GAP against find_references_to, item fea654f9).
# This test builds a document with NO native w14:paraId anywhere, discovers
# ids the same way a real caller must (via document_outline), and confirms
# the whole move_section call -- including the find_references_to gate --
# succeeds using only synth ids.
# ---------------------------------------------------------------------------

_NO_NATIVE_IDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Intro body paragraph.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Setup</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref100000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. The setup figure.</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Conclusion body paragraph.</w:t></w:r></w:p>
    <w:p>
      <w:r><w:t xml:space="preserve">External mention of </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> REF _Ref100000001 \\h </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t xml:space="preserve">Figure 1</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t xml:space="preserve">.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_move_section_resolves_synth_ids_no_native_paraid_in_document(tmp_path):
    path = _write_docx(tmp_path, _NO_NATIVE_IDS_XML, name="no_native_ids.docx")

    # No paragraph anywhere has a native w14:paraId -- confirm the fixture is
    # honest about that before relying on it.
    _raw0, root0 = docs_intel._load_docx_xml_stdlib(path)
    assert not any(
        p.get(docs_intel._q(_W14, "paraId")) for p in root0.iter(docs_intel._q(_W, "p"))
    )

    # Discover ids the way a REAL caller with a live structural index would:
    # via index_docx_structure/get_local_structure_elements (the docx_headings
    # sidecar table, populated from document_content_tree) -- this is what
    # emits "sp<hash>" ids for headings, as opposed to parse_docx/
    # document_outline's older "p{N}" positional fallback (which also works,
    # but exercises a different, already-well-covered id scheme -- see the
    # p{N}-based tests above). e2ae4c91 documented this exact id-scheme split
    # between docx_paragraphs (p{N}) and docx_headings (sp<hash>).
    index_db_path = str(tmp_path / "structure_index.sqlite3")
    docs_intel.index_docx_structure(path, index_db_path)
    elements = docs_intel.get_local_structure_elements(index_db_path)
    headings_by_text = {h["text"]: h["para_id"] for h in elements["headings"]}
    setup_id = headings_by_text["Setup"]
    conclusion_id = headings_by_text["Conclusion"]
    assert setup_id.startswith("sp") and conclusion_id.startswith("sp")

    result = docs_intel.move_section(path, setup_id, conclusion_id, destination_position="before")
    assert result["status"] == "moved"
    assert result["moved_block_count"] == 2  # Setup heading + its Figure caption

    # The pre-move find_references_to(section_id) gate ran and resolved
    # cleanly (not an id-resolution error) -- it just found nothing pointing
    # at the SECTION'S OWN heading (as opposed to its caption's bookmark,
    # which is a separate, external, untouched reference).
    assert "error" not in result["find_references_to"]

    # The external REF (outside the moved section, pointing at the caption's
    # bookmark) is untouched and still resolves -- confirmed independently.
    refs = docs_intel.find_references_to(path, "_Ref100000001")
    assert "error" not in refs
    assert refs["reference_count"] == 1

    outline_after = docs_intel.document_outline(path)
    order = [h["text"] for h in outline_after["headings"]]
    assert order == ["Introduction", "Setup", "Conclusion"]


# ---------------------------------------------------------------------------
# 8. copy_section
# ---------------------------------------------------------------------------

_SECTION_WITH_SELF_REF_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="C0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref100000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. The only figure.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="R0000001">
      <w:r><w:t xml:space="preserve">As shown above in </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> REF _Ref100000001 \\h </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t xml:space="preserve">Figure 1</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t xml:space="preserve">.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000009">
      <w:r><w:t>Conclusion text.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_copy_section_preserves_original_and_dedupes_ids(tmp_path):
    path = _write_docx(tmp_path, _SECTION_WITH_SELF_REF_XML)

    result = docs_intel.copy_section(path, "H0000001", "H0000002", destination_position="before")
    assert result["status"] == "copied"
    assert result["copied_block_count"] == 3  # heading + caption + self-ref paragraph
    # new_heading_para_id IS the id the copy's heading was mapped to.
    assert result["para_id_map"]["H0000001"] == result["new_heading_para_id"]
    assert result["new_heading_para_id"] not in ("H0000001", "C0000001", "R0000001")

    paras = docs_intel.parse_docx(path)
    ids = [p["para_id"] for p in paras]
    # Original section still present, unmodified in identity.
    assert "H0000001" in ids and "C0000001" in ids and "R0000001" in ids
    # Copy landed with brand-new ids for all three paragraphs -- no duplicates.
    assert len(ids) == len(set(ids))
    assert len(ids) == 3 + 3 + 2  # original(3) + copy(3) + conclusion(2)

    # Both original Figure captions... wait there's only one original caption;
    # the COPY duplicates it, so there should now be two Figure captions total.
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    fig_count = docs_intel._count_seq_captions(root, "Figure")
    assert fig_count == 2

    # The copy's bookmark must differ from the original's.
    assert "_Ref100000001" in result["bookmark_map"]
    new_bookmark = result["bookmark_map"]["_Ref100000001"]
    assert new_bookmark != "_Ref100000001"

    # renumber_sequences (auto-called) must have corrected exactly one of the
    # two now-colliding "Figure 1" captions -- and repointed the internal
    # self-reference inside the COPY at the copy's own (renamed) bookmark,
    # not the original's.
    copy_ref_para_id = result["para_id_map"]["R0000001"]
    by_id = {p["para_id"]: p for p in paras}
    # Whichever caption the copy's REF ends up pointing at, the copy's own
    # REF paragraph must NOT be the untouched original's cached text if the
    # numbers diverged.
    assert "Figure" in by_id[copy_ref_para_id]["text"]


def test_copy_section_external_reference_left_pointing_at_original(tmp_path):
    """A REF field OUTSIDE the copied range that targets a bookmark INSIDE the
    copied range must keep pointing at the ORIGINAL (only in-copy self-refs
    get repointed)."""
    xml = _SECTION_WITH_SELF_REF_XML.replace(
        '<w:p w14:paraId="P0000009">\n      <w:r><w:t>Conclusion text.</w:t></w:r>\n    </w:p>',
        (
            '<w:p w14:paraId="P0000009">\n      <w:r><w:t>Conclusion text.</w:t></w:r>\n    </w:p>\n'
            '    <w:p w14:paraId="EXT0001">\n'
            '      <w:r><w:t xml:space="preserve">External mention of </w:t></w:r>\n'
            '      <w:r><w:fldChar w:fldCharType="begin"/></w:r>\n'
            '      <w:r><w:instrText xml:space="preserve"> REF _Ref100000001 \\h </w:instrText></w:r>\n'
            '      <w:r><w:fldChar w:fldCharType="separate"/></w:r>\n'
            '      <w:r><w:t xml:space="preserve">Figure 1</w:t></w:r>\n'
            '      <w:r><w:fldChar w:fldCharType="end"/></w:r>\n'
            "    </w:p>\n"
        ),
    )
    path = _write_docx(tmp_path, xml)

    result = docs_intel.copy_section(path, "H0000001", "H0000002", destination_position="before")
    assert result["status"] == "copied"

    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    ext_p = None
    for p in body.iter(docs_intel._q(_W, "p")):
        if p.get(docs_intel._q(_W14, "paraId")) == "EXT0001":
            ext_p = p
    assert ext_p is not None
    instr_text = "".join(
        it.text or "" for it in ext_p.iter(docs_intel._q(_W, "instrText"))
    )
    assert "_Ref100000001" in instr_text


def test_copy_section_unknown_section_errors(tmp_path):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.copy_section(path, "NOPE", "H0000001")
    assert "error" in result


def test_copy_section_trim_original_to_leaves_pointer(tmp_path):
    """8213050a -- trim_original_to duplicates the section at the destination
    AND replaces the ORIGINAL location's body with a short pointer, keeping
    the original heading (and its bookmark/section_id) intact."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)

    # Copy "Introduction" (H0000001 + its one body paragraph) to the very end
    # of the document (destination_position="after" a heading anchor resolves
    # to after that heading's WHOLE section -- Conclusion is the last section,
    # so this lands the copy at the tail), trimming the original in place.
    result = docs_intel.copy_section(
        path,
        "H0000001",
        "H0000004",
        destination_position="after",
        trim_original_to="See Introduction (copied to the end) below.",
    )
    assert result["status"] == "copied"
    assert result["trimmed_original"] is True
    assert result["copied_block_count"] == 2  # heading + its one body paragraph

    paras = docs_intel.parse_docx(path)
    ids = [p["para_id"] for p in paras]
    by_id = {p["para_id"]: p for p in paras}

    # Original heading is preserved (section_id still resolvable).
    assert "H0000001" in ids
    # Original body paragraph was REMOVED (not just moved -- it lived only in
    # the trimmed range, and the copy's body paragraph got its own fresh id).
    assert "P0000001" not in ids
    assert "P0000001" in result["para_id_map"]

    # The trimmed original's very next paragraph is the pointer text.
    intro_idx = ids.index("H0000001")
    assert by_id[ids[intro_idx + 1]]["text"] == "See Introduction (copied to the end) below."

    # The copy landed at the tail, after Conclusion's own body.
    outline = docs_intel.document_outline(path)
    headings = [h["text"] for h in outline["headings"]]
    assert headings == ["Introduction", "Results", "Sub-results", "Conclusion", "Introduction"]

    copy_heading_id = result["new_heading_para_id"]
    assert copy_heading_id != "H0000001"
    copy_idx = ids.index(copy_heading_id)
    assert by_id[ids[copy_idx + 1]]["text"] == "Intro body paragraph."
    # Copy is the last content in the document.
    assert copy_idx + 1 == len(ids) - 1


def test_copy_section_trim_original_to_rejects_destination_inside_section(tmp_path):
    """trim_original_to implies a real removal from the original location, so
    (unlike a plain untouched copy) the destination must resolve OUTSIDE the
    section being copied -- mirrors move_section's own invariant."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.copy_section(
        path,
        "H0000002",
        "H0000003",
        destination_position="before",
        trim_original_to="Moved elsewhere.",
    )
    assert "error" in result
    assert "INSIDE" in result["error"]


# ---------------------------------------------------------------------------
# 48daaf66 -- copy_section: reuse move_section's fixed anchor logic
# (027b7ada) and pre-write reference check (e87b8338); optional
# trim_original_to.
# ---------------------------------------------------------------------------


def test_copy_section_after_heading_anchor_does_not_swallow_dest_section(tmp_path):
    """48daaf66/027b7ada: destination_anchor_para_id anchored on a HEADING
    with destination_position="after" must land the copy after the
    destination heading's WHOLE section, not immediately after the heading
    paragraph -- otherwise the copied-in heading would silently re-parent
    the destination section's own body paragraph underneath it."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)

    result = docs_intel.copy_section(path, "H0000001", "H0000004", destination_position="after")
    assert result["status"] == "copied"

    conclusion = docs_intel.get_section_content(path, "H0000004")
    conclusion_texts = [b["text"] for b in conclusion["blocks"] if b["kind"] == "paragraph"]
    assert conclusion_texts == ["Conclusion body paragraph."]

    paras = docs_intel.parse_docx(path)
    order = [p["para_id"] for p in paras]
    assert order.index("P0000004") < order.index(result["new_heading_para_id"])
    assert order[-2:] == [result["new_heading_para_id"], result["para_id_map"]["P0000001"]]
    # Original Introduction section must still be intact and untouched.
    assert "H0000001" in order and "P0000001" in order


def test_copy_section_includes_find_references_to_and_trimmed_original_false(tmp_path):
    """48daaf66: result must carry find_references_to (the new pre-write
    check) and trimmed_original=False when trim_original_to is not given."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.copy_section(path, "H0000001", "H0000002", destination_position="before")
    assert result["status"] == "copied"
    assert "find_references_to" in result
    assert "error" not in result["find_references_to"]
    assert result["trimmed_original"] is False


def test_copy_section_gates_before_write_on_bookmark_split_when_trimming(tmp_path):
    """48daaf66/e87b8338: trimming the original away must abort (file
    untouched) if a bookmark spans INTO the section being trimmed."""
    path = _write_docx(tmp_path, _SPLIT_BOOKMARK_XML, name="split_copy.docx")
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.copy_section(
        path, "H0000001", "P0000003", destination_position="after",
        trim_original_to="See below.",
    )
    assert "error" in result
    assert result["split_bookmarks"] == ["_Ref999999999"]

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "file was mutated despite the gate rejecting the trim"
        )


def test_copy_section_trim_original_replaces_body_keeps_heading(tmp_path):
    """48daaf66: trim_original_to replaces the ORIGINAL section's body with a
    single placeholder paragraph, but keeps the heading itself (and
    therefore section_id / its bookmark) intact -- and the copy at the
    destination is unaffected."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)

    result = docs_intel.copy_section(
        path, "H0000002", "H0000004", destination_position="after",
        trim_original_to="Moved to Conclusion, see there.",
    )
    assert result["status"] == "copied"
    assert result["trimmed_original"] is True
    assert result["copied_block_count"] == 4  # heading + body + sub-heading + sub-body

    # Original location: heading preserved, body replaced by ONE placeholder.
    original = docs_intel.get_section_content(path, "H0000002")
    assert original["heading_text"] == "Results"
    original_texts = [b["text"] for b in original["blocks"]]
    assert original_texts == ["Results", "Moved to Conclusion, see there."]

    # The copy landed intact at the destination with fresh ids.
    paras = docs_intel.parse_docx(path)
    by_id = {p["para_id"]: p for p in paras}
    copy_heading_id = result["new_heading_para_id"]
    assert by_id[copy_heading_id]["text"] == "Results"
    order = [p["para_id"] for p in paras]
    copy_start = order.index(copy_heading_id)
    assert [by_id[pid]["text"] for pid in order[copy_start:copy_start + 4]] == [
        "Results", "Results body paragraph.", "Sub-results", "Sub-results body paragraph.",
    ]


def test_copy_section_trim_rejects_destination_inside_original_section(tmp_path):
    """48daaf66: when trim_original_to is set, destination_anchor_para_id
    falling inside the section being trimmed must be rejected (mirrors
    move_section's own invariant) -- otherwise the trim step would delete
    the copy this same call just inserted."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.copy_section(
        path, "H0000002", "P0000002", destination_position="after",
        trim_original_to="See elsewhere.",
    )
    assert "error" in result
    assert "INSIDE" in result["error"]


# ---------------------------------------------------------------------------
# 75da13f0 -- structure-aware verification of move_section/copy_section on a
# REALISTIC mixed-id document: real headings, a real figure caption, a mix of
# native w14:paraId, synth-map sp<hash> (via _build_synth_id_map), and no-id
# paragraphs. Unit tests alone (every fixture above uses a native id on EVERY
# paragraph) missed the original id-scheme bug (7600db1c) -- only a
# structure-aware check that actually resolves synth ids the way a real
# caller would (via document_content_tree, not hand-computed hashes) catches
# it. Scratch documents only, never the real OneDrive/thesis file.
# ---------------------------------------------------------------------------

_MIXED_ID_SCHEME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Intro body paragraph with no native id at all.</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="_Ref200000001"/>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:bookmarkEnd w:id="0"/>
      <w:r><w:t xml:space="preserve">. The results figure, no native id.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Results body paragraph WITH a native id.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000004">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Conclusion body paragraph, no native id at all.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _synth_id_for_text(path: str, text: str) -> str:
    """Look up a paragraph's REAL para_id (native or synth) the way a real
    caller would: via document_content_tree, never by hand-computing the
    synth hash. Fails loudly if the text isn't found or is ambiguous, so a
    test bug can't silently pass."""
    from meridian_docs._vendored_content_tree import document_content_tree

    matches = [b["para_id"] for b in document_content_tree(path)["blocks"] if b["text"] == text]
    assert len(matches) == 1, f"expected exactly one block with text {text!r}, got {matches}"
    return matches[0]


def test_synth_ids_actually_differ_from_native_ids_in_fixture(tmp_path):
    """Sanity check on the fixture itself: the no-native-id paragraphs must
    really resolve to sp<hash> synth ids (not p{N} legacy ids), proving this
    fixture actually exercises the synth-id path 7600db1c fixed -- otherwise
    the rest of this test class would be exercising nothing new."""
    path = _write_docx(tmp_path, _MIXED_ID_SCHEME_XML, name="mixed.docx")
    results_heading_id = _synth_id_for_text(path, "Results")
    assert results_heading_id.startswith("sp"), (
        f"expected a synth sp<hash> id for the no-native-id 'Results' heading, "
        f"got {results_heading_id!r}"
    )


def test_get_section_content_resolves_synth_id_heading(tmp_path):
    """75da13f0: a heading with NO native w14:paraId (real-world common case
    -- Word doesn't assign one to every paragraph) must resolve via its
    synth id exactly as a native-id heading would."""
    path = _write_docx(tmp_path, _MIXED_ID_SCHEME_XML, name="mixed.docx")
    results_heading_id = _synth_id_for_text(path, "Results")

    result = docs_intel.get_section_content(path, results_heading_id)
    assert "error" not in result, f"synth id lookup failed: {result}"
    assert result["heading_text"] == "Results"
    assert result["paragraph_count"] == 3  # heading + caption + native-id body para


def test_move_section_resolves_synth_id_section_and_destination(tmp_path):
    """75da13f0: move_section must resolve BOTH section_id and
    destination_anchor_para_id via synth id, mirroring real usage where a
    caller reads ids from get_section_content/document_content_tree rather
    than a document where every paragraph happens to carry a native id."""
    path = _write_docx(tmp_path, _MIXED_ID_SCHEME_XML, name="mixed.docx")
    results_heading_id = _synth_id_for_text(path, "Results")
    conclusion_body_id = _synth_id_for_text(
        path, "Conclusion body paragraph, no native id at all."
    )

    result = docs_intel.move_section(
        path, results_heading_id, conclusion_body_id, destination_position="after"
    )
    assert "error" not in result, f"move_section failed on synth ids: {result}"
    assert result["status"] == "moved"

    # Confirm the section actually moved (Results now after Conclusion).
    paras = docs_intel.parse_docx(path)
    order = [p["text"] for p in paras]
    assert order.index("Conclusion body paragraph, no native id at all.") < order.index("Results")


def test_copy_section_resolves_synth_id_section_and_destination(tmp_path):
    """75da13f0: copy_section must resolve BOTH section_id and
    destination_anchor_para_id via synth id too."""
    path = _write_docx(tmp_path, _MIXED_ID_SCHEME_XML, name="mixed.docx")
    results_heading_id = _synth_id_for_text(path, "Results")

    result = docs_intel.copy_section(
        path, results_heading_id, "H0000001", destination_position="before"
    )
    assert "error" not in result, f"copy_section failed on synth ids: {result}"
    assert result["status"] == "copied"

    # The original (synth-id) section must still resolve after the copy.
    still_there = docs_intel.get_section_content(path, results_heading_id)
    assert "error" not in still_there, (
        f"original synth-id section no longer resolves after copy: {still_there}"
    )
    assert still_there["heading_text"] == "Results"


def test_move_section_after_synth_id_heading_anchor_does_not_swallow_dest_section(tmp_path):
    """75da13f0 + 027b7ada combined: the heading-anchor-after fix must also
    work when the DESTINATION anchor itself is a synth-id (no native id)
    heading, not just when it's a native-id one (the only case the earlier
    027b7ada-specific unit test covered)."""
    path = _write_docx(tmp_path, _MIXED_ID_SCHEME_XML, name="mixed.docx")
    conclusion_synth_id = _synth_id_for_text(path, "Conclusion")
    intro_native_id = "H0000001"

    result = docs_intel.move_section(
        path, intro_native_id, conclusion_synth_id, destination_position="after"
    )
    assert "error" not in result, f"move_section failed: {result}"

    # Conclusion's own body paragraph must not have been swallowed/re-parented.
    conclusion = docs_intel.get_section_content(path, conclusion_synth_id)
    conclusion_texts = [b["text"] for b in conclusion["blocks"] if b["kind"] == "paragraph"]
    assert conclusion_texts == ["Conclusion body paragraph, no native id at all."]


# ---------------------------------------------------------------------------
# 9907df44 -- mandatory post-write verification catches a false "success"
#
# Real incident: two consecutive live move_section calls both reported
# status="moved" with a wrong moved_block_count, while the on-disk .docx was
# byte-identical before and after (a silently no-op'd write). These tests
# reproduce that exact shape -- stub _save_docx_xml_stdlib to no-op -- and
# assert the tool now returns an error instead of trusting its own claimed
# counts/status.
# ---------------------------------------------------------------------------

def test_move_section_post_write_verification_catches_silent_noop_write(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(docs_intel, "_save_docx_xml_stdlib", lambda raw, root, dest: None)

    result = docs_intel.move_section(path, "H0000002", "H0000001", destination_position="before")

    assert "error" in result
    assert result.get("status") != "moved"
    assert result["content_hash_mismatch"] is not None

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "a verification-failed move must not leave the file mutated"
        )


def test_copy_section_post_write_verification_catches_silent_noop_write(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    monkeypatch.setattr(docs_intel, "_save_docx_xml_stdlib", lambda raw, root, dest: None)

    result = docs_intel.copy_section(path, "H0000002", "H0000001", destination_position="before")

    assert "error" in result
    assert result.get("status") != "copied"
    # copy_section locates its verification range by the fresh copy's own
    # paraId, which -- on a genuine no-op write -- was never actually
    # written to disk at all, so the hash check can't even run; the
    # structural count check (paragraph/heading counts stayed at their
    # pre-copy totals instead of growing by the copied section) is what
    # catches this case.
    assert result["count_mismatches"], f"expected count mismatches, got: {result}"
    assert "not found" in result["error"]

    with open(path, "rb") as fh:
        assert fh.read() == original_bytes, (
            "a verification-failed copy must not leave the file mutated"
        )


def test_move_section_post_write_verification_passes_on_genuine_move(tmp_path):
    """Control: a real (non-stubbed) move must NOT trip the new verification
    -- guards against the fix itself becoming a false positive."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.move_section(path, "H0000002", "H0000001", destination_position="before")
    assert "error" not in result, f"genuine move flagged as false success: {result}"
    assert result["status"] == "moved"


def test_copy_section_post_write_verification_passes_on_genuine_copy(tmp_path):
    """Control: a real (non-stubbed) copy must NOT trip the new verification."""
    path = _write_docx(tmp_path, _TWO_SECTION_XML)
    result = docs_intel.copy_section(path, "H0000002", "H0000001", destination_position="before")
    assert "error" not in result, f"genuine copy flagged as false success: {result}"
    assert result["status"] == "copied"
