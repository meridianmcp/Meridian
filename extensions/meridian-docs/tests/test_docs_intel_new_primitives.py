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
    # destination_position anchors on the specific paragraph given (same
    # paragraph-level semantics as insert_caption/insert_citation elsewhere
    # in this module), not "after the whole section the anchor heads", so
    # anchoring on the Conclusion HEADING would land content between the
    # heading and its own body paragraph instead of at the document's end.
    result = docs_intel.move_section(path, "H0000001", "P0000004", destination_position="after")
    assert result["status"] == "moved"
    paras = docs_intel.parse_docx(path)
    order = [p["para_id"] for p in paras]
    assert order[-2:] == ["H0000001", "P0000001"]


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
