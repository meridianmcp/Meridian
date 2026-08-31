"""Tests for the semantic notation registry."""
from __future__ import annotations

from meridian_docs import server
from meridian_docs.notation_manifest import (
    normalize_notation_manifest,
    validate_notation_manifest,
)


def test_manifest_is_deterministic_and_accepts_semantic_fields():
    manifest = {
        "version": 2,
        "symbols": [
            {
                "id": "cost.dt",
                "symbol": "C_DT",
                "role": "distance-transform cost",
                "kind": "quantity",
                "scope": "section:3.2",
                "indices": ["named_signal"],
            "typography": {"base": "italic", "subscript": "upright"},
                "preferred_notation": "C_DT, C_Depth",
            }
        ],
    }
    normalized = normalize_notation_manifest(manifest)
    assert normalized["symbols"][0]["scope"] == ["section:3.2"]
    assert normalized["symbols"][0]["typography"]["subscript"] == "upright"
    assert normalized["symbols"][0]["preferred_notation"] == ["C_Depth", "C_DT"]
    assert validate_notation_manifest(manifest)["valid"] is True


def test_preferred_notation_does_not_split_function_arguments():
    normalized = normalize_notation_manifest(
        {"symbols": [{"symbol": "R", "preferred_notation": "R_depth(x,y)"}]}
    )
    assert normalized["symbols"][0]["preferred_notation"] == ["R_depth(x,y)"]


def test_preferred_notation_does_not_split_grouped_subscripts():
    normalized = normalize_notation_manifest(
        {"symbols": [{"symbol": "A", "preferred_notation": "M_{allowed,i}, A_overlap"}]}
    )
    assert normalized["symbols"][0]["preferred_notation"] == [
        "A_overlap",
        "M_{allowed,i}",
    ]


def test_overlapping_glyphs_with_different_roles_are_blocking():
    result = validate_notation_manifest(
        {
            "symbols": [
                {"id": "radius", "symbol": "R", "role": "ray radius", "kind": "scalar"},
                {"id": "recess", "symbol": "R", "role": "depth signal", "kind": "quantity"},
            ]
        }
    )
    assert result["valid"] is False
    assert {finding["type"] for finding in result["findings"]} == {
        "semantic_symbol_collision"
    }


def test_disjoint_scope_reuse_is_reviewable_not_blocking():
    result = validate_notation_manifest(
        {
            "symbols": [
                {"id": "territory", "symbol": "T", "role": "territory", "scope": "section:3.1"},
                {"id": "trench", "symbol": "T", "role": "trench signal", "scope": "section:3.2"},
            ]
        }
    )
    assert result["valid"] is True
    assert result["findings"][0]["type"] == "scoped_symbol_reuse"


def test_explicit_reuse_suppresses_collision_finding():
    result = validate_notation_manifest(
        {
            "symbols": [
                {"id": "a", "symbol": "A", "role": "area", "allow_reuse": True},
                {"id": "b", "symbol": "A", "role": "allowed mask", "allow_reuse": True},
            ]
        }
    )
    assert result["valid"] is True
    assert result["findings"] == []


def test_server_tool_is_read_only_manifest_validation():
    result = server.validate_notation_manifest(
        {"symbols": [{"id": "x", "symbol": "x", "kind": "scalar"}]}
    )
    assert result["valid"] is True
