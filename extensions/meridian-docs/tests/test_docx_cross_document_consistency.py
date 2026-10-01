"""Tests for audit_cross_document_consistency (df716454) -- the
manuscript-vs-Supplementary-Information style-DEFINITION diff checker, plus
its body_text_font_family/size_mismatch checks against a style_policy
(the real motivating case: a JCSHM SI found with 12pt BodyText/Normal
against the journal's own stated "10-point Times Roman" guideline).

Unlike every other audit_* function in this module, this one takes TWO docx
paths and compares word/styles.xml STYLE DEFINITIONS, not paragraph
instances -- see docs_intel.audit_cross_document_consistency's own
docstring for why findings carry a "document" tag or
manuscript_style_id/si_style_id instead of a para_id/locator.

All tests use synthetic .docx bytes built inline (document.xml + a
minimal styles.xml) -- no real files, no network. Mirrors
test_docx_table_style_audit.py's fixture conventions.
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
_NS_HEADER = f'xmlns:w="{_W}"'

_EMPTY_DOCUMENT_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS_HEADER}>
  <w:body/>
</w:document>"""


def _style_block(
    style_id: str,
    name: str,
    before: int | None = None,
    after: int | None = None,
    font_family: str | None = None,
    sz_half_pt: int | None = None,
) -> str:
    ppr = ""
    if before is not None or after is not None:
        attrs = ""
        if before is not None:
            attrs += f' w:before="{before}"'
        if after is not None:
            attrs += f' w:after="{after}"'
        ppr = f"<w:pPr><w:spacing{attrs}/></w:pPr>"
    rpr = ""
    if font_family is not None or sz_half_pt is not None:
        rfonts = f'<w:rFonts w:ascii="{font_family}"/>' if font_family is not None else ""
        sz = f'<w:sz w:val="{sz_half_pt}"/>' if sz_half_pt is not None else ""
        rpr = f"<w:rPr>{rfonts}{sz}</w:rPr>"
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}">'
        f'<w:name w:val="{name}"/>{ppr}{rpr}'
        f"</w:style>"
    )


def _styles_xml(*blocks: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles {_NS_HEADER}>
{"".join(blocks)}
</w:styles>"""


def _write_docx(tmp_path, name: str, styles_xml: str | None) -> str:
    path = tmp_path / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _EMPTY_DOCUMENT_XML)
        if styles_xml is not None:
            zf.writestr("word/styles.xml", styles_xml)
    path.write_bytes(buf.getvalue())
    return str(path)


_BODY_TEXT_10PT_TNR = _style_block(
    "BodyText", "Body Text", font_family="Times New Roman", sz_half_pt=20,
)
_BODY_TEXT_12PT_TNR = _style_block(
    "BodyText", "Body Text", font_family="Times New Roman", sz_half_pt=24,
)


# ---------------------------------------------------------------------------
# cross_document_style_mismatch detection
# ---------------------------------------------------------------------------

def test_title_spacing_mismatch_flagged(tmp_path):
    manuscript = _write_docx(
        tmp_path, "manuscript.docx",
        _styles_xml(_style_block("Heading0", "Title", before=480, after=240)),
    )
    si = _write_docx(
        tmp_path, "si.docx",
        _styles_xml(_style_block("Heading0", "Title", before=240, after=240)),
    )
    result = docs_intel.audit_cross_document_consistency(manuscript, si)
    types = {f["type"] for f in result["findings"]}
    assert "cross_document_style_mismatch" in types
    f = next(f for f in result["findings"] if f["type"] == "cross_document_style_mismatch")
    assert f["style_key"] == "title_style"
    assert f["field"] == "spacing_before_twips"
    assert f["manuscript_value"] == 480
    assert f["si_value"] == 240


def test_figure_image_spacing_mismatch_flagged(tmp_path):
    manuscript = _write_docx(
        tmp_path, "manuscript.docx",
        _styles_xml(_style_block("FigureImage", "Figure Image", before=120, after=120)),
    )
    si = _write_docx(
        tmp_path, "si.docx",
        _styles_xml(_style_block("FigureImage", "Figure Image", before=240, after=120)),
    )
    result = docs_intel.audit_cross_document_consistency(manuscript, si)
    mismatches = [
        f for f in result["findings"]
        if f["type"] == "cross_document_style_mismatch" and f["style_key"] == "figure_image_style"
    ]
    assert len(mismatches) == 1
    assert mismatches[0]["field"] == "spacing_before_twips"


def test_matching_styles_produce_no_mismatch(tmp_path):
    manuscript = _write_docx(
        tmp_path, "manuscript.docx",
        _styles_xml(_style_block("Heading0", "Title", before=480, after=240)),
    )
    si = _write_docx(
        tmp_path, "si.docx",
        _styles_xml(_style_block("Heading0", "Title", before=480, after=240)),
    )
    result = docs_intel.audit_cross_document_consistency(manuscript, si)
    assert result["findings"] == []


def test_style_present_in_only_one_document_is_skipped_not_guessed(tmp_path):
    manuscript = _write_docx(
        tmp_path, "manuscript.docx",
        _styles_xml(_style_block("FigureImage", "Figure Image", before=120, after=120)),
    )
    si = _write_docx(tmp_path, "si.docx", _styles_xml())
    result = docs_intel.audit_cross_document_consistency(manuscript, si)
    mismatches = [f for f in result["findings"] if f["type"] == "cross_document_style_mismatch"]
    assert mismatches == []


def test_style_matched_by_name_fallback_when_id_differs(tmp_path):
    """styleId doesn't match any candidate exactly, but the <w:name> does
    (case-insensitive substring) -- the same id-then-name tolerance
    _is_caption_style/_is_heading use elsewhere in this module."""
    manuscript = _write_docx(
        tmp_path, "manuscript.docx",
        _styles_xml(_style_block("CustomTitleStyle1", "Title", before=480, after=240)),
    )
    si = _write_docx(
        tmp_path, "si.docx",
        _styles_xml(_style_block("CustomTitleStyle2", "Title", before=240, after=240)),
    )
    result = docs_intel.audit_cross_document_consistency(manuscript, si)
    types = {f["type"] for f in result["findings"]}
    assert "cross_document_style_mismatch" in types


# ---------------------------------------------------------------------------
# body_text_font_family_mismatch / body_text_font_size_mismatch (policy-gated)
# ---------------------------------------------------------------------------

def test_body_text_font_size_mismatch_flagged_against_jcshm_policy(tmp_path):
    """The real motivating case: an SI with 12pt BodyText against JCSHM's
    own stated 10pt Times Roman guideline -- must be caught even though the
    manuscript itself is compliant (i.e. not just a manuscript-vs-SI diff)."""
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml(_BODY_TEXT_10PT_TNR))
    si = _write_docx(tmp_path, "si.docx", _styles_xml(_BODY_TEXT_12PT_TNR))
    policy = docs_intel.get_journal_style_preset("jcshm")
    result = docs_intel.audit_cross_document_consistency(manuscript, si, style_policy=policy)
    size_findings = [f for f in result["findings"] if f["type"] == "body_text_font_size_mismatch"]
    assert len(size_findings) == 1
    assert size_findings[0]["document"] == "si"
    assert size_findings[0]["expected_font_size_pt"] == 10
    assert size_findings[0]["actual_font_size_pt"] == 12


def test_body_text_font_family_mismatch_flagged_against_jcshm_policy(tmp_path):
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml(_BODY_TEXT_10PT_TNR))
    si = _write_docx(
        tmp_path, "si.docx",
        _styles_xml(_style_block("BodyText", "Body Text", font_family="Arial", sz_half_pt=20)),
    )
    policy = docs_intel.get_journal_style_preset("jcshm")
    result = docs_intel.audit_cross_document_consistency(manuscript, si, style_policy=policy)
    family_findings = [
        f for f in result["findings"] if f["type"] == "body_text_font_family_mismatch"
    ]
    assert len(family_findings) == 1
    assert family_findings[0]["document"] == "si"
    assert family_findings[0]["expected_font_family"] == "Times New Roman"
    assert family_findings[0]["actual_font_family"] == "Arial"


def test_both_documents_checked_independently(tmp_path):
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml(_BODY_TEXT_12PT_TNR))
    si = _write_docx(tmp_path, "si.docx", _styles_xml(_BODY_TEXT_12PT_TNR))
    policy = docs_intel.get_journal_style_preset("jcshm")
    result = docs_intel.audit_cross_document_consistency(manuscript, si, style_policy=policy)
    size_findings = [f for f in result["findings"] if f["type"] == "body_text_font_size_mismatch"]
    assert {f["document"] for f in size_findings} == {"manuscript", "si"}


def test_body_text_checks_skipped_when_policy_unset(tmp_path):
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml(_BODY_TEXT_12PT_TNR))
    si = _write_docx(tmp_path, "si.docx", _styles_xml(_BODY_TEXT_12PT_TNR))
    result = docs_intel.audit_cross_document_consistency(manuscript, si)  # no style_policy
    types = {f["type"] for f in result["findings"]}
    assert "body_text_font_size_mismatch" not in types
    assert "body_text_font_family_mismatch" not in types


def test_compliant_body_text_not_flagged(tmp_path):
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml(_BODY_TEXT_10PT_TNR))
    si = _write_docx(tmp_path, "si.docx", _styles_xml(_BODY_TEXT_10PT_TNR))
    policy = docs_intel.get_journal_style_preset("jcshm")
    result = docs_intel.audit_cross_document_consistency(manuscript, si, style_policy=policy)
    types = {f["type"] for f in result["findings"]}
    assert "body_text_font_size_mismatch" not in types
    assert "body_text_font_family_mismatch" not in types


# ---------------------------------------------------------------------------
# Read-only, error handling, server wrapper parity
# ---------------------------------------------------------------------------

def test_invalid_style_policy_returns_error_not_exception(tmp_path):
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml())
    si = _write_docx(tmp_path, "si.docx", _styles_xml())
    result = docs_intel.audit_cross_document_consistency(
        manuscript, si, style_policy={"body_text_font_size_pt": -1}
    )
    assert "error" in result


def test_missing_manuscript_file_returns_error(tmp_path):
    si = _write_docx(tmp_path, "si.docx", _styles_xml())
    result = docs_intel.audit_cross_document_consistency("/no/such/manuscript.docx", si)
    assert "error" in result


def test_missing_si_file_returns_error(tmp_path):
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml())
    result = docs_intel.audit_cross_document_consistency(manuscript, "/no/such/si.docx")
    assert "error" in result


def test_read_only_never_mutates_either_file(tmp_path):
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml(_BODY_TEXT_10PT_TNR))
    si = _write_docx(tmp_path, "si.docx", _styles_xml(_BODY_TEXT_12PT_TNR))
    m_before = open(manuscript, "rb").read()
    s_before = open(si, "rb").read()
    docs_intel.audit_cross_document_consistency(
        manuscript, si, style_policy=docs_intel.get_journal_style_preset("jcshm")
    )
    assert open(manuscript, "rb").read() == m_before
    assert open(si, "rb").read() == s_before


def test_server_wrapper_matches_docs_intel(tmp_path):
    manuscript = _write_docx(tmp_path, "manuscript.docx", _styles_xml(_BODY_TEXT_10PT_TNR))
    si = _write_docx(tmp_path, "si.docx", _styles_xml(_BODY_TEXT_12PT_TNR))
    policy = docs_intel.get_journal_style_preset("jcshm")
    assert server.audit_cross_document_consistency(
        manuscript, si, style_policy=policy
    ) == docs_intel.audit_cross_document_consistency(manuscript, si, style_policy=policy)


def test_jcshm_preset_body_text_font_values():
    policy = docs_intel.get_journal_style_preset("jcshm")
    assert policy["body_text_font_family"] == "Times New Roman"
    assert policy["body_text_font_size_pt"] == 10


# ---------------------------------------------------------------------------
# _style_definition_facts / _resolve_style_facts_by_candidates unit tests
# ---------------------------------------------------------------------------

def test_style_definition_facts_returns_none_when_styles_xml_absent(tmp_path):
    path = _write_docx(tmp_path, "no_styles.docx", None)
    with open(path, "rb") as fh:
        raw = fh.read()
    assert docs_intel._style_definition_facts(raw, "Normal") is None


def test_style_definition_facts_returns_none_when_style_not_found(tmp_path):
    path = _write_docx(tmp_path, "sample.docx", _styles_xml(_BODY_TEXT_10PT_TNR))
    with open(path, "rb") as fh:
        raw = fh.read()
    assert docs_intel._style_definition_facts(raw, "NoSuchStyle") is None
