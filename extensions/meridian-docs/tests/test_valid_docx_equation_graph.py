"""Read-only graph, integrity, and notation coverage on a valid DOCX ZIP."""

from __future__ import annotations

import hashlib

from meridian_docs import docs_intel, nomenclature, ooxml_integrity
from meridian_docs.equation_graph import build_equation_graph
from meridian_docs.notation_audit import audit_equation_notation
from meridian_docs.notation_manifest import validate_notation_manifest

from .valid_docx_fixture import fixture_notation_manifest, valid_docx_bytes


def test_valid_docx_equation_fixture_is_deterministic_and_read_only(tmp_path):
    fixture = valid_docx_bytes()
    assert fixture == valid_docx_bytes()

    path = tmp_path / "valid-equation-fixture.docx"
    path.write_bytes(fixture)
    before = path.read_bytes()
    before_digest = hashlib.sha256(before).hexdigest()
    manifest = fixture_notation_manifest()

    package = ooxml_integrity.validate_docx_package(str(path))
    assert package["ok"] is True, package
    assert package["issues"] == []
    assert package["part_count"] == 5
    assert path.read_bytes() == before

    graph = build_equation_graph(str(path), manifest)
    assert graph == build_equation_graph(str(path), manifest)
    assert graph["source_fingerprint"] == before_digest
    assert graph["equation_count"] == 4
    equations = {equation["para_id"]: equation for equation in graph["equations"]}
    assert graph["placements"]["inline"] == [equations["F0000002"]["id"]]
    assert len(graph["placements"]["line_separated"]) == 2
    assert equations["F0000003"]["placement"] == "line_separated"
    assert equations["F0000006"]["placement"] == "line_separated"
    table_equation = equations["F0000005"]
    assert graph["placements"]["table_numbered"] == [table_equation["id"]]
    assert table_equation["number"] == "(2)"
    assert table_equation["table_path"] == "t1/r1/c1"
    assert table_equation["flat_text"] == "Ri=x"
    assert {node["kind"] for node in graph["nodes"]} >= {"bookmark", "word_field"}
    assert graph["reference_extraction"]["record_count"] == 3
    assert {
        record.get("field_type") for record in graph["reference_extraction"]["records"] if record["kind"] == "field"
    } == {"SEQ", "REF"}
    assert any(record.get("bookmark_name") == "_RefEquation1" for record in graph["reference_extraction"]["records"])
    assert path.read_bytes() == before

    integrity = docs_intel.audit_equation_integrity(str(path))
    assert integrity == docs_intel.audit_equation_integrity(str(path))
    assert integrity["source_fingerprint"] == before_digest
    assert integrity["equation_count"] == 4
    assert path.read_bytes() == before

    notation = audit_equation_notation(str(path), manifest)
    assert notation == audit_equation_notation(str(path), manifest)
    assert notation["occurrence_count"] >= 2
    assert any(
        occurrence["term"] == "R_depth" and occurrence["symbol_id"] == "radius"
        for occurrence in notation["occurrences"]
    )
    assert path.read_bytes() == before

    nomenclature_report = nomenclature.lint_nomenclature(str(path), manifest)
    assert nomenclature_report == nomenclature.lint_nomenclature(str(path), manifest)
    assert nomenclature_report["findings_by_type"]["omml_invalid"] == 1
    assert any(finding.get("anchor") == "F0000006" for finding in nomenclature_report["findings"])
    assert nomenclature_report["valid"] is False
    assert path.read_bytes() == before

    manifest_report = validate_notation_manifest(manifest)
    assert manifest_report["valid"] is True, manifest_report
    assert path.read_bytes() == before
