"""Tests for audit_reference_consistency (docs-intel-jcshm-linter-gap-
cleanup-20260918) -- reference-list <-> in-text-citation consistency for
JCSHM's numbered_bracket citation style. This session's own manual
pre-submission QA pass found a real problem here that no automated check
existed for before this; sync_bibliography (CSL_CITATION/Zotero-only) and
find_references_to/_LITERAL_REF_ALIASES (Figure/Table/Equation cross-refs
only) both cover a different mechanism/domain, confirmed by inspection.

Covers:
  - citation_missing_reference_entry / reference_entry_never_cited /
    reference_list_number_gap / reference_list_duplicate_number, each in
    isolation.
  - Bracket-range ("[3-7]") and comma-list ("[3, 5]") in-text citation
    expansion.
  - The reference list's own bibliography-block paragraphs are EXCLUDED
    from in-text citation scanning (an entry's own text incidentally
    containing a bracketed number must not be miscounted as a citation).
  - Positional fallback numbering when an entry carries no literal "[N]"
    marker of its own.
  - No References/Bibliography heading found -> no findings at all (can't
    check consistency against a list that doesn't exist).
  - Unconditional -- NOT style-policy-gated (structural correctness).
  - build_document_review's new "citation" category.
  - Read-only invariant, error handling, server wrapper parity.

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
    return f'<w:p w14:paraId="{para_id}"><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _references_heading(para_id: str = "R0000000") -> str:
    return _heading_para(1, "References", para_id)


def _bib_entry(number: int | None, text: str, para_id: str) -> str:
    """A bibliography entry. ``number=None`` omits the literal "[N]" marker
    (exercises the positional-fallback path)."""
    label = f"[{number}] " if number is not None else ""
    return f'<w:p w14:paraId="{para_id}"><w:pPr><w:pStyle w:val="Bibliography"/></w:pPr><w:r><w:t xml:space="preserve">{label}{text}</w:t></w:r></w:p>'


# ---------------------------------------------------------------------------
# Consistent reference lists -- no findings
# ---------------------------------------------------------------------------

def test_fully_consistent_reference_list_no_findings(tmp_path):
    body = (
        _body_para("As shown in [1] and later confirmed in [2].", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
        + _bib_entry(2, "Doe, A. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    assert result["finding_count"] == 0, result["findings"]
    assert result["reference_count"] == 2
    assert result["citation_count"] == 2


# ---------------------------------------------------------------------------
# citation_missing_reference_entry / reference_entry_never_cited
# ---------------------------------------------------------------------------

def test_citation_with_no_matching_entry_is_flagged(tmp_path):
    body = (
        _body_para("As discussed in [5].", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    types = {f["type"] for f in result["findings"]}
    assert "citation_missing_reference_entry" in types
    f = next(f for f in result["findings"] if f["type"] == "citation_missing_reference_entry")
    assert f["citation_number"] == 5
    assert f["para_id"] == "B0000001"


def test_entry_never_cited_is_flagged(tmp_path):
    body = (
        _body_para("As discussed in [1].", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
        + _bib_entry(2, "Doe, A. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    types = {f["type"] for f in result["findings"]}
    assert "reference_entry_never_cited" in types
    f = next(f for f in result["findings"] if f["type"] == "reference_entry_never_cited")
    assert f["reference_number"] == 2
    assert f["para_id"] == "E0000002"


# ---------------------------------------------------------------------------
# reference_list_number_gap / reference_list_duplicate_number
# ---------------------------------------------------------------------------

def test_reference_list_gap_is_flagged(tmp_path):
    body = (
        _body_para("[1] [3]", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
        + _bib_entry(3, "Lee, K. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    types = {f["type"] for f in result["findings"]}
    assert "reference_list_number_gap" in types
    f = next(f for f in result["findings"] if f["type"] == "reference_list_number_gap")
    assert f["missing_number"] == 2


def test_reference_list_duplicate_is_flagged(tmp_path):
    body = (
        _body_para("[1]", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
        + _bib_entry(1, "Doe, A. et al. (duplicate number)", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    types = {f["type"] for f in result["findings"]}
    assert "reference_list_duplicate_number" in types
    f = next(f for f in result["findings"] if f["type"] == "reference_list_duplicate_number")
    assert f["duplicate_number"] == 1
    # the FIRST-seen entry keeps the "canonical" slot for that number and is
    # what the finding's para_id points to (the second occurrence is what
    # triggered the duplicate, but the canonical entry is what a reader
    # should be directed to)
    assert f["para_id"] == "E0000001"


# ---------------------------------------------------------------------------
# In-text citation number expansion: ranges and comma lists
# ---------------------------------------------------------------------------

def test_citation_range_is_expanded(tmp_path):
    body = (
        _body_para("Multiple studies [1-3] agree.", "B0000001")
        + _references_heading()
        + _bib_entry(1, "A", "E0000001")
        + _bib_entry(2, "B", "E0000002")
        + _bib_entry(3, "C", "E0000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    assert result["finding_count"] == 0, result["findings"]
    assert result["citation_count"] == 3


def test_citation_comma_list_is_expanded_not_merged_into_a_range(tmp_path):
    """"[1, 3]" must expand to exactly {1, 3} -- entry 2 correctly reported
    as never cited, proving the comma list is NOT mistakenly treated as a
    "1-3" range (which would wrongly cover 2 as well)."""
    body = (
        _body_para("See [1, 3] for details.", "B0000001")
        + _references_heading()
        + _bib_entry(1, "A", "E0000001")
        + _bib_entry(2, "B", "E0000002")
        + _bib_entry(3, "C", "E0000003")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    assert result["citation_count"] == 2
    uncited = {
        f["reference_number"] for f in result["findings"]
        if f["type"] == "reference_entry_never_cited"
    }
    assert uncited == {2}
    assert {f["type"] for f in result["findings"] if f["type"].startswith("reference_list_")} == set()


# ---------------------------------------------------------------------------
# Bibliography entries excluded from in-text scanning
# ---------------------------------------------------------------------------

def test_bracket_text_inside_a_bibliography_entry_is_not_counted_as_a_citation(tmp_path):
    """An entry's own text incidentally containing a bracketed number (e.g.
    referencing another entry in a "see also" note) must not be
    miscounted as an in-text citation -- only body text outside the
    reference-list block is scanned for citations."""
    body = (
        _body_para("No in-text citations at all here.", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al., see also [2] for context.", "E0000001")
        + _bib_entry(2, "Doe, A. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    assert result["citation_count"] == 0
    types = {f["type"] for f in result["findings"]}
    assert "reference_entry_never_cited" in types
    uncited = {f["reference_number"] for f in result["findings"] if f["type"] == "reference_entry_never_cited"}
    assert uncited == {1, 2}


# ---------------------------------------------------------------------------
# Positional fallback numbering (no literal "[N]" marker on entries)
# ---------------------------------------------------------------------------

def test_positional_fallback_when_entries_have_no_literal_marker(tmp_path):
    body = (
        _body_para("[1] and [2]", "B0000001")
        + _references_heading()
        + _bib_entry(None, "Smith, J. et al.", "E0000001")
        + _bib_entry(None, "Doe, A. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    # positional numbering (1, 2) is trivially sequential -- no gap/dup
    # findings can be manufactured by the fallback itself
    gap_or_dup_types = {
        f["type"] for f in result["findings"]
        if f["type"] in ("reference_list_number_gap", "reference_list_duplicate_number")
    }
    assert gap_or_dup_types == set()
    assert result["finding_count"] == 0, result["findings"]


# ---------------------------------------------------------------------------
# No References/Bibliography heading found
# ---------------------------------------------------------------------------

def test_no_references_heading_found_produces_no_findings(tmp_path):
    body = _body_para("A citation-shaped [1] token with no reference list at all.", "B0000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    assert result["findings"] == []
    assert result["reference_count"] == 0


# ---------------------------------------------------------------------------
# code-review regression (docs-intel-jcshm-linter-gap-cleanup-20260918) --
# a References/Bibliography heading that IS located but has ZERO parseable
# entries after it (immediately followed by the next heading, or by end of
# document) must still flag every in-text citation as
# citation_missing_reference_entry. All four finding-generating loops were
# originally nested under ``if reference_numbers:`` (true only when at
# least one entry was found), so this exact case -- confirmed by direct
# repro during review -- silently produced ZERO findings even though real
# citations existed with no possible matching entry. This is DIFFERENT from
# test_no_references_heading_found_produces_no_findings above (no heading
# located AT ALL, which correctly stays silent -- "can't check against a
# list that doesn't exist").
# ---------------------------------------------------------------------------

def test_references_heading_found_with_zero_entries_still_flags_citations(tmp_path):
    body = (
        _body_para("See prior work [1] and [2] for details.", "B0000001")
        + _references_heading()
        # NOTE: no entries at all after the heading -- end of document.
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)
    assert result["reference_count"] == 0
    assert result["citation_count"] == 2
    types_and_numbers = {
        (f["type"], f["citation_number"]) for f in result["findings"]
        if f["type"] == "citation_missing_reference_entry"
    }
    assert types_and_numbers == {("citation_missing_reference_entry", 1), ("citation_missing_reference_entry", 2)}
    # No entries at all -> the gap/duplicate checks (which need a non-empty
    # reference_numbers to compute a "highest") must not fire.
    types = {f["type"] for f in result["findings"]}
    assert "reference_list_number_gap" not in types
    assert "reference_list_duplicate_number" not in types


# ---------------------------------------------------------------------------
# Unconditional -- not style-policy-gated
# ---------------------------------------------------------------------------

def test_findings_fire_regardless_of_style_policy(tmp_path):
    """Unlike every style-preference audit_* function, these are structural
    facts -- passing NO style_policy still catches a real gap."""
    body = (
        _body_para("[1] [3]", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
        + _bib_entry(3, "Lee, K. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(path)  # no style_policy
    types = {f["type"] for f in result["findings"]}
    assert "reference_list_number_gap" in types


# ---------------------------------------------------------------------------
# build_document_review wiring
# ---------------------------------------------------------------------------

def test_build_document_review_citation_category_includes_reference_findings(tmp_path):
    body = (
        _body_para("[1] [3]", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
        + _bib_entry(3, "Lee, K. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path)
    assert review["status"] == "ok"
    citation_findings = [f for f in review["findings"] if f["category"] == "citation"]
    types = {f["type"] for f in citation_findings}
    assert "reference_list_number_gap" in types
    gap_finding = next(f for f in citation_findings if f["type"] == "reference_list_number_gap")
    assert gap_finding["severity"] == "error"
    assert review["findings_by_category"]["citation"] == len(citation_findings)


def test_build_document_review_consistent_references_produce_no_citation_findings(tmp_path):
    body = (
        _body_para("[1]", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
    )
    path = _write_docx(tmp_path, _doc(body))
    review = docs_intel.build_document_review(path)
    citation_findings = [f for f in review["findings"] if f["category"] == "citation"]
    assert citation_findings == []


# ---------------------------------------------------------------------------
# Read-only, error handling, server wrapper parity
# ---------------------------------------------------------------------------

def test_invalid_style_policy_returns_error_not_exception(tmp_path):
    body = _body_para("[1]", "B0000001") + _references_heading() + _bib_entry(1, "A", "E0000001")
    path = _write_docx(tmp_path, _doc(body))
    result = docs_intel.audit_reference_consistency(
        path, style_policy={"citation_style": "bogus"}
    )
    assert "error" in result


def test_missing_file_returns_error():
    result = docs_intel.audit_reference_consistency("/no/such/file.docx")
    assert "error" in result


def test_read_only_never_mutates(tmp_path):
    body = (
        _body_para("[1] [3]", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
        + _bib_entry(3, "Lee, K. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    before_bytes = open(path, "rb").read()
    docs_intel.audit_reference_consistency(path)
    after_bytes = open(path, "rb").read()
    assert before_bytes == after_bytes


def test_server_wrapper_matches_docs_intel(tmp_path):
    body = (
        _body_para("[1] [3]", "B0000001")
        + _references_heading()
        + _bib_entry(1, "Smith, J. et al.", "E0000001")
        + _bib_entry(3, "Lee, K. et al.", "E0000002")
    )
    path = _write_docx(tmp_path, _doc(body))
    assert server.audit_reference_consistency(path) == docs_intel.audit_reference_consistency(path)
