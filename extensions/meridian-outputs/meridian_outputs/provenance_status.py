"""Composed, authoritative per-file provenance status.

Sprint item bd5b8d79.

Prior state (confirmed by investigation)
-----------------------------------------
Two separate, disconnected systems in this package each answer some version
of "what do we know about this file":

  1. :mod:`meridian_outputs.annotate` -- narrow, MACHINE-oriented per-file
     provenance. :func:`annotate.get_provenance` does ONLY an exact
     normalized-path lookup into ``provenance_ledger.json`` and returns a
     bare ``None`` when the key isn't present.
  2. :mod:`meridian_outputs.outputs_local` -- a broader, human-oriented
     ``annotations`` table. ``_ingest_meridian_notes`` reads any
     ``MERIDIAN_NOTES.md`` found during the walk and records it as a
     DIRECTORY-level annotation; ``get_annotations_for_path`` then returns
     that note for every file underneath the directory it lives in, mixed in
     a flat list with true exact-path annotations.

Neither system, on its own, can answer "is a bare ``None`` from
``get_provenance`` because this path is completely unknown to the outputs
tree, or because it's a real, indexed output that just never had
``record_provenance`` called on it?" -- and neither surfaces the OTHER
system's weaker, directory-level signal as a distinguishable fallback when
no exact record exists.

What this module adds
----------------------
:func:`get_provenance_status` composes both systems (plus a best-effort
existence/hash staleness check, reusing :func:`fingerprint.script_content_hash`
-- the SAME hasher :mod:`fingerprint` already uses for its own script-
staleness ledger; no new hash scheme) into one ranked answer:

  1. ``"exact"``              -- an exact ``annotate`` record exists. Most
                                  authoritative; comes with a ``staleness``
                                  block (existence + content-hash check).
  2. ``"directory_fallback"`` -- no exact record, but a directory-level
                                  ``MERIDIAN_NOTES.md`` annotation (from
                                  ``outputs_local``) covers this path.
                                  Explicitly weaker -- never returned in the
                                  same shape as an exact hit.
  3. ``"unregistered"``       -- no exact record, no directory fallback, but
                                  ``outputs_local``'s own index has DISCOVERED
                                  this exact path (a real output that simply
                                  never had provenance recorded for it).
  4. ``"unknown"``            -- none of the above: this path has never been
                                  discovered by the outputs walker at all.

Neither :func:`annotate.get_provenance` nor
``outputs_local.get_annotations_for_path``/``get_indexed_output`` are
modified to produce this -- this module only reads from both (via
outputs_local's existing public module-level API, plus the two small
read-only helpers item bd5b8d79 added: ``get_indexed_output`` and
``get_path_annotations``) and ranks/labels what they already return.

NO hosted call is made anywhere in this module -- fully local, matching the
rest of the ``meridian_outputs`` package.

Hash- and convergence-awareness (sprint item d3374b0e)
--------------------------------------------------------
This module was extended, without changing any of the above contract, to add
the pieces item d3374b0e's acceptance criteria call for -- kept at genuine
parity with this same item's other deliverable, the dependency-light local
fallback ``tools/meridian_fallbacks/output_provenance_gate.py`` (same status
strings, same ledger formats, same SHA-256 algorithm):

  - A fifth explicit status, :data:`STALE_BY_SCRIPT` -- ranked ABOVE
    :data:`EXACT`. An exact provenance record can still be promoted to this
    status when the output was ALSO independently fingerprint-tagged
    (``fingerprint.tag_output``, a separate ledger from ``annotate``'s own --
    see that module's docstring) and the tagged generating-script's content
    hash no longer matches the script's CURRENT on-disk hash
    (:func:`_stale_by_script_result`, composing :func:`fingerprint.
    check_staleness`). This is a strictly stronger, more specific signal than
    the existing ``staleness`` block: the script that produced this output
    has itself changed, independent of whether the output FILE's own content
    ever changed.
  - New ``archival`` field (every branch) -- canonical/archival identity
    (``is_archival``/``canonical_path``/``sha256``), sourced from
    ``outputs_local.get_indexed_output_status``'s row (the SAME DuckDB row
    ``classify_canonical_archival`` already classified during indexing -- no
    new hashing here, no re-implementation of that heuristic).
  - New ``convergence`` field (every branch) and ``inconclusive`` flag
    (:data:`UNREGISTERED`/:data:`UNKNOWN` branch) -- an unconverged index is
    inconclusive, never proof of missing provenance. Reuses
    ``outputs_local.get_indexed_output_status``'s own ``degraded`` signal
    (True when its ``get_convergence_state()`` snapshot is not yet
    ``converged``) rather than re-deriving convergence logic here.
    ``inconclusive`` is additive-only: it never changes what
    ``provenance_type`` a caller sees (a previously-``UNKNOWN`` answer stays
    ``UNKNOWN``), it only tells a caller whether that answer is a confirmed
    absence or "not found yet, walk still in progress." :data:`EXACT`/
    :data:`STALE_BY_SCRIPT`/:data:`DIRECTORY_FALLBACK` are always
    ``inconclusive=False`` -- finding SOMETHING is never inconclusive,
    regardless of whether the rest of the tree has finished converging.

All of the above are purely ADDITIVE new dict keys -- every field this
module returned before item d3374b0e (``path``, ``provenance_type``,
``record``, ``directory_note``, ``staleness``) keeps the exact same shape and
values for every scenario the original implementation covered.
"""
from __future__ import annotations

import os
from typing import Any

from . import annotate, fingerprint, outputs_local

__all__ = [
    "EXACT",
    "DIRECTORY_FALLBACK",
    "UNREGISTERED",
    "UNKNOWN",
    "STALE_BY_SCRIPT",
    "get_provenance_status",
]

EXACT = "exact"
DIRECTORY_FALLBACK = "directory_fallback"
UNREGISTERED = "unregistered"
UNKNOWN = "unknown"
STALE_BY_SCRIPT = "stale_by_script"


def _staleness(query_path: str, record: dict[str, Any]) -> dict[str, Any]:
    """Best-effort existence/hash staleness check for an EXACT record.

    Checks the path AS RECORDED (``record["path"]``, falling back to the
    caller's ``query_path`` only if the record is somehow missing one) --
    normalized via :func:`annotate._normalize_path` (the SAME normalizer
    ``annotate``'s own ledger keys already use, per item bd5b8d79's
    instruction not to reinvent path normalization) so the existence/hash
    check is robust to a relative path recorded under a different CWD than
    the one this lookup is running from.

    Never raises: an unreadable/relocated file just makes ``exists_on_disk``
    False and ``current_content_hash`` None, both of which are themselves
    meaningful staleness signals, not failures.
    """
    recorded_path = record.get("path") or query_path
    normalized = annotate._normalize_path(recorded_path) or recorded_path
    exists = os.path.isfile(normalized)
    current_hash = fingerprint.script_content_hash(normalized) if exists else None
    recorded_hash = record.get("content_hash")

    stale = False
    reasons: list[str] = []
    if not exists:
        stale = True
        reasons.append(
            "recorded path no longer exists at its original location "
            "(relocated or deleted since provenance was recorded)"
        )
    elif recorded_hash is None:
        reasons.append(
            "no content hash was captured when this record was made -- "
            "staleness cannot be confirmed either way"
        )
    elif current_hash is None:
        reasons.append(
            "path exists but its current content could not be hashed -- "
            "staleness cannot be confirmed either way"
        )
    elif current_hash != recorded_hash:
        stale = True
        reasons.append(
            "current content hash differs from the hash recorded at "
            "provenance time -- the file has changed since"
        )
    else:
        reasons.append("content hash unchanged since provenance was recorded")

    return {
        "exists_on_disk": exists,
        "recorded_content_hash": recorded_hash,
        "current_content_hash": current_hash,
        "stale": stale,
        "reason": "; ".join(reasons),
    }


def _indexed_lookup(outputs_dir: str, path: str) -> dict[str, Any]:
    """Single-call composition of ``outputs_local.get_indexed_output_status``
    for this module's convergence- and archival-awareness (item d3374b0e).

    Reused across every branch of :func:`get_provenance_status` so ONE
    lookup answers three questions at once: is ``path`` a known indexed row
    (``row``), is that answer backed by a converged index (``degraded`` --
    see that function's own docstring for why a non-converged index must
    never be read as confirmed absence), and -- new here -- the row's own
    ``is_archival``/``canonical_path``/``sha256`` fields, surfaced as
    ``archival`` regardless of which ``provenance_type`` this call ultimately
    resolves to (a file can be BOTH e.g. exactly recorded AND a classified
    archival copy of some canonical twin).
    """
    status = outputs_local.get_indexed_output_status(outputs_dir, path)
    row = status.get("row")
    archival: dict[str, Any] | None = None
    if row is not None:
        archival = {
            "is_archival": bool(row.get("is_archival")),
            "canonical_path": row.get("canonical_path"),
            "sha256": row.get("sha256"),
        }
    return {
        "row": row,
        "degraded": bool(status.get("degraded")),
        "convergence": status.get("convergence"),
        "archival": archival,
    }


def _stale_by_script_result(outputs_dir: str, path: str) -> dict[str, Any] | None:
    """Cross-references :mod:`fingerprint`'s INDEPENDENT script-tagging
    ledger for ``path`` (item d3374b0e).

    ``annotate.record_provenance``'s ledger records WHAT produced an output
    (a ``generating_script`` name hint) and a snapshot of the OUTPUT's own
    content hash -- it does not itself track the generating SCRIPT's content
    hash; that is ``fingerprint.tag_output``'s job (a deliberately separate
    ledger, per that module's own docstring). This composes the two: if
    ``path`` was ALSO tagged via ``fingerprint.tag_output`` at some point,
    :func:`fingerprint.check_staleness` tells us whether the script that
    tagging pointed at has since changed -- the "was this regenerated under
    a now-fixed/now-different script version" signal this module could not
    previously surface at all.

    Returns ``None`` when ``path`` was never fingerprint-tagged (nothing to
    compare) -- this is NOT the same as "not stale"; callers must check for
    ``None`` before reading ``is_stale``. Otherwise the matching
    :class:`fingerprint.StalenessResult`, as a dict.
    """
    target = annotate._normalize_path(path)
    if not target:
        return None
    for result in fingerprint.check_staleness(outputs_dir):
        if annotate._normalize_path(result.path) == target:
            return result.to_dict()
    return None


def _directory_fallback(outputs_dir: str, path: str) -> dict[str, Any] | None:
    """The best (most path-specific, most recent) MERIDIAN_NOTES.md-sourced
    annotation covering ``path``, or ``None`` if none exists.

    ``outputs_local.get_path_annotations`` already returns exact-path AND
    ancestor-directory annotations sorted (exact-path-match first, then by
    recency) and mixed regardless of ``source`` -- this filters down to only
    the directory-note source, since a ``source="tool"`` annotation on this
    exact path is a DIFFERENT, broader human-notes system (out of scope for
    this "no exact provenance record" fallback).
    """
    for note in outputs_local.get_path_annotations(outputs_dir, path):
        if note.get("source") == outputs_local.MERIDIAN_NOTES_FILENAME:
            return note
    return None


def get_provenance_status(outputs_dir: str, path: str) -> dict[str, Any]:
    """The richer, authoritative answer to "what do we know about this
    file's provenance", composed from both underlying systems and ranked by
    confidence.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      path:         The output file to look up.

    Returns:
      ``{"error": ...}`` if ``outputs_dir``/``path`` are missing. Otherwise
      always ``{"path": path, "provenance_type": ..., "record": ...,
      "directory_note": ..., "staleness": ..., "script_staleness": ...,
      "archival": ..., "convergence": ..., "inconclusive": ...}`` where
      exactly one of ``record``/``directory_note`` is non-``None`` (or
      neither, for ``"unregistered"``/``"unknown"``) and ``provenance_type``
      is one of:

        - :data:`STALE_BY_SCRIPT` -- ``record`` is the exact
          ``annotate.get_provenance`` record (same as :data:`EXACT`), but
          ``script_staleness`` (see :func:`_stale_by_script_result`) shows
          the fingerprint-tagged generating script has changed content since
          tagging -- promoted above :data:`EXACT` because this is a
          stronger, more specific staleness signal (item d3374b0e).
        - :data:`EXACT` -- ``record`` is the exact ``annotate.get_provenance``
          record; ``staleness`` is populated (see :func:`_staleness`).
          ``script_staleness`` is ``None`` (never fingerprint-tagged) or
          not stale.
        - :data:`DIRECTORY_FALLBACK` -- ``directory_note`` is the covering
          ``MERIDIAN_NOTES.md`` annotation; ``staleness`` is ``None`` (a
          directory-level note has no single file to check staleness
          against).
        - :data:`UNREGISTERED` -- this exact path IS indexed/known to
          ``outputs_local``, but no provenance record and no directory note
          cover it. ``record``/``directory_note``/``staleness`` all ``None``.
        - :data:`UNKNOWN` -- none of the above: this path has never been
          discovered by the outputs walker at all. Same ``None`` shape as
          ``UNREGISTERED`` -- distinguish the two by ``provenance_type``
          alone, never by inferring from field presence. Check
          ``inconclusive`` before treating this as confirmed absence -- an
          unconverged index makes this "not found yet," not "confirmed
          absent" (item d3374b0e).

      ``archival`` (``{"is_archival", "canonical_path", "sha256"}`` or
      ``None`` if ``path`` was never indexed) and ``convergence`` (the
      index's :class:`outputs_local.ConvergenceState`-shaped dict, or
      ``None``) are populated on every branch. ``inconclusive`` is ``True``
      only when ``provenance_type`` is :data:`UNREGISTERED`/:data:`UNKNOWN`
      AND the index has not yet converged -- always ``False`` for the other
      three statuses (finding something is never inconclusive).
    """
    if not outputs_dir or not str(outputs_dir).strip():
        return {"error": "outputs_dir is required"}
    if not path or not str(path).strip():
        return {"error": "path is required"}

    indexed = _indexed_lookup(outputs_dir, path)

    record = annotate.get_provenance(outputs_dir, path)
    if record is not None:
        script_staleness = _stale_by_script_result(outputs_dir, path)
        provenance_type = (
            STALE_BY_SCRIPT
            if script_staleness is not None and script_staleness.get("is_stale")
            else EXACT
        )
        return {
            "path": path,
            "provenance_type": provenance_type,
            "record": record,
            "directory_note": None,
            "staleness": _staleness(path, record),
            "script_staleness": script_staleness,
            "archival": indexed["archival"],
            "convergence": indexed["convergence"],
            "inconclusive": False,
        }

    directory_note = _directory_fallback(outputs_dir, path)
    if directory_note is not None:
        return {
            "path": path,
            "provenance_type": DIRECTORY_FALLBACK,
            "record": None,
            "directory_note": directory_note,
            "staleness": None,
            "script_staleness": None,
            "archival": indexed["archival"],
            "convergence": indexed["convergence"],
            "inconclusive": False,
        }

    provenance_type = UNREGISTERED if indexed["row"] is not None else UNKNOWN
    inconclusive = indexed["row"] is None and indexed["degraded"]
    return {
        "path": path,
        "provenance_type": provenance_type,
        "record": None,
        "directory_note": None,
        "staleness": None,
        "script_staleness": None,
        "archival": indexed["archival"],
        "convergence": indexed["convergence"],
        "inconclusive": inconclusive,
    }
