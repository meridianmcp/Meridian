from __future__ import annotations

import hashlib

import pytest

from meridian_docs.latex_compile_receipt import (
    LatexCompileReceipt,
    LatexReceiptError,
    build_latex_compile_receipt,
)


def test_receipt_resolves_nested_inputs_and_bibliography_deterministically(tmp_path):
    (tmp_path / "chapters").mkdir()
    (tmp_path / "main.tex").write_text(
        r"\documentclass{article}\input{chapters/one}\bibliography{refs}\begin{document}x\end{document}",
        encoding="utf-8",
    )
    (tmp_path / "chapters" / "one.tex").write_text(r"\input{two}", encoding="utf-8")
    (tmp_path / "chapters" / "two.tex").write_text("y", encoding="utf-8")
    (tmp_path / "refs.bib").write_text("@article{x, title={X}}", encoding="utf-8")

    receipt = build_latex_compile_receipt(tmp_path / "main.tex", status="unavailable")

    assert [item["locator"] for item in receipt.input_files] == ["chapters/one.tex", "chapters/two.tex"]
    assert [item["locator"] for item in receipt.bibliography_files] == ["refs.bib"]
    assert receipt.verify_current_files(tmp_path) == {"stale": False, "reasons": []}
    assert receipt.digest() == LatexCompileReceipt.from_dict(receipt.to_dict()).digest()


def test_input_names_with_a_period_keep_their_stem(tmp_path):
    root = tmp_path / "main.tex"
    child = tmp_path / "chapter.v1.tex"
    root.write_text(r"\input{chapter.v1}", encoding="utf-8")
    child.write_text("x", encoding="utf-8")

    receipt = build_latex_compile_receipt(root)

    assert [item["locator"] for item in receipt.input_files] == ["chapter.v1.tex"]


def test_receipt_json_xml_round_trip_preserves_unknown_fields(tmp_path):
    root = tmp_path / "main.tex"
    root.write_text("x", encoding="utf-8")
    receipt = build_latex_compile_receipt(root, status="degraded", compiler_status="unavailable")
    value = receipt.to_dict()
    value["future_field"] = {"preserve": [1, 2]}

    rebuilt = LatexCompileReceipt.from_xml(LatexCompileReceipt.from_dict(value).to_xml())

    assert rebuilt.to_dict()["future_field"] == {"preserve": [1, 2]}
    assert rebuilt.status == "degraded"


def test_changed_input_is_stale_not_passing(tmp_path):
    root = tmp_path / "main.tex"
    child = tmp_path / "child.tex"
    root.write_text(r"\input{child}", encoding="utf-8")
    child.write_text("before", encoding="utf-8")
    receipt = build_latex_compile_receipt(root, status="degraded", compiler_status="unavailable")

    child.write_text("after", encoding="utf-8")
    result = receipt.verify_current_files(tmp_path)

    assert result["stale"] is True
    assert any("hash mismatch: child.tex" in reason for reason in result["reasons"])


def test_missing_dependency_is_recorded_and_cannot_pass(tmp_path):
    root = tmp_path / "main.tex"
    root.write_text(r"\input{missing}\bibliography{refs}", encoding="utf-8")

    receipt = build_latex_compile_receipt(root, status="degraded", compiler_status="unavailable")

    assert {(item["kind"], item["name"]) for item in receipt.unresolved_dependencies} == {
        ("input", "missing"),
        ("bibliography", "refs"),
    }
    assert receipt.verify_current_files(tmp_path)["stale"] is True
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4\n/Type /Pages\n")
    with pytest.raises(LatexReceiptError, match="every TeX"):
        build_latex_compile_receipt(
            root,
            status="passed",
            compiler_status="available",
            engine="pdflatex",
            command="pdflatex main.tex",
            compiler_log="build completed",
            toolchain_versions={"pdflatex": "1"},
            pdf_path=tmp_path / "main.pdf",
            page_count=1,
        )


def test_unavailable_compiler_cannot_be_recorded_as_passed(tmp_path):
    root = tmp_path / "main.tex"
    root.write_text("x", encoding="utf-8")

    with pytest.raises(LatexReceiptError, match="available compiler"):
        build_latex_compile_receipt(root, status="passed", compiler_status="unavailable", page_count=1)


def test_passed_receipt_binds_pdf_hash_and_page_count(tmp_path):
    root = tmp_path / "main.tex"
    pdf = tmp_path / "main.pdf"
    root.write_text("x", encoding="utf-8")
    pdf.write_bytes(b"%PDF-1.4\n/Type /Pages\n")

    receipt = build_latex_compile_receipt(
        root,
        status="passed",
        compiler_status="available",
        engine="pdflatex",
        command="pdflatex main.tex",
        compiler_log="build completed",
        toolchain_versions={"pdflatex": "1"},
        pdf_path=pdf,
        page_count=1,
    )

    assert receipt.pdf_sha256 == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert receipt.page_count == 1
    assert receipt.verify_current_files(tmp_path) == {"stale": False, "reasons": []}


def test_passed_receipt_requires_complete_compiler_evidence(tmp_path):
    root = tmp_path / "main.tex"
    pdf = tmp_path / "main.pdf"
    root.write_text("x", encoding="utf-8")
    pdf.write_bytes(b"%PDF-1.4\n")

    with pytest.raises(LatexReceiptError, match="complete compiler"):
        build_latex_compile_receipt(
            root,
            status="passed",
            compiler_status="available",
            engine="pdflatex",
            pdf_path=pdf,
            page_count=1,
        )


def test_pdf_changes_make_passed_receipt_stale(tmp_path):
    root = tmp_path / "main.tex"
    pdf = tmp_path / "main.pdf"
    root.write_text("x", encoding="utf-8")
    pdf.write_bytes(b"%PDF-1.4\n")
    receipt = build_latex_compile_receipt(
        root,
        status="passed",
        compiler_status="available",
        engine="pdflatex",
        command="pdflatex main.tex",
        compiler_log="build completed",
        toolchain_versions={"pdflatex": "1"},
        pdf_path=pdf,
        page_count=1,
    )

    pdf.write_bytes(b"%PDF-1.4\nchanged\n")

    result = receipt.verify_current_files(tmp_path)
    assert result["stale"] is True
    assert any("PDF hash mismatch" in reason for reason in result["reasons"])
