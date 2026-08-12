"""Regression coverage for semantic equation preservation manifests."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from meridian_docs import docs_intel


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _paragraph(*equations: str) -> ET.Element:
    paragraph = ET.Element(_q(W, "p"))
    for equation in equations:
        paragraph.append(ET.fromstring(equation))
    return paragraph


def _structured_subscript() -> str:
    return (
        f'<m:oMath xmlns:m="{M}">'
        "<m:sSub><m:e><m:r><m:t>d</m:t></m:r></m:e>"
        "<m:sub><m:r><m:t>i</m:t></m:r></m:sub></m:sSub>"
        "</m:oMath>"
    )


def _flattened_subscript() -> str:
    return f'<m:oMath xmlns:m="{M}"><m:r><m:t>d_i</m:t></m:r></m:oMath>'


def test_manifest_distinguishes_structured_and_flattened_same_text():
    structured = docs_intel._equation_semantic_manifest(
        [_paragraph(_structured_subscript())]
    )
    flattened = docs_intel._equation_semantic_manifest(
        [_paragraph(_flattened_subscript())]
    )

    assert structured["count"] == flattened["count"] == 1
    assert structured["entries"][0]["fingerprint"] != flattened["entries"][0]["fingerprint"]
    assert structured["entries"][0]["structural_tags"] == {"sSub": 1}
    assert flattened["entries"][0]["issues"]


def test_manifest_accepts_inline_and_omathpara_wrapped_equations():
    wrapper = ET.fromstring(
        f'<m:oMathPara xmlns:m="{M}"><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath></m:oMathPara>'
    )
    paragraph = ET.Element(_q(W, "p"))
    paragraph.append(wrapper)
    paragraph.append(ET.fromstring(f'<m:oMath xmlns:m="{M}"><m:r><m:t>y</m:t></m:r></m:oMath>'))

    manifest = docs_intel._equation_semantic_manifest([paragraph])

    assert manifest["count"] == 2
    assert not any(entry["issues"] for entry in manifest["entries"])


def test_manifest_records_structural_cases_functions_norms_and_limits():
    equation = ET.fromstring(
        f'<m:oMath xmlns:m="{M}">'
        "<m:func><m:fName><m:r><m:t>argmin</m:t></m:r></m:fName>"
        "<m:e><m:d><m:e><m:r><m:t>x</m:t></m:r></m:e></m:d></m:e></m:func>"
        "<m:eqArr><m:e><m:e><m:r><m:t>case</m:t></m:r></m:e></m:e></m:eqArr>"
        "<m:limLow><m:e><m:r><m:t>f</m:t></m:r></m:e>"
        "<m:lim><m:r><m:t>n</m:t></m:r></m:lim></m:limLow>"
        "</m:oMath>"
    )

    entry = docs_intel._equation_semantic_manifest([equation])["entries"][0]

    assert entry["issues"] == []
    assert entry["structural_tags"] == {"d": 1, "eqArr": 1, "func": 1, "limLow": 1}


def test_compare_manifests_rejects_missing_or_replaced_semantic_equation():
    expected = docs_intel._equation_semantic_manifest(
        [_paragraph(_structured_subscript())]
    )
    actual = docs_intel._equation_semantic_manifest(
        [_paragraph(_flattened_subscript())]
    )

    mismatch = docs_intel._compare_equation_manifests(expected, actual)

    assert mismatch is not None
    assert mismatch["missing_fingerprints"]
    assert mismatch["unexpected_fingerprints"]
    assert mismatch["invalid_entries"]


def test_manifest_is_scoped_to_the_supplied_section_elements():
    source_section = _paragraph(_structured_subscript())
    unrelated_section = _paragraph(
        f'<m:oMath xmlns:m="{M}"><m:r><m:t>unrelated</m:t></m:r></m:oMath>'
    )

    scoped = docs_intel._equation_semantic_manifest([source_section])

    assert scoped["count"] == 1
    assert scoped["entries"][0]["flat_text"] == "di"
    assert docs_intel._equation_semantic_manifest([unrelated_section])["count"] == 1
