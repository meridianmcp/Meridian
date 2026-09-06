"""meridian_outputs.search -- fielded local ranking with deterministic
exact-match retrieval.

Sprint item a444313d, building on c6236ef4's literal-filename-match boost
(itself a thin wrapper over :func:`outputs_local.search_outputs`'s
DuckDB/Tantivy BM25 engine -- see that item's own investigation for why
BM25's length-normalized term-frequency scoring alone can rank a longer,
decoy-suffixed file ABOVE its true exact-match canonical twin).

Prior state: c6236ef4 closed the single worst case (a literal basename
substring match) with a binary reorder -- "any literal match precedes any
non-match" -- but had no notion of DEGREE of match (exact filename vs. exact
stem vs. a phrase inside a longer name), no field separation beyond
basename (a query matching a CSV column header, a generating-script name, or
a directory-level provenance note got no credit at all), and no overfetch --
a genuinely relevant file ranked outside the raw BM25 top-`limit` could never
be recovered by reranking, because it was never even fetched.

What this module adds
----------------------
Still delegates ALL indexing/BM25 scoring to :func:`outputs_local.
search_outputs` (unchanged, not duplicated -- ``OutputsFtsIndex`` is a large,
stateful, cached, lock-guarded class; forking it would be a maintenance
hazard). On top of that, this module adds:

  1. Distinct FIELD SIGNALS computed from data each hit already carries (no
     new indexing, no Tantivy schema change, no per-hit extra I/O beyond
     what ``outputs_local.search_outputs`` already fetches):
       - filename / filename-stem (from ``hit["path"]``'s basename)
       - relative_path (``hit["path"]`` relative to ``outputs_dir``)
       - script (``hit["generating_script"]``)
       - metadata (``hit["csv_columns"]`` + ``hit["json_keys"]``)
       - provenance (:func:`annotate.get_provenance`'s ``note`` field --
         deliberately the DEDICATED per-file provenance record this
         package's ``annotate`` module tracks, not ``outputs_local``'s
         separate, broader directory-level/tool-note ``annotations`` table;
         see :mod:`provenance_status`'s own module docstring for why this
         codebase treats those as two distinct systems. One
         :func:`annotate.get_provenance` call per hit -- cheap, since
         fa600e42 added an mtime/size-based cache to the underlying ledger
         read, so this is one dict lookup against an already-cached parse,
         not a fresh full-ledger scan per hit)
     ``body`` is NOT separately re-scored here -- Tantivy's own BM25 already
     rewards body term/phrase co-occurrence; re-deriving that in Python would
     double-count the same signal, not add a new one.

     Genuine limitation, stated plainly: every boost here can only RERANK a
     hit Tantivy's own BM25 retrieval already returned -- filename/relative-
     path/metadata are baked into the SAME indexed ``content`` field
     ``outputs_local`` already searches, so a match on those always makes a
     hit at least minimally retrievable. The provenance field is different:
     ``annotate.py``'s ledger is a wholly separate store the FTS engine
     never indexes, so a hit whose ONLY connection to the query is its
     provenance note is never fetched by Tantivy in the first place, and no
     amount of post-hoc reranking can recall it. The provenance boost can
     PROMOTE an already-retrieved (if weakly-connected) hit; it cannot
     RECALL one with zero other connection to the query at all.

  2. Explicit EXACT-MATCH / PHRASE boosts, ranked by specificity (a query
     that IS basically a filename should outrank pure BM25 token overlap,
     matching how a human doing a manual hunt would trust an exact name over
     a loose relevance score): exact filename > exact stem > filename phrase
     > relative-path phrase, each additive with a script-name match and
     token-overlap bonuses for metadata/provenance. See ``_field_boost``.

  3. OVERFETCH before filter/rerank: this module always asks
     ``outputs_local.search_outputs`` for more candidates than the caller
     requested (bounded, see ``_OVERFETCH_FACTOR``/``_OVERFETCH_MAX_EXTRA``),
     WITH archival results included regardless of the caller's own
     ``include_archival`` preference. Reranking and archival filtering both
     happen on that larger pool, THEN the result is truncated to the
     caller's requested ``limit``. This closes two confirmed gaps in the
     undecorated ``outputs_local.search()``: (a) a true match ranked outside
     the raw top-``limit`` by BM25 alone was never even fetched, so no
     amount of reranking could recover it; (b) ``include_archival=False``
     hard-excluded archival rows AFTER only ``limit`` candidates had been
     fetched, so a query whose top raw hits were mostly archival silently
     returned fewer than ``limit`` results instead of backfilling from
     lower-ranked non-archival candidates.

  4. Deterministic TIE-BREAK: every hit gets a composite
     ``final_score = bm25-derived score + field boost``; ties (increasingly
     likely once boosts are discrete, additive bonuses rather than a
     continuous BM25 float) are broken by normalized relative path, then by
     the raw absolute path as a last-resort stable identity -- so two calls
     against an UNCHANGED tree always return hits in the exact same order,
     never leaving tie order to incidental DuckDB/Tantivy row iteration.

  5. NO embeddings, NO LLM reranker -- every signal above is a plain lexical
     substring/token comparison against fields the index already returns.
     This is a deliberate scope boundary for this item, not an oversight.

Every additive bonus above is added ON TOP of the hit's existing BM25-
derived ``score`` -- never replaces it. A hit that matches nothing in any of
the new fields keeps its exact pre-existing rank relative to other such
hits, so c6236ef4's own "no literal match falls back to pure BM25 order"
contract still holds for the case where nothing new applies.

Degraded-state signaling (item e631d54f) is untouched: ``degraded``,
``convergence``, ``partial``, ``fts_pending``, ``pending_stale_count``,
``db_write_error``, ``tantivy_lock_warning``, ``index_lock_warning``,
``zero_hits_warning``, ``total_indexed``, ``total_in_index`` all pass
through from ``outputs_local.search_outputs`` UNCHANGED -- this module only
ever reorders/filters/truncates ``hits``, never recomputes or infers any of
those fields itself. An ``error`` response or a genuinely empty ``hits``
list (nothing indexed matched at all, even before overfetch) is returned
unchanged, exactly as c6236ef4 already did.

Benchmark data (a query fixture with known-relevant files, precision/
recall@k, MRR/nDCG, cold/warm latency, degraded-index behavior) lives in the
companion research repository per this item's own scope instruction, NOT
here -- this module and its tests validate ranking LOGIC in isolation
(synthetic fixtures, exact assertions), not aggregate retrieval-quality
metrics against a real corpus.
"""
from __future__ import annotations

import os
import re
from typing import Any

from . import annotate, outputs_local

__all__ = ["search_outputs"]

# Separator-agnostic normalization so "radius_sweep_130", "radius-sweep-130"
# and "radius sweep 130" are all treated as the same literal needle when
# compared against a basename/path -- matching how a human doing a manual
# hunt mentally treats filename separators as interchangeable.
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_ALNUM_RUN_RE = re.compile(r"[0-9a-z]+")


def _normalize(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower())


def _normalize_tokens(text: str) -> list[str]:
    """Lowercase alnum-run tokens, e.g. "loss_curve v2" -> ["loss","curve","v2"]."""
    return _ALNUM_RUN_RE.findall(text.lower())


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _relative_path(path: str, outputs_dir: str) -> str:
    """``path`` relative to ``outputs_dir``, forward-slashed for consistent
    normalization. Falls back to ``path`` itself if ``os.path.relpath``
    can't express the relationship (e.g. different drives on Windows) --
    this is a ranking signal, not an identity check, so a degraded-but-safe
    fallback is preferable to raising.
    """
    try:
        rel = os.path.relpath(path, outputs_dir)
    except ValueError:
        rel = path
    return rel.replace("\\", "/")


# Boost weights, highest-confidence signal first. Each additive bonus is
# added ON TOP of a hit's existing BM25-derived `score` -- see module
# docstring point 4. Chosen so a query that IS basically a filename always
# outranks pure BM25 token-overlap (matching a manual literal-substring
# hunt's own trust ordering -- see the c6236ef4 investigation this module
# builds on), without being so large that a real, highly-relevant body-text
# match can never compete with an incidental filename/metadata token hit.
_EXACT_FILENAME_BOOST = 100.0
_EXACT_STEM_BOOST = 60.0
_FILENAME_PHRASE_BOOST = 40.0
_RELATIVE_PATH_PHRASE_BOOST = 25.0
_SCRIPT_MATCH_BOOST = 15.0
_METADATA_TERM_BOOST = 8.0
_PROVENANCE_TERM_BOOST = 5.0
# Caps so a file with an unusually large CSV header or a long free-form
# annotation note can't accumulate an outsized bonus purely from volume.
_MAX_METADATA_TERMS_COUNTED = 5
_MAX_PROVENANCE_TERMS_COUNTED = 5

# How many extra candidates to fetch beyond the caller's requested `limit`
# (see module docstring point 3). Two tiers, not one -- measured live
# (code review): the dominant cost of overfetching is NOT the per-hit
# annotate.get_provenance lookup (~0.4ms each, and cached), it is
# outputs_local's own DuckDB `WHERE path IN (...)` hydration + JSON-decode
# of csv_columns/json_keys for every extra row -- fetching 5x candidates
# capped at +200 measured 3.8x-4.6x slower end-to-end than the unwrapped
# call on synthetic trees of a few hundred to a few thousand files, for the
# NOW-LIVE `search_outputs` MCP tool (server.py delegates here directly).
# The archival-backfill benefit (point 3b) only matters when the caller
# actually asks for `include_archival=False`; the common default
# (`include_archival=True`) only needs enough headroom for reranking
# (point 3a: a true match ranked outside the raw top-`limit` must still be
# fetched to be recoverable) and pays a much smaller tax for it.
_OVERFETCH_FACTOR = 3
_OVERFETCH_MAX_EXTRA = 30
_ARCHIVAL_OVERFETCH_FACTOR = 8
_ARCHIVAL_OVERFETCH_MAX_EXTRA = 100


def _provenance_note_tokens(outputs_dir: str, path: str) -> set[str]:
    """Normalized tokens of :func:`annotate.get_provenance`'s ``note`` field
    for ``path``, or an empty set if never provenance-tagged. Never raises --
    a lookup failure just means "no provenance-field signal", not an error.
    """
    try:
        record = annotate.get_provenance(outputs_dir, path)
    except Exception:  # noqa: BLE001 -- best-effort ranking signal only
        return set()
    note = (record or {}).get("note")
    if not note:
        return set()
    return set(_normalize_tokens(str(note)))


def _field_boost(
    query_norm: str,
    query_tokens: list[str],
    hit: dict[str, Any],
    outputs_dir: str,
) -> tuple[float, bool]:
    """Composite field-boost bonus for one hit, plus the legacy
    ``literal_match`` flag (c6236ef4: literal substring in basename) kept
    for backward compatibility with existing callers/tests.

    Returns ``(boost, literal_match)``. ``boost`` is 0.0 whenever nothing
    matches in any field -- see module docstring's "falls back to pure BM25
    order" guarantee.
    """
    if not query_norm:
        return 0.0, False

    path = hit.get("path") or ""
    filename = _basename(path)
    filename_norm = _normalize(filename)
    stem_norm = _normalize(os.path.splitext(filename)[0])
    rel_norm = _normalize(_relative_path(path, outputs_dir))
    literal_match = query_norm in filename_norm

    boost = 0.0
    # Filename-based tiers are mutually exclusive (a query is either an
    # exact full-name match, an exact stem match, a phrase inside the name,
    # or a phrase inside the wider relative path -- never more than one of
    # these at once for the SAME comparison), so this is an elif chain, not
    # independently additive like the signals below.
    if filename_norm == query_norm:
        boost += _EXACT_FILENAME_BOOST
    elif stem_norm == query_norm:
        boost += _EXACT_STEM_BOOST
    elif query_norm in filename_norm:
        boost += _FILENAME_PHRASE_BOOST
    elif query_norm in rel_norm:
        boost += _RELATIVE_PATH_PHRASE_BOOST

    script = hit.get("generating_script")
    if script and query_norm in _normalize(str(script)):
        boost += _SCRIPT_MATCH_BOOST

    if query_tokens:
        metadata_terms = list(hit.get("csv_columns") or []) + list(hit.get("json_keys") or [])
        metadata_norm = {_normalize(str(t)) for t in metadata_terms if t}
        matched_metadata = sum(1 for tok in query_tokens if tok in metadata_norm)
        if matched_metadata:
            boost += _METADATA_TERM_BOOST * min(matched_metadata, _MAX_METADATA_TERMS_COUNTED)

        provenance_tokens = _provenance_note_tokens(outputs_dir, path)
        if provenance_tokens:
            matched_provenance = sum(1 for tok in query_tokens if tok in provenance_tokens)
            if matched_provenance:
                boost += _PROVENANCE_TERM_BOOST * min(
                    matched_provenance, _MAX_PROVENANCE_TERMS_COUNTED,
                )

    return boost, literal_match


def search_outputs(
    outputs_dir: str,
    query: str,
    *,
    limit: int = 10,
    include_archival: bool = True,
    max_seconds: float | None = outputs_local.DEFAULT_REBUILD_BUDGET_SECONDS,
    subtree: str | None = None,
) -> dict[str, Any]:
    """Fielded local ranking with deterministic exact-match retrieval.

    Delegates all indexing/BM25 scoring to :func:`outputs_local.
    search_outputs` -- this function changes what CANDIDATES get fetched
    (overfetch, see module docstring point 3) and how the resulting hits are
    scored/ordered/filtered; it never invents a hit the underlying index
    doesn't already know about.

    Args:
      outputs_dir:      Absolute path to the outputs directory to index.
      query:            Search query string (same semantics as
                        ``outputs_local.search_outputs``).
      limit:            Maximum number of hits to return (default 10) --
                        applied AFTER overfetch/rerank/filter, so this is
                        exactly how many hits the caller gets, same as
                        before.
      include_archival: Include archival-flagged files in the FINAL result
                        (default True). Internally this module always
                        overfetches WITH archival included, then applies
                        this filter itself post-rerank -- see module
                        docstring point 3 for why (archival backfill).
      max_seconds:      Rebuild wall-clock budget -- passed straight
                        through (same default as ``outputs_local``).
      subtree:          Optional sub-path of ``outputs_dir`` to scope
                        indexing/searching to -- passed straight through.

    Returns:
      Identical shape to ``outputs_local.search_outputs``'s return value,
      with ``hits`` reranked/filtered/truncated as described above. Every
      hit keeps its existing fields and gains ``literal_match: bool``
      (c6236ef4, kept for backward compatibility). All other top-level
      fields (``degraded``, ``convergence``, ``partial``, ``fts_pending``,
      ``pending_stale_count``, ``db_write_error``, ``tantivy_lock_warning``,
      ``index_lock_warning``, ``zero_hits_warning``, ``total_indexed``,
      ``total_in_index``) pass through UNCHANGED -- reordering/filtering
      hits never changes whether the underlying index has converged, so a
      caller doing an authoritative existence/dedup check must still honor
      ``degraded`` here exactly as on the undecorated call. On error (missing
      dir, empty query, etc.) or a genuinely empty raw hit set, the result is
      returned unchanged -- no reranking is attempted.
    """
    if include_archival:
        overfetch_limit = max(
            limit, min(limit * _OVERFETCH_FACTOR, limit + _OVERFETCH_MAX_EXTRA),
        )
    else:
        overfetch_limit = max(
            limit,
            min(limit * _ARCHIVAL_OVERFETCH_FACTOR, limit + _ARCHIVAL_OVERFETCH_MAX_EXTRA),
        )
    result = outputs_local.search_outputs(
        outputs_dir,
        query,
        limit=overfetch_limit,
        include_archival=True,
        max_seconds=max_seconds,
        subtree=subtree,
    )
    hits = result.get("hits")
    if not hits or result.get("error"):
        return result

    query_norm = _normalize(query)
    query_tokens = _normalize_tokens(query)
    if not query_norm:
        return result

    for hit in hits:
        boost, literal_match = _field_boost(query_norm, query_tokens, hit, outputs_dir)
        hit["literal_match"] = literal_match
        hit["_field_boost"] = boost

    if not include_archival:
        hits = [h for h in hits if not h.get("is_archival")]

    def _sort_key(h: dict[str, Any]) -> tuple[float, str, str]:
        final_score = float(h.get("score") or 0.0) + h["_field_boost"]
        rel = _relative_path(h.get("path") or "", outputs_dir)
        return (-final_score, rel, h.get("path") or "")

    hits.sort(key=_sort_key)
    # Code review: for a non-positive `limit`, outputs_local's own internal
    # `safe_limit = max(1, int(limit))` clamp already bounds how many hits
    # come back (the old, pre-a444313d search.py never truncated locally at
    # all -- it only reordered whatever outputs_local returned). Slicing
    # HERE with the caller's raw negative limit uses Python's negative-
    # index slice semantics ("drop the last N"), not "return at most N" --
    # e.g. `limit=-5` against a 1-item list yields `[]` instead of the 1 hit
    # the underlying engine (and the old code) both returned. Only apply
    # this truncation for a genuine positive limit; a non-positive one keeps
    # exactly whatever the underlying call already produced.
    if limit > 0:
        hits = hits[:limit]
    for h in hits:
        h.pop("_field_boost", None)

    result["hits"] = hits
    return result
