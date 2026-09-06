from __future__ import annotations

import hashlib

from meridian_docs import server
from meridian_docs.latex_bridge import latex_to_omml


def test_server_exposes_read_only_equation_comparison():
    latex = r"\frac{a}{b}"
    result = server.compare_equation_artifacts(
        latex_to_omml(latex).value,
        latex,
        {"word_punctuation": None},
    )

    assert result["semantic_match"] is True
    assert result["automatic_insertion_allowed"] is True
    assert result["blocked"] is False


def test_server_builds_unavailable_compile_receipt_without_compiling(tmp_path):
    root = tmp_path / "main.tex"
    root.write_text("x", encoding="utf-8")

    result = server.build_latex_compile_receipt(
        str(root),
        status="unavailable",
        compiler_status="unavailable",
    )

    assert result["status"] == "unavailable"
    assert result["root_sha256"]
    assert result["pdf_sha256"] is None


def test_server_exposes_versioned_equation_artifact_creation():
    result = server.make_equation_artifact(
        r"\frac{a}{b}",
        "latex",
        "eq-1",
        "doc-1",
        "display",
    )

    assert result["success"] is True
    assert result["value"]["schema_version"] == 1
    assert result["value"]["source_format"] == "latex"


def test_server_builds_hash_bound_analysis_receipt(tmp_path):
    source = tmp_path / "document.docx"
    source.write_bytes(b"docx")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    result = server.build_analysis_receipt(
        str(source),
        equation_graph={"valid": True, "source_fingerprint": source_hash},
    )

    assert result["source_locator"] == "document.docx"
    assert result["status"] == "pass"
    assert result["components"]["equation_graph"]["status"] == "pass"


def test_server_builds_docx_analysis_receipt_from_read_only_analyzers(tmp_path):
    from .valid_docx_fixture import fixture_notation_manifest, valid_docx_bytes

    source = tmp_path / "document.docx"
    source.write_bytes(valid_docx_bytes())

    result = server.build_docx_analysis_receipt(str(source), fixture_notation_manifest())

    assert result["source_locator"] == "document.docx"
    assert result["components"]["equation_graph"]["status"] == "pass"
    assert result["components"]["integrity"]["status"] == "blocked"


def test_server_evaluates_analysis_gate_without_promotion(tmp_path):
    source = tmp_path / "document.docx"
    source.write_bytes(b"docx")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt = server.build_analysis_receipt(
        str(source),
        equation_graph={"valid": True, "source_fingerprint": source_hash},
    )

    gate = server.evaluate_analysis_promotion_gate(
        receipt,
        required_components=["equation_graph"],
    )

    assert gate["allowed"] is True
    assert gate["status"] == "pass"
