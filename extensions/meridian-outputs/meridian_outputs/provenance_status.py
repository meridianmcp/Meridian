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

Typed research-evidence bridge (sprint item 0ea8fd3c)
------------------------------------------------------
:mod:`research_evidence` (same package) defines a canonical, lossless
:class:`~research_evidence.ProvenanceEnvelope` model -- typed
:class:`~research_evidence.EvidenceRecord` nodes with an explicit six-state
:class:`~research_evidence.ResolverStatus` (verified/stale/held/ambiguous/
unavailable/degraded) plus a ``confidence`` float, instead of this module's
own bespoke four/five-string ``provenance_type`` enum. Neither module changed
its OWN existing contract for this -- every field :func:`get_provenance_status`
already returned keeps the exact same shape/values as before -- this is a pure
ADDITION on top: :func:`evidence_record_from_provenance_status` maps one
:func:`get_provenance_status` dict onto a typed
:class:`~research_evidence.EvidenceRecord` (kind
:data:`~research_evidence.EvidenceKind.OUTPUT`), and
:func:`build_provenance_envelope` packages a whole batch of paths into one
:class:`~research_evidence.ProvenanceEnvelope`.

The ``provenance_type`` -> :class:`~research_evidence.ResolverStatus` mapping
(see :func:`_resolver_state_for_provenance_status` for the exact rules):

  - :data:`STALE_BY_SCRIPT` -> ``STALE`` -- the generating script itself has
    since changed content; the strongest, most specific staleness signal this
    module has.
  - :data:`EXACT` -> ``VERIFIED`` when the output's own content hash still
    matches what was recorded, ``STALE`` when it does not, or ``AMBIGUOUS``
    when there simply isn't enough hash data to say either way (e.g. no
    content hash was ever captured) -- never silently reported as verified
    just because an exact record exists.
  - :data:`DIRECTORY_FALLBACK` -> ``DEGRADED`` -- a real signal, but a
    directory-wide one, never as strong as a file-specific exact record.
  - :data:`UNREGISTERED` -> ``HELD`` (a known, indexed path whose provenance
    capture is simply pending) unless the index has not yet converged, in
    which case ``AMBIGUOUS`` (matches this module's own ``inconclusive``
    semantics -- see :func:`get_provenance_status`'s own docstring).
  - :data:`UNKNOWN` -> ``UNAVAILABLE`` when the index has converged (a
    confirmed absence), or ``AMBIGUOUS`` when it has not (not confirmed
    either way yet) -- exactly mirroring this module's own ``inconclusive``
    flag, never a blanket "not found."

:attr:`~research_evidence.EvidenceRecord.partial` is set (with a required
``partial_reason``) exactly for :data:`UNREGISTERED`/:data:`UNKNOWN` --
per this codebase's "partial records ... never presented as authoritative"
requirement, a record built from either of those two provenance_type values
is, by construction, known-incomplete (no exact record/directory note was
ever captured for it), regardless of how confident the resolver mapping
above is about its VERIFIED/STALE/etc status.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from . import annotate, fingerprint, outputs_local, research_evidence

__all__ = [
    "EXACT",
    "DIRECTORY_FALLBACK",
    "UNREGISTERED",
    "UNKNOWN",
    "STALE_BY_SCRIPT",
    "get_provenance_status",
    "get_manifest_backed_provenance_status",
    "evidence_record_from_provenance_status",
    "build_provenance_envelope",
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


# ---------------------------------------------------------------------------
# 3b3020ac -- execution-manifest-backed provenance status.
#
# meridian.executor_contract.aggregate_worker_completions() (a hash-pinned,
# fail-closed aggregation over a scientific fan-out run's per-worker
# completion records) is consumed here as a PLAIN DICT, duck-typed --
# this package does not depend on Meridian core being importable (it is a
# separate, optionally-installed extension; see pixi.toml's 52cbe5d8 note),
# so it cannot import meridian.executor_contract even if it wanted to. A
# caller in the thesis project (or Meridian core itself) builds the
# aggregation and passes it straight in.
#
# Per the sprint spec, "completion/provenance gates must consume the
# manifest rather than trusting narrative notes or directory presence" --
# this is that consumption point for meridian-outputs' own authoritative
# per-file provenance answer specifically.
# ---------------------------------------------------------------------------

def get_manifest_backed_provenance_status(
    outputs_dir: str, path: str, aggregation: "dict[str, Any] | None",
) -> dict[str, Any]:
    """:func:`get_provenance_status`, PLUS an additive ``manifest_status``
    key answering a stronger question than any of that function's existing
    branches can: "does a hash-pinned execution-manifest aggregation
    actually vouch for this exact file's current content?"

    ``aggregation`` is the dict returned by
    ``meridian.executor_contract.aggregate_worker_completions`` (or an
    equivalent caller-built dict with the same ``{ok, status,
    worker_records: {worker_id: {output_hashes: {path: sha256}, ...}}}``
    shape). This function never re-derives aggregation logic -- it only
    cross-references the ALREADY-COMPUTED verdict against this one path.

    Returns ``get_provenance_status(outputs_dir, path)``'s exact dict (or
    its ``{"error": ...}`` shape, unchanged, when ``outputs_dir``/``path``
    are missing) with one additive key:

      ``manifest_status``: ``{"manifest_verified": bool, "reason": str|None,
      "recorded_output_hash": str|None, "current_content_hash": str|None}``

    ``manifest_verified`` is ``True`` ONLY when ALL of: ``aggregation`` is a
    dict with ``ok=True``; ``path`` (normalized via
    ``annotate._normalize_path``, so a differently-spelled but equivalent
    path still matches) appears among the aggregation's recorded worker
    ``output_hashes``; and the file's CURRENT content hash
    (:func:`fingerprint.script_content_hash` -- the SAME sha256 hasher this
    package already uses elsewhere, never a new hash scheme) matches the
    recorded one. Fail-closed on every other combination (missing/not-ok
    aggregation, path not recorded, unreadable file, hash mismatch) — never
    silently treats "we could not check" as "verified". Never raises.
    """
    base = get_provenance_status(outputs_dir, path)
    if "error" in base:
        return base

    if not isinstance(aggregation, dict) or not aggregation.get("ok"):
        status = aggregation.get("status") if isinstance(aggregation, dict) else None
        return {
            **base,
            "manifest_status": {
                "manifest_verified": False,
                "reason": (
                    "no ok execution-manifest aggregation supplied "
                    f"(status={status!r})"
                ),
                "recorded_output_hash": None,
                "current_content_hash": None,
            },
        }

    target = annotate._normalize_path(path)
    recorded_hash: "str | None" = None
    for rec in (aggregation.get("worker_records") or {}).values():
        if not isinstance(rec, dict):
            continue
        for out_path, out_hash in (rec.get("output_hashes") or {}).items():
            if annotate._normalize_path(out_path) == target:
                recorded_hash = out_hash
                break
        if recorded_hash is not None:
            break

    if recorded_hash is None:
        return {
            **base,
            "manifest_status": {
                "manifest_verified": False,
                "reason": (
                    f"{path!r} is not among the execution-manifest "
                    "aggregation's recorded worker output hashes"
                ),
                "recorded_output_hash": None,
                "current_content_hash": None,
            },
        }

    current_hash = fingerprint.script_content_hash(path)
    verified = current_hash is not None and current_hash == recorded_hash
    return {
        **base,
        "manifest_status": {
            "manifest_verified": verified,
            "reason": (
                None if verified else (
                    "current content hash does not match the manifest-recorded "
                    "output hash (file changed, or is unreadable)"
                )
            ),
            "recorded_output_hash": recorded_hash,
            "current_content_hash": current_hash,
        },
    }


# ---------------------------------------------------------------------------
# 0ea8fd3c -- typed research-evidence bridge (see module docstring for the
# full provenance_type -> ResolverStatus mapping table and rationale).
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_iso_ts(value: Any) -> "str | None":
    """Best-effort coercion of a timestamp of unknown shape into a
    non-empty ISO-8601 string, or ``None`` if it cannot be interpreted.

    ``annotate.record_provenance``'s ledger already stores ``recorded_at_iso``
    as a real ISO string, but ``outputs_local``'s own annotation timestamps
    (``created_at``/``updated_at`` on a directory note) are epoch
    floats/ints (``time.time()``) -- :class:`research_evidence.
    EvidenceTimestamps` requires a non-empty STRING, so this normalises
    either shape without assuming either module's storage format changes.
    """
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _resolver_state_for_provenance_status(
    status: "dict[str, Any]",
) -> "research_evidence.ResolverState":
    """Map one :func:`get_provenance_status`-shaped dict onto a typed
    :class:`research_evidence.ResolverState`. See the module docstring's
    "Typed research-evidence bridge" section for the full rule table --
    this function is a direct, literal implementation of that table and
    intentionally carries no additional logic of its own.

    Never raises: an unrecognised/future ``provenance_type`` value falls
    through to the most conservative reading (``UNAVAILABLE``/``AMBIGUOUS``
    depending on ``inconclusive``) rather than raising, so a forward-
    compatible caller on a newer ``provenance_status`` version never crashes
    building an envelope from an older/unknown status shape.
    """
    ptype = status.get("provenance_type")
    staleness = status.get("staleness") or {}
    script_staleness = status.get("script_staleness") or {}
    inconclusive = bool(status.get("inconclusive"))

    if ptype == STALE_BY_SCRIPT:
        return research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.STALE,
            confidence=0.3,
            reason=(
                script_staleness.get("reason")
                or "generating script content has changed since it was tagged"
            ),
        )
    if ptype == EXACT:
        if staleness.get("stale"):
            return research_evidence.ResolverState(
                status=research_evidence.ResolverStatus.STALE,
                confidence=0.3,
                reason=(
                    staleness.get("reason")
                    or "content changed since provenance was recorded"
                ),
            )
        if (
            staleness.get("recorded_content_hash") is None
            or staleness.get("current_content_hash") is None
        ):
            return research_evidence.ResolverState(
                status=research_evidence.ResolverStatus.AMBIGUOUS,
                confidence=0.5,
                reason=(
                    staleness.get("reason")
                    or "staleness could not be confirmed either way"
                ),
            )
        return research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.VERIFIED,
            confidence=0.95,
            reason=(
                staleness.get("reason")
                or "content hash matches the hash recorded at provenance time"
            ),
        )
    if ptype == DIRECTORY_FALLBACK:
        return research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.DEGRADED,
            confidence=0.4,
            reason=(
                "covered only by a directory-level MERIDIAN_NOTES.md "
                "annotation, not an exact per-file provenance record"
            ),
        )
    if ptype == UNREGISTERED:
        if inconclusive:
            return research_evidence.ResolverState(
                status=research_evidence.ResolverStatus.AMBIGUOUS,
                confidence=0.2,
                reason=(
                    "indexed but unregistered, and the outputs index has "
                    "not yet converged -- this may change"
                ),
            )
        return research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.HELD,
            confidence=0.5,
            reason=(
                "path is indexed by outputs_local but has no exact "
                "provenance record or directory-level note"
            ),
        )
    # UNKNOWN, or any unrecognised/future provenance_type -- fail toward the
    # most conservative, least-trusting reading rather than raising.
    if inconclusive:
        return research_evidence.ResolverState(
            status=research_evidence.ResolverStatus.AMBIGUOUS,
            confidence=0.1,
            reason=(
                "never discovered by the outputs walker, and the index has "
                "not yet converged -- may still be found"
            ),
        )
    return research_evidence.ResolverState(
        status=research_evidence.ResolverStatus.UNAVAILABLE,
        confidence=0.0,
        reason=(
            "never discovered by the outputs walker -- confirmed absent "
            "from a converged index"
        ),
    )


def evidence_record_from_provenance_status(
    status: "dict[str, Any]", *, record_id: "str | None" = None,
) -> "research_evidence.EvidenceRecord":
    """Convert one :func:`get_provenance_status`-shaped dict into a typed
    :class:`research_evidence.EvidenceRecord` (kind
    :data:`research_evidence.EvidenceKind.OUTPUT`) -- the item 0ea8fd3c
    bridge. See the module docstring's "Typed research-evidence bridge"
    section for the full resolver-state mapping and the partial-record rule.

    Args:
      status:     A dict as returned by :func:`get_provenance_status` (or
                  :func:`get_manifest_backed_provenance_status`, whose extra
                  ``manifest_status`` key is preserved verbatim in the
                  resulting record's ``attributes``, additive only).
      record_id:  Optional explicit :class:`research_evidence.EvidenceIdentity`
                  id. Defaults to ``status["path"]``, which is unique per
                  call and stable across repeated lookups of the same path.

    Returns:
      A fully-typed, validated :class:`research_evidence.EvidenceRecord`.
      Every field :func:`get_provenance_status` returned is preserved
      losslessly in ``attributes`` (nothing is dropped on the floor), so the
      original dict can always be reconstructed from the typed record.

    Raises:
      research_evidence.EnvelopeValidationError: ``status`` is the
      ``{"error": ...}`` shape (missing ``outputs_dir``/``path`` upstream) --
      there is no meaningful record to build from an error response, so this
      fails closed rather than silently fabricating one.
    """
    if "error" in status:
        raise research_evidence.EnvelopeValidationError(
            "cannot build an EvidenceRecord from an error provenance status: "
            f"{status['error']!r}"
        )
    path = status["path"]
    ptype = status["provenance_type"]
    record = status.get("record")
    directory_note = status.get("directory_note")
    staleness = status.get("staleness") or {}
    archival = status.get("archival") or None

    observed_at = _coerce_iso_ts(
        (record or {}).get("recorded_at_iso")
        or (directory_note or {}).get("created_at")
    )
    updated_at = _coerce_iso_ts(
        (record or {}).get("recorded_at_iso")
        or (directory_note or {}).get("updated_at")
        or (directory_note or {}).get("created_at")
    ) or observed_at
    if not observed_at:
        observed_at = _now_iso()
    if not updated_at:
        updated_at = observed_at

    hashes: "list[research_evidence.EvidenceHash]" = []
    primary_hash = staleness.get("current_content_hash") or staleness.get(
        "recorded_content_hash"
    )
    if primary_hash:
        hashes.append(
            research_evidence.EvidenceHash(algorithm="sha256", value=primary_hash)
        )
    archival_hash = (archival or {}).get("sha256")
    if archival_hash and archival_hash != primary_hash:
        hashes.append(
            research_evidence.EvidenceHash(
                algorithm="sha256", value=archival_hash,
                fingerprint="archival_row_sha256",
            )
        )

    partial = ptype in (UNREGISTERED, UNKNOWN)
    partial_reason: "str | None" = None
    if ptype == UNREGISTERED:
        partial_reason = (
            "indexed by outputs_local but no exact provenance record and no "
            "directory-level note cover this path"
        )
    elif ptype == UNKNOWN:
        partial_reason = "never discovered by the outputs indexing walker"
        if status.get("inconclusive"):
            partial_reason += (
                "; the index has not yet converged, so this may change"
            )

    external_ids: "dict[str, str]" = {}
    canonical_path = (archival or {}).get("canonical_path")
    if canonical_path:
        external_ids["canonical_path"] = canonical_path

    identity = research_evidence.EvidenceIdentity(
        id=record_id or path,
        kind=research_evidence.EvidenceKind.OUTPUT,
        locator=path,
        external_ids=external_ids,
    )
    timestamps = research_evidence.EvidenceTimestamps(
        observed_at=observed_at, updated_at=updated_at,
    )
    resolver = _resolver_state_for_provenance_status(status)

    return research_evidence.EvidenceRecord(
        identity=identity,
        timestamps=timestamps,
        resolver=resolver,
        hashes=hashes,
        partial=partial,
        partial_reason=partial_reason,
        attributes={
            "provenance_type": ptype,
            "record": record,
            "directory_note": directory_note,
            "staleness": status.get("staleness"),
            "script_staleness": status.get("script_staleness"),
            "archival": archival,
            "convergence": status.get("convergence"),
            "inconclusive": bool(status.get("inconclusive")),
            "manifest_status": status.get("manifest_status"),
        },
    )


def build_provenance_envelope(
    outputs_dir: str,
    paths: "list[str]",
    *,
    envelope_id: "str | None" = None,
    generated_at: "str | None" = None,
) -> "research_evidence.ProvenanceEnvelope":
    """Bridge: build one lossless, typed
    :class:`research_evidence.ProvenanceEnvelope` (item 0ea8fd3c) from THIS
    module's own per-file provenance answers -- one
    :class:`~research_evidence.EvidenceRecord` (kind ``OUTPUT``) per path,
    via :func:`get_provenance_status` +
    :func:`evidence_record_from_provenance_status`.

    No edges (:class:`research_evidence.EvidenceLink`) are produced here --
    ``outputs_local``/``annotate`` have no claim/citation/dataset graph of
    their own to link against; a caller composing a richer envelope (e.g.
    joining these OUTPUT records to CLAIM/CITATION records from elsewhere)
    adds links on the returned :class:`~research_evidence.ProvenanceEnvelope`
    directly (its ``links`` field is a plain, mutable list).

    Args:
      outputs_dir:   Absolute path to the outputs directory.
      paths:         Output file paths to build records for. ``[]`` yields a
                    valid, empty (non-partial) envelope.
      envelope_id:   Optional explicit id; auto-generated (uuid4) when omitted.
      generated_at:  Optional explicit ISO timestamp; current UTC time when
                    omitted.

    Returns:
      A :class:`research_evidence.ProvenanceEnvelope` whose ``partial`` flag
      is ``True`` (with a ``partial_reason`` naming how many of the records
      are partial) whenever ANY contained record is
      :attr:`~research_evidence.EvidenceRecord.partial` -- an envelope
      containing even one known-incomplete record is never reported as a
      fully-authoritative whole.

    Raises:
      research_evidence.EnvelopeValidationError: ``outputs_dir`` is missing,
      or any individual ``path`` lookup itself errors (e.g. an empty-string
      path) -- fails closed rather than silently dropping the bad path from
      the envelope.
    """
    if not outputs_dir or not str(outputs_dir).strip():
        raise research_evidence.EnvelopeValidationError("outputs_dir is required")

    records: "list[research_evidence.EvidenceRecord]" = []
    for path in paths:
        status = get_provenance_status(outputs_dir, path)
        if "error" in status:
            raise research_evidence.EnvelopeValidationError(
                f"cannot build provenance envelope: {status['error']} "
                f"(path={path!r})"
            )
        records.append(evidence_record_from_provenance_status(status))

    partial_count = sum(1 for r in records if r.partial)
    partial = partial_count > 0
    partial_reason = (
        f"{partial_count} of {len(records)} record(s) are partial -- see "
        "each record's own partial_reason"
    ) if partial else None

    return research_evidence.build_envelope(
        records=records,
        envelope_id=envelope_id,
        generated_at=generated_at,
        partial=partial,
        partial_reason=partial_reason,
    )
