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

from . import outputs_local, provenance_status

__all__ = [
    "resolve_figure_output",
    "find_outputs_by_source",
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
    base = {
        "artifact_id": artifact_id,
        "kind": kind,
        "canonical_path": canonical_path,
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

        # Basename tier: relocation-tolerant, but resolve_figure_output never
        # attaches a hash for this tier (see its own docstring) -- an
        # ambiguous or hash-unconfirmable basename match can never be
        # promoted to RESOLVED when the caller asked for a hash check.
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
        if expected_sha256:
            return {
                **base,
                "status": UNRESOLVED,
                "match_type": match_type,
                "evidence": "meridian_outputs_basename",
                "resolved_sha256": None,
                "reason": (
                    "matched by relocated basename only -- meridian-outputs "
                    "has no hash on file for this tier, so this artifact's "
                    "expected hash cannot be confirmed"
                ),
            }
        return {
            **base,
            "status": RESOLVED,
            "match_type": match_type,
            "evidence": "meridian_outputs_basename",
            "resolved_sha256": None,
            "reason": None,
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
      "evidence", "resolved_sha256", "reason"}`` and ``status`` is one of:

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
