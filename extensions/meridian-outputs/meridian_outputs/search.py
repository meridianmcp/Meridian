"""meridian_outputs.search -- BM25 output search, hardened for literal
file-pattern-hunt queries.

Sprint item c6236ef4.

Investigation summary (2026-07-20): the ``search_outputs`` MCP tool was NOT
a stub. It already existed, fully implemented and tested, as a thin
``server.py`` wrapper around :func:`outputs_local.search_outputs` -- a real
DuckDB/Tantivy BM25 full-text engine (:class:`outputs_local.OutputsFtsIndex`)
that indexes every file's basename plus CSV column headers / JSON top-level
keys / a ``generating_script`` hint, plus full CSV/JSON body text, all
tokenized and BM25-ranked. All of its existing tests
(``TestSearchOutputsAPI``, the cold-tree/partial-rebuild tests, the
tantivy-lock-warning test, etc. in ``tests/test_outputs_local.py``) pass
unmodified. A live smoke test against a real 57-file thesis outputs
directory confirmed it genuinely finds e.g. ``parabolic_radius_sweep_130``
files in ~1.5s with no glob/grep hunt required. There is no pre-existing
``search.py`` -- that was an incorrect assumption about where the tool
lived (mirrors ``classify.py``'s and ``fingerprint.py``'s sibling
investigations this same session, which independently reached the same
"not a stub, just misfiled" conclusion for their own tools).

It was, however, genuinely WORSE than a manual ``grep -l <pattern>`` /
``find -iname '*pattern*'`` hunt in one specific, reproducible case: when
the query is (or closely resembles) an exact filename stem, BM25's
length-normalized term-frequency scoring can rank a longer, decoy-named
file ABOVE the true exact-match canonical file. Reproduced live against the
real thesis tree: querying ``"parabolic_radius_sweep_130_results"`` ranked

    parabolic_radius_sweep_130_results_FULL130.csv.bak_41img_mislabeled
    (score 14.53)

ABOVE the exact-substring match

    parabolic_radius_sweep_130_results.csv (score 13.61)

-- exactly the same real "*.bak_41img_mislabeled" file that ``classify.py``'s
sibling investigation independently flagged as an unrecognised archival
copy (its ``.bak``/``_mislabeled`` suffix isn't covered by
``outputs_local``'s ``_old``/``_old_N`` heuristic). A manual literal
substring hunt would never make this mistake -- it would find the exact
name and stop. This is the concrete gap between "a search tool exists" and
"the search tool actually replaces manual file-pattern hunts": relevance
ranking that a human doing a manual hunt would trust less than plain
substring matching for a query that IS basically a filename.

This module does NOT duplicate ``outputs_local``'s indexing/BM25 engine
(``OutputsFtsIndex`` is a large, stateful, cached, lock-guarded class --
forking it would be a maintenance hazard, not a fix) -- it delegates
entirely to the PUBLIC :func:`outputs_local.search_outputs` for the actual
search, and ADDS ONE THING: a literal-substring-match boost re-ranking
pass that guarantees any hit whose basename literally contains the
(separator-normalized) query string outranks any hit that only matched via
loose BM25 token overlap. Additive only: never drops a hit, never adds a
hit, never changes ``total_indexed``/``total_in_index``/hit count/any
existing field -- only reorders ``hits`` and adds one new
``literal_match: bool`` field per hit.

Coordination with sibling modules (``annotate.py``/``fingerprint.py``
already landed; ``classify.py``/``provenance.py`` were being built in
parallel this same session): mirrors their precedent of importing only
``outputs_local``'s PUBLIC API (no leading-underscore names).

NOT wired into ``server.py``'s ``@mcp.tool() search_outputs`` yet -- that
tool still delegates directly to ``outputs_local.search_outputs``
(unchanged) and is intentionally left alone here (server.py and
outputs_local.py are both out of scope for this fix; see sprint item
c6236ef4's instructions). :func:`search_outputs` below is the drop-in
superset a follow-up wiring change would point the tool at -- same
``{outputs_dir, query, hits, total_indexed, ...}`` shape, additive fields
only.
"""
from __future__ import annotations

import re
from typing import Any

from . import outputs_local

__all__ = ["search_outputs"]

# Separator-agnostic normalization so "radius_sweep_130", "radius-sweep-130"
# and "radius sweep 130" are all treated as the same literal needle when
# compared against a basename -- matching how a human doing a manual hunt
# mentally treats filename separators as interchangeable.
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def _normalize(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.lower())


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def search_outputs(
    outputs_dir: str,
    query: str,
    *,
    limit: int = 10,
    include_archival: bool = True,
    max_seconds: float | None = outputs_local.DEFAULT_REBUILD_BUDGET_SECONDS,
) -> dict[str, Any]:
    """BM25 output search with a literal-filename-match boost.

    Delegates to :func:`outputs_local.search_outputs` for all indexing and
    BM25 scoring -- this function changes nothing about what is found, only
    how the already-returned hits are ordered. See the module docstring for
    the motivating gap (a decoy/archival-suffixed file BM25-outranking its
    exact-match canonical twin) that this re-ranking pass closes.

    Args:
      outputs_dir:      Absolute path to the outputs directory to index.
      query:            BM25 search query string (same semantics as
                        ``outputs_local.search_outputs``).
      limit:            Maximum number of hits to return (default 10).
      include_archival: Include archival-flagged files in results (default
                        True) -- passed straight through.
      max_seconds:      Rebuild wall-clock budget -- passed straight
                        through (same default as ``outputs_local``).

    Returns:
      Identical shape to ``outputs_local.search_outputs``'s return value
      (``{outputs_dir, query, hits, total_indexed, total_in_index, ...}``
      plus optional ``partial``/``pending_stale_count``/``fts_pending``/
      ``tantivy_lock_warning``/``db_write_error``/``zero_hits_warning``/
      ``error``), except every hit dict gains a
      ``literal_match: bool``
      field, and ``hits`` is stably re-sorted so all ``literal_match=True``
      hits precede all ``literal_match=False`` hits (BM25 order is
      preserved within each group). On error (missing dir, empty query,
      etc.) OR a zero-hit result the result is returned unchanged -- no
      re-ranking is attempted, and (per ``outputs_local.search_outputs``'s
      contract) a zero-hit response carries ``zero_hits_warning`` whenever
      the index isn't fully converged, so this passthrough must never be
      mistaken for a confirmed "not found".
    """
    result = outputs_local.search_outputs(
        outputs_dir,
        query,
        limit=limit,
        include_archival=include_archival,
        max_seconds=max_seconds,
    )
    hits = result.get("hits")
    if not hits or result.get("error"):
        return result

    needle = _normalize(query)
    if not needle:
        return result

    for hit in hits:
        basename = _normalize(_basename(hit.get("path") or ""))
        hit["literal_match"] = needle in basename

    # Stable sort: literal-basename matches first, BM25 order preserved
    # within each of the two groups. Purely a re-order -- same hits, same
    # count, same every-other-field.
    result["hits"] = sorted(hits, key=lambda h: not h["literal_match"])
    return result
