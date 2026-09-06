from __future__ import annotations

import hashlib

import pytest

from meridian_docs.analysis_receipt import (
    AnalysisReceipt,
    AnalysisReceiptError,
    assert_source_hash,
    bind_render_receipt,
    build_analysis_receipt,
    build_docx_analysis_receipt,
    evaluate_analysis_gate,
    project_registry_evidence,
)


def test_analysis_receipt_joins_components_by_exact_source_hash(tmp_path):
    source = tmp_path / "document.docx"
    source.write_bytes(b"DOCX bytes")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    receipt = build_analysis_receipt(
        source,
        equation_graph={"source_fingerprint": source_hash, "equation_count": 2, "valid": True},
        notation_audit={"source_fingerprint": source_hash, "occurrence_count": 2, "valid": True},
        outputs_evidence={"source_sha256": "0" * 64, "status": "pass"},
    )

    assert receipt.source_locator == "document.docx"
    assert receipt.status == "stale"
    assert receipt.components["outputs_evidence"]["status"] == "stale"
    assert receipt.components["equation_graph"]["status"] == "pass"


def test_missing_components_are_not_supplied_and_partial_is_distinct():
    receipt = build_analysis_receipt(b"x", equation_graph={"status": "partial", "reasons": ["incomplete"]})

    assert receipt.status == "partial"
    assert receipt.components["notation_audit"]["status"] == "not_supplied"
    assert receipt.components["equation_graph"]["status"] == "partial"


def test_analysis_receipt_json_xml_round_trip_preserves_unknown_fields():
    receipt = build_analysis_receipt(
        b"x",
        artifacts=[{"artifact_id": "eq-1", "identity_status": "registry"}],
        relations=[{"type": "derived_from", "from": "eq-1", "to": "doc-1"}],
    )
    value = receipt.to_dict()
    value["future_field"] = {"kept": True}

    rebuilt = AnalysisReceipt.from_xml(AnalysisReceipt.from_dict(value).to_xml())

    assert rebuilt.to_dict()["future_field"] == {"kept": True}
    assert rebuilt.digest() == AnalysisReceipt.from_dict(rebuilt.to_dict()).digest()


def test_source_hash_verification_is_read_only_and_mismatch_rejected():
    receipt = build_analysis_receipt(b"x")

    assert receipt.verify_source(b"x")["valid"] is True
    assert receipt.verify_source(b"y")["stale"] is True
    assert_source_hash(receipt, receipt.source_sha256)
    with pytest.raises(AnalysisReceiptError, match="source hash mismatch"):
        assert_source_hash(receipt, "0" * 64)


def test_invalid_relation_type_is_rejected():
    with pytest.raises(AnalysisReceiptError, match="relation type"):
        build_analysis_receipt(
            b"x",
            relations=[{"type": "made_up", "from": "a", "to": "b"}],
        )


def test_registry_projection_keeps_identity_and_legacy_status_separate():
    source_hash = "a" * 64
    projected = project_registry_evidence(
        {
            "artifact_id": "artifact-1",
            "status": "pass",
            "source_sha256": source_hash,
            "source_edges": [{"kind": "derived"}],
        },
        legacy_status={"provenance_type": "unknown"},
        source_sha256=source_hash,
    )

    assert projected["registry_identity"] == "artifact-1"
    assert projected["source_edges"] == [{"kind": "derived"}]
    assert projected["legacy_status"]["provenance_type"] == "unknown"
    assert projected["status"] == "pass"


def test_valid_component_without_source_hash_is_partial():
    receipt = build_analysis_receipt(b"x", equation_graph={"valid": True})

    assert receipt.status == "partial"
    assert receipt.components["equation_graph"]["status"] == "partial"


def test_render_binding_and_gate_are_read_only_and_fail_closed_on_stale_data():
    receipt = build_analysis_receipt(b"x", equation_graph={"valid": True})
    bound = bind_render_receipt({"status": "pass", "source_sha256": "0" * 64}, receipt)

    assert bound["status"] == "stale"
    gate = evaluate_analysis_gate(receipt)
    assert gate["allowed"] is False
    assert any("notation_audit" in reason for reason in gate["reasons"])


def test_rendered_docx_receipt_uses_source_docx_hash_binding():
    receipt = build_analysis_receipt(b"x")
    bound = bind_render_receipt(
        {"status": "rendered", "source_docx_sha256": receipt.source_sha256},
        receipt,
    )

    assert bound["status"] == "pass"
    assert bound["analysis_binding"]["status"] == "pass"


def test_default_gate_requires_integrity_as_a_separate_component():
    source_hash = hashlib.sha256(b"x").hexdigest()
    receipt = build_analysis_receipt(
        b"x",
        equation_graph={"source_sha256": source_hash, "valid": True},
        notation_audit={"source_sha256": source_hash, "valid": True},
    )

    gate = evaluate_analysis_gate(receipt)

    assert gate["allowed"] is False
    assert any("integrity" in reason for reason in gate["reasons"])


def test_receipt_xml_metadata_must_match_payload():
    receipt = build_analysis_receipt(b"x")
    forged = receipt.to_xml().replace('status="not_supplied"', 'status="pass"')

    with pytest.raises(AnalysisReceiptError, match="metadata"):
        AnalysisReceipt.from_xml(forged)


def test_docx_adapter_runs_read_only_analyzers_and_binds_one_source_hash(tmp_path):
    from .valid_docx_fixture import fixture_notation_manifest, valid_docx_bytes

    path = tmp_path / "document.docx"
    path.write_bytes(valid_docx_bytes())
    before = path.read_bytes()

    receipt = build_docx_analysis_receipt(path, notation_manifest=fixture_notation_manifest())

    assert receipt.source_sha256 == hashlib.sha256(before).hexdigest()
    assert receipt.components["equation_graph"]["status"] == "pass"
    assert receipt.components["integrity"]["status"] == "blocked"
    assert receipt.components["nomenclature_audit"]["status"] == "blocked"
    assert path.read_bytes() == before
