"""Core doc_store parity tests for proposal e1d0552e."""
from __future__ import annotations

from lxml import etree as LET
import pytest

from meridian import doc_store


def _omml(body: str) -> str:
    return (
        '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        + body
        + "</m:oMath>"
    )


def test_core_validator_rejects_wrapper_and_missing_fraction_expression():
    with pytest.raises(ValueError, match="m:oMath root"):
        doc_store._validate_omml_structure(
            '<m:oMathPara xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" />'
        )
    with pytest.raises(ValueError, match="m:num"):
        doc_store._validate_omml_structure(
            _omml('<m:f><m:num /><m:den><m:e /></m:den></m:f>')
        )


def test_core_mathml_fraction_is_wrapped_in_m_e():
    math = LET.fromstring(
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        '<mfrac><mi>a</mi><mi>b</mi></mfrac>'
        "</math>"
    )
    root = LET.Element(doc_store._om("oMath"), nsmap=doc_store._OMML_NSMAP)
    doc_store._append_mathml(math, root)
    raw = LET.tostring(root, encoding="unicode")
    doc_store._validate_omml_structure(raw)
    assert root.find(f".//{doc_store._om('f')}/{doc_store._om('num')}/{doc_store._om('e')}") is not None


def test_display_insertion_mints_ids_and_center_alignment():
    root = LET.fromstring(
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
        'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        '<w:body><w:p w14:paraId="ANCHOR01"><w:r><w:t>anchor</w:t></w:r></w:p></w:body>'
        "</w:document>"
    )
    paragraph = root.find(f".//{doc_store._LET.QName(doc_store._DOCX_W_NS, 'p')}")
    omath = LET.fromstring(_omml('<m:r><m:t>x</m:t></m:r>'))
    para_id, text_id = doc_store._insert_omath_at_position(paragraph, omath, "after")
    assert para_id and text_id
    inserted = next(
        p for p in root.iter(doc_store._LET.QName(doc_store._DOCX_W_NS, "p"))
        if p.get(f"{{{doc_store._DOCX_W14_NS}}}paraId") == para_id
    )
    assert inserted.get(f"{{{doc_store._DOCX_W14_NS}}}paraId") == para_id
    assert inserted.get(f"{{{doc_store._DOCX_W14_NS}}}textId") == text_id
    assert inserted.find(f"./{doc_store._LET.QName(doc_store._DOCX_W_NS, 'pPr')}/{doc_store._LET.QName(doc_store._DOCX_W_NS, 'jc')}").get(f"{{{doc_store._DOCX_W_NS}}}val") == "center"


# ---------------------------------------------------------------------------
# Sprint 29fe401f review finding: core mtable (cases) -> Word-valid m:eqArr.
# A cell m:e nested directly inside the row m:e is schema-invalid, so Word
# rejects the equation. The core converter must emit the same shape as the
# docs_intel port: one flat m:e per row, cells separated by "&", and an
# m:eqArrPr carrying m:ctrlPr.
# ---------------------------------------------------------------------------

_CORE_CASES_LATEX = r"\begin{cases} x & x>0 \\ -x & x \le 0 \end{cases}"


def _core_cases_root():
    raw = doc_store.latex_to_omml(_CORE_CASES_LATEX)
    assert raw is not None, "cases LaTeX must convert through latex_to_omml"
    doc_store._validate_omml_structure(raw)
    return LET.fromstring(raw.encode("utf-8"))


def test_core_cases_environment_never_nests_m_e_inside_m_e():
    root = _core_cases_root()
    for element in root.iter(doc_store._om("e")):
        nested = [child for child in element if child.tag == doc_store._om("e")]
        assert not nested, "m:e directly nested inside m:e"


def test_core_cases_rows_are_flat_with_one_alignment_point_each():
    root = _core_cases_root()
    eqarr = root.find(f".//{doc_store._om('eqArr')}")
    assert eqarr is not None
    rows = eqarr.findall(doc_store._om("e"))
    assert len(rows) == 2, f"expected 2 case branches, got {len(rows)}"
    for row in rows:
        alignment = [
            child for child in row
            if child.tag == doc_store._om("r")
            and "".join(t.text or "" for t in child.iter(doc_store._om("t"))) == "&"
        ]
        assert len(alignment) == 1, "cells must be separated by one & alignment point"


def test_core_cases_eqarr_leads_with_eqarrpr_carrying_ctrl_pr():
    root = _core_cases_root()
    eqarr = root.find(f".//{doc_store._om('eqArr')}")
    assert eqarr is not None
    assert LET.QName(eqarr[0]).localname == "eqArrPr"
    ctrl = eqarr.find(f"{doc_store._om('eqArrPr')}/{doc_store._om('ctrlPr')}")
    assert ctrl is not None, "eqArr must carry an eqArrPr/ctrlPr"
    assert ctrl.find(f"{{{doc_store._DOCX_W_NS}}}rPr") is not None
