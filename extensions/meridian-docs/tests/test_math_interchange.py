"""Tests for the bounded math interchange layer."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from meridian_docs.document_profile import (
    DocumentProfileError,
    merge_document_profiles,
    normalize_document_profile,
    profile_digest,
)
from meridian_docs.latex_bridge import (
    ir_to_latex,
    latex_to_ir,
    latex_to_omml,
    omml_to_ir,
    omml_to_latex,
)
from meridian_docs.math_ir import from_dict, make_node, opaque
from meridian_docs.notation_rules import apply_notation_rules, normalize_notation_rules
from meridian_docs import server
from meridian_docs.docs_intel import latex_to_omml_local


OMML = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _simple_omml() -> str:
    return (
        f'<m:oMath xmlns:m="{OMML}">'
        '<m:f><m:num><m:e><m:r><m:t>a</m:t></m:r></m:e></m:num>'
        '<m:den><m:e><m:r><m:t>b</m:t></m:r></m:e></m:den></m:f>'
        '<m:sSup><m:e><m:r><m:t>x</m:t></m:r></m:e>'
        '<m:sup><m:e><m:r><m:t>2</m:t></m:r></m:e></m:sup></m:sSup>'
        '</m:oMath>'
    )


def test_math_ir_is_immutable_and_digest_is_stable():
    node = make_node("fraction", make_node("symbol", text="a"), make_node("symbol", text="b"))
    rebuilt = from_dict(node.to_dict())

    assert rebuilt == node
    assert rebuilt.digest() == node.digest()
    assert node.validate() == []


def test_opaque_node_is_explicit_and_serializable():
    node = opaque("unsupported matrix feature", source_format="omml", raw_digest="abc")

    assert node.to_dict()["kind"] == "opaque"
    assert node.attrs["source_format"] == "omml"
    assert node.validate() == []


def test_latex_to_ir_reports_common_constructs():
    result = latex_to_ir(r"\frac{a_1}{b^2}")

    assert result.success is True
    assert result.ir_sha256
    assert result.value["kind"] == "fraction"
    assert result.unsupported == ()


def test_latex_nary_scripts_keep_nary_semantics():
    result = latex_to_omml(r"\sum_{i=1}^{n} x_i")

    assert result.success is True
    root = ET.fromstring(result.value)
    assert any(element.tag.rsplit("}", 1)[-1] == "nary" for element in root.iter())


def test_square_root_does_not_round_trip_hidden_degree_as_empty_index():
    result = omml_to_latex(latex_to_omml(r"\sqrt{x}").value)

    assert result.success is True
    assert r"\sqrt[]" not in result.value


def test_latex_to_omml_emits_native_omml():
    result = latex_to_omml(r"\sqrt{x^2}")

    assert result.success is True
    assert result.value.startswith('<ns0:oMath') or result.value.startswith('<m:oMath')
    assert result.result_sha256


def test_existing_docs_converter_prefers_structural_bridge_for_fraction():
    raw = latex_to_omml_local(r"\frac{a}{b}")

    assert raw is not None
    root = ET.fromstring(raw)
    assert any(element.tag.rsplit("}", 1)[-1] == "f" for element in root.iter())


def test_mcp_conversion_tool_is_exposed_without_document_writes():
    result = server.convert_equation(r"\frac{a}{b}", "latex", "omml")

    assert result["success"] is True
    assert result["target_format"] == "omml"
    assert "<" in result["value"]


def test_omml_to_latex_preserves_fraction_and_superscript():
    result = omml_to_latex(_simple_omml())

    assert result.success is True
    assert r"\frac" in result.value
    assert "^2" in result.value or "^{2}" in result.value
    assert result.unsupported == ()


def test_omml_to_ir_accepts_display_wrapper_for_inspection():
    wrapped = f'<m:oMathPara xmlns:m="{OMML}">{_simple_omml()}</m:oMathPara>'
    result = omml_to_ir(wrapped)

    assert result.success is True
    assert any("wrapper accepted" in warning for warning in result.warnings)


def test_unsupported_omml_is_reported_as_lossy_not_flattened_silently():
    raw = f'<m:oMath xmlns:m="{OMML}"><m:unknown><m:r><m:t>x</m:t></m:r></m:unknown></m:oMath>'
    result = omml_to_latex(raw)

    assert result.success is False
    assert result.unsupported
    assert result.lossy is True
    assert "unsupported math" in result.value


def test_malformed_omml_fails_closed():
    raw = f'<m:oMath xmlns:m="{OMML}"><m:f><m:num /></m:f></m:oMath>'
    result = omml_to_ir(raw)

    assert result.success is False
    assert any("missing" in warning for warning in result.warnings)


def test_ir_to_latex_marks_opaque_nodes_lossy():
    result = ir_to_latex(opaque("unsupported", source_format="omml"))

    assert result.success is False
    assert result.lossy is True
    assert r"\text{[unsupported math]}" == result.value


def test_unknown_formats_fail_even_when_they_match():
    from meridian_docs.latex_bridge import convert_equation

    result = convert_equation("x", "svg", "svg")

    assert result.success is False
    assert "unsupported format" in result.warnings[0]


def test_notation_rule_pack_is_deterministic_and_can_ignore_advisory_findings():
    assert normalize_notation_rules({"alias_used": "ignore"})["alias_used"] == "ignore"
    findings = [{"type": "alias_used", "severity": "warning"}, {"type": "missing_symbol", "severity": "error"}]

    assert apply_notation_rules(findings, {"alias_used": "ignore"}) == [findings[1]]


def test_document_profile_merges_without_paths_or_credentials():
    profile = merge_document_profiles(
        None,
        {"equations": {"numbering": "section"}, "style": {"math_font": "Cambria Math"}},
    )

    assert profile["equations"]["numbering"] == "section"
    assert profile["equations"]["representation"] == "native_omml"
    assert profile_digest(profile) == profile_digest(profile)


def test_document_profile_rejects_machine_local_paths():
    try:
        normalize_document_profile({"style": {"template": r"C:\\temp\\template.dotx"}})
    except DocumentProfileError as exc:
        assert "machine-local paths" in str(exc)
    else:
        raise AssertionError("machine-local profile paths must be rejected")


def test_document_profile_canonicalizes_version_type():
    assert profile_digest({"version": 1}) == profile_digest({"version": "1"})
