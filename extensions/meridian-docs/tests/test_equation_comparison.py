from __future__ import annotations

from meridian_docs.equation_comparison import compare_equation_artifacts
from meridian_docs.latex_bridge import latex_to_omml


def test_equal_supported_equations_are_allowed_without_writes():
    latex = r"\frac{a}{b}"
    omml = latex_to_omml(latex).value

    result = compare_equation_artifacts(omml, latex, {"word_punctuation": None})

    assert result.semantic_match is True
    assert result.placement_match is True
    assert result.automatic_insertion_allowed is True
    assert result.as_dict() == compare_equation_artifacts(omml, latex, {"word_punctuation": None}).as_dict()


def test_semantic_mismatch_blocks_automatic_insertion():
    omml = latex_to_omml(r"a+b").value

    result = compare_equation_artifacts(omml, r"a-b")

    assert result.automatic_insertion_allowed is False
    assert any(item["kind"] == "semantic_mismatch" for item in result.findings)


def test_unknown_latex_construct_is_a_hard_loss_finding():
    omml = latex_to_omml(r"x").value

    result = compare_equation_artifacts(omml, r"\\unknownmacro{x}")

    assert result.blocked is True
    assert any(item["kind"] == "unsupported_construct" for item in result.findings)


def test_display_inline_placement_difference_blocks():
    omml = latex_to_omml(r"x").value

    result = compare_equation_artifacts(omml, r"\[x\]")

    assert result.placement_match is False
    assert any(item["kind"] == "placement_mismatch" for item in result.findings)


def test_explicit_latex_style_without_omml_run_properties_is_unresolved():
    omml = latex_to_omml(r"\mathrm{DT}").value

    result = compare_equation_artifacts(omml, r"\mathrm{DT}")

    assert result.typography_match is None
    assert result.blocked is True
    assert any(item["kind"] == "unresolved_typography" for item in result.findings)


def test_malformed_latex_warning_blocks_automatic_insertion():
    omml = latex_to_omml(r"x").value

    result = compare_equation_artifacts(omml, r"x}", {"word_punctuation": None})

    assert result.blocked is True
    assert any(item["kind"] == "lossy_conversion" for item in result.findings)


def test_missing_punctuation_evidence_blocks_automatic_insertion():
    omml = latex_to_omml(r"x").value

    result = compare_equation_artifacts(omml, r"x")

    assert result.punctuation_match is None
    assert result.blocked is True


def test_word_only_typography_is_unresolved():
    omml = '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:rPr><m:sty m:val="p"/></m:rPr><m:t>x</m:t></m:r></m:oMath>'

    result = compare_equation_artifacts(omml, r"x", {"word_punctuation": None})

    assert result.typography_match is None
    assert result.blocked is True


def test_numeric_omml_and_latex_have_the_same_semantic_identity():
    latex = r"x^2"
    omml = latex_to_omml(latex).value

    result = compare_equation_artifacts(omml, latex, {"word_punctuation": None})

    assert result.semantic_match is True
    assert result.word_ir_sha256 == result.latex_ir_sha256
