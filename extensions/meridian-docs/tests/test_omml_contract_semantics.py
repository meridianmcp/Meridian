"""Renderer-independent semantic OMML contract tests for proposal e1d0552e."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel


_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _omml(body: str) -> str:
    return f'<m:oMath xmlns:m="{_M}">{body}</m:oMath>'


def test_validator_requires_omath_root_and_rejects_omathpara():
    with pytest.raises(ValueError, match="m:oMath root"):
        docs_intel._validate_omml_structure(
            f'<m:oMathPara xmlns:m="{_M}"><m:oMath /></m:oMathPara>'
        )
    with pytest.raises(ValueError, match="m:oMath root"):
        docs_intel._validate_omml_structure(f'<m:r xmlns:m="{_M}" />')


def test_validator_rejects_malformed_fraction_and_flattened_fallback():
    with pytest.raises(ValueError, match="m:num"):
        docs_intel._validate_omml_structure(
            _omml('<m:f><m:num /><m:den><m:e /></m:den></m:f>')
        )
    with pytest.raises(ValueError, match="flattened fallback"):
        docs_intel._validate_omml_structure(
            _omml('<m:r><m:t>fraction a over b</m:t></m:r>')
        )


def test_validator_accepts_structural_fraction_subscript_and_array():
    raw = _omml(
        "<m:f><m:num><m:e><m:r><m:t>a</m:t></m:r></m:e></m:num>"
        "<m:den><m:e><m:r><m:t>b</m:t></m:r></m:e></m:den></m:f>"
        "<m:sSub><m:e><m:r><m:t>x</m:t></m:r></m:e>"
        "<m:sub><m:r><m:t>i</m:t></m:r></m:sub></m:sSub>"
        "<m:eqArr><m:e><m:r><m:t>case</m:t></m:r></m:e></m:eqArr>"
    )
    root = docs_intel._validate_omml_structure(raw)
    assert root.tag == _q(_M, "oMath")


def test_mathml_fraction_and_table_conversion_preserve_semantic_wrappers():
    math = ET.fromstring(
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        '<mfrac><mi>a</mi><mi>b</mi></mfrac>'
        '<mtable><mtr><mtd><mi>x</mi></mtd></mtr></mtable>'
        "</math>"
    )
    root = ET.Element(_q(_M, "oMath"))
    docs_intel._stdlib_append_mathml(math, root)
    raw = ET.tostring(root, encoding="unicode")
    docs_intel._validate_omml_structure(raw)
    assert root.find(f".//{_q(_M, 'f')}/{_q(_M, 'num')}/{_q(_M, 'e')}") is not None
    assert root.find(_q(_M, "eqArr")) is not None


def test_display_builder_assigns_fresh_identity_and_style():
    first = docs_intel._build_omath_paragraph(_omml('<m:r><m:t>x</m:t></m:r>'), alignment="center")
    second = docs_intel._build_omath_paragraph(_omml('<m:r><m:t>y</m:t></m:r>'), alignment="center")
    para_attr = _q(_W14, "paraId")
    text_attr = _q(_W14, "textId")
    assert first.get(para_attr) and second.get(para_attr) != first.get(para_attr)
    assert first.get(text_attr) and second.get(text_attr) != first.get(text_attr)
    assert first.find(f"./{_q(_W, 'pPr')}/{_q(_W, 'jc')}").get(_q(_W, "val")) == "center"
