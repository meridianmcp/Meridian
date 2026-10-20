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
one on top of this module's basename tier. ``artifact_registry`` never
imports this module; the only dependency in the other direction is
:func:`get_output_lineage` (gap set C), which reads the registry, the
provenance and fingerprint ledgers, and the run-manifest ledger through
deferred imports to join them into one lineage view.
"""
from __future__ import annotations

import os
from typing import Any

from . import ledger_io, outputs_local, provenance_status

__all__ = [
    "resolve_figure_output",
    "find_outputs_by_source",
    "trace_forward_outputs",
    "classify_temp_output_ownership",
    "bind_artifact_provenance",
    "get_output_lineage",
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


def _ledger_unreadable_binding(artifact: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    """UNRESOLVED binding for an artifact whose lookup hit a store that
    cannot be read. A ``LedgerCorruptError`` names only the ledger's
    basename; any other error is reduced to its type name, since an
    ``OSError``'s text can carry a machine-local absolute path."""
    detail = str(exc) if isinstance(exc, ledger_io.LedgerCorruptError) else type(exc).__name__
    return {
        "artifact_id": artifact.get("artifact_id"),
        "kind": artifact.get("kind"),
        "canonical_path": artifact.get("canonical_path"),
        "generating_script": None,
        "status": UNRESOLVED,
        "match_type": None,
        "evidence": "ledger_unreadable",
        "resolved_sha256": None,
        "reason": (
            f"a meridian-outputs provenance store could not be read ({detail}) "
            "-- this artifact's provenance cannot be confirmed either way"
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
          large/cold tree, OR (``evidence="ledger_unreadable"``) a
          provenance store this artifact's lookup needed could not be read
          (a corrupt ledger or an ``OSError``) -- that artifact gets its own
          unresolved binding and every other artifact is still classified.

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
    bindings: list[dict[str, Any]] = []
    for artifact in (artifacts or []):
        # One unreadable store must not cost the other artifacts their
        # verdicts: it becomes this artifact's own UNRESOLVED binding.
        try:
            binding = _bind_one_artifact(outputs_dir, artifact, fuzzy_limit=fuzzy_limit)
        except (ledger_io.LedgerCorruptError, OSError) as exc:
            binding = _ledger_unreadable_binding(artifact, exc)
        bindings.append(binding)
    counts = {status: 0 for status in ARTIFACT_STATUSES}
    for binding in bindings:
        counts[binding["status"]] = counts.get(binding["status"], 0) + 1
    all_clear = counts[RESOLVED] == len(bindings)
    return {"bindings": bindings, "counts": counts, "all_clear": all_clear}


# ---------------------------------------------------------------------------
# Gap set C, "joined-lineage": output -> its generating sources -> their own
# provenance records, as ONE view.
#
# Three stores each record an output -> source link, and nothing reconciled
# them: the artifact registry's SourceEdges, the provenance ledger's
# ``generating_script`` (record_provenance), and the fingerprint ledger's
# ``generating_script``/``script_path`` (tag_output). get_output_lineage
# unions all three, tags every source with the store it came from, and never
# collapses two origins into one entry -- when the stores disagree, the
# disagreement stays visible. The view is fail-closed: ``complete`` is True
# only when at least one source is known and every source, the run manifest
# (if the artifact names a run), and every store read resolved cleanly.
#
# The result is portable: every path is outputs_dir-relative or a relative
# source reference, never a machine-local absolute path (the registry's
# local_only_path values are stripped via strip_local_metadata).
# ---------------------------------------------------------------------------

LINEAGE_ORIGIN_REGISTRY = "artifact_registry"
LINEAGE_ORIGIN_PROVENANCE = "provenance_record"
LINEAGE_ORIGIN_FINGERPRINT = "fingerprint_ledger"
_LINEAGE_ORIGIN_RANK = {
    LINEAGE_ORIGIN_REGISTRY: 0,
    LINEAGE_ORIGIN_PROVENANCE: 1,
    LINEAGE_ORIGIN_FINGERPRINT: 2,
}

# Per-source resolution statuses. Only SOURCE_RECORDED counts as resolved.
SOURCE_RECORDED = "recorded"
SOURCE_NO_PROVENANCE_RECORD = "no_provenance_record"
SOURCE_NOT_FOUND = "not_found"
SOURCE_OUTSIDE_OUTPUTS_DIR = "not_recorded_outside_outputs_dir"
# Only a bare file name was stored for a generating script (the basename
# fallback for a script on another volume) and no file of that name exists
# under outputs_dir or the working directory: its location is unknown.
SOURCE_LOCATION_UNRECORDED = "script_location_unrecorded"


def _portable_locator(outputs_dir: str, value: Any) -> str | None:
    """A ledger locator safe to return: never a machine-local absolute path.

    Relative values pass through (slashes normalised). An absolute value is
    rewritten as a reference relative to ``outputs_dir``, or reduced to its
    basename when it sits on a different volume (the same fallback
    annotate/fingerprint use when they persist a generating script).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if not os.path.isabs(raw):
        return raw.replace("\\", "/")
    from .path_security import relative_reference

    return relative_reference(outputs_dir, raw) or os.path.basename(raw.replace("\\", "/"))


def _relative_output(outputs_dir: str, path: Any) -> str | None:
    from .path_security import relative_output_path

    try:
        return relative_output_path(outputs_dir, path)
    except (TypeError, ValueError):
        return _portable_locator(outputs_dir, path)


def _portable_provenance_record(outputs_dir: str, record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    clean = dict(record)
    if "path" in clean:
        clean["path"] = _relative_output(outputs_dir, clean.get("path"))
    if "generating_script" in clean:
        clean["generating_script"] = _portable_locator(outputs_dir, clean.get("generating_script"))
    return clean


def _portable_fingerprint_entry(outputs_dir: str, entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    clean = dict(entry)
    for key in ("path", "script_path", "generating_script"):
        if key in clean:
            clean[key] = _portable_locator(outputs_dir, clean.get(key))
    return clean


def _portable_artifact(outputs_dir: str, record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    from . import artifact_registry

    clean = artifact_registry.strip_local_metadata(record)
    for key in ("generator", "source_locator"):
        if key in clean:
            clean[key] = _portable_locator(outputs_dir, clean.get(key))
    return clean


def _same_output(outputs_dir: str, ledger_path: Any, target_real: str) -> bool:
    from .path_security import resolve_reference

    if not isinstance(ledger_path, str) or not ledger_path.strip():
        return False
    try:
        resolved = resolve_reference(outputs_dir, ledger_path)
    except (OSError, TypeError, ValueError):
        return False
    return os.path.normcase(os.path.normpath(resolved)) == os.path.normcase(os.path.normpath(target_real))


def _locate_source(outputs_dir: str, locator: str) -> tuple[str, str | None]:
    """``("inside", <real path>)`` or ``("outside", None)``.

    A relative locator is read relative to ``outputs_dir`` (the convention
    every ledger in this package uses for stored references); the current
    working directory is never consulted, so the answer does not depend on
    where the server process happens to run.
    """
    from .path_security import _require_within

    root = os.path.realpath(os.path.abspath(outputs_dir))
    candidate = locator if os.path.isabs(locator) else os.path.join(root, locator)
    try:
        return "inside", _require_within(outputs_dir, candidate)
    except ValueError:
        return "outside", None


def _resolve_lineage_source(
    outputs_dir: str,
    *,
    origin: str,
    locator: str,
    relation: str,
    location_hint: str | None = None,
    edge_id: str | None = None,
) -> dict[str, Any]:
    """Classify one source and attach its own provenance record if it has one."""
    from . import annotate

    where, real = _locate_source(outputs_dir, location_hint or locator)
    entry: dict[str, Any] = {
        "origin": origin,
        "locator": locator,
        "relation": relation,
        "scope": "output",
        "location": "inside_outputs_dir" if where == "inside" else "outside_outputs_dir",
        "path": None,
        "status": SOURCE_OUTSIDE_OUTPUTS_DIR,
        "resolved": False,
        "provenance_record": None,
        "stale": None,
        "stale_reason": None,
        "recorded_script_hash": None,
        "current_script_hash": None,
    }
    if edge_id is not None:
        entry["edge_id"] = edge_id
    if where != "inside" or real is None:
        return entry

    entry["path"] = _relative_output(outputs_dir, real)
    try:
        record = annotate.get_provenance(outputs_dir, real)
    except Exception as exc:  # noqa: BLE001 -- a store failure is reported, never raised
        entry["status"] = SOURCE_NO_PROVENANCE_RECORD
        entry["store_error"] = type(exc).__name__
        return entry
    if record is not None:
        entry["status"] = SOURCE_RECORDED
        entry["resolved"] = True
        entry["provenance_record"] = _portable_provenance_record(outputs_dir, record)
    elif os.path.exists(real):
        entry["status"] = SOURCE_NO_PROVENANCE_RECORD
    else:
        entry["status"] = SOURCE_NOT_FOUND
    return entry


def _is_bare_name(value: Any) -> bool:
    """True for a stored script reference with no directory part
    (``"gen.py"``), which is all a cross-volume script leaves behind."""
    return (
        isinstance(value, str) and bool(value.strip()) and not os.path.isabs(value)
        and "/" not in value and "\\" not in value
    )


def _check_registry_readable(outputs_dir: str) -> None:
    """Strict read of the artifact registry for :func:`get_output_lineage`.

    ``artifact_registry``'s reader returns an empty registry for a file it
    cannot parse, so without this check a damaged registry looks like "no
    artifact registered" and the lineage can still report ``complete``. A
    missing registry is fine (nothing was registered). Raises
    ``ledger_io.LedgerCorruptError`` when the file exists but is not a JSON
    object whose ``artifacts`` is an object and ``edges`` a list; other
    ``OSError`` read failures propagate.
    """
    from . import artifact_registry

    path = artifact_registry._registry_path(outputs_dir, create=False)
    data = ledger_io.read_ledger_strict(path)
    if not isinstance(data.get("artifacts", {}), dict) or not isinstance(data.get("edges", []), list):
        raise ledger_io.LedgerCorruptError(
            path, "artifacts is not an object or edges is not a list",
        )


def _set_script_staleness(entry: dict[str, Any], recorded: str | None, current: str | None,
                          *, script_known: bool) -> None:
    """Fill the stale fields the way fingerprint.check_staleness reasons,
    except that an absent recorded hash is reported as unknown (None)
    rather than silently "not stale"."""
    entry["recorded_script_hash"] = recorded
    entry["current_script_hash"] = current
    if not script_known:
        entry["stale"] = None
        entry["stale_reason"] = "no generating script was resolved when this link was recorded"
    elif current is None:
        entry["stale"] = True
        entry["stale_reason"] = "generating script no longer readable/present"
    elif recorded is None:
        entry["stale"] = None
        entry["stale_reason"] = "no script hash was recorded, so staleness cannot be confirmed"
    elif current != recorded:
        entry["stale"] = True
        entry["stale_reason"] = "generating script content changed since it was recorded"
    else:
        entry["stale"] = False
        entry["stale_reason"] = "generating script unchanged since it was recorded"


def get_output_lineage(
    outputs_dir: str,
    *,
    path: str | None = None,
    artifact_id: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """Joined lineage for one output: its sources from every store, each
    source's own provenance record, and the run inputs, as ONE view.

    Exactly how the stores are joined:

      1. The output is located by ``path`` (validated to lie inside
         ``outputs_dir``) and/or ``artifact_id``. A path is matched to a
         registry artifact through ``artifact_registry.resolve_artifact``
         (content hash, then exact prior path sighting -- never a basename
         guess); an ``artifact_id`` without a ``path`` uses the artifact's
         most recent local sighting inside ``outputs_dir``. The registry is
         read strictly first: a registry file that exists but cannot be
         parsed is a ``store_unreadable`` gap, never read as empty.
      2. Sources are the UNION of the registry's SourceEdges for that
         artifact, the provenance ledger's ``generating_script`` and the
         fingerprint ledger's ``generating_script`` (or ``script_path``).
         Every source carries ``origin`` naming its store; the same script
         seen by two stores appears twice, once per origin, never merged.
      3. Each source inside ``outputs_dir`` gets its own provenance record
         attached (``status="recorded"``) or is marked
         ``no_provenance_record``/``not_found``. A source outside
         ``outputs_dir`` is marked ``not_recorded_outside_outputs_dir``.
         A provenance record whose generating script was stored as a bare
         file name (the cross-volume basename fallback) that is found
         nowhere is marked ``script_location_unrecorded`` with ``stale``
         ``None``. Only ``recorded`` counts as resolved.
      4. ``stale`` compares the script hash a store recorded with the
         script's current hash: the fingerprint ledger's tag-time hash, and
         the provenance record's ``script_content_hash`` when present.
      5. When the artifact names a ``run_id`` with a run manifest, the run's
         input hashes are attached under ``run.inputs`` with
         ``scope="run"`` (they describe the whole run, not this output).
         Each ``sha256`` is the run's ``input_stat`` hash (the input
         resolved against ``outputs_dir``, the file input staleness
         checks), or its ``input_identity`` hash for a run recorded before
         ``input_stat`` existed.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      path:         The output file (inside ``outputs_dir``).
      artifact_id:  A registered artifact id, instead of or with ``path``.
      depth:        Hops to follow. Only 1 is accepted: deeper lineage is
                    reserved (it needs a visited set) and any other value is
                    an error rather than a silently shallower answer.

    Returns:
      ``{"error": ...}`` for invalid arguments (missing ``outputs_dir``, a
      ``path`` outside it, neither ``path`` nor ``artifact_id``, or a
      ``depth`` other than 1) -- never a traceback. Otherwise ``{output,
      sources, run, complete, unresolved}``:

        - ``output``: ``{path, artifact_id, artifact_evidence, artifact,
          provenance_record, fingerprint, provenance_status}`` with every
          path made portable.
        - ``sources``: one dict per (origin, locator) -- ``{origin,
          locator, relation, scope, location, path, status, resolved,
          provenance_record, stale, stale_reason, recorded_script_hash,
          current_script_hash}`` plus ``edge_id`` for registry edges --
          sorted by origin (registry, provenance, fingerprint), then
          locator.
        - ``run``: ``None`` when no run is linked, else ``{run_id, found,
          phase, manifest_hash, command, inputs}``. The run ledger is read
          strictly: when it cannot be read, ``found`` is ``None`` (unknown)
          and the gap is ``store_unreadable``, never ``run_manifest_not_found``.
        - ``complete``: True only when ``sources`` is non-empty and
          ``unresolved`` is empty (fail closed).
        - ``unresolved``: one dict per gap: an unresolved source, an
          ambiguous or unknown artifact, a missing run manifest, an
          unreadable store, or no lineage recorded at all.
    """
    from . import annotate, artifact_registry, fingerprint
    from . import run_manifest as run_manifest_mod
    from .path_security import _require_within, resolve_reference

    if not outputs_dir or not str(outputs_dir).strip():
        return {"error": "outputs_dir is required"}
    if not os.path.isdir(outputs_dir):
        return {"error": "outputs_dir does not exist"}
    if isinstance(depth, bool) or not isinstance(depth, int) or depth != 1:
        return {
            "error": "depth must be 1: deeper lineage is reserved and not implemented",
        }
    has_path = bool(path and str(path).strip())
    has_id = bool(artifact_id and str(artifact_id).strip())
    if not has_path and not has_id:
        return {"error": "path or artifact_id is required"}

    target_real: str | None = None
    if has_path:
        try:
            target_real = _require_within(outputs_dir, str(path))
        except ValueError as exc:
            return {"error": str(exc)}

    unresolved: list[dict[str, Any]] = []

    # -- 1. the output and its registry artifact -----------------------------
    artifact: dict[str, Any] | None = None
    artifact_evidence: str | None = None
    try:
        # artifact_registry's own reader turns a damaged registry into an
        # empty one; read it strictly first so damage is a store gap here.
        _check_registry_readable(outputs_dir)
        if has_id:
            artifact = artifact_registry.get_artifact(outputs_dir, str(artifact_id))
            if artifact is None:
                unresolved.append({
                    "kind": "artifact", "artifact_id": artifact_id,
                    "reason": "artifact_not_registered",
                })
            else:
                artifact_evidence = "artifact_id"
        elif target_real is not None:
            hit = artifact_registry.resolve_artifact(outputs_dir, canonical_path=target_real)
            if hit["status"] == artifact_registry.RESOLVED:
                artifact = hit["record"]
                artifact_evidence = hit["evidence"]
            elif hit["status"] == artifact_registry.AMBIGUOUS:
                unresolved.append({
                    "kind": "artifact", "reason": "artifact_ambiguous",
                    "candidates": list(hit["candidates"]),
                })
    except Exception as exc:  # noqa: BLE001 -- store failure is a gap, not a crash
        unresolved.append({
            "kind": "store", "store": LINEAGE_ORIGIN_REGISTRY,
            "reason": "store_unreadable", "error_type": type(exc).__name__,
        })

    if target_real is None and artifact is not None:
        sightings = sorted(
            artifact.get("local_paths") or [],
            key=lambda e: e.get("last_seen") or "", reverse=True,
        )
        for sighting in sightings:
            candidate = sighting.get("local_only_path")
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            try:
                target_real = _require_within(outputs_dir, candidate)
                break
            except ValueError:
                continue
        if target_real is None:
            unresolved.append({
                "kind": "output", "artifact_id": artifact.get("artifact_id"),
                "reason": "output_path_unknown",
            })

    provenance_record: dict[str, Any] | None = None
    fingerprint_entry: dict[str, Any] | None = None
    status_summary: dict[str, Any] | None = None
    if target_real is not None:
        try:
            provenance_record = annotate.get_provenance(outputs_dir, target_real)
        except Exception as exc:  # noqa: BLE001
            unresolved.append({
                "kind": "store", "store": LINEAGE_ORIGIN_PROVENANCE,
                "reason": "store_unreadable", "error_type": type(exc).__name__,
            })
        try:
            ledger = fingerprint._read_ledger(outputs_dir)
            for key in sorted(ledger):
                entry = ledger[key]
                if isinstance(entry, dict) and _same_output(
                    outputs_dir, entry.get("path") or key, target_real,
                ):
                    fingerprint_entry = dict(entry)
                    break
        except Exception as exc:  # noqa: BLE001
            unresolved.append({
                "kind": "store", "store": LINEAGE_ORIGIN_FINGERPRINT,
                "reason": "store_unreadable", "error_type": type(exc).__name__,
            })
        try:
            status_row = provenance_status.get_provenance_status(outputs_dir, target_real)
            staleness = status_row.get("staleness") or {}
            # Summary fields only: the full row repeats the record and its
            # free-text reasons can embed absolute paths (RELOCATED branch).
            status_summary = {
                "provenance_type": status_row.get("provenance_type"),
                "stale": staleness.get("stale"),
                "inconclusive": status_row.get("inconclusive"),
            }
        except Exception as exc:  # noqa: BLE001 -- output status is advisory
            status_summary = {"error": type(exc).__name__}

    # -- 2/3/4. sources from every store, never merged across origins -------
    sources: list[dict[str, Any]] = []
    if artifact is not None:
        try:
            edges = artifact_registry.get_artifact_sources(outputs_dir, artifact["artifact_id"])
        except Exception as exc:  # noqa: BLE001
            edges = []
            unresolved.append({
                "kind": "store", "store": LINEAGE_ORIGIN_REGISTRY,
                "reason": "store_unreadable", "error_type": type(exc).__name__,
            })
        for edge in edges:
            locator = _portable_locator(outputs_dir, edge.get("source_locator"))
            if not locator:
                continue
            sources.append(_resolve_lineage_source(
                outputs_dir, origin=LINEAGE_ORIGIN_REGISTRY, locator=locator,
                relation=str(edge.get("relation") or "produced_by"),
                edge_id=edge.get("edge_id"),
            ))

    if provenance_record is not None:
        locator = _portable_locator(outputs_dir, provenance_record.get("generating_script"))
        if locator:
            entry = _resolve_lineage_source(
                outputs_dir, origin=LINEAGE_ORIGIN_PROVENANCE, locator=locator,
                relation="generating_script",
            )
            recorded = provenance_record.get("script_content_hash")
            # Resolve the script exactly as record_provenance did when it
            # captured the hash, so the comparison is like for like.
            stored_script = provenance_record["generating_script"]
            script_file = fingerprint._resolve_script_path(stored_script, search_root=outputs_dir)
            if script_file is None and _is_bare_name(stored_script):
                # Only a file name was stored (record_provenance keeps just
                # the basename of a script on another volume) and nothing of
                # that name is found: the script's location was never
                # recorded, so neither "not found" nor "stale" follows.
                if entry["status"] == SOURCE_NOT_FOUND:
                    entry["status"] = SOURCE_LOCATION_UNRECORDED
                entry["recorded_script_hash"] = recorded
                entry["stale"] = None
                entry["stale_reason"] = (
                    "only the generating script's file name was recorded and no "
                    "file of that name was found, so its location and staleness "
                    "are unknown"
                )
            elif recorded is not None:
                current = fingerprint.script_content_hash(script_file) if script_file else None
                _set_script_staleness(entry, recorded, current, script_known=True)
            else:
                entry["stale_reason"] = (
                    "no script hash was recorded, so staleness cannot be confirmed"
                )
            sources.append(entry)

    if fingerprint_entry is not None:
        script_ref = fingerprint_entry.get("script_path")
        locator = (
            _portable_locator(outputs_dir, fingerprint_entry.get("generating_script"))
            or _portable_locator(outputs_dir, script_ref)
        )
        if locator:
            hint = _portable_locator(outputs_dir, script_ref)
            entry = _resolve_lineage_source(
                outputs_dir, origin=LINEAGE_ORIGIN_FINGERPRINT, locator=locator,
                relation="generating_script", location_hint=hint,
            )
            recorded = (
                fingerprint._digest_hex(fingerprint_entry.get("content_digest"))
                or fingerprint_entry.get("script_hash")
            )
            current = (
                fingerprint.script_content_hash(resolve_reference(outputs_dir, script_ref))
                if isinstance(script_ref, str) and script_ref else None
            )
            _set_script_staleness(
                entry, recorded, current,
                script_known=isinstance(script_ref, str) and bool(script_ref),
            )
            sources.append(entry)

    sources.sort(key=lambda s: (
        _LINEAGE_ORIGIN_RANK.get(s["origin"], 99), s["locator"], s["relation"],
        s.get("edge_id") or "",
    ))
    for src in sources:
        if not src["resolved"]:
            gap = {
                "kind": "source", "origin": src["origin"], "locator": src["locator"],
                "reason": src["status"],
            }
            if "store_error" in src:
                gap["error_type"] = src["store_error"]
            unresolved.append(gap)

    # -- 5. run-scoped inputs ------------------------------------------------
    run: dict[str, Any] | None = None
    run_id = artifact.get("run_id") if artifact is not None else None
    if isinstance(run_id, str) and run_id.strip():
        manifest = None
        store_unreadable = False
        try:
            # Strict: get_run_manifest reads a damaged ledger as empty, which
            # would report this run as not found instead of a store gap.
            manifest = run_manifest_mod._read_ledger_strict(outputs_dir).get(run_id.strip())
        except Exception as exc:  # noqa: BLE001
            store_unreadable = True
            unresolved.append({
                "kind": "store", "store": "run_manifest",
                "reason": "store_unreadable", "error_type": type(exc).__name__,
            })
        if manifest is None:
            # An unreadable ledger leaves the run unknown (found None), not
            # absent: only a readable ledger without it is run_manifest_not_found.
            run = {"run_id": run_id, "found": None if store_unreadable else False,
                   "phase": None, "manifest_hash": None, "command": None, "inputs": []}
            if not store_unreadable:
                unresolved.append({"kind": "run", "run_id": run_id, "reason": "run_manifest_not_found"})
        else:
            file_hashes = (manifest.get("input_identity") or {}).get("file_hashes") or {}
            # file_hashes hashes each input as given (a relative one against
            # the start-time working directory); input_stat hashes the file
            # resolved against outputs_dir, the one staleness checks. Report
            # that one when the run recorded it, file_hashes otherwise.
            input_stat = manifest.get("input_stat")
            if not isinstance(input_stat, dict):
                input_stat = {}

            def _input_sha256(raw: str, digest: Any) -> Any:
                try:
                    key = run_manifest_mod._input_key(
                        outputs_dir, run_manifest_mod._resolve_ref(outputs_dir, raw),
                    )
                except (OSError, TypeError, ValueError):
                    return digest
                stat_entry = input_stat.get(key)
                return stat_entry.get("sha256") if isinstance(stat_entry, dict) else digest

            inputs = [
                {
                    "locator": _portable_locator(outputs_dir, raw) or "",
                    "sha256": _input_sha256(raw, digest),
                    "scope": "run",
                    "origin": "run_manifest",
                }
                for raw, digest in file_hashes.items()
            ]
            inputs.sort(key=lambda item: item["locator"])
            run = {
                "run_id": run_id,
                "found": True,
                "phase": manifest.get("phase"),
                "manifest_hash": manifest.get("manifest_hash"),
                "command": (manifest.get("command_identity") or {}).get("tool_name"),
                "inputs": inputs,
            }

    if not sources:
        unresolved.append({"kind": "output", "reason": "no_source_recorded"})

    output = {
        "path": _relative_output(outputs_dir, target_real) if target_real else None,
        "artifact_id": artifact.get("artifact_id") if artifact is not None else (
            artifact_id if has_id else None
        ),
        "artifact_evidence": artifact_evidence,
        "artifact": _portable_artifact(outputs_dir, artifact),
        "provenance_record": _portable_provenance_record(outputs_dir, provenance_record),
        "fingerprint": _portable_fingerprint_entry(outputs_dir, fingerprint_entry),
        "provenance_status": status_summary,
    }
    return {
        "output": output,
        "sources": sources,
        "run": run,
        "complete": bool(sources) and not unresolved,
        "unresolved": unresolved,
    }
