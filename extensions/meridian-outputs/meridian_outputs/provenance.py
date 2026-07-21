"""Bidirectional docx-figure <-> source-file provenance resolution.

Sprint item e422de44 -- investigate + fix ``resolve_figure_output``.

Prior state (confirmed by investigation, 2026-07-20)
-----------------------------------------------------
The ``resolve_figure_output`` tool already existed and was already wired into
the MCP tool catalog -- but ``meridian_outputs/provenance.py`` (this file)
did not exist. The implementation lived entirely in
``outputs_local.py::resolve_figure_output`` (module-level function) and its
registration point was the ``@mcp.tool()`` wrapper in ``server.py``
(``resolve_figure_output``, which just calls
``outputs_local.resolve_figure_output(outputs_dir, file_path)``).

Its actual behaviour, confirmed by reading ``OutputsFtsIndex.resolve_output``
(the code it delegates to):

  - FORWARD-ONLY: given a figure's ``file_path``, it only ever answers "is
    THIS EXACT path already an indexed output" -- it never traces a script/
    data file forward to the figures it produced.
  - EXACT-PATH-ONLY: the lookup is a single SQL equality check on a
    normalised path string. If the figure on disk isn't recorded at that
    literal path -- copied into a docs/media folder, renamed, or the run
    that produced it was re-executed at a different location -- it returns
    ``None`` with no further signal (not "stale", not "closest match":
    nothing).

That shape is exactly what would swallow a stale relocation note or a figure
quietly citing old data: the one lookup that existed silently degrades to
"not found" the moment a path doesn't match byte-for-byte, and there was no
way to approach the problem from the source side at all.

What this module adds
----------------------
Two genuinely bidirectional, relocation-tolerant primitives, built entirely
on outputs_local's existing PUBLIC, stable module-level API
(``resolve_figure_output`` and ``search_outputs``) -- no changes to
``outputs_local.py`` and no private-attribute reach-through:

  - :func:`resolve_figure_output` -- forward (figure -> source). Same name
    and call signature as the legacy exact-path function (drop-in
    replacement candidate), but adds a basename-fallback tier so a figure
    that was relocated/renamed relative to the index can still be resolved.
  - :func:`find_outputs_by_source` -- reverse (source -> figures/outputs).
    Given a script or data file, finds every indexed output whose recorded
    ``generating_script`` traces back to it, newest first. This is the
    direction needed to catch a docx figure quietly citing stale data: walk
    the source's outputs forward and compare against what the docx shows.

Scope note: server.py's existing registration and any sibling
meridian_outputs modules are intentionally left untouched here (out of scope
for this item / being worked on in parallel elsewhere). Wiring these two
functions into the live MCP tool catalog is a follow-up, not part of this
change.
"""
from __future__ import annotations

import os
from typing import Any

from . import outputs_local

__all__ = [
    "resolve_figure_output",
    "find_outputs_by_source",
]


def _basename_key(path: Any) -> str:
    """Case/slash-insensitive basename key, for relocation-tolerant matching."""
    if not path:
        return ""
    s = str(path).replace("\\", "/").rstrip("/")
    return os.path.normcase(os.path.basename(s))


def _path_key(path: Any) -> str:
    """Case/slash-insensitive key for a path-like STRING (no ``abspath``).

    Deliberately does NOT resolve against the current working directory
    (unlike ``outputs_local._normalize_output_path``): ``generating_script``
    values are inferred from free text (a CSV header comment, a JSON key) and
    are frequently a bare filename or a short relative fragment rather than a
    path meant to be resolved on this machine. Running them through
    ``os.path.abspath`` would silently rebase them onto an unrelated CWD and
    produce false negatives/positives. This key only normalises case and
    slash direction so two textually-equivalent references compare equal.
    """
    if not path:
        return ""
    return os.path.normcase(str(path).strip().replace("\\", "/"))


def resolve_figure_output(
    outputs_dir: str, file_path: str, *, fuzzy_limit: int = 25,
) -> dict[str, Any] | None:
    """Forward resolution: a docx figure's ``file_path`` -> its generating source.

    Two tiers, tried in order:

      1. Exact-path (unchanged legacy contract, delegated straight to
         ``outputs_local.resolve_figure_output``): the figure file IS itself
         an indexed output at that same path.
      2. Basename fallback (NEW): when the exact path misses -- the figure
         was relocated/copied/renamed relative to when it was indexed --
         searches the outputs index for files sharing the same basename and
         returns the best-scoring candidate. This is what catches a figure
         whose docx-embedded copy no longer lives where it was generated.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      file_path:    The figure's file path to resolve.
      fuzzy_limit:  Max search_outputs hits considered for the basename tier.

    Returns:
      ``None`` only when NEITHER tier finds anything. Otherwise the resolved
      row (path, generating_script, is_archival, canonical_path, sha256*,
      kind, size, mtime, csv_columns, json_keys -- *sha256 only present on an
      exact match, since it comes from a different code path) plus:

        - ``match_type``: ``"exact"`` or ``"basename"``.
        - ``queried_path``: the ``file_path`` that was looked up (for audit).
        - ``candidate_count``: (basename tier only) how many same-basename
          files were found -- more than 1 means the match is ambiguous and
          ``generating_script`` should be treated as a best guess, not a
          certainty.
    """
    if not file_path or not str(file_path).strip():
        return None
    if not os.path.isdir(outputs_dir):
        return None

    exact = outputs_local.resolve_figure_output(outputs_dir, file_path)
    if exact is not None:
        return {**exact, "match_type": "exact", "queried_path": file_path}

    target_base = _basename_key(file_path)
    if not target_base:
        return None
    query = os.path.basename(str(file_path).replace("\\", "/").rstrip("/"))
    if not query:
        return None
    result = outputs_local.search_outputs(
        outputs_dir, query, limit=max(int(fuzzy_limit), 1), include_archival=True,
    )
    hits = result.get("hits") or []
    candidates = [h for h in hits if _basename_key(h.get("path")) == target_base]
    if not candidates:
        return None
    candidates.sort(key=lambda h: (h.get("score") or 0.0), reverse=True)
    best = dict(candidates[0])
    best.pop("score", None)
    best.pop("bm25", None)
    best.pop("annotations", None)
    best["match_type"] = "basename"
    best["queried_path"] = file_path
    best["candidate_count"] = len(candidates)
    return best


def find_outputs_by_source(
    outputs_dir: str,
    source_path: str,
    *,
    limit: int = 25,
    search_limit: int = 200,
) -> dict[str, Any]:
    """Reverse resolution: a script/data ``source_path`` -> the outputs it produced.

    This is the direction plain exact/basename resolution can never answer,
    because that always starts from the OUTPUT side. Given the generating
    script or data file, this scans the outputs index for rows whose recorded
    ``generating_script`` traces back to it (exact-string or basename match)
    -- i.e. "what did this thing produce?". That is the direction needed to
    catch a docx figure quietly citing STALE data: walk the source's outputs
    forward, newest first, and compare against what the docx actually shows.

    Args:
      outputs_dir:   Absolute path to the outputs directory.
      source_path:   The script or data file to trace forward from.
      limit:         Max number of matched outputs to return.
      search_limit:  How many search_outputs hits to scan before filtering
                     (generous, since only a subset will actually match).

    Returns:
      ``{source_path, outputs: [...], total}`` where each output row has the
      same fields as a ``search_outputs`` hit (path, generating_script,
      is_archival, canonical_path, kind, size, mtime, csv_columns,
      json_keys), sorted newest-first by ``mtime``. ``total`` is the full
      match count before ``limit`` truncation. ``outputs`` is empty (not an
      error) when nothing in the tree cites this source.
    """
    empty: dict[str, Any] = {"source_path": source_path, "outputs": [], "total": 0}
    if not source_path or not str(source_path).strip():
        return empty
    if not os.path.isdir(outputs_dir):
        return empty

    target_path = _path_key(source_path)
    target_base = _basename_key(source_path)
    query = os.path.basename(str(source_path).replace("\\", "/").rstrip("/")) or str(source_path)
    result = outputs_local.search_outputs(
        outputs_dir, query, limit=max(int(search_limit), 1), include_archival=True,
    )
    hits = result.get("hits") or []

    matches: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for hit in hits:
        gs = hit.get("generating_script")
        if not gs:
            continue
        if _path_key(gs) != target_path and _basename_key(gs) != target_base:
            continue
        p = hit.get("path")
        if p in seen_paths:
            continue
        seen_paths.add(p)
        clean = dict(hit)
        clean.pop("score", None)
        clean.pop("bm25", None)
        matches.append(clean)

    matches.sort(key=lambda h: (h.get("mtime") or 0), reverse=True)
    trimmed = matches[: max(int(limit), 1)]
    return {"source_path": source_path, "outputs": trimmed, "total": len(matches)}
