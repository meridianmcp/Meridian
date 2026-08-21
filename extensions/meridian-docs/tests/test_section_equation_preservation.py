"""Section replacement gates must preserve semantic OMML, not counts only."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile

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


def _write_docx(path, body_children: str) -> None:
    document = (
        f'<w:document xmlns:w="{W}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
        f'xmlns:m="{M}"><w:body>{body_children}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def test_post_write_gate_rejects_flattened_equation_with_same_count(tmp_path):
    path = tmp_path / "flattened.docx"
    _write_docx(path, f"<w:p>{_flattened_subscript()}</w:p>")
    expected = docs_intel._equation_semantic_manifest(
        [_paragraph(_structured_subscript())]
    )

    mismatch = docs_intel._verify_docx_write(
        str(path), expected_counts={}, expected_equation_manifest=expected
    )

    assert mismatch is not None
    assert mismatch["semantic_equation_mismatches"]["missing_fingerprints"]
    assert mismatch["semantic_equation_mismatches"]["invalid_entries"]


def test_copy_section_preserves_structured_unnumbered_equation(tmp_path):
    path = tmp_path / "copy.docx"
    heading = '<w:p w14:paraId="H1"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Source</w:t></w:r></w:p>'
    destination = '<w:p w14:paraId="H2"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Destination</w:t></w:r></w:p>'
    equation = f'<w:p w14:paraId="P1">{_structured_subscript()}</w:p>'
    _write_docx(path, heading + equation + destination)

    result = docs_intel.copy_section(
        str(path), "H1", "H2", destination_position="before"
    )

    assert "error" not in result, result
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    equations = list(root.iter(_q(M, "oMath")))
    assert len(equations) == 2
    assert all(
        any(child.tag == _q(M, "sSub") for child in equation.iter())
        for equation in equations
    )


def test_named_wave36_definitions_cover_inline_operators_and_cases():
    equations = [
        f'<m:oMath xmlns:m="{M}"><m:acc><m:accPr><m:chr m:val="^"/></m:accPr>'
        "<m:e><m:r><m:t>j(i)</m:t></m:r></m:e></m:acc></m:oMath>",
        _structured_subscript(),
        f'<m:oMath xmlns:m="{M}"><m:sSub><m:e><m:r><m:t>d</m:t></m:r></m:e>'
        "<m:sub><m:r><m:t>i,r</m:t></m:r></m:sub></m:sSub>"
        "<m:r><m:t>≤g_proj</m:t></m:r></m:oMath>",
        f'<m:oMath xmlns:m="{M}"><m:d><m:e><m:r><m:t>x</m:t></m:r></m:e></m:d>'
        "</m:oMath>",
        f'<m:oMath xmlns:m="{M}"><m:eqArr><m:e><m:e><m:r><m:t>a</m:t></m:r></m:e>'
        "</m:e><m:e><m:e><m:r><m:t>b</m:t></m:r></m:e></m:e></m:eqArr></m:oMath>",
    ]

    manifest = docs_intel._equation_semantic_manifest([_paragraph(*equations)])

    assert manifest["count"] == 5
    assert all(not entry["issues"] for entry in manifest["entries"])
    structures = [entry["structural_tags"] for entry in manifest["entries"]]
    assert {"acc": 1} in structures
    assert {"sSub": 1} in structures
    assert {"d": 1} in structures
    assert {"eqArr": 1} in structures


def test_copy_section_rejects_flattened_source_before_mutating(tmp_path):
    path = tmp_path / "reject-flat.docx"
    heading = '<w:p w14:paraId="H1"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Source</w:t></w:r></w:p>'
    destination = '<w:p w14:paraId="H2"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Destination</w:t></w:r></w:p>'
    _write_docx(path, heading + f"<w:p>{_flattened_subscript()}</w:p>" + destination)
    before = path.read_bytes()

    result = docs_intel.copy_section(
        str(path), "H1", "H2", destination_position="before"
    )

    assert "error" in result
    assert "flattened semantic OMML" in result["error"]
    assert path.read_bytes() == before
