"""Deterministic notation-manifest lint tests for the Docs contract."""
from __future__ import annotations

from zipfile import ZIP_DEFLATED, ZipFile

from meridian_docs import nomenclature


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _write_docx(path, text: str) -> None:
    document = (
        f'<w:document xmlns:w="{_W}"><w:body><w:p><w:r><w:t>'
        f"{text}"
        f"</w:t></w:r></w:p></w:body></w:document>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as package:
        package.writestr("word/document.xml", document)


def _write_omml_docx(path, omml: str) -> None:
    document = (
        f'<w:document xmlns:w="{_W}" xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        f"<w:body><w:p>{omml}</w:p></w:body></w:document>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as package:
        package.writestr("word/document.xml", document)


def test_missing_manifest_is_explicit_and_read_only(tmp_path):
    source = tmp_path / "source.docx"
    _write_docx(source, "R = 5")

    result = nomenclature.lint_nomenclature(str(source), None)

    assert result["valid"] is False
    assert result["findings_by_type"] == {"notation_manifest_missing": 1}
    assert result["document_path"] == str(source)


def test_flattened_alias_and_missing_required_symbol_are_deterministic(tmp_path):
    source = tmp_path / "source.docx"
    _write_docx(source, "Rray = 5. The depth signal is R_depth(x,y).")
    manifest = {
        "symbols": [
            {"symbol": "R_ray", "flattened_aliases": ["Rray"], "required": True},
            {"symbol": "R_depth", "required": True},
            {"symbol": "unused", "required": False},
        ]
    }

    first = nomenclature.lint_nomenclature(str(source), manifest)
    second = nomenclature.lint_nomenclature(str(source), manifest)

    assert first == second
    assert first["findings_by_type"] == {
        "declared_symbol_unused": 1,
        "flattened_subscript": 1,
        "missing_symbol": 1,
    }
    assert first["used_symbols"] == ["R_depth"]


def test_alias_case_and_role_collision_are_reported_without_rewrite(tmp_path):
    source = tmp_path / "source.docx"
    _write_docx(source, "DT is used here.")
    manifest = {
        "case_sensitive": True,
        "symbols": [
            {"symbol": "dt", "aliases": ["DT"], "role": "distance"},
            {"symbol": "decision_tree", "aliases": ["DT"], "role": "decision tree"},
        ],
    }

    result = nomenclature.lint_nomenclature(str(source), manifest)
    finding_types = {finding["type"] for finding in result["findings"]}

    assert "alias_used" in finding_types
    assert "symbol_role_collision" in finding_types
    assert result["manifest"]["symbols"][0]["symbol"] == "decision_tree"
    assert result["valid"] is False


def test_invalid_manifest_is_machine_readable(tmp_path):
    source = tmp_path / "source.docx"
    _write_docx(source, "x")

    result = nomenclature.lint_nomenclature(
        str(source), {"symbols": [{"symbol": "x", "required": "yes"}]}
    )

    assert result == {
        "error": "symbols[0].required must be boolean",
        "error_type": "manifest_invalid",
    }


def test_manifest_normalization_is_order_independent():
    left = {"symbols": [{"symbol": "B"}, {"symbol": "A", "role": "alpha"}]}
    right = {"symbols": [{"role": "alpha", "symbol": "A"}, {"symbol": "B"}]}

    assert nomenclature.normalize_nomenclature_manifest(left) == nomenclature.normalize_nomenclature_manifest(right)


def test_alias_order_is_canonical_and_dictionary_key_cannot_be_overridden():
    left = {"symbols": [{"symbol": "R", "aliases": ["ray", "radius"]}]}
    right = {"symbols": [{"symbol": "R", "aliases": ["radius", "ray"]}]}

    assert nomenclature.normalize_nomenclature_manifest(left) == nomenclature.normalize_nomenclature_manifest(right)
    try:
        nomenclature.normalize_nomenclature_manifest(
            {"symbols": {"R": {"symbol": "other"}}}
        )
    except nomenclature.NomenclatureManifestError as exc:
        assert "conflicts with its dictionary key" in str(exc)
    else:
        raise AssertionError("conflicting dictionary symbol must be rejected")


def test_malformed_native_omml_is_a_blocking_nomenclature_finding(tmp_path):
    source = tmp_path / "source.docx"
    _write_omml_docx(
        source,
        '<m:oMath><m:f xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        '<m:num /><m:den><m:e /></m:den></m:f></m:oMath>',
    )

    result = nomenclature.lint_nomenclature(
        str(source), {"symbols": [{"symbol": "x", "required": False}]}
    )

    assert result["findings_by_type"]["omml_invalid"] == 1
    assert result["valid"] is False
