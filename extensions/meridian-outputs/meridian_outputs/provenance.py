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

Relationship to the artifact registry (item e1c979e3, added later)
--------------------------------------------------------------------
:func:`resolve_figure_output`'s basename-fallback tier and
:func:`bind_artifact_provenance`'s per-write classification are both
DELIBERATELY still path-first, per-call lookups against the live outputs FTS
index -- neither was changed by e1c979e3, and both keep exactly their
existing contracts (including the basename tier's non-authoritative fuzzy
match, which is still useful evidence for a per-write classification even
though it is never treated as identity-confirming). ``artifact_registry``
(a separate module) is the NEW, narrower answer to "give me a STABLE public
id for this artifact that survives relocation, resolved with an explicit
resolved/ambiguous/unresolved/orphaned outcome and never a silent basename
guess" -- callers that need a durable, portable identity (rather than a
one-off per-write provenance check) should prefer
``artifact_registry.register_artifact``/``resolve_artifact`` over building
one on top of this module's basename tier. The two modules do not import
each other.
"""
from __future__ import annotations

import os
from typing import Any

from . import outputs_local, provenance_status

__all__ = [
    "resolve_figure_output",
    "find_outputs_by_source",
    "trace_forward_outputs",
    "classify_temp_output_ownership",
    "bind_artifact_provenance",
    "RESOLVED",
    "ORPHANED",
    "HASH_MISMATCH",
    "UNRESOLVED",
]

# Binding statuses returned by bind_artifact_provenance (sprint item
# 6d02f343). Ranked from strongest to weakest evidence; ARTIFACT_STATUSES
# fixes the canonical set so a caller can validate an unexpected status
# string instead of silently treating a typo as "some other status".
RESOLVED = "resolved"
ORPHANED = "orphaned"
HASH_MISMATCH = "hash_mismatch"
UNRESOLVED = "unresolved"
ARTIFACT_STATUSES = (RESOLVED, ORPHANED, HASH_MISMATCH, UNRESOLVED)


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


# Bounded forward trace (gap set C, "forward-trace-cap"). Hard ceilings on
# the caller-supplied knobs: before this, ``limit``/``search_limit`` were
# floored at 1 but never capped, so one call could ask the index for an
# unbounded scan window. A value above its ceiling is clamped (never an
# error) and the clamp is reported in the response's ``limits`` block.
FORWARD_LIMIT_CEILING = 1000
FORWARD_SEARCH_LIMIT_CEILING = 5000
TRACE_MAX_DEPTH_CEILING = 25
TRACE_MAX_NODES_CEILING = 5000

# Truncation reasons reported by trace_forward_outputs, in the fixed order
# ``truncation["reasons"]`` lists them (``truncation["reason"]`` is the first).
TRUNCATION_REASONS = (
    "max_nodes",
    "max_depth",
    "outputs_capped",
    "scan_saturated",
    "index_partial",
)


def _clamp_limit(value: Any, ceiling: int) -> tuple[int, bool]:
    """Floor ``value`` at 1 and cap it at ``ceiling``; report whether capped."""
    requested = int(value)
    effective = min(max(requested, 1), ceiling)
    return effective, requested > ceiling


def _no_truncation() -> dict[str, bool]:
    return {"scan_saturated": False, "index_partial": False, "outputs_capped": False}


def _exact_path_keys(outputs_dir: str, path: Any) -> set[str]:
    """Exact (non-basename) identity keys for one output/source path.

    Used for hops 2+ of :func:`trace_forward_outputs`, where basename
    matching over-matches (any ``results.csv`` anywhere would chain onward).
    Two spellings of the same file compare equal: the path as given, its
    real absolute form, and -- when it lies under ``outputs_dir`` -- its
    ``outputs_dir``-relative form, so a ``generating_script`` recorded as
    ``"sub/a.json"`` matches the indexed ``<outputs_dir>/sub/a.json`` row.
    Every key goes through :func:`_path_key` (case/slash-insensitive).
    """
    raw = str(path or "").strip()
    if not raw:
        return set()
    keys = {_path_key(os.path.normpath(raw))}
    try:
        root = os.path.realpath(os.path.abspath(outputs_dir))
        candidate = raw if os.path.isabs(raw) else os.path.join(root, raw)
        resolved = os.path.realpath(os.path.abspath(candidate))
    except (OSError, ValueError):
        return keys
    keys.add(_path_key(resolved))
    try:
        if os.path.normcase(os.path.commonpath((root, resolved))) == os.path.normcase(root):
            keys.add(_path_key(os.path.relpath(resolved, root)))
    except ValueError:
        pass  # different drive: no outputs_dir-relative spelling exists
    return keys


def _scan_outputs_by_source(
    outputs_dir: str,
    source_path: str,
    *,
    limit: int,
    search_limit: int,
    exact_only: bool,
) -> dict[str, Any]:
    """One-hop expander shared by find_outputs_by_source and the trace.

    ``exact_only=False`` is the historical match rule (normalised exact
    string OR basename). ``exact_only=True`` matches only exact normalised
    paths (see :func:`_exact_path_keys`); the trace uses it beyond hop 1.
    """
    eff_limit, limit_clamped = _clamp_limit(limit, FORWARD_LIMIT_CEILING)
    eff_search, search_clamped = _clamp_limit(search_limit, FORWARD_SEARCH_LIMIT_CEILING)
    limits = {
        "limit": eff_limit,
        "search_limit": eff_search,
        "limit_clamped": limit_clamped,
        "search_limit_clamped": search_clamped,
    }
    empty: dict[str, Any] = {
        "source_path": source_path,
        "outputs": [],
        "total": 0,
        "truncated": _no_truncation(),
        "limits": limits,
    }
    if not source_path or not str(source_path).strip():
        return empty
    if not os.path.isdir(outputs_dir):
        return empty

    target_path = _path_key(source_path)
    target_base = _basename_key(source_path)
    exact_keys = _exact_path_keys(outputs_dir, source_path) if exact_only else set()
    query = os.path.basename(str(source_path).replace("\\", "/").rstrip("/")) or str(source_path)
    result = outputs_local.search_outputs(
        outputs_dir, query, limit=eff_search, include_archival=True,
    )
    hits = result.get("hits") or []

    matches: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for hit in hits:
        gs = hit.get("generating_script")
        if not gs:
            continue
        if exact_only:
            if not (_exact_path_keys(outputs_dir, gs) & exact_keys):
                continue
        elif _path_key(gs) != target_path and _basename_key(gs) != target_base:
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
    trimmed = matches[:eff_limit]
    truncated = {
        # The search returned a full window: more cited rows may sit past it.
        "scan_saturated": len(hits) >= eff_search,
        # The index itself is mid-walk / FTS not built: hits under-report.
        "index_partial": bool(result.get("partial") or result.get("fts_pending")),
        "outputs_capped": len(matches) > len(trimmed),
    }
    return {
        "source_path": source_path,
        "outputs": trimmed,
        "total": len(matches),
        "truncated": truncated,
        "limits": limits,
    }


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

    One hop only. For transitive dependents (outputs of outputs) use
    :func:`trace_forward_outputs`, which is bounded by depth and node caps.

    Args:
      outputs_dir:   Absolute path to the outputs directory.
      source_path:   The script or data file to trace forward from.
      limit:         Max number of matched outputs to return. Floored at 1,
                     capped at ``FORWARD_LIMIT_CEILING`` (1000).
      search_limit:  How many search_outputs hits to scan before filtering
                     (generous, since only a subset will actually match).
                     Floored at 1, capped at ``FORWARD_SEARCH_LIMIT_CEILING``
                     (5000).

    Returns:
      ``{source_path, outputs: [...], total, truncated, limits}`` where each
      output row has the same fields as a ``search_outputs`` hit (path,
      generating_script, is_archival, canonical_path, kind, size, mtime,
      csv_columns, json_keys), sorted newest-first by ``mtime``. ``total``
      is the full match count before ``limit`` truncation. ``outputs`` is
      empty (not an error) when nothing in the tree cites this source.

      ``truncated`` says whether the answer may be incomplete, as flags
      (never an error): ``scan_saturated`` -- the search filled its whole
      ``search_limit`` window, so matches past it were never scanned;
      ``index_partial`` -- the outputs index reported ``partial`` or
      ``fts_pending`` (still walking/building), so hits under-report;
      ``outputs_capped`` -- ``total`` exceeds the returned ``outputs``.
      ``limits`` reports the effective ``limit``/``search_limit`` and
      whether each was clamped to its ceiling.
    """
    return _scan_outputs_by_source(
        outputs_dir, source_path, limit=limit, search_limit=search_limit,
        exact_only=False,
    )


def trace_forward_outputs(
    outputs_dir: str,
    source_path: str,
    *,
    max_depth: int = 3,
    max_nodes: int = 200,
    limit: int = 25,
    search_limit: int = 200,
) -> dict[str, Any]:
    """Bounded transitive forward trace: source -> outputs -> their outputs.

    A breadth-first walk that uses the one-hop expander behind
    :func:`find_outputs_by_source` once per node. Hop 1 keeps that
    function's match rule (exact normalised string OR basename), so
    ``max_depth=1`` returns exactly today's one-hop answer. Hops 2+ match
    only exact normalised paths (an output's absolute or
    ``outputs_dir``-relative spelling), because basename matching there
    would chain through any unrelated file that shares a name.

    A visited set keyed by :func:`_path_key` (seeded with the source itself)
    makes every path appear at most once, so a cycle (A generates B, B
    generates A) terminates.

    Caps are flags, never errors. The walk stops at ``max_depth`` hops or
    ``max_nodes`` nodes (the source is not counted), and the response says
    so.

    Args:
      outputs_dir:   Absolute path to the outputs directory.
      source_path:   The script or data file to trace forward from.
      max_depth:     Max hops from the source (floored at 1, capped at
                     ``TRACE_MAX_DEPTH_CEILING``).
      max_nodes:     Max dependent outputs returned (floored at 1, capped at
                     ``TRACE_MAX_NODES_CEILING``).
      limit:         Per-hop ``limit`` forwarded to the one-hop expander.
      search_limit:  Per-hop ``search_limit`` forwarded to the expander.

    Returns:
      ``{source_path, nodes, total, truncated, truncation, limits}``.
      ``nodes`` is in BFS order; each node is the one-hop output row plus
      ``depth`` (1 = cites the source directly) and ``parent`` (the path it
      was reached from; the caller's ``source_path`` for depth 1).
      ``truncated`` is a bool. ``truncation`` is ``{reason, reasons,
      depth_reached, nodes_visited, scan_saturated, index_partial,
      outputs_capped}``: ``reasons`` lists every cause in the fixed
      ``TRUNCATION_REASONS`` order and ``reason`` is the first, or ``None``.
      ``max_depth`` is reported when a node at the depth cap has an
      unvisited dependent, or when the one extra probe hop that checks this
      cannot rule one out (the probe's scan saturated or the index is
      partial). ``scan_saturated``/``index_partial``/``outputs_capped`` are
      the OR of those flags over every hop the answer includes. A missing
      ``outputs_dir`` or empty ``source_path`` returns no nodes and
      ``truncated=False``, matching find_outputs_by_source's empty answer.
    """
    eff_depth, depth_clamped = _clamp_limit(max_depth, TRACE_MAX_DEPTH_CEILING)
    eff_nodes, nodes_clamped = _clamp_limit(max_nodes, TRACE_MAX_NODES_CEILING)
    eff_limit, limit_clamped = _clamp_limit(limit, FORWARD_LIMIT_CEILING)
    eff_search, search_clamped = _clamp_limit(search_limit, FORWARD_SEARCH_LIMIT_CEILING)
    limits = {
        "max_depth": eff_depth,
        "max_nodes": eff_nodes,
        "limit": eff_limit,
        "search_limit": eff_search,
        "max_depth_clamped": depth_clamped,
        "max_nodes_clamped": nodes_clamped,
        "limit_clamped": limit_clamped,
        "search_limit_clamped": search_clamped,
    }

    nodes: list[dict[str, Any]] = []
    flags = _no_truncation()
    hit_max_nodes = False
    hit_max_depth = False
    depth_reached = 0

    if source_path and str(source_path).strip() and os.path.isdir(outputs_dir):
        visited: set[str] = set(_exact_path_keys(outputs_dir, source_path))
        visited.add(_path_key(source_path))
        frontier: list[tuple[str, int]] = [(source_path, 0)]

        def _expand(node_path: str, depth: int) -> dict[str, Any]:
            hop = _scan_outputs_by_source(
                outputs_dir, node_path, limit=eff_limit, search_limit=eff_search,
                exact_only=depth >= 1,
            )
            for key, value in hop["truncated"].items():
                flags[key] = flags[key] or bool(value)
            return hop

        def _is_new(row: dict[str, Any]) -> bool:
            path = row.get("path")
            if not path:
                return False
            return not (_exact_path_keys(outputs_dir, path) | {_path_key(path)}) & visited

        while frontier and not hit_max_nodes:
            next_frontier: list[tuple[str, int]] = []
            for node_path, depth in frontier:
                if depth >= eff_depth:
                    # Probe one hop past the cap so ``max_depth`` is reported
                    # only when a dependent lies beyond it, or the probe
                    # cannot rule one out (its own scan saturated, or the
                    # index is partial). The probe's flags are NOT merged
                    # into ``flags``: they describe a hop the answer does
                    # not include.
                    if not hit_max_depth:
                        probe = _scan_outputs_by_source(
                            outputs_dir, node_path, limit=eff_limit,
                            search_limit=eff_search, exact_only=True,
                        )
                        probe_flags = probe["truncated"]
                        if (
                            any(_is_new(row) for row in probe["outputs"])
                            or probe_flags["scan_saturated"]
                            or probe_flags["index_partial"]
                        ):
                            hit_max_depth = True
                    continue
                hop = _expand(node_path, depth)
                for row in hop["outputs"]:
                    # Hop 1 keeps every row the one-hop expander returned
                    # (it already dedupes by path), so max_depth=1 equals
                    # find_outputs_by_source exactly; later hops skip
                    # anything already visited, which is what ends cycles.
                    if depth > 0 and not _is_new(row):
                        continue
                    if not row.get("path"):
                        continue
                    if len(nodes) >= eff_nodes:
                        hit_max_nodes = True
                        break
                    path = row["path"]
                    visited.update(_exact_path_keys(outputs_dir, path))
                    visited.add(_path_key(path))
                    nodes.append({**row, "depth": depth + 1, "parent": node_path})
                    depth_reached = max(depth_reached, depth + 1)
                    next_frontier.append((path, depth + 1))
                if hit_max_nodes:
                    break
            frontier = next_frontier

    active = {
        "max_nodes": hit_max_nodes,
        "max_depth": hit_max_depth,
        "outputs_capped": flags["outputs_capped"],
        "scan_saturated": flags["scan_saturated"],
        "index_partial": flags["index_partial"],
    }
    reasons = [reason for reason in TRUNCATION_REASONS if active[reason]]
    return {
        "source_path": source_path,
        "nodes": nodes,
        "total": len(nodes),
        "truncated": bool(reasons),
        "truncation": {
            "reason": reasons[0] if reasons else None,
            "reasons": reasons,
            "depth_reached": depth_reached,
            "nodes_visited": len(nodes),
            **flags,
        },
        "limits": limits,
    }


def classify_temp_output_ownership(outputs_dir: str, path: str) -> dict[str, Any]:
    """Sprint item 2ae3f011 -- confirm whether ``path`` is a Meridian-owned
    TEMPORARY output eligible for reversible quarantine, or must never be
    touched.

    This is the ``ownership_check`` this module's callers should inject into
    ``meridian.worktree_cleanup.build_quarantine_manifest`` /
    ``purge_quarantined_output`` (that module has no hard dependency on this
    extension -- see its own docstring -- so it always needs a real
    classifier supplied by whoever calls it with real ownership knowledge;
    this is that classifier). Built entirely on
    ``outputs_local.get_indexed_output`` (this module's only existing
    dependency, per its own module docstring) -- no new hashing, no private
    reach-through, no re-implementation of the canonical/archival heuristic
    that already lives in ``outputs_local.classify_canonical_archival``
    (``get_indexed_output`` already surfaces its ``is_archival`` verdict on
    the same row).

    Ownership rule (conservative by design -- false negatives are safe,
    false positives are not):
      - Never discovered by the outputs index at all (``get_indexed_output``
        returns ``None``) -> NOT eligible, regardless of filename. Could be
        anything, including a user's own file that merely happens to live
        under the outputs tree -- "never delete user or canonical outputs".
      - Discovered, but ``is_archival`` is ``False`` (this IS the canonical/
        live output) -> NOT eligible, regardless of how confidently it's
        "known" -- canonical outputs are never quarantine candidates.
      - Discovered AND ``is_archival`` is ``True`` -> eligible: a confirmed
        Meridian-owned temporary/archival copy.

    Returns ``{"path", "eligible", "is_archival", "canonical_path",
    "sha256", "size", "reason"}``. Never raises: a missing/invalid
    ``outputs_dir``/``path`` just returns ``eligible=False`` with an
    explanatory ``reason``, same fail-closed posture as every other
    ownership decision this function makes.
    """
    if not path or not str(path).strip():
        return {
            "path": path, "eligible": False, "is_archival": None,
            "canonical_path": None, "sha256": None, "size": None,
            "reason": "path is required",
        }
    if not outputs_dir or not os.path.isdir(outputs_dir):
        return {
            "path": path, "eligible": False, "is_archival": None,
            "canonical_path": None, "sha256": None, "size": None,
            "reason": "outputs_dir is required",
        }

    indexed = outputs_local.get_indexed_output(outputs_dir, path)
    if indexed is None:
        return {
            "path": path, "eligible": False, "is_archival": None,
            "canonical_path": None, "sha256": None, "size": None,
            "reason": "never discovered by the outputs index -- ownership cannot be confirmed",
        }

    is_archival = bool(indexed.get("is_archival"))
    common = {
        "path": path,
        "is_archival": is_archival,
        "canonical_path": indexed.get("canonical_path"),
        "sha256": indexed.get("sha256"),
        "size": indexed.get("size"),
    }
    if not is_archival:
        return {
            **common,
            "eligible": False,
            "reason": "classified as the canonical output, not a temporary/archival copy",
        }
    return {
        **common,
        "eligible": True,
        "reason": "confirmed Meridian-owned temporary output (archival copy, known to the outputs index)",
    }


# ---------------------------------------------------------------------------
# 6d02f343 -- artifact manifest: bind structural docx figure/table/equation
# artifacts to per-file meridian-outputs provenance, fail-closed.
#
# A docx-editing caller (meridian-docs' docs_intel.py, or any other writer)
# knows the STRUCTURAL identity of an artifact -- a figure index, a table's
# w14:paraId, an equation's element id -- and, when the artifact was
# originally produced by a script, the CANONICAL output path it was inserted
# from. It has no reason to know anything about meridian-outputs' own
# index/ledger internals. This module already has everything needed to turn
# that canonical path into an authoritative provenance verdict
# (resolve_figure_output's exact+basename tiers, provenance_status's
# directory-level fallback) -- bind_artifact_provenance is the single join
# point that composes those into one fail-closed classification per
# artifact, so a caller can reject/quarantine a write instead of silently
# promoting an orphaned or hash-mismatched replacement.
#
# Deliberately duck-typed, no cross-package import in the other direction:
# per the established pattern in this file (see provenance_status.
# get_manifest_backed_provenance_status's own docstring), a docx-writing
# caller in a SEPARATE package (meridian-docs, meridian core) is expected to
# call bind_artifact_provenance itself and pass the resulting plain dict
# into its own write-gating code -- never the reverse (this module never
# imports docx-aware code).
# ---------------------------------------------------------------------------


def _bind_one_artifact(
    outputs_dir: str,
    artifact: dict[str, Any],
    *,
    fuzzy_limit: int,
) -> dict[str, Any]:
    artifact_id = artifact.get("artifact_id")
    kind = artifact.get("kind")
    canonical_path = artifact.get("canonical_path")
    expected_sha256 = artifact.get("expected_sha256")
    # `generating_script` defaults to None here and is populated below the
    # `resolve_figure_output` call, the instant meridian-outputs' own index
    # has an opinion on it (exact OR basename tier). Every branch after that
    # point returns via `**base`, so the generator identity this function
    # already fetches (bd63385b / W31-A) is no longer silently discarded --
    # it rides along with every binding, resolved or not, instead of being
    # computed and then thrown away.
    base = {
        "artifact_id": artifact_id,
        "kind": kind,
        "canonical_path": canonical_path,
        "generating_script": None,
    }

    if not canonical_path or not str(canonical_path).strip():
        return {
            **base,
            "status": UNRESOLVED,
            "match_type": None,
            "evidence": "none",
            "resolved_sha256": None,
            "reason": (
                "artifact has no recorded canonical_path -- there is nothing "
                "to resolve against meridian-outputs provenance"
            ),
        }

    resolved = resolve_figure_output(
        outputs_dir, canonical_path, fuzzy_limit=fuzzy_limit,
    )
    if resolved is not None:
        # W31-A: carry the generator identity resolve_figure_output already
        # fetched through to every binding this call produces, authoritative
        # or not -- see the `base` dict's own comment above for why this is
        # set once here rather than repeated in each branch below.
        base["generating_script"] = resolved.get("generating_script")
        match_type = resolved.get("match_type")
        resolved_sha256 = resolved.get("sha256")

        if match_type == "exact":
            if expected_sha256:
                if resolved_sha256 is None:
                    # A hash was explicitly requested, but the exact-match
                    # index record has none on file (e.g. a lightweight walk
                    # that discovered the path without hashing it) -- this
                    # is exactly the "could not check" case that must never
                    # be silently promoted to "passed".
                    return {
                        **base,
                        "status": UNRESOLVED,
                        "match_type": match_type,
                        "evidence": "meridian_outputs_exact",
                        "resolved_sha256": None,
                        "reason": (
                            f"canonical_path {canonical_path!r} resolves to "
                            "an exact meridian-outputs record, but that "
                            "record has no hash on file -- this artifact's "
                            "expected hash cannot be confirmed"
                        ),
                    }
                if str(expected_sha256) != str(resolved_sha256):
                    return {
                        **base,
                        "status": HASH_MISMATCH,
                        "match_type": match_type,
                        "evidence": "meridian_outputs_exact",
                        "resolved_sha256": resolved_sha256,
                        "reason": (
                            f"canonical_path {canonical_path!r} resolves to "
                            "an exact meridian-outputs record, but its "
                            "recorded hash does not match this artifact's "
                            "expected hash -- the replacement content "
                            "differs from the authoritative output"
                        ),
                    }
            return {
                **base,
                "status": RESOLVED,
                "match_type": match_type,
                "evidence": "meridian_outputs_exact",
                "resolved_sha256": resolved_sha256,
                "reason": None,
            }

        # Basename tier: relocation-tolerant, but never authoritative. A
        # basename/label match is diagnostic evidence, not proof that this
        # exact DOCX media file came from this exact output. Even a single
        # candidate can be the wrong run's regenerated copy, and this tier
        # never carries the exact hash needed for a safe artifact binding.
        candidate_count = resolved.get("candidate_count") or 0
        if candidate_count > 1:
            return {
                **base,
                "status": UNRESOLVED,
                "match_type": match_type,
                "evidence": "meridian_outputs_basename_ambiguous",
                "resolved_sha256": None,
                "reason": (
                    f"{candidate_count} same-basename candidates found for "
                    f"{canonical_path!r} in meridian-outputs -- ambiguous "
                    "match, cannot bind with confidence"
                ),
            }
        return {
            **base,
            "status": UNRESOLVED,
            "match_type": match_type,
            "evidence": "meridian_outputs_basename_non_authoritative",
            "resolved_sha256": None,
            "reason": (
                "matched by relocated basename only -- basename/label "
                "evidence is diagnostic and cannot establish exact DOCX "
                "media-to-output provenance"
            ),
        }

    # Nothing in meridian-outputs' authoritative index at all -- per this
    # item's spec, fall back to directory-level evidence ONLY as a weaker,
    # non-authoritative signal (never enough on its own to call an artifact
    # RESOLVED).
    status_row = provenance_status.get_provenance_status(outputs_dir, canonical_path)
    if "error" in status_row:
        return {
            **base,
            "status": ORPHANED,
            "match_type": None,
            "evidence": "lookup_error",
            "resolved_sha256": None,
            "reason": status_row["error"],
        }

    if status_row.get("provenance_type") == provenance_status.DIRECTORY_FALLBACK:
        return {
            **base,
            "status": UNRESOLVED,
            "match_type": None,
            "evidence": "directory_fallback",
            "resolved_sha256": None,
            "reason": (
                f"no authoritative meridian-outputs record for "
                f"{canonical_path!r} -- only directory-level "
                f"{outputs_local.MERIDIAN_NOTES_FILENAME} evidence is "
                "available, which this item's spec treats as fallback "
                "evidence only, never authoritative"
            ),
        }

    # 3f758063 -- fail-closed on incomplete evidence. `status_row` already
    # carries `inconclusive` (provenance_status.get_provenance_status's own
    # signal that its outputs index has not finished walking this tree, so
    # a bare absence here is "not found YET", never "confirmed absent" --
    # see that function's own docstring). Before this fix, that signal was
    # computed but never consulted here: an artifact whose canonical output
    # genuinely exists but simply hasn't been reached yet by a still-
    # converging walk (a large/cold outputs_dir; resolve_figure_output's own
    # forced rebuild() above can legitimately run out of budget mid-pass,
    # same as any other rebuild() call -- see its own docstring) fell
    # straight through to the same confident ORPHANED verdict as a genuinely
    # absent one. A caller gating a write on `all_clear`/`status` would then
    # reject or quarantine a perfectly valid, not-yet-indexed artifact based
    # on a false "orphaned from any known provenance" claim -- exactly the
    # "provenance attachment reported unavailable when it's actually just
    # not confirmed yet" failure mode this item exists to close. Routed to
    # UNRESOLVED (not a new status): its existing contract already covers
    # "some evidence exists but is not strong enough to confirm", which is
    # precisely this case.
    if status_row.get("inconclusive"):
        return {
            **base,
            "status": UNRESOLVED,
            "match_type": None,
            "evidence": "index_not_converged",
            "resolved_sha256": None,
            "reason": (
                f"canonical_path {canonical_path!r} was not found by "
                "meridian-outputs, but its outputs index has not finished "
                "walking this outputs_dir yet (convergence incomplete) -- "
                "absence cannot be confirmed from incomplete evidence; "
                "re-check once the index has converged before treating "
                "this artifact as orphaned"
            ),
        }

    return {
        **base,
        "status": ORPHANED,
        "match_type": None,
        "evidence": "none",
        "resolved_sha256": None,
        "reason": (
            f"canonical_path {canonical_path!r} is not resolvable by "
            "meridian-outputs (no exact or basename match) and has no "
            "directory-level fallback evidence either -- this artifact is "
            "orphaned from any known provenance"
        ),
    }


def bind_artifact_provenance(
    outputs_dir: str,
    artifacts: "list[dict[str, Any]]",
    *,
    fuzzy_limit: int = 25,
) -> dict[str, Any]:
    """Join structural figure/table/equation artifacts to authoritative
    per-file provenance, and classify each fail-closed (sprint item 6d02f343).

    This is the "artifact manifest" join point: given a document's own
    structural artifact list (one entry per figure/table/equation the
    document currently embeds), resolve each against meridian-outputs'
    per-file provenance and classify it so a caller can reject or quarantine
    anything that isn't cleanly RESOLVED, instead of silently promoting an
    orphaned or hash-mismatched replacement.

    Args:
      outputs_dir: Absolute path to the outputs directory.
      artifacts:   One dict per structural artifact, each
                   ``{"artifact_id": <str>, "kind": <"figure"|"table"|
                   "equation">, "canonical_path": <str|None>,
                   "expected_sha256": <str|None>}``. ``artifact_id`` and
                   ``kind`` are carried through unchanged for the caller's
                   own bookkeeping (never interpreted here). Any extra keys
                   are ignored.
      fuzzy_limit: Forwarded to :func:`resolve_figure_output`'s basename tier.

    Returns:
      ``{"bindings": [...], "counts": {...}, "all_clear": bool}`` where each
      binding is
      ``{"artifact_id", "kind", "canonical_path", "status", "match_type",
      "evidence", "resolved_sha256", "generating_script", "reason"}`` and
      ``status`` is one of:

        - ``"resolved"``      -- authoritatively confirmed: an exact
          meridian-outputs record (hash match, when ``expected_sha256`` was
          requested AND the record has a hash on file), or an unambiguous
          relocation-tolerant basename match with no hash to contradict it.
        - ``"hash_mismatch"`` -- an exact meridian-outputs record exists for
          ``canonical_path`` and DOES have a hash on file, but it does not
          match ``expected_sha256``.
        - ``"orphaned"``      -- no meridian-outputs record at all (exact,
          basename, or directory-level) covers ``canonical_path``, AND the
          outputs index that failed to find it has fully converged (its
          absence is confirmed, not just "not found so far").
        - ``"unresolved"``    -- some evidence exists but is not strong
          enough to confirm: no ``canonical_path`` recorded on the artifact,
          an exact match whose record has no hash on file when
          ``expected_sha256`` was requested (the outputs walker's own
          size-prefilter can legitimately skip hashing a uniquely-sized
          file -- "no hash to compare" is never silently treated as "hash
          matches"), an ambiguous multi-candidate basename match, a basename
          match that cannot confirm a requested hash (that tier never
          carries one at all), ONLY non-authoritative directory-level
          fallback evidence, OR (``evidence="index_not_converged"``, item
          3f758063) no record found AND the outputs index has not finished
          walking ``outputs_dir`` yet -- fail-closed: incomplete evidence is
          never promoted to the confident ``"orphaned"`` verdict, since the
          artifact's real output may simply not have been indexed yet on a
          large/cold tree.

      ``counts`` tallies each status across ``bindings`` (always all four
      keys, zero-filled). ``all_clear`` is ``True`` only when every artifact
      is ``"resolved"`` -- ``False`` whenever anything should be rejected or
      quarantined. Never raises: an artifact this function cannot resolve at
      all still gets a binding entry (``"orphaned"``/``"unresolved"``), never
      an exception -- fail-closed means an explicit reject verdict, not a
      crash.

      ``generating_script`` (W31-A) -- the media-to-output-to-GENERATOR half
      of this binding: whatever :func:`resolve_figure_output` reported for
      ``canonical_path``, carried through unchanged on every binding it
      touched (``"resolved"``, ``"hash_mismatch"``, and both ``"unresolved"``
      basename-tier cases alike) rather than only on the authoritative exact
      match. ``None`` whenever ``resolve_figure_output`` itself found nothing
      (a directory-fallback/index-not-converged/orphaned binding, or one with
      no ``canonical_path`` at all) -- never fabricated. Same authority tier
      as the binding's own ``match_type``: on an exact match this is a
      confirmed generator identity; on a basename match it is diagnostic only
      (the same relocation-tolerant, non-authoritative caveat that already
      applies to the rest of that tier's evidence), never proof the exact
      DOCX media file came from that generator.
    """
    bindings = [
        _bind_one_artifact(outputs_dir, artifact, fuzzy_limit=fuzzy_limit)
        for artifact in (artifacts or [])
    ]
    counts = {status: 0 for status in ARTIFACT_STATUSES}
    for binding in bindings:
        counts[binding["status"]] = counts.get(binding["status"], 0) + 1
    all_clear = counts[RESOLVED] == len(bindings)
    return {"bindings": bindings, "counts": counts, "all_clear": all_clear}
