"""Tests for meridian.retrieval_contract -- the shared BM25-first plus
Model2Vec second-stage retrieval hit schema (sprint item 5044d8eb).

Pure, dependency-free module: no DB, no model2vec, no I/O. Every test here
is a plain unit test.
"""
from __future__ import annotations

import dataclasses

import pytest

from meridian.retrieval_contract import (
    FRESHNESS_CURRENT,
    FRESHNESS_DEGRADED,
    FRESHNESS_UNKNOWN,
    FUSE_LEXICAL_WEIGHT,
    FUSE_SEMANTIC_WEIGHT,
    PROVENANCE_NOT_TRACKED,
    PROVENANCE_STALE,
    PROVENANCE_UNVERIFIED,
    PROVENANCE_VERIFIED,
    RETRIEVAL_HIT_FIELDS,
    RetrievalHit,
    build_retrieval_hit,
    fuse_scores,
)


# ---------------------------------------------------------------------------
# fuse_scores -- pure fusion helper.
# ---------------------------------------------------------------------------


def test_fuse_scores_both_present_uses_weighted_blend():
    fused = fuse_scores(1.0, 1.0)
    assert fused == pytest.approx(FUSE_LEXICAL_WEIGHT + FUSE_SEMANTIC_WEIGHT)
    assert fused == pytest.approx(1.0)


def test_fuse_scores_weights_match_default_06_04_split():
    # Locks in the specific split documented as mirroring
    # meridian.semantic_search._FUSE_LEXICAL_WEIGHT / _FUSE_SEMANTIC_WEIGHT
    # (2204ce80 / 3d3ccf2d) -- a silent drift here would desync the two
    # "independently mirrored" constant pairs without either failing on its
    # own.
    assert FUSE_LEXICAL_WEIGHT == pytest.approx(0.6)
    assert FUSE_SEMANTIC_WEIGHT == pytest.approx(0.4)
    fused = fuse_scores(0.8, 0.2)
    assert fused == pytest.approx(0.6 * 0.8 + 0.4 * 0.2)


def test_fuse_scores_only_lexical_present_passes_through():
    assert fuse_scores(0.75, None) == pytest.approx(0.75)


def test_fuse_scores_only_semantic_present_passes_through():
    assert fuse_scores(None, 0.42) == pytest.approx(0.42)


def test_fuse_scores_neither_present_returns_zero():
    assert fuse_scores(None, None) == 0.0


def test_fuse_scores_respects_custom_weights():
    fused = fuse_scores(1.0, 0.0, lexical_weight=0.9, semantic_weight=0.1)
    assert fused == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# RETRIEVAL_HIT_FIELDS / RetrievalHit -- schema shape.
# ---------------------------------------------------------------------------


def test_retrieval_hit_fields_matches_dataclass_fields():
    # The documented field tuple and the dataclass's actual fields must never
    # drift apart -- this is the "common hit schema" contract itself.
    dc_fields = {f.name for f in dataclasses.fields(RetrievalHit)}
    assert set(RETRIEVAL_HIT_FIELDS) == dc_fields


def test_retrieval_hit_is_frozen():
    hit = RetrievalHit(
        source="test", id="x", lexical_score=1.0, semantic_score=None, fused_score=1.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.source = "mutated"  # type: ignore[misc]


def test_retrieval_hit_to_dict_has_every_schema_field():
    hit = RetrievalHit(
        source="test", id="x", lexical_score=1.0, semantic_score=0.5, fused_score=0.8,
        structure={"path": "a.py"}, content_hash="abc123",
        freshness=FRESHNESS_CURRENT, provenance_status=PROVENANCE_VERIFIED,
    )
    d = hit.to_dict()
    assert set(d.keys()) == set(RETRIEVAL_HIT_FIELDS)
    assert d["structure"] == {"path": "a.py"}


def test_retrieval_hit_to_dict_structure_is_a_copy_not_aliased():
    original_structure = {"path": "a.py"}
    hit = RetrievalHit(
        source="test", id="x", lexical_score=1.0, semantic_score=None, fused_score=1.0,
        structure=original_structure,
    )
    d = hit.to_dict()
    d["structure"]["path"] = "mutated.py"
    assert original_structure["path"] == "a.py"


# ---------------------------------------------------------------------------
# build_retrieval_hit -- the plain-dict constructor.
# ---------------------------------------------------------------------------


def test_build_retrieval_hit_returns_plain_dict_matching_schema():
    hit = build_retrieval_hit(source="code_index", id="chunk-1", lexical_score=4.2)
    assert isinstance(hit, dict)
    assert set(hit.keys()) == set(RETRIEVAL_HIT_FIELDS)


def test_build_retrieval_hit_computes_fused_score_when_omitted():
    hit = build_retrieval_hit(
        source="planning", id="task-1", lexical_score=1.0, semantic_score=1.0,
    )
    assert hit["fused_score"] == pytest.approx(1.0)


def test_build_retrieval_hit_passes_through_explicit_fused_score():
    # An RRF-fused hit (rank-position fusion, not a weighted score blend)
    # must not have its fused_score silently recomputed.
    hit = build_retrieval_hit(
        source="code_index", id="chunk-1", lexical_score=4.2, fused_score=0.031,
    )
    assert hit["fused_score"] == pytest.approx(0.031)


def test_build_retrieval_hit_defaults_are_honest_sentinels_not_fabricated():
    hit = build_retrieval_hit(source="code_index", id="chunk-1")
    assert hit["semantic_score"] is None
    assert hit["content_hash"] is None
    assert hit["freshness"] == FRESHNESS_UNKNOWN
    assert hit["provenance_status"] == PROVENANCE_NOT_TRACKED
    assert hit["structure"] == {}


def test_build_retrieval_hit_structure_dict_is_copied_not_aliased():
    caller_structure = {"path": "a.py"}
    hit = build_retrieval_hit(source="code_index", id="chunk-1", structure=caller_structure)
    hit["structure"]["path"] = "mutated.py"
    assert caller_structure["path"] == "a.py"


@pytest.mark.parametrize(
    "provenance", [PROVENANCE_NOT_TRACKED, PROVENANCE_UNVERIFIED, PROVENANCE_VERIFIED, PROVENANCE_STALE],
)
def test_build_retrieval_hit_accepts_every_provenance_sentinel(provenance):
    hit = build_retrieval_hit(source="outputs", id="f1", provenance_status=provenance)
    assert hit["provenance_status"] == provenance


@pytest.mark.parametrize("freshness", [FRESHNESS_CURRENT, FRESHNESS_DEGRADED, FRESHNESS_UNKNOWN])
def test_build_retrieval_hit_accepts_every_freshness_sentinel(freshness):
    hit = build_retrieval_hit(source="code_index", id="c1", freshness=freshness)
    assert hit["freshness"] == freshness
