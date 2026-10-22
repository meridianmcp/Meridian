"""Tests for audit_table_style (9c1a3fd2), the table-structure counterpart
to audit_caption_style: table_misaligned (style-policy-gated), and the two
unconditional structural checks table_header_not_repeating and
blank_line_before_table.

Also covers:
  - insert_table's new table_alignment style_policy parameter (writes
    <w:jc> in the schema-correct position, right after <w:tblW>).
  - build_document_review's "structure" category wiring: audit_table_style
    findings now populate what was previously an always-0 placeholder.

All tests use synthetic .docx bytes built inline -- no real files, no
network. Mirrors test_docx_caption_style_audit.py's fixture conventions.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

_EXT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel, server  # noqa: E402

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_NS_HEADER = f'xmlns:w="{_W}" xmlns:w14="{_W14}"'


def _doc(body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS_HEADER}>
  <w:body>
{body_xml}
  </w:body>
</w:document>"""


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "sample.docx") -> str:
    path = tmp_path / name
    path.write_bytes(_zip_docx(xml))
    return str(path)


def _table_caption_para(label: str, rest: str, para_id: str) -> str:
    return (
        f'<w:p w14:paraId="{para_id}"><w:pPr><w:pStyle w:val="Caption"/></w:pPr>'
        f'<w:r><w:rPr><w:b/></w:rPr><w:t>{label}</w:t></w:r>'
        f'<w:r><w:t xml:space="preserve">{rest}</w:t></w:r>'
        f"</w:p>"
    )


def _blank_para(para_id: str) -> str:
    return f'<w:p w14:paraId="{para_id}"/>'


def _table(jc: str | None = None, header: bool = True, rows: int = 2) -> str:
    """A minimal 1x2 real content table. *jc* -- "center"/"left"/None (no
    <w:jc> at all, Word's own default alignment). *header* -- whether the
    first row carries <w:tblHeader/>."""
    jc_xml = f'<w:jc w:val="{jc}"/>' if jc else ""
    tbl_pr = f"<w:tblPr><w:tblW w:w=\"5000\" w:type=\"pct\"/>{jc_xml}</w:tblPr>"
    row_xml = ""
    for i in range(rows):
        tr_pr = "<w:trPr><w:tblHeader/></w:trPr>" if (header and i == 0) else ""
        row_xml += (
            f"<w:tr>{tr_pr}"
            f'<w:tc><w:p><w:r><w:t>cell{i}</w:t></w:r></w:p></w:tc>'
            f"</w:tr>"
        )
    return f"<w:tbl>{tbl_pr}<w:tblGrid><w:gridCol/></w:tblGrid>{row_xml}</w:tbl>"


# ---------------------------------------------------------------------------
# Detection: each finding type in isolation
# ---------------------------------------------------------------------------

def test_misaligned_table_flagged_when_policy_set(tmp_path):
    body = _table_caption_para("Table 1", " Summary statistics", "00000001") + _table(
        jc=None, header=True
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path, style_policy={"table_alignment": "center"})
    assert result["table_count"] == 1
    types = {f["type"] for f in result["findings"]}
    assert "table_misaligned" in types
    f = next(f for f in result["findings"] if f["type"] == "table_misaligned")
    assert f["expected_alignment"] == "center"
    assert f["actual_alignment"] is None


def test_correctly_aligned_table_not_flagged(tmp_path):
    body = _table_caption_para("Table 1", " Summary statistics", "00000001") + _table(
        jc="center", header=True
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path, style_policy={"table_alignment": "center"})
    types = {f["type"] for f in result["findings"]}
    assert "table_misaligned" not in types


def test_alignment_check_skipped_when_policy_unset(tmp_path):
    """A left-aligned (no <w:jc>) table produces no table_misaligned finding
    when table_alignment is left at its default None -- "no verified rule"
    must mean "don't check", not "assume center and flag everything"."""
    body = _table_caption_para("Table 1", " Summary statistics", "00000001") + _table(
        jc=None, header=True
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path)  # no style_policy at all
    types = {f["type"] for f in result["findings"]}
    assert "table_misaligned" not in types


def test_missing_header_repeat_flagged_unconditionally(tmp_path):
    body = _table_caption_para("Table 1", " Summary statistics", "00000001") + _table(
        jc="center", header=False
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path)  # no style_policy at all
    types = {f["type"] for f in result["findings"]}
    assert "table_header_not_repeating" in types


def test_header_repeat_present_not_flagged(tmp_path):
    body = _table_caption_para("Table 1", " Summary statistics", "00000001") + _table(
        jc="center", header=True
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path)
    types = {f["type"] for f in result["findings"]}
    assert "table_header_not_repeating" not in types


def test_blank_line_before_table_flagged_unconditionally(tmp_path):
    body = (
        _table_caption_para("Table 1", " Summary statistics", "00000001")
        + _blank_para("00000002")
        + _table(jc="center", header=True)
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path)
    types = {f["type"] for f in result["findings"]}
    assert "blank_line_before_table" in types
    f = next(f for f in result["findings"] if f["type"] == "blank_line_before_table")
    assert f["blank_paragraph_count"] == 1


def test_no_blank_line_before_table_not_flagged(tmp_path):
    body = _table_caption_para("Table 1", " Summary statistics", "00000001") + _table(
        jc="center", header=True
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path)
    types = {f["type"] for f in result["findings"]}
    assert "blank_line_before_table" not in types


def test_multiple_consecutive_blank_lines_counted(tmp_path):
    body = (
        _table_caption_para("Table 1", " Summary statistics", "00000001")
        + _blank_para("00000002")
        + _blank_para("00000003")
        + _table(jc="center", header=True)
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path)
    f = next(f for f in result["findings"] if f["type"] == "blank_line_before_table")
    assert f["blank_paragraph_count"] == 2


def test_all_three_findings_together(tmp_path):
    body = (
        _table_caption_para("Table 1", " Summary statistics", "00000001")
        + _blank_para("00000002")
        + _table(jc=None, header=False)
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path, style_policy={"table_alignment": "center"})
    types = {f["type"] for f in result["findings"]}
    assert types == {"table_misaligned", "table_header_not_repeating", "blank_line_before_table"}


# ---------------------------------------------------------------------------
# Scoping: only CAPTIONED tables are audited
# ---------------------------------------------------------------------------

def test_uncaptioned_table_is_ignored(tmp_path):
    """An un-captioned table (e.g. an equation-numbering layout table) must
    not be scanned at all -- this audit is scoped to captioned content
    tables only, same discipline as audit_caption_style's own detection."""
    body = _table(jc=None, header=False)
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path, style_policy={"table_alignment": "center"})
    assert result["table_count"] == 0
    assert result["finding_count"] == 0


def test_figure_caption_is_not_treated_as_a_table_caption(tmp_path):
    body = _table_caption_para("Fig. 1", " A figure, not a table", "00000001") + _table(
        jc=None, header=False
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path, style_policy={"table_alignment": "center"})
    assert result["table_count"] == 0


def test_table_caption_not_immediately_followed_by_table_is_skipped(tmp_path):
    body = _table_caption_para("Table 1", " Referenced but not adjacent", "00000001") + (
        f'<w:p w14:paraId="00000002"><w:r><w:t>some prose in between</w:t></w:r></w:p>'
    ) + _table(jc=None, header=False)
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path, style_policy={"table_alignment": "center"})
    assert result["table_count"] == 0


# ---------------------------------------------------------------------------
# Read-only, error handling, server wrapper parity
# ---------------------------------------------------------------------------

def test_invalid_style_policy_returns_error_not_exception(tmp_path):
    body = _table_caption_para("Table 1", " ok", "00000001") + _table(jc="center", header=True)
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_table_style(path, style_policy={"table_alignment": "bogus"})
    assert "error" in result


def test_missing_file_returns_error():
    result = docs_intel.audit_table_style("/no/such/file.docx")
    assert "error" in result


def test_read_only_never_mutates(tmp_path):
    body = _table_caption_para("Table 1", " ok", "00000001") + _table(jc=None, header=False)
    path = _write_docx(tmp_path, _doc(body))
    before = open(path, "rb").read()
    docs_intel.audit_table_style(path, style_policy={"table_alignment": "center"})
    after = open(path, "rb").read()
    assert before == after


def test_server_wrapper_matches_docs_intel(tmp_path):
    body = _table_caption_para("Table 1", " ok", "00000001") + _table(jc=None, header=False)
    path = _write_docx(tmp_path, _doc(body))
    policy = {"table_alignment": "center"}
    assert server.audit_table_style(path, style_policy=policy) == docs_intel.audit_table_style(
        path, style_policy=policy
    )


def _rendered_ok(monkeypatch):
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "rendered", "backend": "test-stub", "detail": {}},
    )


def test_jcshm_preset_has_table_alignment_center():
    policy = docs_intel.get_journal_style_preset("jcshm")
    assert policy["table_alignment"] == "center"


# ---------------------------------------------------------------------------
# build_document_review wiring: audit_table_style now populates the
# "structure" category, previously an always-0 reserved placeholder.
# ---------------------------------------------------------------------------

def test_build_document_review_structure_category_zero_without_style_policy_for_alignment(tmp_path):
    """No style_policy -- table_misaligned is skipped (opt-in check), but
    the unconditional header-repeat/blank-line checks still run and DO
    populate "structure", unlike the old always-0 placeholder."""
    body = (
        _table_caption_para("Table 1", " ok", "00000001")
        + _blank_para("00000002")
        + _table(jc=None, header=False)
    )
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path)
    assert review["status"] == "ok"
    structure_findings = [f for f in review["findings"] if f["category"] == "structure"]
    types = {f["type"] for f in structure_findings}
    assert "table_misaligned" not in types
    assert "table_header_not_repeating" in types
    assert "blank_line_before_table" in types
    assert review["findings_by_category"]["structure"] == len(structure_findings)


def test_build_document_review_surfaces_table_misaligned_with_journal_policy(tmp_path):
    policy = docs_intel.get_journal_style_preset("jcshm")
    body = _table_caption_para("Table 1", " ok", "00000001") + _table(jc=None, header=True)
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path, style_policy=policy)
    structure_findings = [f for f in review["findings"] if f["category"] == "structure"]
    types = {f["type"] for f in structure_findings}
    assert "table_misaligned" in types
    for f in structure_findings:
        assert f["locator"]["status"] == "resolved"
        assert f["locator"]["target_para_id"] == "00000001"


def test_build_document_review_compliant_table_produces_no_structure_findings(tmp_path):
    policy = docs_intel.get_journal_style_preset("jcshm")
    body = _table_caption_para("Table 1", " ok", "00000001") + _table(jc="center", header=True)
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path, style_policy=policy)
    structure_findings = [f for f in review["findings"] if f["category"] == "structure"]
    assert structure_findings == []
    assert review["findings_by_category"]["structure"] == 0


# ---------------------------------------------------------------------------
# insert_table's new table_alignment style_policy parameter
# ---------------------------------------------------------------------------

def test_insert_table_writes_no_jc_by_default(tmp_path, monkeypatch):
    """Regression guard: insert_table must remain byte-identical (no <w:jc>
    at all) for every caller that doesn't pass table_alignment -- the exact
    pre-9c1a3fd2 behavior that caused every table it ever wrote to default
    to Word's own left alignment."""
    _rendered_ok(monkeypatch)
    body = _blank_para("00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.insert_table(
        path,
        anchor_para_id="00000001",
        position="after",
        rows=2,
        cols=2,
    )
    assert result["status"] == "inserted", result
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "<w:jc" not in xml


def test_insert_table_writes_jc_in_schema_correct_position_when_requested(tmp_path, monkeypatch):
    """<w:jc> must land immediately after </w:tblW>, before tblGrid/rows --
    CT_TblPrBase's required child order. A first attempt at table centering
    (a one-off script, not this function) got this wrong; this function's
    own implementation must get it right from the start."""
    _rendered_ok(monkeypatch)
    body = _blank_para("00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.insert_table(
        path,
        anchor_para_id="00000001",
        position="after",
        rows=2,
        cols=2,
        style_policy={"table_alignment": "center"},
    )
    assert result["status"] == "inserted", result
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    tblw_pos = xml.index("<w:tblW")
    tblw_end = xml.index("/>", tblw_pos) + 2
    jc_pos = xml.index('<w:jc w:val="center"/>')
    assert jc_pos == tblw_end
