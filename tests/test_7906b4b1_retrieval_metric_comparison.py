"""Regression coverage for sprint item 7906b4b1 — INVESTIGATE: compare BM25,
cosine similarity, normalized Euclidean distance, and hybrid score fusion for
Meridian retrieval.

Investigation findings (recorded in full via pin_decision against this
project; this module locks in the falsifiable, testable claims):

Meridian's retrieval surface already runs FOUR independently-evolved lexical/
semantic scoring paths, no single one of which was "invented" by this item:

  1. ``meridian/db/__init__.py`` planning search (``_planning_bm25_scores`` /
     ``_planning_sqlite_bm25_rank``) — BM25 (SQLite FTS5 native ``bm25()``,
     or a locally computed Okapi BM25 fallback when FTS5 is absent; Postgres
     uses ``ts_rank`` over ``websearch_to_tsquery``) — pure lexical, no
     vector leg.
  2. ``meridian/db/__init__.py`` ``hybrid_candidate_retrieval`` +
     ``meridian/semantic_search.py`` — pg_trgm ``similarity()`` (lexical, on
     Postgres; an OR-ranked substring match on SQLite) fused with Model2Vec
     cosine similarity (semantic) via a LINEAR weighted sum
     (``_HYBRID_LEXICAL_WEIGHT`` * lexical_norm + ``_HYBRID_SEMANTIC_WEIGHT``
     * semantic in db.py, mirrored 1:1 by ``_FUSE_LEXICAL_WEIGHT`` /
     ``_FUSE_SEMANTIC_WEIGHT`` in semantic_search.py — see
     ``test_hybrid_fusion_weights_are_shared_...`` below).
  3. ``extensions/meridian-codeindex/meridian_codeindex/code_index.py`` —
     DuckDB FTS5 BM25 (``match_bm25``) fused with DuckDB VSS HNSW cosine
     distance (``array_cosine_distance``) via Reciprocal Rank Fusion
     (``_reciprocal_rank_fusion``, k=60) — a RANK-based fusion, not a
     score-based one, specifically because BM25's unbounded score and cosine
     distance's 0..2 range are not directly comparable without calibration.
  4. ``extensions/meridian-docs/meridian_docs/docs_intel.py`` and
     ``extensions/meridian-outputs/meridian_outputs/outputs_local.py`` — BM25
     only (SQLite FTS5 external-content table; Tantivy respectively). Neither
     extension has a semantic/vector leg today.

Normalized Euclidean distance is NOT used anywhere in the codebase. The tests
below establish the concrete mathematical reason: every semantic leg above
L2-normalizes its vectors before scoring
(``semantic_search._l2_normalize``/``_l2_normalize_rows``; DuckDB's
``cosine`` VSS metric is likewise scale-invariant by construction). For unit
vectors ``a``, ``b``::

    ||a - b||^2 = ||a||^2 + ||b||^2 - 2 a.b = 2 - 2 cos(a, b)

so ranking by ascending normalized-Euclidean-distance over the SAME
normalized vectors is an exact, strictly monotonic transform of ranking by
descending cosine similarity — the two produce IDENTICAL orderings and
IDENTICAL floor decisions over the same candidate set. Introducing a
normalized-Euclidean-distance scoring option would duplicate
``semantic_search.rank()``'s existing behavior for zero retrieval-quality
benefit. The investigation's recommendation is to keep cosine similarity as
Meridian's sole vector-distance metric and NOT add a Euclidean-distance code
path; the open FEAT items that build on this (5044d8eb "shared BM25-first
plus Model2Vec second-stage retrieval contract", e5a7ce7f "declarative
deterministic tool-routing rules") should pick BM25 (lexical) + cosine
(semantic) with an explicit, documented choice of linear-fusion (in-domain,
calibrated scores) vs. RRF (cross-domain, uncalibrated scores) per call site
rather than introducing a third distance metric.

No production code was changed by this investigation.
"""

from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import semantic_search as ss
from meridian.semantic_search import _l2_normalize, _l2_normalize_rows, score_confidence


# ---------------------------------------------------------------------------
# Normalized Euclidean distance vs. cosine similarity — exact equivalence for
# L2-normalized vectors (the only kind Meridian's semantic legs ever score).
# ---------------------------------------------------------------------------


def test_normalized_euclidean_ranking_matches_cosine_ranking():
    """Ranking by cosine similarity (descending) and by Euclidean distance
    (ascending) over the SAME L2-normalized vectors yields an identical
    order — proving normalized Euclidean distance offers no retrieval-quality
    difference from the cosine similarity
    ``semantic_search.SemanticSearcher.rank`` already uses.
    """
    np = pytest.importorskip("numpy")

    query = np.array([1.0, 0.3, -0.2], dtype="float64")
    candidates = {
        "near": np.array([0.9, 0.4, -0.1], dtype="float64"),
        "mid": np.array([0.2, 0.9, 0.1], dtype="float64"),
        "far": np.array([-1.0, 0.1, 0.5], dtype="float64"),
        "orthogonal": np.array([0.0, 0.0, 1.0], dtype="float64"),
        "opposite": np.array([-1.0, -0.3, 0.2], dtype="float64"),
    }

    q = _l2_normalize(query)
    ids = list(candidates.keys())
    mat = np.vstack([candidates[i] for i in ids])
    c = _l2_normalize_rows(mat)

    cosine = c @ q
    euclidean = np.linalg.norm(c - q, axis=1)

    # Exact algebraic identity for unit vectors: d^2 == 2 - 2*cos.
    assert np.allclose(euclidean ** 2, 2.0 - 2.0 * cosine, atol=1e-9)

    order_by_cosine_desc = [ids[i] for i in np.argsort(-cosine)]
    order_by_euclidean_asc = [ids[i] for i in np.argsort(euclidean)]
    assert order_by_cosine_desc == order_by_euclidean_asc


def test_normalized_euclidean_floor_is_equivalent_to_cosine_floor():
    """A cosine-similarity floor (``semantic_search.cosine_floor`` /
    ``rank()``'s ``floor`` kwarg) has an exact Euclidean-distance-ceiling
    counterpart for L2-normalized vectors:
    ``cos >= floor  <=>  euclidean <= sqrt(2 - 2*floor)``. Confirms a
    hypothetical distance-based reimplementation of the confidence floor
    (score_confidence's absolute-confidence gate) would admit/reject exactly
    the same candidates as the cosine floor already in production.
    """
    np = pytest.importorskip("numpy")

    floor = 0.37
    euclidean_ceiling = float(np.sqrt(2.0 - 2.0 * floor))

    q = _l2_normalize(np.array([1.0, 0.0], dtype="float64"))
    vecs = {
        "above": np.array([0.98, 0.20], dtype="float64"),  # cosine ~0.98
        "at_edge": np.array([floor, (1 - floor ** 2) ** 0.5], dtype="float64"),
        "below": np.array([0.10, 0.995], dtype="float64"),  # cosine ~0.10
    }
    for _cid, v in vecs.items():
        cv = _l2_normalize(v)
        cosine = float(cv @ q)
        euclidean = float(np.linalg.norm(cv - q))
        # Symmetric epsilon on both sides of the boundary check — avoids a
        # spurious mismatch from floating-point rounding exactly AT the edge.
        assert (cosine >= floor - 1e-9) == (euclidean <= euclidean_ceiling + 1e-9)


# ---------------------------------------------------------------------------
# Hybrid score fusion — the two linear-fusion call sites share one contract.
# ---------------------------------------------------------------------------


def test_hybrid_fusion_weights_are_shared_between_semantic_search_and_db_hybrid_retrieval():
    """``meridian.semantic_search`` and ``meridian.db.hybrid_candidate_retrieval``
    are two independent linear-fusion call sites that deliberately mirror the
    SAME 0.6/0.4 lexical/semantic split (see the comment above
    ``semantic_search._FUSE_LEXICAL_WEIGHT``) rather than each tuning its own
    constant. Lock the mirror in so a future edit to one side can't silently
    desync from the other.
    """
    assert ss._FUSE_LEXICAL_WEIGHT == db_module._HYBRID_LEXICAL_WEIGHT
    assert ss._FUSE_SEMANTIC_WEIGHT == db_module._HYBRID_SEMANTIC_WEIGHT
    assert ss._FUSE_LEXICAL_WEIGHT + ss._FUSE_SEMANTIC_WEIGHT == pytest.approx(1.0)


def test_score_confidence_fused_score_equals_semantic_when_no_lexical_signal():
    """Baseline sanity check on the fusion contract ``score_confidence``
    relies on: a candidate with no lexical corroboration fuses to its raw
    cosine (semantic) score — hybrid fusion degrades gracefully to pure
    cosine-similarity ranking when only the semantic leg has data, exactly
    the keyword-miss escalation path this investigation traced through
    ``meridian.db._maybe_semantic_escalate``.
    """
    ranked = [("a", 0.9), ("b", 0.5)]
    matches = score_confidence(ranked, floor=0.0, min_margin=0.0)
    by_id = {m.id: m for m in matches}
    assert by_id["a"].fused_score == by_id["a"].semantic_score
    assert by_id["b"].fused_score == by_id["b"].semantic_score
