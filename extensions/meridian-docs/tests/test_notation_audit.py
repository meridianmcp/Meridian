"""Tests for equation-to-notation occurrence binding."""
from __future__ import annotations

import io
import zipfile

from meridian_docs.notation_audit import audit_equation_notation


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _docx(*paragraphs: str) -> bytes:
    xml = (
        f'<w:document xmlns:w="{W}" xmlns:m="{M}"><w:body>'
        + "".join(paragraphs)
        + "<w:sectPr /></w:body></w:document>"
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("word/document.xml", xml)
    return output.getvalue()


def _p(text: str) -> str:
    return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'


def _equation(text: str) -> str:
    return f'<m:oMath><m:r><m:t>{text}</m:t></m:r></m:oMath>'


def _subscripted_equation(base: str, subscript: str) -> str:
    return (
        '<m:oMath><m:sSub><m:e><m:r><m:t>'
        + base
        + '</m:t></m:r></m:e><m:sub><m:r><m:t>'
        + subscript
        + '</m:t></m:r></m:sub></m:sSub></m:oMath>'
    )


def test_occurrence_binding_records_equation_locator_and_role():
    result = audit_equation_notation(
        _docx(_p("3.2 Cost construction"), f"<w:p>{_equation('C_DT')}</w:p>"),
        {
            "symbols": [
                {
                    "id": "cost.dt",
                    "symbol": "C_DT",
                    "role": "distance-transform cost",
                    "kind": "quantity",
                    "scope": ["section:3.2"],
                    "required": True,
                }
            ]
        },
    )
    assert result["occurrence_count"] == 1
    occurrence = result["occurrences"][0]
    assert occurrence["symbol_id"] == "cost.dt"
    assert occurrence["scope_match"] == ["section:3.2"]
    assert result["valid"] is True


def test_overlapping_active_roles_are_blocking_at_occurrence_level():
    result = audit_equation_notation(
        _docx(f"<w:p>{_equation('R')}</w:p>"),
        {
            "symbols": [
                {"id": "ray", "symbol": "R", "role": "ray radius", "kind": "scalar"},
                {"id": "depth", "symbol": "R", "role": "depth signal", "kind": "quantity"},
            ]
        },
    )
    types = {finding["type"] for finding in result["findings"]}
    assert result["valid"] is False
    assert "semantic_symbol_collision" in types
    assert "ambiguous_symbol_occurrence" in types


def test_flattened_alias_is_explicitly_reported():
    result = audit_equation_notation(
        _docx(f"<w:p>{_equation('Cdt')}</w:p>"),
        {
            "symbols": [
                {
                    "id": "cost.dt",
                    "symbol": "C_DT",
                    "flattened_aliases": ["Cdt"],
                    "role": "distance-transform cost",
                }
            ]
        },
    )
    assert any(
        finding["type"] == "flattened_subscript_occurrence"
        for finding in result["findings"]
    )


def test_native_subscript_is_reported_as_structured_not_flattened():
    result = audit_equation_notation(
        _docx(f"<w:p>{_subscripted_equation('R', 'depth')}</w:p>"),
        {
            "symbols": [
                {
                    "id": "depth.signal",
                    "symbol": "R",
                    "preferred_notation": "R_depth",
                    "role": "depth signal",
                }
            ]
        },
    )
    occurrence = result["occurrences"][0]
    assert occurrence["term"] == "R_depth"
    assert occurrence["subscript"] == "depth"
    assert occurrence["binding_confidence"] == "structured"
    assert not any(
        finding["type"] == "flattened_subscript_occurrence"
        for finding in result["findings"]
    )


def test_bare_base_against_native_subscript_is_reviewable():
    result = audit_equation_notation(
        _docx(f"<w:p>{_subscripted_equation('R', 'i')}</w:p>"),
        {
            "symbols": [
                {
                    "id": "ray.radius",
                    "symbol": "R",
                    "preferred_notation": ["R_ray"],
                    "role": "ray radius",
                }
            ]
        },
    )
    assert any(
        finding["type"] == "unqualified_symbol_occurrence"
        for finding in result["findings"]
    )
    assert result["status"] == "review_required"


def test_distinct_symbols_in_one_equation_are_not_one_ambiguity_group():
    result = audit_equation_notation(
        _docx(f"<w:p>{_equation('R+C')}</w:p>"),
        {
            "symbols": [
                {"id": "ray", "symbol": "R", "role": "ray radius", "kind": "scalar"},
                {"id": "cost", "symbol": "C", "role": "cost", "kind": "quantity"},
            ]
        },
    )
    assert not any(
        finding["type"] == "ambiguous_symbol_occurrence"
        for finding in result["findings"]
    )


def test_case_sensitive_manifest_does_not_bind_lowercase_base():
    result = audit_equation_notation(
        _docx(f"<w:p>{_subscripted_equation('a', 'i')}</w:p>"),
        {
            "case_sensitive": True,
            "symbols": [
                {
                    "id": "allowed.mask",
                    "symbol": "A",
                    "preferred_notation": ["A_i"],
                    "role": "allowed mask",
                }
            ],
        },
    )
    assert result["occurrence_count"] == 0


def test_equation_number_is_found_anywhere_in_the_layout_row():
    table = (
        "<w:tbl><w:tr>"
        f"<w:tc><w:p>{_equation('x')}</w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>alignment</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>(17)</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>"
    )
    result = audit_equation_notation(
        _docx(table),
        {"symbols": [{"id": "x", "symbol": "x", "role": "variable"}]},
    )
    assert result["occurrences"][0]["equation_number"] == "(17)"
