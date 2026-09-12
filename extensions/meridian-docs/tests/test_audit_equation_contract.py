"""Tests for audit_equation_contract (6bbce476, W1-L -- Phase 1 of proposal
cfe4c6df: "Meridian Docs project proposal: equation and nomenclature
consistency contract").

Phase 1 scope only: a single, deterministic, dry-run-only consolidation of
this module's three previously-scattered equation-audit utilities
(parse_docx_equations_local, audit_equation_integrity, audit_equation_style)
into one {violation_type, location, severity, suggested_fix} manifest, plus
per-equation stable ids, visible-number-vs-audit-serial mapping, and
display/inline/table-numbered classification. No repair, no staging writes
-- read-only analysis only.

All tests use synthetic .docx bytes/files built inline -- no real files, no
network, no dependency on any external document.
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile

import pytest

_EXT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel, server  # noqa: E402

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


def _omath(*, inner: str) -> str:
    return f"<m:oMath>{inner}</m:oMath>"


def _run(*texts: str) -> str:
    return "".join(f"<m:r><m:t>{t}</m:t></m:r>" for t in texts)


def _numbered_row(para_id: str, omath_xml: str, number_text: str) -> str:
    return f'''    <w:tr>
      <w:tc><w:p w14:paraId="{para_id}">{omath_xml}</w:p></w:tc>
      <w:tc><w:p><w:r><w:t>{number_text}</w:t></w:r></w:p></w:tc>
    </w:tr>'''


def _violations_of_type(result, violation_type):
    return [v for v in result["violations"] if v["violation_type"] == violation_type]


def _equation_by_anchor(result, anchor):
    return next(e for e in result["equations"] if e["anchor"] == anchor)


# ---------------------------------------------------------------------------
# Basic contract: read-only, error paths, dry_run enforcement.
# ---------------------------------------------------------------------------

def test_missing_file_returns_error():
    result = docs_intel.audit_equation_contract("/no/such/file.docx")
    assert "error" in result


def test_dry_run_defaults_to_true_and_succeeds(tmp_path):
    xml = _doc(f'<w:p w14:paraId="AAA00001">{_omath(inner=_run("x"))}</w:p>')
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_equation_contract(path)
    assert result["status"] == "ok"
    assert result["dry_run"] is True


def test_dry_run_false_is_rejected_and_never_touches_the_document(tmp_path):
    xml = _doc(f'<w:p w14:paraId="AAA00002">{_omath(inner=_run("x"))}</w:p>')
    path = _write_docx(tmp_path, xml)
    with open(path, "rb") as fh:
        before = fh.read()

    result = docs_intel.audit_equation_contract(path, dry_run=False)

    assert "error" in result
    assert "dry_run" not in result or result.get("status") != "ok"

    with open(path, "rb") as fh:
        after = fh.read()
    assert before == after


def test_project_id_is_optional_and_passed_through(tmp_path):
    xml = _doc(f'<w:p w14:paraId="AAA00003">{_omath(inner=_run("x"))}</w:p>')
    path = _write_docx(tmp_path, xml)

    no_project = docs_intel.audit_equation_contract(path)
    assert no_project["status"] == "ok"
    assert no_project["project_id"] is None

    with_project = docs_intel.audit_equation_contract(path, project_id="proj-123")
    assert with_project["project_id"] == "proj-123"


def test_document_with_no_body_returns_clean_empty_result(tmp_path):
    path = tmp_path / "empty.docx"
    path.write_bytes(_zip_docx(f'<?xml version="1.0"?><w:document {_NS_HEADER}></w:document>'))
    result = docs_intel.audit_equation_contract(str(path))
    assert result["status"] == "ok"
    assert result["equation_count"] == 0
    assert result["equations"] == []
    assert result["violations"] == []


def test_result_is_json_serializable(tmp_path):
    xml = _doc(
        "<w:tbl>\n"
        + _numbered_row("AAA00004", _omath(inner=_run("a")), "(1)") + "\n"
        + "</w:tbl>"
    )
    path = _write_docx(tmp_path, xml)
    result = docs_intel.audit_equation_contract(path)
    json.dumps(result)  # must not raise


# ---------------------------------------------------------------------------
# The flawed fixture: one document exercising several distinct defect
# classes plus the display/inline/table-numbered classification spectrum.
# ---------------------------------------------------------------------------

def _flawed_fixture_xml() -> str:
    # 1. Display equation: alone in its paragraph, no w:jc (-> misaligned,
    #    default policy expects "center"), no trailing punctuation.
    display_eq = f'<w:p w14:paraId="AAA00001">{_omath(inner=_run("x", "+", "y", "=", "z"))}</w:p>'

    # 2. Inline equation: mixed with surrounding prose in the same paragraph.
    inline_eq = (
        '<w:p w14:paraId="AAA00002">'
        '<w:r><w:t>As shown, </w:t></w:r>'
        f'{_omath(inner=_run("a", "=", "b"))}'
        '<w:r><w:t> holds for all a.</w:t></w:r>'
        "</w:p>"
    )

    # 3. Missing OMML: a plaintext paragraph that reads exactly like an
    #    equation but carries no <m:oMath> at all.
    missing_omml_eq = '<w:p w14:paraId="AAA00003"><w:r><w:t>F = ma</w:t></w:r></w:p>'

    # 4. Duplicate visible equation numbers: two independent table-numbered
    #    rows both labelled "(1)".
    duplicate_numbers = (
        "<w:tbl>\n"
        + _numbered_row("BBB00001", _omath(inner=_run("p")), "(1)") + "\n"
        + _numbered_row("BBB00002", _omath(inner=_run("q")), "(1)") + "\n"
        "</w:tbl>"
    )

    return _doc("\n".join([display_eq, inline_eq, missing_omml_eq, duplicate_numbers]))


@pytest.fixture()
def flawed_fixture_path(tmp_path):
    return _write_docx(tmp_path, _flawed_fixture_xml(), name="flawed.docx")


def test_flawed_fixture_returns_structured_violation_manifest(flawed_fixture_path):
    result = docs_intel.audit_equation_contract(flawed_fixture_path)

    assert result["status"] == "ok"
    # 1 display + 1 inline + 2 table-numbered = 4 real OMML equations
    # (the missing_omml paragraph has no <m:oMath> at all -- it's a
    # violation, not an equation record).
    assert result["equation_count"] == 4

    assert result["violations"], "expected at least one violation from the flawed fixture"
    for violation in result["violations"]:
        assert set(violation.keys()) == {
            "violation_type", "location", "severity", "suggested_fix", "detail",
        }
        assert violation["severity"] in ("error", "warning")
        assert isinstance(violation["suggested_fix"], str) and violation["suggested_fix"]
        location = violation["location"]
        assert set(location.keys()) == {"anchor", "section_path", "ordinal", "docx_path"}
        assert location["docx_path"] == flawed_fixture_path

    # Each deliberately-planted defect class is present.
    assert _violations_of_type(result, "missing_omml")
    assert _violations_of_type(result, "equation_number_duplicate")
    assert _violations_of_type(result, "misaligned_equation")
    assert _violations_of_type(result, "missing_trailing_punctuation")

    # Counters agree with the manifest.
    assert result["violation_count"] == len(result["violations"])
    assert sum(result["violations_by_type"].values()) == result["violation_count"]
    assert sum(result["violations_by_severity"].values()) == result["violation_count"]


def test_style_only_numbering_findings_are_not_double_reported(flawed_fixture_path):
    """audit_equation_style ALSO detects the same duplicate-number defect
    under its own name ("duplicate_equation_number") -- the consolidation
    must report it exactly once, under the integrity audit's canonical
    name ("equation_number_duplicate"), never both.
    """
    result = docs_intel.audit_equation_contract(flawed_fixture_path)
    violation_types = {v["violation_type"] for v in result["violations"]}
    assert "equation_number_duplicate" in violation_types
    assert "duplicate_equation_number" not in violation_types


def test_display_inline_and_table_numbered_classification(flawed_fixture_path):
    result = docs_intel.audit_equation_contract(flawed_fixture_path)

    display_eq = _equation_by_anchor(result, "AAA00001")
    assert display_eq["display_kind"] == "display"
    assert display_eq["pattern"] == "standalone"

    inline_eq = _equation_by_anchor(result, "AAA00002")
    assert inline_eq["display_kind"] == "inline"
    assert inline_eq["pattern"] == "standalone"

    numbered = [e for e in result["equations"] if e["pattern"] == "table-numbered"]
    assert len(numbered) == 2
    for eq in numbered:
        assert eq["display_kind"] == "table-numbered"


def test_visible_number_and_audit_serial_are_not_conflated(flawed_fixture_path):
    result = docs_intel.audit_equation_contract(flawed_fixture_path)

    display_eq = _equation_by_anchor(result, "AAA00001")
    assert display_eq["visible_number"] is None
    assert isinstance(display_eq["audit_serial"], int)

    numbered = [e for e in result["equations"] if e["pattern"] == "table-numbered"]
    for eq in numbered:
        assert eq["visible_number"] == "(1)"
        assert isinstance(eq["audit_serial"], int)
        # The two fields are independent identities -- the visible number
        # is shared (both equations render as "(1)") but each equation's
        # own internal audit_serial is still unique.
    serials = {eq["audit_serial"] for eq in numbered}
    assert len(serials) == 2


def test_equation_ids_are_stable_across_repeated_calls(flawed_fixture_path):
    first = docs_intel.audit_equation_contract(flawed_fixture_path)
    second = docs_intel.audit_equation_contract(flawed_fixture_path)
    ids_first = [e["equation_id"] for e in first["equations"]]
    ids_second = [e["equation_id"] for e in second["equations"]]
    assert ids_first == ids_second
    assert len(ids_first) == len(set(ids_first))  # unique within one document


# ---------------------------------------------------------------------------
# Read-only invariant: the exact property the sprint item asks the test
# suite to confirm -- byte-identical before and after.
# ---------------------------------------------------------------------------

def test_never_mutates_the_source_document(flawed_fixture_path):
    with open(flawed_fixture_path, "rb") as fh:
        before = fh.read()

    result = docs_intel.audit_equation_contract(flawed_fixture_path, dry_run=True)
    assert result["status"] == "ok"

    with open(flawed_fixture_path, "rb") as fh:
        after = fh.read()
    assert before == after


def test_source_fingerprint_matches_a_fresh_hash_of_the_same_bytes(flawed_fixture_path):
    import hashlib
    result = docs_intel.audit_equation_contract(flawed_fixture_path)
    with open(flawed_fixture_path, "rb") as fh:
        raw = fh.read()
    assert result["source_fingerprint"] == hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# MCP tool wiring (server.py) -- same pattern audit_equation_style/
# audit_document already use: the decorated function stays directly
# callable and delegates to docs_intel without altering the result shape.
# ---------------------------------------------------------------------------

def test_mcp_tool_delegates_to_docs_intel(flawed_fixture_path):
    direct = docs_intel.audit_equation_contract(flawed_fixture_path)
    via_tool = server.audit_equation_contract(flawed_fixture_path)
    assert via_tool == direct


def test_mcp_tool_accepts_project_id_and_dry_run_kwargs(flawed_fixture_path):
    result = server.audit_equation_contract(
        flawed_fixture_path, project_id="proj-xyz", dry_run=True,
    )
    assert result["status"] == "ok"
    assert result["project_id"] == "proj-xyz"

    rejected = server.audit_equation_contract(flawed_fixture_path, dry_run=False)
    assert "error" in rejected
