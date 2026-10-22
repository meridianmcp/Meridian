"""Tests for flag_for_review (7c3e4b9a) -- the "flag this spot for a human to
look at" primitive: a native <w:highlight> on every run of an anchored
paragraph PLUS a real Word comment explaining what needs attention, anchored
to that same paragraph, written in ONE atomic operation.

Replaces the ad-hoc pattern of hand-rolling a raw-zip script (open the
.docx, string-replace inside word/document.xml, create word/comments.xml
from scratch by hand when the document had none yet, re-zip) every time a
document needed a flagged review spot.

Mirrors test_docs_intel_search_comments.py's fixture conventions (same
_write_docx/_read_xml helpers, same render-capability stub) and
test_docx_word_com_regression.py's render-gate/restore-on-failure test
shapes, since flag_for_review reuses insert_word_comment's own comment-part
plumbing (_stage_word_comment) and the same _save_docx_with_new_parts_stdlib
/ _docx_promotion_lock / _enforce_render_verification write path.
"""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel, server


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """Stub a successful 'rendered' result so every test here exercises
    STRUCTURAL correctness only, independent of whatever render backends
    (LibreOffice, Word COM) happen to be installed on the machine running
    the suite -- mirrors test_docs_intel_search_comments.py's fixture of the
    same name.
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
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Methods</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>The alpha_scale header needs a second look before submission.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>The alpha_scale header needs a second look before submission.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="F0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr=" SEQ Figure \\* ARABIC ">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t xml:space="preserve"> -- placeholder figure</w:t></w:r>
    </w:p>
    <w:p w14:paraId="E0000001"/>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>cell needs review</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _write_docx(tmp_path, name="doc.docx", document_xml=_DOCUMENT_XML):
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return path


# A fixture with comment infrastructure ALREADY present: two non-contiguous
# existing comment ids (0 and 5) anchored to the Methods heading and
# P0000001 respectively, so _next_word_comment_id's max-seen-plus-one logic
# is exercised against real, non-trivial prior state (not just "empty").
_DOCUMENT_XML_WITH_COMMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:commentRangeStart w:id="0"/>
      <w:r><w:t>Methods</w:t></w:r>
      <w:commentRangeEnd w:id="0"/>
      <w:r><w:commentReference w:id="0"/></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:commentRangeStart w:id="5"/>
      <w:r><w:t>Already-reviewed paragraph.</w:t></w:r>
      <w:commentRangeEnd w:id="5"/>
      <w:r><w:commentReference w:id="5"/></w:r>
    </w:p>
    <w:p w14:paraId="F0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr=" SEQ Figure \\* ARABIC ">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t xml:space="preserve"> -- needs a caption sanity check</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_COMMENTS_XML_EXISTING = """<?xml version="1.0" encoding="UTF-8"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="0" w:author="Prior Reviewer" w:initials="PR" w:date="2026-01-01T00:00:00Z">
    <w:p><w:r><w:t>Existing comment zero.</w:t></w:r></w:p>
  </w:comment>
  <w:comment w:id="5" w:author="Prior Reviewer" w:initials="PR" w:date="2026-01-01T00:00:00Z">
    <w:p><w:r><w:t>Existing comment five.</w:t></w:r></w:p>
  </w:comment>
</w:comments>
"""

_RELS_XML_EXISTING = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>
"""

_CONTENT_TYPES_XML_EXISTING = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>
"""


def _write_docx_with_comments(tmp_path, name="doc_with_comments.docx"):
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML_WITH_COMMENTS)
        archive.writestr("word/comments.xml", _COMMENTS_XML_EXISTING)
        archive.writestr("word/_rels/document.xml.rels", _RELS_XML_EXISTING)
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML_EXISTING)
    return path


def _read_xml(path, member):
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read(member))


def _read_document_xml_bytes(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read("word/document.xml")


def _highlight_vals_in_paragraph(paragraph):
    return [
        h.get("{%s}val" % _W)
        for run in paragraph.iter("{%s}r" % _W)
        for h in run.iter("{%s}highlight" % _W)
    ]


def _find_paragraph(root, para_id):
    body = root.find("{%s}body" % _W)
    for p in body.findall("{%s}p" % _W):
        if p.get("{%s}paraId" % _W14) == para_id:
            return p
    return None


# ---------------------------------------------------------------------------
# 1. Zero pre-existing comments: infrastructure created from scratch.
# ---------------------------------------------------------------------------


def test_flag_for_review_no_existing_comments_creates_infra_and_highlight(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.flag_for_review(
        path, "P0000001", "This claim needs a citation before submission.",
        author="Adam", initials="AC",
    )

    assert result["status"] == "flagged"
    assert result["comment_id"] == 0
    assert result["note_id"] == "_MComment0"
    assert result["anchor_para_id"] == "P0000001"
    assert result["highlighted_run_count"] == 1
    assert result["highlight_color"] == "yellow"
    assert result["render_verified"] is True

    document = _read_xml(path, "word/document.xml")
    comments = _read_xml(path, "word/comments.xml")
    rels = _read_xml(path, "word/_rels/document.xml.rels")
    content_types = _read_xml(path, "[Content_Types].xml")

    target = _find_paragraph(document, "P0000001")
    assert target is not None
    assert "yellow" in _highlight_vals_in_paragraph(target)
    assert target.find("{%s}commentRangeStart" % _W).get("{%s}id" % _W) == "0"
    assert target.find("{%s}commentRangeEnd" % _W).get("{%s}id" % _W) == "0"
    assert target.find(".//{%s}commentReference" % _W).get("{%s}id" % _W) == "0"

    comment = comments.find(".//{%s}comment" % _W)
    assert comment.get("{%s}id" % _W) == "0"
    assert comment.get("{%s}author" % _W) == "Adam"
    assert comment.get("{%s}initials" % _W) == "AC"
    comment_text = "".join(t.text or "" for t in comment.iter("{%s}t" % _W))
    assert comment_text == "This claim needs a citation before submission."

    assert rels.find(".//{%s}Relationship" % _REL).get("Type", "").endswith("/comments")
    assert content_types.find(".//{%s}Override" % _CT).get("PartName") == "/word/comments.xml"

    # Other paragraphs are untouched -- highlight/comment landed ONLY on the
    # anchored paragraph, not the whole document.
    other = _find_paragraph(document, "P0000002")
    assert _highlight_vals_in_paragraph(other) == []


# ---------------------------------------------------------------------------
# 2. Pre-existing comments: safe id allocation (no collision), nothing lost.
# ---------------------------------------------------------------------------


def test_flag_for_review_with_existing_comments_allocates_safe_id_and_preserves_existing(tmp_path):
    path = _write_docx_with_comments(tmp_path)

    result = docs_intel.flag_for_review(
        path, "F0000001", "Double-check this caption against the actual figure.",
    )

    assert result["status"] == "flagged"
    # max seen (0, 5) + 1 -- never collides with either pre-existing comment.
    assert result["comment_id"] == 6
    assert result["note_id"] == "_MComment6"

    comments = _read_xml(path, "word/comments.xml")
    ids = {c.get("{%s}id" % _W) for c in comments.findall(".//{%s}comment" % _W)}
    assert ids == {"0", "5", "6"}

    # Both pre-existing comments' text survived byte-for-byte.
    by_id = {c.get("{%s}id" % _W): c for c in comments.findall(".//{%s}comment" % _W)}
    text0 = "".join(t.text or "" for t in by_id["0"].iter("{%s}t" % _W))
    text5 = "".join(t.text or "" for t in by_id["5"].iter("{%s}t" % _W))
    assert text0 == "Existing comment zero."
    assert text5 == "Existing comment five."

    document = _read_xml(path, "word/document.xml")
    # Pre-existing range markers for comment 0/5 are still present and
    # unmodified -- new markers did not overwrite or renumber them.
    all_ids = {
        el.get("{%s}id" % _W)
        for tag in ("commentRangeStart", "commentRangeEnd", "commentReference")
        for el in document.iter("{%s}%s" % (_W, tag))
    }
    assert all_ids == {"0", "5", "6"}

    # Exactly ONE comments relationship / content-type override -- not
    # duplicated by re-running the "create if absent" wiring.
    rels = _read_xml(path, "word/_rels/document.xml.rels")
    comment_rels = [
        r for r in rels.findall(".//{%s}Relationship" % _REL)
        if r.get("Type", "").endswith("/comments")
    ]
    assert len(comment_rels) == 1
    content_types = _read_xml(path, "[Content_Types].xml")
    overrides = [
        o for o in content_types.findall(".//{%s}Override" % _CT)
        if o.get("PartName") == "/word/comments.xml"
    ]
    assert len(overrides) == 1


# ---------------------------------------------------------------------------
# 3. Anchor resolution via a locate_anchor-style text query.
# ---------------------------------------------------------------------------


def test_flag_for_review_text_query_anchor_resolves_and_flags(tmp_path):
    path = _write_docx(tmp_path)

    result = docs_intel.flag_for_review(
        path,
        {"text": "placeholder figure"},
        "Confirm this is the final figure before submission.",
    )

    assert result["status"] == "flagged"
    assert result["anchor_para_id"] == "F0000001"
    assert result["element_type"] == "figure_caption"
    assert "source_fingerprint" in result

    document = _read_xml(path, "word/document.xml")
    target = _find_paragraph(document, "F0000001")
    assert "yellow" in _highlight_vals_in_paragraph(target)


def test_flag_for_review_ambiguous_text_anchor_is_refused_and_file_untouched(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)

    # "alpha_scale header" appears verbatim in BOTH P0000001 and P0000002.
    result = docs_intel.flag_for_review(
        path, {"text": "alpha_scale header"}, "Which one is this referring to?",
    )

    assert "error" in result
    assert result.get("locate_result", {}).get("status") == "ambiguous"
    assert _read_document_xml_bytes(path) == before


def test_flag_for_review_table_target_anchor_is_refused(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)

    result = docs_intel.flag_for_review(
        path, {"text": "cell needs review"}, "This cell looks wrong.",
    )

    assert "error" in result
    assert "table" in result["error"]
    assert _read_document_xml_bytes(path) == before


def test_flag_for_review_unresolved_text_anchor_is_refused(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)

    result = docs_intel.flag_for_review(
        path, {"text": "text that does not appear anywhere in this document"}, "n/a",
    )

    assert "error" in result
    assert result.get("locate_result", {}).get("status") == "not_found"
    assert _read_document_xml_bytes(path) == before


# ---------------------------------------------------------------------------
# 4. Validation failures never touch the file.
# ---------------------------------------------------------------------------


def test_flag_for_review_invalid_highlight_color_rejected_before_write(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)

    result = docs_intel.flag_for_review(
        path, "P0000001", "note", highlight_color="chartreuse",
    )

    assert "error" in result
    assert "highlight_color" in result["error"]
    assert _read_document_xml_bytes(path) == before


def test_flag_for_review_empty_note_rejected_before_write(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)

    result = docs_intel.flag_for_review(path, "P0000001", "   ")

    assert "error" in result
    assert _read_document_xml_bytes(path) == before


def test_flag_for_review_unknown_para_id_rejected(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)

    result = docs_intel.flag_for_review(path, "DOES_NOT_EXIST", "note")

    assert "error" in result
    assert "not found" in result["error"]
    assert _read_document_xml_bytes(path) == before


def test_flag_for_review_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)

    result = docs_intel.flag_for_review(
        path, "P0000001", "note", allow_degraded_render=True, degraded_render_reason="  ",
    )

    assert "error" in result
    assert _read_document_xml_bytes(path) == before


def test_flag_for_review_empty_anchor_paragraph_still_flags_with_zero_highlighted_runs(tmp_path):
    # E0000001 is an empty placeholder paragraph -- flag_for_review should
    # still attach the comment (a legitimate "this blank paragraph shouldn't
    # be here" flag) even though there is no run to highlight.
    path = _write_docx(tmp_path)

    result = docs_intel.flag_for_review(path, "E0000001", "Why is this paragraph empty?")

    assert result["status"] == "flagged"
    assert result["highlighted_run_count"] == 0


# ---------------------------------------------------------------------------
# 5. Sidecar recording (mirrors insert_highlighted_note's mode="comment").
# ---------------------------------------------------------------------------


def test_flag_for_review_records_into_internal_notes_sidecar(tmp_path):
    path = _write_docx(tmp_path)
    db = str(tmp_path / "idx.sqlite")
    # Sidecar sync is gated on the sidecar already existing on disk --
    # pre-create it via index_docx, matching real usage.
    docs_intel.index_docx(path, db)

    result = docs_intel.flag_for_review(
        path, "P0000001", "Sidecar-recorded flag.", index_db_path=db,
    )
    assert result["status"] == "flagged"

    notes = docs_intel.list_internal_notes(db)
    assert len(notes) == 1
    assert notes[0]["note_id"] == result["note_id"]
    assert notes[0]["anchor_para_id"] == "P0000001"
    assert notes[0]["text"] == "Sidecar-recorded flag."


# ---------------------------------------------------------------------------
# 6. Render-gate wiring + structural-verification-failure restore, mirroring
#    test_docx_word_com_regression.py's shape for the other W2-C writers.
# ---------------------------------------------------------------------------


def test_flag_for_review_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "backend": "test-stub", "detail": {}},
    )

    result = docs_intel.flag_for_review(path, "P0000001", "note")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml_bytes(path) == before


def test_flag_for_review_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = docs_intel.flag_for_review(path, "P0000001", "note")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml_bytes(path) == before


def test_flag_for_review_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = docs_intel.flag_for_review(
        path, "P0000001", "note",
        allow_degraded_render=True, degraded_render_reason="no backend in test env",
    )

    assert result["status"] == "flagged"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


def test_flag_for_review_structural_verification_failure_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    before = _read_document_xml_bytes(path)
    render_calls = {"n": 0}

    def _spy(p, **kwargs):
        render_calls["n"] += 1
        return {"status": "rendered", "backend": "test-stub", "detail": {}}

    monkeypatch.setattr(docs_intel.render_gate, "check_render_capability", _spy)
    monkeypatch.setattr(
        docs_intel, "_verify_flag_write",
        lambda *a, **kw: {"error": "post-write verification failed: simulated mismatch"},
    )

    result = docs_intel.flag_for_review(path, "P0000001", "note")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml_bytes(path) == before
    # The render gate must never even run once structural verification has
    # already failed -- same ordering insert_highlighted_note enforces.
    assert render_calls["n"] == 0


# ---------------------------------------------------------------------------
# 7. server.py MCP wrapper delegates correctly.
# ---------------------------------------------------------------------------


def test_flag_for_review_server_wrapper_delegates(tmp_path):
    path = _write_docx(tmp_path)

    result = server.flag_for_review(
        path, "P0000001", "Please review this paragraph.",
        highlight_color="green",
        author="Adam", initials="AC",
    )

    assert result["status"] == "flagged"
    assert result["author"] == "Adam"


def test_flag_for_review_server_wrapper_threads_degraded_render_params(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = server.flag_for_review(
        path, "P0000001", "Please review this paragraph.",
        allow_degraded_render=True,
        degraded_render_reason="no backend in test env",
    )

    assert result["status"] == "flagged"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True
