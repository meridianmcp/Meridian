"""Shared BM25-first + Model2Vec second-stage retrieval contract (5044d8eb).

Meridian has FOUR independent search stacks that all follow the same pattern
--- a lexical (BM25 / FTS / tsvector) pass finds candidates, and an OPTIONAL
local Model2Vec pass reranks a BOUNDED subset of them --- but each grew its
own ad-hoc hit shape:

* ``extensions/meridian-codeindex`` --- DuckDB FTS (Okapi BM25) over
  tree-sitter/ast code chunks (:mod:`meridian_codeindex.code_index`).
* ``extensions/meridian-docs`` --- heading-aware BM25 over DOCX chunks
  (:mod:`meridian_docs.docs_intel`).
* ``extensions/meridian-outputs`` --- Tantivy BM25 over CSV/JSON/NPY outputs
  (:mod:`meridian_outputs.search`).
* Meridian core planning records (tasks/notes/decisions/sprint_items) ---
  keyword/tsvector search with an opt-in Model2Vec escalation
  (:mod:`meridian.semantic_search`, :func:`meridian.db.search_all`).

This module defines the COMMON HIT SCHEMA (:class:`RetrievalHit` /
:data:`RETRIEVAL_HIT_FIELDS`) those four stacks converge on, plus two pure,
dependency-free helpers (:func:`fuse_scores`, :func:`build_retrieval_hit`)
so "lexical score, optional semantic score -> one fused score" is computed
identically everywhere instead of drifting per call site.

**Zero-dependency producers stay zero-dependency.** ``meridian_codeindex``,
``meridian_docs``, and ``meridian_outputs`` are each published as standalone
packages with "zero Meridian involvement" as a stated design goal (see
``meridian_codeindex.code_index``'s module docstring). This module MUST NOT
become a hard import those packages need in order to run. A zero-dependency
producer conforms to the contract by building a plain ``dict`` with these
exact field NAMES (duck typing) --- see
``meridian_codeindex.code_index.CodeIndex._row_to_hit`` for the first such
producer, wired 5044d8eb. Only Meridian-core code (which already depends on
this package) imports this module directly.

**Rollout is staged, not simultaneous** (5044d8eb's own notes): codeindex's
BM25-first leg is wired first; heading-aware docs chunks and provenance-rich
outputs are deliberate, separately-scoped follow-ups, not implemented here.
Meridian planning records (``search_all``) already has a proven lexical+
Model2Vec pattern (:func:`meridian.db._maybe_semantic_escalate` +
:func:`meridian.semantic_search.rank_confident`); this module's
:func:`retrieval_hit_from_semantic_match` (in ``semantic_search.py``, not
here, to avoid a reverse import) gives that pattern a bridge into the shared
schema WITHOUT changing ``search_all``'s existing wire response shape --
wiring it into ``search_all`` end-to-end is left as explicit follow-up so a
widely-consumed MCP response shape does not change in this pass.

**pgvector is out of scope by design.** Per the sprint item's own notes:
"Defer pgvector until benchmarks prove a persistent shared multi-tenant
vector corpus is needed." Meridian already has the machinery to make that
decision on real evidence --- ``meridian.db.upsert_vector_index_state`` /
``record_vector_backend_benchmark`` --- this module does not touch it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RETRIEVAL_HIT_FIELDS",
    "FRESHNESS_CURRENT",
    "FRESHNESS_DEGRADED",
    "FRESHNESS_UNKNOWN",
    "PROVENANCE_NOT_TRACKED",
    "PROVENANCE_UNVERIFIED",
    "PROVENANCE_VERIFIED",
    "PROVENANCE_STALE",
    "RetrievalHit",
    "fuse_scores",
    "build_retrieval_hit",
]

# ---------------------------------------------------------------------------
# Fusion weights --- mirrors meridian.semantic_search._FUSE_LEXICAL_WEIGHT /
# _FUSE_SEMANTIC_WEIGHT (2204ce80 / 3d3ccf2d), which itself mirrors
# meridian.db.hybrid_candidate_retrieval's split. Kept as an INDEPENDENT
# constant here (not imported from semantic_search.py) for the same reason
# semantic_search.py gives for its own copy: this module must stay importable
# --- and, more importantly, its FIELD SHAPE must stay conformable-to by
# duck typing --- from contexts that cannot or should not import
# meridian.semantic_search (a zero-Meridian-dependency extension package
# would never import either module, but keeping this module leaf-simple, with
# no internal meridian imports at all, keeps it trivially safe to import from
# ANY meridian-core module without circular-import risk). A change to one
# weight pair is a deliberate, audited decision (pin_decision) that must be
# mirrored to the other by hand, not silently kept in sync by an import.
# ---------------------------------------------------------------------------
FUSE_LEXICAL_WEIGHT = 0.6
FUSE_SEMANTIC_WEIGHT = 0.4

# The common hit schema's field names, in the order the sprint item's notes
# list them: "source, stable ID, lexical score, optional semantic score,
# fused score, structure metadata, content hash, freshness, provenance
# status." Every :class:`RetrievalHit` / :func:`build_retrieval_hit` output
# carries exactly these keys -- a producer without real data for a field
# (e.g. codeindex has no provenance tracking) reports an explicit sentinel
# (see the ``PROVENANCE_*`` / ``FRESHNESS_*`` constants below), never omits
# the key, so every consumer can rely on the full field set being present
# regardless of which of the four producers emitted a given hit.
RETRIEVAL_HIT_FIELDS: tuple[str, ...] = (
    "source",
    "id",
    "lexical_score",
    "semantic_score",
    "fused_score",
    "structure",
    "content_hash",
    "freshness",
    "provenance_status",
)

# -- freshness sentinels -----------------------------------------------------
# "current": this hit's lexical leg has no partial/stale state of its own to
#   track (the common case for a fresh BM25/FTS query -- there is no
#   persisted index that can lag behind the underlying content).
# "degraded": the hit came from a leg with KNOWN partial/stale state (e.g. a
#   vector index mid-rebuild, or a capped semantic-escalation corpus window
#   -- see meridian.semantic_search.is_corpus_capped) and must not be treated
#   as an exhaustive/authoritative answer.
# "unknown": the producer has not (yet) wired freshness tracking. Distinct
#   from "current" -- "unknown" is an honest gap, "current" is a verified
#   claim.
FRESHNESS_CURRENT = "current"
FRESHNESS_DEGRADED = "degraded"
FRESHNESS_UNKNOWN = "unknown"

# -- provenance sentinels -----------------------------------------------------
# "not_tracked": the producer does not track per-hit provenance at all (the
#   codeindex/docs legs, as of 5044d8eb -- provenance-rich outputs is a
#   separate, later stage of this same rollout per the sprint item's notes).
# "unverified": provenance exists but has not been checked against its
#   source for this hit.
# "verified": provenance has been checked and matches its declared source.
# "stale": provenance was checked and found to mismatch/lag its source.
PROVENANCE_NOT_TRACKED = "not_tracked"
PROVENANCE_UNVERIFIED = "unverified"
PROVENANCE_VERIFIED = "verified"
PROVENANCE_STALE = "stale"


def fuse_scores(
    lexical_score: "float | None",
    semantic_score: "float | None",
    *,
    lexical_weight: float = FUSE_LEXICAL_WEIGHT,
    semantic_weight: float = FUSE_SEMANTIC_WEIGHT,
) -> float:
    """Blend a lexical and an optional semantic score into one fused score.

    Pure, DB-free, no I/O. Mirrors
    :func:`meridian.semantic_search.score_confidence`'s own per-candidate
    fusion rule exactly (same default weights, same "no other signal -> fuse
    to whichever one score exists" behavior), so a hit fused here and a
    ``SemanticMatch.fused_score`` computed there are numerically identical
    for the same inputs:

    * both present -> ``lexical_weight * lexical + semantic_weight * semantic``
    * only one present -> that one score, unweighted
    * neither present -> ``0.0`` (a hit with no score at all is not a real
      match; callers should not construct a :class:`RetrievalHit` for one)
    """
    if lexical_score is None and semantic_score is None:
        return 0.0
    if semantic_score is None:
        return float(lexical_score)  # type: ignore[arg-type]
    if lexical_score is None:
        return float(semantic_score)
    return lexical_weight * float(lexical_score) + semantic_weight * float(semantic_score)


@dataclass(frozen=True)
class RetrievalHit:
    """One search result in the shared BM25-first + Model2Vec-rerank schema.

    See the module docstring for the four producers this is meant to unify
    and the staged rollout order. Fields exactly match
    :data:`RETRIEVAL_HIT_FIELDS`.

    ``lexical_score`` and ``semantic_score`` are independently optional
    (a pure-lexical hit has no semantic leg yet; a pure-semantic escalation
    hit --- e.g. :func:`meridian.db._maybe_semantic_escalate`'s keyword-miss
    path --- has no lexical corroboration). ``fused_score`` is never
    optional: every hit that exists has SOME score it was ranked by, even if
    that score equals one of the two component scores verbatim (see
    :func:`fuse_scores`).

    ``structure`` is a free-form dict of producer-specific structural
    metadata (code: path/kind/name/line_start/line_end; docs: heading_path/
    section; outputs: column headers/generating_script). Deliberately NOT a
    fixed sub-schema -- what "structure" means is inherently different per
    producer, and forcing a single shared structure shape would either lose
    information or force irrelevant null fields onto every hit.
    """

    source: str
    id: str
    lexical_score: "float | None"
    semantic_score: "float | None"
    fused_score: float
    structure: "dict[str, Any]" = field(default_factory=dict)
    content_hash: "str | None" = None
    freshness: str = FRESHNESS_UNKNOWN
    provenance_status: "str | None" = PROVENANCE_NOT_TRACKED

    def to_dict(self) -> "dict[str, Any]":
        """Plain-dict projection matching :func:`build_retrieval_hit`'s
        return shape field-for-field, so a caller never has to special-case
        "did this hit come from a RetrievalHit instance or a duck-typed
        producer dict."""
        return {
            "source": self.source,
            "id": self.id,
            "lexical_score": self.lexical_score,
            "semantic_score": self.semantic_score,
            "fused_score": self.fused_score,
            "structure": dict(self.structure),
            "content_hash": self.content_hash,
            "freshness": self.freshness,
            "provenance_status": self.provenance_status,
        }


def build_retrieval_hit(
    *,
    source: str,
    id: str,  # noqa: A002 - matches the schema's field name; API clarity over shadowing avoidance
    lexical_score: "float | None" = None,
    semantic_score: "float | None" = None,
    fused_score: "float | None" = None,
    structure: "dict[str, Any] | None" = None,
    content_hash: "str | None" = None,
    freshness: str = FRESHNESS_UNKNOWN,
    provenance_status: "str | None" = PROVENANCE_NOT_TRACKED,
) -> "dict[str, Any]":
    """Construct a schema-conformant hit dict (see :data:`RETRIEVAL_HIT_FIELDS`).

    Returns a PLAIN DICT, not a :class:`RetrievalHit` instance, so this
    function's output shape is exactly what a zero-Meridian-dependency
    producer would build by hand without importing this module at all ---
    the dict IS the contract. ``fused_score`` is computed via
    :func:`fuse_scores` when not given explicitly; pass it explicitly when
    the caller already has its own fused score (e.g. an RRF-fused hit from
    ``meridian_codeindex.code_index._reciprocal_rank_fusion``, which fuses by
    rank position rather than by :func:`fuse_scores`'s weighted-score blend
    and should not be silently recomputed here).
    """
    if fused_score is None:
        fused_score = fuse_scores(lexical_score, semantic_score)
    return RetrievalHit(
        source=source,
        id=id,
        lexical_score=lexical_score,
        semantic_score=semantic_score,
        fused_score=fused_score,
        structure=dict(structure) if structure else {},
        content_hash=content_hash,
        freshness=freshness,
        provenance_status=provenance_status,
    ).to_dict()
