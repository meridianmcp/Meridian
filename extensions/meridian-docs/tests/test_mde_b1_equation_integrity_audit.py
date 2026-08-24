"""Tests for audit_equation_integrity (3d0769ab, MDE-B1 P0) -- the raw-OOXML
equation INTEGRITY auditor, distinct from audit_equation_style's alignment/
punctuation/gap STYLE checks.

Golden fixtures per the Track B build proposal's acceptance criteria
(docs/meridian-build-proposal-track-b-2026-08-24.md, "Equation fixtures"):
  - intact inline OMML with no duplicate plaintext (clean);
  - duplicate w:t plus OMML (plaintext_math_duplicate);
  - merged OMML objects (merged_omml_suspected);
  - missing OMML plaintext equations, standalone AND table-numbered
    (missing_omml);
  - legitimate prose variable mentions (must NOT false-positive);
  - valid explicit numbering scopes (clean);
  - genuine duplicate/gap numbering (equation_number_duplicate /
    equation_number_gap);
  - equivalent OMML with different XML prefixes/whitespace (identical
    token_sequence/structure_hash);
  - equation_number_scope_ambiguous (mixed numbering shapes);
  - reference_structure_mismatch (same explicit number, different
    structure);
  - read-only invariant (never mutates the source);
  - deterministic/serializable output.

All tests use synthetic .docx bytes/files built inline -- no real files, no
network, no dependency on any dissertation or manuscript.
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

from meridian_docs import docs_intel  # noqa: E402

_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"

_NS_HEADER = f'xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:m="{_M}"'


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "sample.docx") -> str:
    path = tmp_path / name
    path.write_bytes(_zip_docx(xml))
    return str(path)


def _doc(body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS_HEADER}>
  <w:body>
{body_xml}
  </w:body>
</w:document>"""


def _omath(*, ns: bool = False, inner: str) -> str:
    prefix = f' xmlns:m="{_M}"' if ns else ""
    return f"<m:oMath{prefix}>{inner}</m:oMath>"


def _run(*texts: str) -> str:
    return "".join(f"<m:r><m:t>{t}</m:t></m:r>" for t in texts)


def _numbered_row(para_id: str, omath_xml: str, number_text: str) -> str:
    return f'''    <w:tr>
      <w:tc><w:p w14:paraId="{para_id}">{omath_xml}</w:p></w:tc>
      <w:tc><w:p><w:r><w:t>{number_text}</w:t></w:r></w:p></w:tc>
    </w:tr>'''


def _findings_of_type(result, finding_type):
    return [f for f in result["findings"] if f["type"] == finding_type]


# ---------------------------------------------------------------------------
# Basic contract: read-only, error paths, shape of the return value.
# ---------------------------------------------------------------------------

def test_missing_file_returns_error():
    result = docs_intel.audit_equation_integrity("/no/such/file.docx")
    assert "error" in result


def test_not_a_zip_returns_error(tmp_path):
    path = tmp_path / "bad.docx"
    path.write_bytes(b"not a zip file at all")
    result = docs_intel.audit_equation_integrity(str(path))
    assert "error" in result


def test_accepts_raw_bytes_as_well_as_a_path():
    raw = _zip_docx(_doc(f'<w:p w14:paraId="AAA00001">{_omath(inner=_run("x"))}</w:p>'))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["equation_count"] == 1
    assert result["document_path"] is None


def test_never_mutates_the_source_document(tmp_path):
    xml = _doc(f'<w:p w14:paraId="AAA00002">{_omath(inner=_run("x=y"))}</w:p>')
    path = _write_docx(tmp_path, xml)
    with open(path, "rb") as fh:
        before = fh.read()
    docs_intel.audit_equation_integrity(path)
    with open(path, "rb") as fh:
        after = fh.read()
    assert before == after


def test_result_is_json_serializable():
    import json
    raw = _zip_docx(_doc(f'<w:p w14:paraId="AAA00003">{_omath(inner=_run("x"))}</w:p>'))
    result = docs_intel.audit_equation_integrity(raw)
    json.dumps(result)  # must not raise


def test_document_with_no_body_element_returns_clean_empty_result():
    raw = _zip_docx(f'<?xml version="1.0"?><w:document {_NS_HEADER}></w:document>')
    result = docs_intel.audit_equation_integrity(raw)
    assert result["equation_count"] == 0
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Golden fixture: intact inline OMML, no duplicate plaintext -- clean.
# ---------------------------------------------------------------------------

def test_intact_equation_is_clean():
    raw = _zip_docx(_doc(f'<w:p w14:paraId="AAA00004">{_omath(inner=_run("x", "+", "y", "=", "z"))}</w:p>'))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["equation_count"] == 1
    assert result["findings"] == []
    record = result["records"][0]
    assert record["pattern"] == "standalone"
    assert record["anchor"] == "AAA00004"
    assert record["omml_count"] == 1
    assert record["plain_text_overlap"]["has_duplicate_plaintext"] is False


# ---------------------------------------------------------------------------
# Golden fixture: duplicate w:t plus OMML -- plaintext_math_duplicate.
# NEVER call an equation healthy solely because <m:oMath> exists.
# ---------------------------------------------------------------------------

def test_duplicate_plaintext_alongside_omml_is_flagged():
    body = (
        '<w:p w14:paraId="AAA00005">'
        '<w:r><w:t>F=ma</w:t></w:r>'
        f'{_omath(inner=_run("F", "=", "ma"))}'
        "</w:p>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    findings = _findings_of_type(result, "plaintext_math_duplicate")
    assert len(findings) == 1
    assert findings[0]["anchor"] == "AAA00005"
    assert findings[0]["matched_text"] == "F=ma"
    assert result["records"][0]["plain_text_overlap"]["has_duplicate_plaintext"] is True


def test_duplicate_plaintext_split_across_multiple_runs_is_still_detected():
    body = (
        '<w:p w14:paraId="AAA00006">'
        '<w:r><w:t xml:space="preserve">F</w:t></w:r>'
        '<w:r><w:t xml:space="preserve">=</w:t></w:r>'
        '<w:r><w:t>ma</w:t></w:r>'
        f'{_omath(inner=_run("F", "=", "ma"))}'
        "</w:p>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert len(_findings_of_type(result, "plaintext_math_duplicate")) == 1


# ---------------------------------------------------------------------------
# Golden fixture: legitimate prose variable mentions -- must NOT false-positive.
# ---------------------------------------------------------------------------

def test_legitimate_prose_variable_mention_is_not_flagged():
    body = (
        '<w:p w14:paraId="AAA00007">'
        '<w:r><w:t>The variable x approaches infinity while y remains bounded.</w:t></w:r>'
        "</w:p>"
        f'<w:p w14:paraId="AAA00008">{_omath(inner=_run("x", "+", "y", "=", "0"))}</w:p>'
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["findings"] == []
    assert result["equation_count"] == 1


def test_short_variable_mention_alone_never_trips_duplicate_check():
    """A one-letter overlap ('x' appears in both the prose and the equation)
    must never be treated as a duplicate -- the operator/length gate exists
    exactly for this."""
    body = (
        '<w:p w14:paraId="AAA00009">'
        '<w:r><w:t>Here x is just mentioned.</w:t></w:r>'
        f'{_omath(inner=_run("x"))}'
        "</w:p>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Golden fixture: merged OMML objects -- merged_omml_suspected.
# ---------------------------------------------------------------------------

def test_two_equations_spliced_into_one_omath_is_flagged():
    inner = _run("a", "=", "b") + '<m:r><m:t xml:space="preserve"> </m:t></m:r>' + _run("c", "=", "d")
    raw = _zip_docx(_doc(f'<w:p w14:paraId="AAA00010">{_omath(inner=inner)}</w:p>'))
    result = docs_intel.audit_equation_integrity(raw)
    findings = _findings_of_type(result, "merged_omml_suspected")
    assert len(findings) == 1
    assert findings[0]["segment_count"] == 2
    assert findings[0]["equation_like_segments"] == ["a=b", "c=d"]


def test_ordinary_multi_run_equation_is_never_flagged_as_merged():
    """x + y = z as five separate runs, no whitespace-only seam -- must
    never trip the merge heuristic."""
    raw = _zip_docx(_doc(f'<w:p w14:paraId="AAA00011">{_omath(inner=_run("x", "+", "y", "=", "z"))}</w:p>'))
    result = docs_intel.audit_equation_integrity(raw)
    assert _findings_of_type(result, "merged_omml_suspected") == []


def test_single_equals_sign_with_internal_space_run_is_not_flagged():
    """Only ONE segment contains '=' -- must not be enough alone to flag a
    merge (the heuristic requires 2+ independent equality segments)."""
    inner = _run("x") + '<m:r><m:t xml:space="preserve"> </m:t></m:r>' + _run("=", "y")
    raw = _zip_docx(_doc(f'<w:p w14:paraId="AAA00012">{_omath(inner=inner)}</w:p>'))
    result = docs_intel.audit_equation_integrity(raw)
    assert _findings_of_type(result, "merged_omml_suspected") == []


# ---------------------------------------------------------------------------
# Golden fixture: missing OMML plaintext equations (standalone + table row).
# ---------------------------------------------------------------------------

def test_standalone_plaintext_equation_with_no_omml_is_flagged():
    raw = _zip_docx(_doc('<w:p w14:paraId="AAA00013"><w:r><w:t>F = ma</w:t></w:r></w:p>'))
    result = docs_intel.audit_equation_integrity(raw)
    findings = _findings_of_type(result, "missing_omml")
    assert len(findings) == 1
    assert findings[0]["anchor"] == "AAA00013"
    assert findings[0]["pattern"] == "standalone"
    assert findings[0]["plain_text"] == "F = ma"
    assert result["equation_count"] == 0


def test_table_numbered_row_with_no_omml_in_equation_cell_is_flagged():
    # The equation cell holds plaintext instead of OMML -- realistic "the
    # converter dropped the OMML" defect shape.
    body = (
        '<w:tbl><w:tr>'
        '<w:tc><w:p w14:paraId="AAA00014"><w:r><w:t>F=ma</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>(4)</w:t></w:r></w:p></w:tc>'
        '</w:tr></w:tbl>'
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    findings = _findings_of_type(result, "missing_omml")
    assert len(findings) == 1
    assert findings[0]["anchor"] == "AAA00014"
    assert findings[0]["pattern"] == "table-numbered"
    assert findings[0]["number"] == "(4)"
    assert result["equation_count"] == 0


def test_ordinary_prose_paragraph_is_never_flagged_as_missing_omml():
    raw = _zip_docx(_doc(
        '<w:p w14:paraId="AAA00015"><w:r><w:t>'
        "This paragraph discusses the model but contains no equation at all."
        "</w:t></w:r></w:p>"
    ))
    result = docs_intel.audit_equation_integrity(raw)
    assert _findings_of_type(result, "missing_omml") == []


# ---------------------------------------------------------------------------
# Golden fixture: valid explicit numbering -- clean.
# ---------------------------------------------------------------------------

def test_valid_sequential_numbering_is_clean():
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00016", _omath(inner=_run("a")), "(1)") + "\n"
        + _numbered_row("AAA00017", _omath(inner=_run("b")), "(2)") + "\n"
        + _numbered_row("AAA00018", _omath(inner=_run("c")), "(3)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["findings"] == []
    assert result["equation_count"] == 3
    scopes = {r["number"]: r["numbering_scope"] for r in result["records"]}
    assert scopes["(1)"] == {"shape": "pure_int", "scope_key": "document"}


# ---------------------------------------------------------------------------
# Golden fixture: genuine duplicate/gap numbering.
# ---------------------------------------------------------------------------

def test_duplicate_number_is_flagged():
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00019", _omath(inner=_run("a")), "(1)") + "\n"
        + _numbered_row("AAA00020", _omath(inner=_run("a")), "(1)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    findings = _findings_of_type(result, "equation_number_duplicate")
    assert len(findings) == 1
    assert set(findings[0]["anchors"]) == {"AAA00019", "AAA00020"}


def test_number_gap_is_flagged():
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00021", _omath(inner=_run("a")), "(1)") + "\n"
        + _numbered_row("AAA00022", _omath(inner=_run("b")), "(3)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    findings = _findings_of_type(result, "equation_number_gap")
    assert len(findings) == 1
    assert findings[0]["missing_number"] == 2


def test_alpha_suffixed_numbers_do_not_produce_a_spurious_gap():
    """(1), (2), (2a) -- the 'a' suffix is a legitimate sub-numbering
    convention, not a gap."""
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00023", _omath(inner=_run("a")), "(1)") + "\n"
        + _numbered_row("AAA00024", _omath(inner=_run("b")), "(2)") + "\n"
        + _numbered_row("AAA00025", _omath(inner=_run("c")), "(2a)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert _findings_of_type(result, "equation_number_gap") == []


# ---------------------------------------------------------------------------
# Golden fixture: reference_structure_mismatch -- same number, different
# structure (a stronger signal than plain duplicate numbering).
# ---------------------------------------------------------------------------

def test_duplicate_number_with_different_structure_flags_reference_mismatch():
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00026", _omath(inner=_run("a", "=", "b")), "(1)") + "\n"
        + _numbered_row("AAA00027", _omath(inner=_run("c", "=", "d", "+", "e")), "(1)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert len(_findings_of_type(result, "equation_number_duplicate")) == 1
    mismatch = _findings_of_type(result, "reference_structure_mismatch")
    assert len(mismatch) == 1
    assert len(mismatch[0]["structure_hashes"]) == 2


def test_duplicate_number_with_identical_structure_does_not_flag_mismatch():
    """Same number, same structure -- a legitimate restated equation, not a
    reference/structure defect."""
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00028", _omath(inner=_run("a", "=", "b")), "(1)") + "\n"
        + _numbered_row("AAA00029", _omath(inner=_run("a", "=", "b")), "(1)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert len(_findings_of_type(result, "equation_number_duplicate")) == 1
    assert _findings_of_type(result, "reference_structure_mismatch") == []


# ---------------------------------------------------------------------------
# Golden fixture: equation_number_scope_ambiguous -- mixed numbering shapes.
# ---------------------------------------------------------------------------

def test_mixed_flat_and_sectioned_numbering_is_flagged_ambiguous():
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00030", _omath(inner=_run("a")), "(1)") + "\n"
        + _numbered_row("AAA00031", _omath(inner=_run("b")), "(2.3)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    findings = _findings_of_type(result, "equation_number_scope_ambiguous")
    assert len(findings) == 1
    assert findings[0]["shapes"] == ["dotted", "pure_int"]


def test_uniform_sectioned_numbering_alone_is_not_ambiguous():
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00032", _omath(inner=_run("a")), "(2.1)") + "\n"
        + _numbered_row("AAA00033", _omath(inner=_run("b")), "(2.2)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert _findings_of_type(result, "equation_number_scope_ambiguous") == []


def test_lettered_appendix_numbering_shape():
    raw = _zip_docx(_doc(
        "<w:tbl>\n" + _numbered_row("AAA00034", _omath(inner=_run("a")), "(A.1)") + "\n</w:tbl>"
    ))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["records"][0]["numbering_scope"] == {"shape": "lettered", "scope_key": "A"}


def test_non_numeric_label_has_no_scope_and_no_gap_participation():
    body = (
        "<w:tbl>\n"
        + _numbered_row("AAA00035", _omath(inner=_run("a")), "(1)") + "\n"
        + _numbered_row("AAA00036", _omath(inner=_run("b")), "(eq3)") + "\n"
        "</w:tbl>"
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    # A single pure_int number alone has no gap (nothing missing before it).
    assert _findings_of_type(result, "equation_number_gap") == []
    eq3_record = next(r for r in result["records"] if r["number"] == "(eq3)")
    assert eq3_record["numbering_scope"] == {"shape": "other", "scope_key": None}


# ---------------------------------------------------------------------------
# Golden fixture: equivalent OMML with different XML prefixes/whitespace --
# structure_hash / token_sequence must be identical.
# ---------------------------------------------------------------------------

def test_prefix_and_whitespace_variation_does_not_change_structure_hash():
    import xml.etree.ElementTree as ET

    omath_default_prefix = f'<m:oMath xmlns:m="{_M}"><m:r><m:t>x</m:t></m:r></m:oMath>'
    omath_other_prefix = f'<ns7:oMath xmlns:ns7="{_M}"><ns7:r><ns7:t>x</ns7:t></ns7:r></ns7:oMath>'
    omath_pretty_printed = (
        f'<m:oMath xmlns:m="{_M}">\n  <m:r>\n    <m:t>x</m:t>\n  </m:r>\n</m:oMath>'
    )

    tokens = [
        docs_intel._omml_token_sequence(ET.fromstring(xml_str))
        for xml_str in (omath_default_prefix, omath_other_prefix, omath_pretty_printed)
    ]
    assert tokens[0] == tokens[1] == tokens[2]
    hashes = {docs_intel._omml_structure_hash(t) for t in tokens}
    assert len(hashes) == 1


def test_structure_hash_changes_for_a_real_structural_difference():
    import xml.etree.ElementTree as ET

    plain = ET.fromstring(f'<m:oMath xmlns:m="{_M}"><m:r><m:t>x</m:t></m:r></m:oMath>')
    subscripted = ET.fromstring(
        f'<m:oMath xmlns:m="{_M}"><m:sSub><m:e><m:r><m:t>x</m:t></m:r></m:e>'
        '<m:sub><m:r><m:t>1</m:t></m:r></m:sub></m:sSub></m:oMath>'
    )
    hash_plain = docs_intel._omml_structure_hash(docs_intel._omml_token_sequence(plain))
    hash_sub = docs_intel._omml_structure_hash(docs_intel._omml_token_sequence(subscripted))
    assert hash_plain != hash_sub


# ---------------------------------------------------------------------------
# section_path -- heading-stack tracking.
# ---------------------------------------------------------------------------

def test_section_path_tracks_the_nearest_headings():
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Methods</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Statistical Model</w:t></w:r></w:p>'
        f'<w:p w14:paraId="AAA00037">{_omath(inner=_run("x"))}</w:p>'
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["records"][0]["section_path"] == ["Methods", "Statistical Model"]


def test_section_path_pops_back_to_a_sibling_heading():
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>A</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>A.1</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>B</w:t></w:r></w:p>'
        f'<w:p w14:paraId="AAA00038">{_omath(inner=_run("x"))}</w:p>'
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["records"][0]["section_path"] == ["B"]


# ---------------------------------------------------------------------------
# Positional fallback anchor when no w14:paraId is present.
# ---------------------------------------------------------------------------

def test_positional_fallback_anchor_when_no_para_id():
    raw = _zip_docx(_doc(f'<w:p>{_omath(inner=_run("x"))}</w:p>'))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["records"][0]["anchor"] == "p0"


# ---------------------------------------------------------------------------
# findings_by_type / finding_count consistency.
# ---------------------------------------------------------------------------

def test_findings_by_type_matches_findings_list():
    body = (
        '<w:p w14:paraId="AAA00039"><w:r><w:t>F=ma</w:t></w:r>'
        f'{_omath(inner=_run("F", "=", "ma"))}</w:p>'
        '<w:p w14:paraId="AAA00040"><w:r><w:t>G = mb</w:t></w:r></w:p>'
    )
    raw = _zip_docx(_doc(body))
    result = docs_intel.audit_equation_integrity(raw)
    assert result["finding_count"] == len(result["findings"])
    total = sum(result["findings_by_type"].values())
    assert total == result["finding_count"]
    assert result["findings_by_type"]["plaintext_math_duplicate"] == 1
    assert result["findings_by_type"]["missing_omml"] == 1
