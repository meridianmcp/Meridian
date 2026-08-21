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
