from __future__ import annotations

import hashlib

import pytest

from meridian_docs.math_ir import EquationArtifact, make_node, normalize_math_tree, opaque, sequence
from meridian_docs.latex_bridge import latex_to_omml, source_to_equation_artifact


def _artifact(**overrides):
    source = r"\frac{DT}{2}"
    values = {
        "equation_id": "eq-1",
        "document_id": "doc-1",
        "source_format": "latex",
        "source_hash": hashlib.sha256(source.encode()).hexdigest(),
        "semantic_tree": make_node("fraction", make_node("symbol", text="DT"), make_node("number", text="2")),
        "placement": "display",
        "punctuation_ownership": "surrounding_prose",
        "punctuation": ".",
        "typography_roles": {"/0": ["upright", "abbreviation"], "/1": ["number"]},
        "source_span": {
            "source_file": "main.tex",
            "start_offset": 120,
            "end_offset": 130,
            "start_line": 12,
            "end_line": 12,
        },
        "paragraph_anchor": "p-12",
        "warnings": ("style inferred from source",),
        "loss_flags": ("omml_run_properties_unavailable",),
        "supersedes": (),
        "superseded_by": (),
    }
    values.update(overrides)
    return EquationArtifact(**values)


def test_equation_artifact_is_deterministic_and_distinguishes_identity_from_digest():
    artifact = _artifact()
    rebuilt = EquationArtifact.from_dict(artifact.to_dict())

    assert rebuilt.artifact_id == "5:doc-14:eq-1"
    assert rebuilt.digest() == artifact.digest()
    assert rebuilt.typography_roles == (("/0", ("abbreviation", "upright")), ("/1", ("number",)))
    assert rebuilt.validate() == []


def test_equation_artifact_preserves_unknown_fields():
    value = _artifact().to_dict()
    value["future_extension"] = {"kind": "manual-review", "severity": 2}

    rebuilt = EquationArtifact.from_dict(value)

    assert rebuilt.to_dict()["future_extension"] == {"kind": "manual-review", "severity": 2}


def test_unknown_json_lists_are_not_reinterpreted_as_mappings():
    value = _artifact().to_dict()
    value["future_extension"] = [["x", 1], ["y", 2]]

    rebuilt = EquationArtifact.from_dict(value)

    assert rebuilt.to_dict()["future_extension"] == [["x", 1], ["y", 2]]


def test_unknown_fields_cannot_overwrite_canonical_identity():
    with pytest.raises(ValueError, match="canonical fields"):
        _artifact(unknown_fields={"equation_id": "other"})


def test_normalization_preserves_single_cell_matrix_rows_and_nested_opaque_blocks():
    row = sequence([make_node("symbol", text="x")])
    matrix = make_node("matrix", row)

    normalized = normalize_math_tree(matrix)

    assert normalized.children[0].kind == "sequence"
    assert "opaque" in " ".join(_artifact(semantic_tree=sequence([opaque("loss")])).validate())


def test_equation_artifact_rejects_non_sha256_source_hash():
    with pytest.raises(ValueError, match="source_hash"):
        _artifact(source_hash="not-a-digest")


def test_equation_artifact_reports_punctuation_and_loss_invariants():
    assert "punctuation cannot" in _artifact(punctuation_ownership="none").validate()[0]
    assert "loss_flags require" in _artifact(warnings=()).validate()[-1]


def test_bridge_wraps_latex_and_native_omml_in_the_same_artifact_schema():
    latex = r"\frac{a}{b}"
    kwargs = {
        "equation_id": "eq-1",
        "document_id": "doc-1",
        "placement": "display",
    }
    from_latex = source_to_equation_artifact(latex, "latex", **kwargs)
    from_omml = source_to_equation_artifact(latex_to_omml(latex).value, "omml", **kwargs)

    assert from_latex.success is True
    assert from_omml.success is True
    assert from_latex.value["semantic_tree"] == from_omml.value["semantic_tree"]
    assert from_latex.value["equation_id"] == "eq-1"


def test_bridge_artifact_retains_unsupported_loss_and_blocks_success():
    result = source_to_equation_artifact(
        r"\unknownmacro{x}",
        "latex",
        equation_id="eq-1",
        document_id="doc-1",
        placement="inline",
    )

    assert result.success is False
    assert result.unsupported
    assert result.value["loss_flags"]
