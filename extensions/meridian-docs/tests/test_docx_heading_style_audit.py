"""Tests for audit_heading_style (df716454), the per-level (H1/H2/H3)
heading-spacing counterpart to audit_table_style/audit_caption_style, plus
its read-only heading_terminal_punctuation_mismatch finding -- the missing
read-only counterpart to _apply_heading_terminal_punctuation, which only
ever fires when write_section authors NEW heading content.

Also covers:
  - build_document_review's "structure" category now also carries
    audit_heading_style findings (alongside audit_table_style's).
  - The jcshm preset's six heading_spacing_*_h{1,2,3}_twips values, per this
    session's real Tier-1 PDF-baseline measurement (H1: 2 body-lines
    before/1 after; H2 IDENTICAL to H3: 1 body-line before/1 after).

All tests use synthetic .docx bytes built inline -- no real files, no
network. Mirrors test_docx_table_style_audit.py's fixture conventions.
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


def _heading_para(
    level: int,
    text: str,
    para_id: str,
    before: int | None = None,
    after: int | None = None,
) -> str:
    spacing = ""
    if before is not None or after is not None:
        attrs = ""
        if before is not None:
            attrs += f' w:before="{before}"'
        if after is not None:
            attrs += f' w:after="{after}"'
        spacing = f"<w:spacing{attrs}/>"
    return (
        f'<w:p w14:paraId="{para_id}">'
        f'<w:pPr><w:pStyle w:val="Heading{level}"/>{spacing}</w:pPr>'
        f'<w:r><w:t>{text}</w:t></w:r>'
        f"</w:p>"
    )


def _body_para(text: str, para_id: str) -> str:
    return f'<w:p w14:paraId="{para_id}"><w:r><w:t>{text}</w:t></w:r></w:p>'


# ---------------------------------------------------------------------------
# df716454 bug fix -- styles.xml fixtures for cascaded (style-inherited)
# heading spacing, as opposed to a direct per-paragraph <w:spacing>
# override. A real manuscript's headings correctly rely on their named
# style rather than overriding it directly, and the pre-fix implementation
# only ever read the direct override, incorrectly treating the inherited
# value as "unset" and flagging a false mismatch.
# ---------------------------------------------------------------------------

def _style_def(
    style_id: str,
    based_on: str | None = None,
    before: int | None = None,
    after: int | None = None,
) -> str:
    based_on_xml = f'<w:basedOn w:val="{based_on}"/>' if based_on else ""
    attrs = ""
    if before is not None:
        attrs += f' w:before="{before}"'
    if after is not None:
        attrs += f' w:after="{after}"'
    spacing_xml = f"<w:spacing{attrs}/>" if attrs else ""
    ppr_xml = f"<w:pPr>{spacing_xml}</w:pPr>" if spacing_xml else ""
    return f'<w:style w:type="paragraph" w:styleId="{style_id}">{based_on_xml}{ppr_xml}</w:style>'


def _doc_defaults(before: int | None = None, after: int | None = None) -> str:
    attrs = ""
    if before is not None:
        attrs += f' w:before="{before}"'
    if after is not None:
        attrs += f' w:after="{after}"'
    spacing_xml = f"<w:spacing{attrs}/>" if attrs else ""
    return f"<w:docDefaults><w:pPrDefault><w:pPr>{spacing_xml}</w:pPr></w:pPrDefault></w:docDefaults>"


def _styles_xml(styles_body: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles {_NS_HEADER}>
{styles_body}
</w:styles>"""


def _zip_docx_with_styles(document_xml: str, styles_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/styles.xml", styles_xml)
    return buf.getvalue()


def _write_docx_with_styles(tmp_path, document_xml: str, styles_xml: str, name: str = "sample.docx") -> str:
    path = tmp_path / name
    path.write_bytes(_zip_docx_with_styles(document_xml, styles_xml))
    return str(path)


# ---------------------------------------------------------------------------
# Detection: per-level spacing, in isolation
# ---------------------------------------------------------------------------

def test_h1_spacing_before_mismatch_flagged_when_policy_set(tmp_path):
    body = _heading_para(1, "Introduction", "00000001", before=240, after=240)
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_spacing_before_h1_twips": 480}
    )
    assert result["heading_count"] == 1
    types = {f["type"] for f in result["findings"]}
    assert "heading_spacing_before_mismatch" in types
    f = next(f for f in result["findings"] if f["type"] == "heading_spacing_before_mismatch")
    assert f["level"] == 1
    assert f["expected_spacing_before_twips"] == 480
    assert f["actual_spacing_before_twips"] == 240


def test_h1_spacing_correct_not_flagged(tmp_path):
    body = _heading_para(1, "Introduction", "00000001", before=480, after=240)
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h1_twips": 480,
            "heading_spacing_after_h1_twips": 240,
        },
    )
    assert result["findings"] == []


def test_missing_spacing_element_treated_as_none_and_flagged(tmp_path):
    """No <w:spacing> at all -- Word's own default -- must be treated as
    "actual is None", not silently skipped, when a policy value is set."""
    body = _heading_para(1, "Introduction", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_spacing_before_h1_twips": 480}
    )
    f = next(f for f in result["findings"] if f["type"] == "heading_spacing_before_mismatch")
    assert f["actual_spacing_before_twips"] is None


def test_spacing_check_skipped_when_policy_unset(tmp_path):
    body = _heading_para(1, "Introduction", "00000001", before=999, after=999)
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(path)  # no style_policy at all
    types = {f["type"] for f in result["findings"]}
    assert "heading_spacing_before_mismatch" not in types
    assert "heading_spacing_after_mismatch" not in types


def test_spacing_after_mismatch_flagged_independently_of_before(tmp_path):
    body = _heading_para(1, "Introduction", "00000001", before=480, after=999)
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h1_twips": 480,
            "heading_spacing_after_h1_twips": 240,
        },
    )
    types = {f["type"] for f in result["findings"]}
    assert "heading_spacing_before_mismatch" not in types
    assert "heading_spacing_after_mismatch" in types


def test_h2_and_h3_use_independent_policy_keys(tmp_path):
    body = (
        _heading_para(2, "Methods", "00000001", before=240, after=240)
        + _heading_para(3, "Sub-methods", "00000002", before=999, after=999)
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h2_twips": 240,
            "heading_spacing_after_h2_twips": 240,
            "heading_spacing_before_h3_twips": 240,
            "heading_spacing_after_h3_twips": 240,
        },
    )
    h2_findings = [f for f in result["findings"] if f["level"] == 2]
    h3_findings = [f for f in result["findings"] if f["level"] == 3]
    assert h2_findings == []
    assert {f["type"] for f in h3_findings} == {
        "heading_spacing_before_mismatch", "heading_spacing_after_mismatch",
    }


def test_level_above_3_skips_spacing_check(tmp_path):
    body = _heading_para(4, "Deep subsection", "00000001", before=999, after=999)
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h1_twips": 480,
            "heading_spacing_after_h1_twips": 240,
        },
    )
    assert result["heading_count"] == 1
    types = {f["type"] for f in result["findings"]}
    assert "heading_spacing_before_mismatch" not in types
    assert "heading_spacing_after_mismatch" not in types


def test_non_heading_paragraph_ignored(tmp_path):
    body = _body_para("Just a body paragraph.", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_spacing_before_h1_twips": 480}
    )
    assert result["heading_count"] == 0
    assert result["finding_count"] == 0


# ---------------------------------------------------------------------------
# df716454 bug fix -- cascaded (style-inherited) spacing resolution.
# ---------------------------------------------------------------------------

def test_spacing_inherited_from_own_style_definition_not_flagged(tmp_path):
    """The exact real-manuscript false positive this fixes: a heading with
    NO direct <w:spacing> override correctly relies on its named style, and
    that style's OWN definition in styles.xml already matches the policy --
    this must NOT be flagged."""
    body = _heading_para(1, "Introduction", "00000001")  # no direct spacing
    styles = _styles_xml(_style_def("Heading1", before=480, after=240))
    path = _write_docx_with_styles(tmp_path, _doc(body), styles)
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h1_twips": 480,
            "heading_spacing_after_h1_twips": 240,
        },
    )
    assert result["findings"] == []


def test_spacing_resolves_up_basedon_chain(tmp_path):
    """Heading1 itself sets no spacing but is w:basedOn a parent style that
    does -- the cascade must walk the chain, not stop at the immediate
    style."""
    body = _heading_para(1, "Introduction", "00000001")
    styles = _styles_xml(
        _style_def("Heading1", based_on="HeadingBase")
        + _style_def("HeadingBase", before=480, after=240)
    )
    path = _write_docx_with_styles(tmp_path, _doc(body), styles)
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h1_twips": 480,
            "heading_spacing_after_h1_twips": 240,
        },
    )
    assert result["findings"] == []


def test_spacing_inherited_from_style_mismatch_still_flagged(tmp_path):
    """The cascade resolves a real (non-None) value from the style -- and
    that value is reported as actual_spacing_before_twips when it genuinely
    doesn't match the policy, not silently swallowed."""
    body = _heading_para(1, "Introduction", "00000001")
    styles = _styles_xml(_style_def("Heading1", before=240, after=240))
    path = _write_docx_with_styles(tmp_path, _doc(body), styles)
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_spacing_before_h1_twips": 480}
    )
    f = next(f for f in result["findings"] if f["type"] == "heading_spacing_before_mismatch")
    assert f["actual_spacing_before_twips"] == 240


def test_direct_override_wins_over_cascaded_style_value(tmp_path):
    """A direct per-paragraph override always takes precedence over
    whatever the style cascade would otherwise resolve."""
    body = _heading_para(1, "Introduction", "00000001", before=999, after=240)
    styles = _styles_xml(_style_def("Heading1", before=480, after=240))
    path = _write_docx_with_styles(tmp_path, _doc(body), styles)
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h1_twips": 480,
            "heading_spacing_after_h1_twips": 240,
        },
    )
    types = {f["type"] for f in result["findings"]}
    assert types == {"heading_spacing_before_mismatch"}
    f = next(f for f in result["findings"] if f["type"] == "heading_spacing_before_mismatch")
    assert f["actual_spacing_before_twips"] == 999


def test_before_and_after_edges_resolve_independently(tmp_path):
    """A direct override for w:before only must not block w:after from
    still falling back to the cascaded style value -- each edge resolves
    independently, mirroring how Word itself cascades them."""
    body = _heading_para(1, "Introduction", "00000001", before=480)  # after unset
    styles = _styles_xml(_style_def("Heading1", after=240))
    path = _write_docx_with_styles(tmp_path, _doc(body), styles)
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h1_twips": 480,
            "heading_spacing_after_h1_twips": 240,
        },
    )
    assert result["findings"] == []


def test_docdefaults_fallback_when_neither_paragraph_nor_style_sets_spacing(tmp_path):
    """Word's own final fallback: neither the paragraph nor its style (nor
    any ancestor) sets spacing, but <w:docDefaults> does -- the cascade
    must fall all the way back to that."""
    body = _heading_para(1, "Introduction", "00000001")
    styles = _styles_xml(
        _doc_defaults(before=480, after=240) + _style_def("Heading1")
    )
    path = _write_docx_with_styles(tmp_path, _doc(body), styles)
    result = docs_intel.audit_heading_style(
        path,
        style_policy={
            "heading_spacing_before_h1_twips": 480,
            "heading_spacing_after_h1_twips": 240,
        },
    )
    assert result["findings"] == []


def test_no_spacing_anywhere_is_still_none(tmp_path):
    """Genuinely unset everywhere (no direct override, no style spacing, no
    docDefaults) must still resolve to None, not a guessed value."""
    body = _heading_para(1, "Introduction", "00000001")
    styles = _styles_xml(_style_def("Heading1"))
    path = _write_docx_with_styles(tmp_path, _doc(body), styles)
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_spacing_before_h1_twips": 480}
    )
    f = next(f for f in result["findings"] if f["type"] == "heading_spacing_before_mismatch")
    assert f["actual_spacing_before_twips"] is None


# ---------------------------------------------------------------------------
# Detection: read-only heading_terminal_punctuation_mismatch
# ---------------------------------------------------------------------------

def test_heading_terminal_punctuation_mismatch_flagged_when_policy_set(tmp_path):
    body = _heading_para(1, "Results:", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_terminal_punctuation": ""}
    )
    types = {f["type"] for f in result["findings"]}
    assert "heading_terminal_punctuation_mismatch" in types
    f = next(f for f in result["findings"] if f["type"] == "heading_terminal_punctuation_mismatch")
    assert f["heading_text"] == "Results:"
    assert f["expected_heading_text"] == "Results"


def test_heading_terminal_punctuation_compliant_not_flagged(tmp_path):
    body = _heading_para(1, "Results", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_terminal_punctuation": ""}
    )
    types = {f["type"] for f in result["findings"]}
    assert "heading_terminal_punctuation_mismatch" not in types


def test_heading_terminal_punctuation_requiring_a_char_flags_missing_one(tmp_path):
    body = _heading_para(1, "Results", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_terminal_punctuation": ":"}
    )
    f = next(f for f in result["findings"] if f["type"] == "heading_terminal_punctuation_mismatch")
    assert f["expected_heading_text"] == "Results:"


def test_terminal_punctuation_check_skipped_when_policy_unset(tmp_path):
    body = _heading_para(1, "Results:", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(path)  # no style_policy at all
    types = {f["type"] for f in result["findings"]}
    assert "heading_terminal_punctuation_mismatch" not in types


def test_terminal_punctuation_checked_even_above_level_3(tmp_path):
    """Terminal-punctuation checking is level-independent -- unlike spacing,
    it applies to every heading paragraph regardless of level."""
    body = _heading_para(5, "Deep subsection:", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_terminal_punctuation": ""}
    )
    types = {f["type"] for f in result["findings"]}
    assert "heading_terminal_punctuation_mismatch" in types


# ---------------------------------------------------------------------------
# Read-only, error handling, server wrapper parity
# ---------------------------------------------------------------------------

def test_invalid_style_policy_returns_error_not_exception(tmp_path):
    body = _heading_para(1, "Introduction", "00000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_heading_style(
        path, style_policy={"heading_spacing_before_h1_twips": -1}
    )
    assert "error" in result


def test_missing_file_returns_error():
    result = docs_intel.audit_heading_style("/no/such/file.docx")
    assert "error" in result


def test_read_only_never_mutates(tmp_path):
    body = _heading_para(1, "Introduction", "00000001", before=240, after=240)
    path = _write_docx(tmp_path, _doc(body))
    before_bytes = open(path, "rb").read()
    docs_intel.audit_heading_style(
        path, style_policy={"heading_spacing_before_h1_twips": 480}
    )
    after_bytes = open(path, "rb").read()
    assert before_bytes == after_bytes


def test_server_wrapper_matches_docs_intel(tmp_path):
    body = _heading_para(1, "Introduction", "00000001", before=240, after=240)
    path = _write_docx(tmp_path, _doc(body))
    policy = {"heading_spacing_before_h1_twips": 480}
    assert server.audit_heading_style(path, style_policy=policy) == docs_intel.audit_heading_style(
        path, style_policy=policy
    )


def test_jcshm_preset_heading_spacing_values():
    policy = docs_intel.get_journal_style_preset("jcshm")
    assert policy["heading_spacing_before_h1_twips"] == 480
    assert policy["heading_spacing_after_h1_twips"] == 240
    assert policy["heading_spacing_before_h2_twips"] == 240
    assert policy["heading_spacing_after_h2_twips"] == 240
    assert policy["heading_spacing_before_h3_twips"] == 240
    assert policy["heading_spacing_after_h3_twips"] == 240
    # H3 must equal H2, not be half of it -- the specific real measurement
    # finding this preset encodes.
    assert policy["heading_spacing_before_h2_twips"] == policy["heading_spacing_before_h3_twips"]
    assert policy["heading_spacing_after_h2_twips"] == policy["heading_spacing_after_h3_twips"]


# ---------------------------------------------------------------------------
# build_document_review wiring
# ---------------------------------------------------------------------------

def test_build_document_review_structure_category_includes_heading_findings(tmp_path):
    policy = docs_intel.get_journal_style_preset("jcshm")
    body = _heading_para(1, "Introduction:", "00000001", before=240, after=240)
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path, style_policy=policy)
    assert review["status"] == "ok"
    structure_findings = [f for f in review["findings"] if f["category"] == "structure"]
    types = {f["type"] for f in structure_findings}
    assert "heading_spacing_before_mismatch" in types
    assert "heading_terminal_punctuation_mismatch" in types
    for f in structure_findings:
        assert f["locator"]["status"] == "resolved"
        assert f["locator"]["target_para_id"] == "00000001"
    assert review["findings_by_category"]["structure"] == len(structure_findings)


def test_build_document_review_compliant_heading_produces_no_heading_findings(tmp_path):
    policy = docs_intel.get_journal_style_preset("jcshm")
    body = _heading_para(1, "Introduction", "00000001", before=480, after=240)
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path, style_policy=policy)
    structure_findings = [f for f in review["findings"] if f["category"] == "structure"]
    heading_types = {
        f["type"] for f in structure_findings
        if f["type"].startswith("heading_")
    }
    assert heading_types == set()
