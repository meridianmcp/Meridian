"""Tests for audit_manuscript_structure (docs-intel-jcshm-linter-gap-
cleanup-20260918) -- Abstract word count / Keywords count, the missing
checks for two explicit, numeric JCSHM submission requirements
(150-250 words / 4-6 keywords) that no tooling in this module verified
before this.

Covers:
  - abstract_word_count_out_of_range / keyword_count_out_of_range, each in
    isolation, plus the "unset policy bound -> don't check that bound"
    gating (independently per min/max, matching audit_heading_style's own
    per-edge independence).
  - Section location is TEXT-pattern based (an "Abstract" heading is any
    heading whose text matches _ABSTRACT_RE; a "Keywords" line is any
    paragraph matching _KEYWORDS_LABEL_RE immediately after the Abstract
    body) -- not tied to a specific heading style name.
  - A document with no locatable Abstract heading / no locatable Keywords
    line produces a None count and no finding for that half, never a
    guessed value.
  - build_document_review's "section_page" category now carries these
    findings (previously always 0 -- see REVIEW_CATEGORIES's own comment).
  - Read-only invariant, error handling, server wrapper parity, jcshm
    preset values.

All tests use synthetic .docx bytes built inline -- no real files, no
network. Mirrors test_docx_heading_style_audit.py's fixture conventions.
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


def _heading_para(level: int, text: str, para_id: str) -> str:
    return (
        f'<w:p w14:paraId="{para_id}">'
        f'<w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
        f'<w:r><w:t>{text}</w:t></w:r>'
        f"</w:p>"
    )


def _body_para(text: str, para_id: str) -> str:
    return f'<w:p w14:paraId="{para_id}"><w:r><w:t>{text}</w:t></w:r></w:p>'


def _words(n: int) -> str:
    return " ".join(f"word{i}" for i in range(n))


# ---------------------------------------------------------------------------
# Abstract word count
# ---------------------------------------------------------------------------

def test_abstract_word_count_computed_correctly(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(200), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)
    assert result["abstract_word_count"] == 200


def test_abstract_too_short_flagged_when_policy_set(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(50), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path,
        style_policy={"abstract_word_count_min": 150, "abstract_word_count_max": 250},
    )
    types = {f["type"] for f in result["findings"]}
    assert "abstract_word_count_out_of_range" in types
    f = next(f for f in result["findings"] if f["type"] == "abstract_word_count_out_of_range")
    assert f["actual_word_count"] == 50
    assert f["expected_min"] == 150
    assert f["expected_max"] == 250
    assert f["para_id"] == "00000001"


def test_abstract_too_long_flagged_when_policy_set(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(300), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path,
        style_policy={"abstract_word_count_min": 150, "abstract_word_count_max": 250},
    )
    types = {f["type"] for f in result["findings"]}
    assert "abstract_word_count_out_of_range" in types


def test_abstract_within_range_not_flagged(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(200), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path,
        style_policy={"abstract_word_count_min": 150, "abstract_word_count_max": 250},
    )
    types = {f["type"] for f in result["findings"]}
    assert "abstract_word_count_out_of_range" not in types


def test_abstract_check_skipped_when_policy_unset(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(5), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)  # no style_policy at all
    types = {f["type"] for f in result["findings"]}
    assert "abstract_word_count_out_of_range" not in types


def test_only_min_bound_set_checks_only_that_edge(tmp_path):
    """Each bound is independently gated -- setting only the min must not
    also enforce an implicit max, and vice versa."""
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(500), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path, style_policy={"abstract_word_count_min": 150}
    )
    types = {f["type"] for f in result["findings"]}
    assert "abstract_word_count_out_of_range" not in types


def test_multi_paragraph_abstract_body_concatenated(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(100), "00000002")
        + _body_para(_words(100), "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)
    assert result["abstract_word_count"] == 200


def test_abstract_body_stops_at_next_heading(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(150), "00000002")
        + _heading_para(1, "Introduction", "00000003")
        + _body_para(_words(999), "00000004")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)
    assert result["abstract_word_count"] == 150


def test_abstract_recognised_case_insensitively(tmp_path):
    body = _heading_para(1, "ABSTRACT", "00000001") + _body_para(_words(10), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)
    assert result["abstract_word_count"] == 10


def test_no_abstract_heading_found_returns_none_and_no_findings(tmp_path):
    body = _heading_para(1, "Introduction", "00000001") + _body_para(_words(50), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path,
        style_policy={
            "abstract_word_count_min": 150, "abstract_word_count_max": 250,
            "keyword_count_min": 4, "keyword_count_max": 6,
        },
    )
    assert result["abstract_word_count"] is None
    assert result["keyword_count"] is None
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Keywords count
# ---------------------------------------------------------------------------

def test_keyword_count_computed_correctly(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(150), "00000002")
        + _body_para("Keywords: crack detection, structural health monitoring, drones, deep learning", "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)
    assert result["keyword_count"] == 4


def test_keyword_count_too_low_flagged(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(150), "00000002")
        + _body_para("Keywords: crack detection, drones", "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path, style_policy={"keyword_count_min": 4, "keyword_count_max": 6}
    )
    types = {f["type"] for f in result["findings"]}
    assert "keyword_count_out_of_range" in types
    f = next(f for f in result["findings"] if f["type"] == "keyword_count_out_of_range")
    assert f["actual_keyword_count"] == 2
    assert f["para_id"] == "00000003"


def test_keyword_count_too_high_flagged(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(150), "00000002")
        + _body_para("Keywords: a, b, c, d, e, f, g, h", "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path, style_policy={"keyword_count_min": 4, "keyword_count_max": 6}
    )
    types = {f["type"] for f in result["findings"]}
    assert "keyword_count_out_of_range" in types


def test_keyword_count_within_range_not_flagged(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(150), "00000002")
        + _body_para("Keywords: a, b, c, d", "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path, style_policy={"keyword_count_min": 4, "keyword_count_max": 6}
    )
    types = {f["type"] for f in result["findings"]}
    assert "keyword_count_out_of_range" not in types


def test_keyword_count_check_skipped_when_policy_unset(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(150), "00000002")
        + _body_para("Keywords: a", "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)  # no style_policy at all
    types = {f["type"] for f in result["findings"]}
    assert "keyword_count_out_of_range" not in types


def test_semicolon_separated_keywords_also_counted(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(150), "00000002")
        + _body_para("Key words: crack detection; drones; deep learning", "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)
    assert result["keyword_count"] == 3


def test_no_keywords_line_found_keyword_count_is_none(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(150), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path, style_policy={"keyword_count_min": 4, "keyword_count_max": 6}
    )
    assert result["keyword_count"] is None
    types = {f["type"] for f in result["findings"]}
    assert "keyword_count_out_of_range" not in types


def test_keywords_line_not_counted_toward_abstract_word_count(tmp_path):
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(150), "00000002")
        + _body_para("Keywords: a, b, c, d", "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(path)
    assert result["abstract_word_count"] == 150


# ---------------------------------------------------------------------------
# jcshm preset values
# ---------------------------------------------------------------------------

def test_jcshm_preset_manuscript_structure_values():
    policy = docs_intel.get_journal_style_preset("jcshm")
    assert policy["abstract_word_count_min"] == 150
    assert policy["abstract_word_count_max"] == 250
    assert policy["keyword_count_min"] == 4
    assert policy["keyword_count_max"] == 6


def test_jcshm_preset_flags_a_real_violation_and_passes_a_compliant_manuscript(tmp_path):
    policy = docs_intel.get_journal_style_preset("jcshm")

    violating = _write_docx(
        tmp_path,
        _doc(
            _heading_para(1, "Abstract", "00000001")
            + _body_para(_words(80), "00000002")
            + _body_para("Keywords: crack detection, drones", "00000003")
        ),
        "violating.docx",
    )
    result = docs_intel.audit_manuscript_structure(violating, style_policy=policy)
    types = {f["type"] for f in result["findings"]}
    assert "abstract_word_count_out_of_range" in types
    assert "keyword_count_out_of_range" in types

    compliant = _write_docx(
        tmp_path,
        _doc(
            _heading_para(1, "Abstract", "00000001")
            + _body_para(_words(200), "00000002")
            + _body_para("Keywords: crack detection, drones, deep learning, SHM", "00000003")
        ),
        "compliant.docx",
    )
    result2 = docs_intel.audit_manuscript_structure(compliant, style_policy=policy)
    assert result2["finding_count"] == 0, result2["findings"]


# ---------------------------------------------------------------------------
# build_document_review wiring
# ---------------------------------------------------------------------------

def test_build_document_review_section_page_category_includes_manuscript_structure_findings(tmp_path):
    policy = docs_intel.get_journal_style_preset("jcshm")
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(50), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path, style_policy=policy)
    assert review["status"] == "ok"
    section_page_findings = [f for f in review["findings"] if f["category"] == "section_page"]
    types = {f["type"] for f in section_page_findings}
    assert "abstract_word_count_out_of_range" in types
    for f in section_page_findings:
        assert f["severity"] == "error"
        assert f["locator"]["status"] == "resolved"
    assert review["findings_by_category"]["section_page"] == len(section_page_findings)


def test_build_document_review_compliant_manuscript_produces_no_section_page_findings(tmp_path):
    policy = docs_intel.get_journal_style_preset("jcshm")
    body = (
        _heading_para(1, "Abstract", "00000001")
        + _body_para(_words(200), "00000002")
        + _body_para("Keywords: a, b, c, d", "00000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path, style_policy=policy)
    section_page_findings = [f for f in review["findings"] if f["category"] == "section_page"]
    assert section_page_findings == []


# ---------------------------------------------------------------------------
# Read-only, error handling, server wrapper parity
# ---------------------------------------------------------------------------

def test_invalid_style_policy_returns_error_not_exception(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(10), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_manuscript_structure(
        path, style_policy={"abstract_word_count_min": -1}
    )
    assert "error" in result


def test_missing_file_returns_error():
    result = docs_intel.audit_manuscript_structure("/no/such/file.docx")
    assert "error" in result


def test_read_only_never_mutates(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(10), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    before_bytes = open(path, "rb").read()
    docs_intel.audit_manuscript_structure(
        path, style_policy={"abstract_word_count_min": 150}
    )
    after_bytes = open(path, "rb").read()
    assert before_bytes == after_bytes


def test_server_wrapper_matches_docs_intel(tmp_path):
    body = _heading_para(1, "Abstract", "00000001") + _body_para(_words(10), "00000002")
    path = _write_docx(tmp_path, _doc(body))
    policy = {"abstract_word_count_min": 150}
    assert server.audit_manuscript_structure(
        path, style_policy=policy
    ) == docs_intel.audit_manuscript_structure(path, style_policy=policy)
