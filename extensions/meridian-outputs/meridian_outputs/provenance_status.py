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
    "get_provenance_status",
]

EXACT = "exact"
DIRECTORY_FALLBACK = "directory_fallback"
UNREGISTERED = "unregistered"
UNKNOWN = "unknown"


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
      "directory_note": ..., "staleness": ...}`` where exactly one of
      ``record``/``directory_note`` is non-``None`` (or neither, for
      ``"unregistered"``/``"unknown"``) and ``provenance_type`` is one of:

        - :data:`EXACT` -- ``record`` is the exact ``annotate.get_provenance``
          record; ``staleness`` is populated (see :func:`_staleness`).
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
          alone, never by inferring from field presence.
    """
    if not outputs_dir or not str(outputs_dir).strip():
        return {"error": "outputs_dir is required"}
    if not path or not str(path).strip():
        return {"error": "path is required"}

    record = annotate.get_provenance(outputs_dir, path)
    if record is not None:
        return {
            "path": path,
            "provenance_type": EXACT,
            "record": record,
            "directory_note": None,
            "staleness": _staleness(path, record),
        }

    directory_note = _directory_fallback(outputs_dir, path)
    if directory_note is not None:
        return {
            "path": path,
            "provenance_type": DIRECTORY_FALLBACK,
            "record": None,
            "directory_note": directory_note,
            "staleness": None,
        }

    indexed = outputs_local.get_indexed_output(outputs_dir, path)
    provenance_type = UNREGISTERED if indexed is not None else UNKNOWN
    return {
        "path": path,
        "provenance_type": provenance_type,
        "record": None,
        "directory_note": None,
        "staleness": None,
    }
