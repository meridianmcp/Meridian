"""Tests for convert_equation_to_numbered_row (DOCS-R2-D).

Converts a standalone display equation into a numbered, borderless
two-column table row -- the exact pattern="table-numbered" structure
parse_docx_equations_local / audit_equation_style already recognize
elsewhere in docs_intel.py. Mirrors insert_equation_local's tri-state
render-gate contract (rendered / unavailable-with-reason / failed) and
restore-on-failure discipline.
"""
from __future__ import annotations

import zipfile

import pytest

from meridian_docs import docs_intel, render_gate


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_NS = f'xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:m="{_M}"'

_SIMPLE_OMATH = f'<m:oMath xmlns:m="{_M}"><m:r><m:t>x=y</m:t></m:r></m:oMath>'


def _document_xml(body_inner: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
{body_inner}
    <w:sectPr/>
  </w:body>
</w:document>
'''


def _standalone_equation_body() -> str:
    return f'''    <w:p w14:paraId="P0000001">
      <w:r><w:t>Preceding prose paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      {_SIMPLE_OMATH}
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>Following prose paragraph.</w:t></w:r>
    </w:p>'''


def _write_docx(tmp_path, body_inner: str, name: str = "doc.docx") -> str:
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _document_xml(body_inner))
    return path


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml")


def _mock_rendered(monkeypatch):
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "rendered", "backend": "test-stub", "detail": {}},
    )


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


def test_dry_run_returns_manifest_without_writing(tmp_path):
    path = _write_docx(tmp_path, _standalone_equation_body())
    before = _read_document_xml(path)

    result = docs_intel.convert_equation_to_numbered_row(
        path, "P0000002", "(1)", dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["number"] == "(1)"
    assert "omml_sha256" in result
    assert "new_equation_cell_para_id" in result
    assert "new_number_cell_para_id" in result
    assert _read_document_xml(path) == before


# ---------------------------------------------------------------------------
# successful conversion
# ---------------------------------------------------------------------------


def test_converts_standalone_equation_into_table_numbered_row(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _standalone_equation_body())
    _mock_rendered(monkeypatch)

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000002", "(1)")

    assert result["status"] == "converted"
    assert result["number"] == "(1)"
    assert result["render_status"] == "rendered"
    assert result["render_verified"] is True

    equations = docs_intel.parse_docx_equations_local(path)
    numbered = [eq for eq in equations if eq["pattern"] == "table-numbered"]
    assert len(numbered) == 1
    assert numbered[0]["number"] == "(1)"
    assert numbered[0]["flat_text"] == "x=y"

    # Surrounding prose paragraphs are untouched.
    xml = _read_document_xml(path).decode("utf-8")
    assert "Preceding prose paragraph." in xml
    assert "Following prose paragraph." in xml
    # Old standalone paraId is gone (replaced, not duplicated).
    assert 'w14:paraId="P0000002"' not in xml


def test_converted_row_is_borderless(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _standalone_equation_body())
    _mock_rendered(monkeypatch)

    docs_intel.convert_equation_to_numbered_row(path, "P0000002", "(1)")

    xml = _read_document_xml(path).decode("utf-8")
    assert '<w:tblBorders>' in xml
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        assert f'<w:{edge} w:val="none"' in xml


def test_new_cell_para_ids_are_fresh_and_do_not_collide(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _standalone_equation_body())
    _mock_rendered(monkeypatch)

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000002", "(1)")

    eq_id = result["new_equation_cell_para_id"]
    num_id = result["new_number_cell_para_id"]
    assert eq_id != num_id
    assert eq_id not in ("P0000001", "P0000002", "P0000003")
    assert num_id not in ("P0000001", "P0000002", "P0000003")


# ---------------------------------------------------------------------------
# render-gate tri-state contract (mirrors insert_equation_local exactly)
# ---------------------------------------------------------------------------


def test_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _standalone_equation_body())
    before = _read_document_xml(path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "soffice crashed"},
    )

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000002", "(1)")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before


def test_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _standalone_equation_body())
    before = _read_document_xml(path)

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000002", "(1)")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before


def test_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _standalone_equation_body())

    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {
            "status": "unavailable-with-reason",
            "reason": "no render backend available in this environment",
        },
    )

    result = docs_intel.convert_equation_to_numbered_row(
        path, "P0000002", "(1)",
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "converted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True
    assert result["degraded_render_reason"] == "CI sandbox has no LibreOffice/Word installed"
    numbered = [
        eq for eq in docs_intel.parse_docx_equations_local(path)
        if eq["pattern"] == "table-numbered"
    ]
    assert len(numbered) == 1


def test_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path, _standalone_equation_body())
    before = _read_document_xml(path)

    result = docs_intel.convert_equation_to_numbered_row(
        path, "P0000002", "(1)", allow_degraded_render=True,
    )

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    assert _read_document_xml(path) == before


def test_structural_verification_failure_restores_and_errors(tmp_path, monkeypatch):
    """Simulate the post-write structural check failing (e.g. a concurrent
    writer corrupted the promoted file) -- must restore and error, and must
    never even consult the render gate once structural verification fails."""
    path = _write_docx(tmp_path, _standalone_equation_body())
    before = _read_document_xml(path)
    render_calls = {"n": 0}

    def _spy(p, **kwargs):
        render_calls["n"] += 1
        return {"status": "rendered", "backend": "test-stub", "detail": {}}

    monkeypatch.setattr(docs_intel.render_gate, "check_render_capability", _spy)
    monkeypatch.setattr(
        docs_intel, "_verify_numbered_equation_conversion",
        lambda *a, **kw: {"error": "post-write verification failed: simulated mismatch"},
    )

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000002", "(1)")

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before
    assert render_calls["n"] == 0


# ---------------------------------------------------------------------------
# input validation -- fail closed, never guess
# ---------------------------------------------------------------------------


def test_rejects_invalid_number_format(tmp_path):
    path = _write_docx(tmp_path, _standalone_equation_body())
    before = _read_document_xml(path)

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000002", "1")

    assert "error" in result
    assert _read_document_xml(path) == before


def test_rejects_paragraph_with_no_equation(tmp_path):
    path = _write_docx(tmp_path, _standalone_equation_body())

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000001", "(1)")

    assert "error" in result
    assert "no <m:oMath>" in result["error"]


def test_rejects_paragraph_with_mixed_prose(tmp_path):
    body = f'''    <w:p w14:paraId="P0000001">
      <w:r><w:t>Some prose before </w:t></w:r>
      {_SIMPLE_OMATH}
      <w:r><w:t> and after.</w:t></w:r>
    </w:p>'''
    path = _write_docx(tmp_path, body)
    before = _read_document_xml(path)

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000001", "(1)")

    assert "error" in result
    assert "surrounding prose" in result["error"]
    assert _read_document_xml(path) == before


def test_rejects_paragraph_with_multiple_equations(tmp_path):
    body = f'''    <w:p w14:paraId="P0000001">
      {_SIMPLE_OMATH}
      {_SIMPLE_OMATH}
    </w:p>'''
    path = _write_docx(tmp_path, body)
    before = _read_document_xml(path)

    result = docs_intel.convert_equation_to_numbered_row(path, "P0000001", "(1)")

    assert "error" in result
    assert "2 equations" in result["error"]
    assert _read_document_xml(path) == before


def test_rejects_unknown_para_id(tmp_path):
    path = _write_docx(tmp_path, _standalone_equation_body())

    result = docs_intel.convert_equation_to_numbered_row(path, "NOPE0000", "(1)")

    assert "error" in result
