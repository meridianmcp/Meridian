"""meridian_outputs.annotate -- lightweight reproducibility metadata per output.

Sprint item 6f4ef8bf.

This is deliberately NARROWER than ``outputs_local.annotate_outputs`` (which
stores free-form HUMAN notes in the DuckDB ``annotations`` table). That tool
answers "what does a person think about this file"; this module answers a
more mechanical question that came up repeatedly while debugging by hand:
"what produced this file, with what parameters, when, and under which
sprint-item/decision" -- without having to re-open the output (a CSV/NPY/etc.
that may be large, stale-named, or ambiguous about which run wrote it) or
trace back through shell history / logs to reconstruct the answer.

Motivating case: reconstructing an output's DT path spacing and confirming
which formula version a cached file reflected required manually tracing
things by hand more than once in one evening. The fix: the SAME script that
writes an output also calls :func:`record_provenance` right after writing it,
so the answer to "what made this, and with what params" is one
:func:`get_provenance` call away afterward, for anyone (including a different
agent/session), without opening the file.

Conventions (matched to sibling modules built in parallel this same sprint,
notably ``fingerprint.py`` / item 7518bfcd, to keep the package's shape
consistent without touching their files):
  - Cache location: ``<outputs_dir>/.meridian-outputs-cache/`` -- the SAME
    directory ``outputs_local``'s own FTS index and ``fingerprint.py``'s
    ledger already live under, created + gitignored via
    ``outputs_local.ensure_gitignored`` on first use. This module's own file
    within it is ``provenance_ledger.json`` -- a distinct filename from
    ``fingerprint.py``'s ``fingerprint_ledger.json``, so the two never
    contend for the same on-disk record.
  - Storage shape: a single JSON object (path -> record dict), read-modify-
    written with an atomic ``os.replace`` -- the same ledger shape
    ``fingerprint.py`` uses, rather than an ever-growing append-only log, so
    a long-lived project's provenance store doesn't grow unbounded and a
    fresh call always overwrites (never duplicates) the prior record for a
    path.
  - Locking: a plain in-process ``threading.Lock`` (NOT
    ``outputs_local.IndexFileLock``'s cross-process portalocker layer) --
    mirroring ``fingerprint.py``'s own reasoning: this is a lower-stakes,
    mostly-single-writer sidecar, not the shared FTS index, so cross-process
    exclusivity isn't warranted for it either.
  - Reuse over duplication: falls back to ``outputs_local.file_fingerprint``
    for a best-effort ``generating_script`` hint when the caller doesn't
    supply one explicitly, the same helper ``fingerprint.py`` builds on, so
    both modules read the SAME underlying signal instead of inventing a
    second heuristic.

NO hosted call is made anywhere in this module -- fully local, matching the
rest of the ``meridian_outputs`` package.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import fingerprint, outputs_local

_log = logging.getLogger(__name__)

__all__ = [
    "ProvenanceRecord",
    "record_provenance",
    "get_provenance",
    "list_provenance",
]

_CACHE_DIRNAME = ".meridian-outputs-cache"
_LEDGER_FILENAME = "provenance_ledger.json"

# In-process only (no cross-process portalocker layer) -- see module
# docstring: this ledger is a lower-stakes, mostly-single-writer sidecar, not
# the shared FTS index, mirroring fingerprint.py's own lock choice/rationale.
#
# fa600e42 -- RLock, not Lock: _write_ledger_entry acquires this lock and
# THEN calls _read_ledger (its own read-modify-write pattern); _read_ledger
# now ALSO acquires this same lock internally (to guard the fa600e42 cache
# dict added below). A plain, non-reentrant Lock re-acquired by the same
# thread in that nested call blocks forever -- confirmed live (a real test
# run hung indefinitely on the very first record_provenance call once the
# cache was added). RLock allows the same thread to re-enter safely while
# still serializing genuinely concurrent threads exactly as before.
_write_lock = threading.RLock()


def _normalize_path(path: str) -> str:
    """Canonical cross-platform key for a path (handles back-slashes vs
    forward-slashes and case differences on Windows), so a path recorded via
    one spelling is still found when queried via another -- the same
    matching behaviour ``outputs_local.resolve_figure_output`` gives search
    hits.
    """
    if not isinstance(path, str) or not path.strip():
        return ""
    s = path.strip()
    try:
        s = os.path.abspath(s)
    except (OSError, ValueError):
        pass
    return os.path.normcase(os.path.normpath(s)).replace("\\", "/")


def _cache_dir(outputs_dir: str) -> str:
    """``<outputs_dir>/.meridian-outputs-cache`` -- created + gitignored on
    first use. Never raises; falls back to the (uncreated) path on failure so
    callers degrade to a no-op read/write rather than crashing."""
    cache_dir = os.path.join(outputs_dir, _CACHE_DIRNAME)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        outputs_local.ensure_gitignored(cache_dir)
    except OSError:
        pass
    return cache_dir


def _ledger_path(outputs_dir: str) -> str:
    return os.path.join(_cache_dir(outputs_dir), _LEDGER_FILENAME)


# fa600e42 -- path -> (mtime, size, parsed ledger dict). Avoids re-reading
# and re-JSON-parsing an UNCHANGED ledger file on every call: originally a
# non-issue (this ledger had no caller that read it in a loop), but
# provenance_status.get_provenance_status's new relocation-detection tier
# now calls list_provenance() (which goes through this function) on every
# lookup that doesn't hit an exact record, including from real batch/loop
# callers (bind_artifact_provenance, build_provenance_envelope) that can
# call it once per path against a project with a large accumulated ledger
# -- an O(N) re-parse of the same unchanged file for an N-path batch,
# without this cache. Staleness signature is the SAME mtime+size check this
# whole package already relies on throughout outputs_local.py for file-
# change detection -- not a new class of risk this package doesn't already
# accept elsewhere. Guarded by the same _write_lock ledger writes already
# use, so a reader can never observe a torn read of the cache tuple.
_ledger_read_cache: dict[str, tuple[float, int, dict[str, dict[str, Any]]]] = {}


def _read_ledger(outputs_dir: str) -> dict[str, dict[str, Any]]:
    path = _ledger_path(outputs_dir)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    with _write_lock:
        cached = _ledger_read_cache.get(path)
        if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return dict(cached[2])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    with _write_lock:
        _ledger_read_cache[path] = (st.st_mtime, st.st_size, data)
    return dict(data)


def _write_ledger_entry(outputs_dir: str, key: str, record: dict[str, Any]) -> None:
    path = _ledger_path(outputs_dir)
    with _write_lock:
        ledger = _read_ledger(outputs_dir)
        ledger[key] = record
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)  # atomic on both POSIX and Windows


@dataclass
class ProvenanceRecord:
    """One lightweight reproducibility record for a single output path."""

    path: str
    generating_script: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    sprint_item_id: str | None = None
    decision_id: str | None = None
    note: str | None = None
    recorded_at: float = 0.0
    recorded_at_iso: str = ""
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_provenance(
    outputs_dir: str,
    path: str,
    *,
    generating_script: str | None = None,
    params: dict[str, Any] | None = None,
    sprint_item_id: str | None = None,
    decision_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Attach lightweight reproducibility metadata to one output file.

    Fully local -- no hosted call, no DB engine. Upserts one entry (keyed by
    normalized path) into ``<outputs_dir>/.meridian-outputs-cache/
    provenance_ledger.json``; calling this again for the same path (e.g.
    after re-running the generating script with different params) simply
    overwrites the previous record -- no separate update step needed.

    Args:
      outputs_dir:        Absolute path to the outputs directory (the shared
                           cache dir lives under here, same convention as
                           outputs_local's own search index and
                           fingerprint.py's ledger).
      path:                The output file this record describes. Need not
                           exist yet -- a script may record provenance right
                           before or after writing it.
      generating_script:  Path (or name) of the script that produced `path`.
                           If omitted, falls back to
                           ``outputs_local.file_fingerprint(path)``'s own
                           inferred ``generating_script`` (best-effort, e.g.
                           a "generated_by" key already embedded in a JSON
                           output) -- never raises even if `path` doesn't
                           exist or isn't readable.
      params:              Key parameters for this run (e.g.
                           ``{"radius_scale": 4.0, "use_pca": False}``). Kept
                           as opaque JSON -- no schema enforced.
      sprint_item_id:      Optional linked Meridian sprint-item id.
      decision_id:         Optional linked Meridian decision id.
      note:                Optional short machine/human note. Independent of
                           ``outputs_local.annotate_outputs``'s free-form
                           notes store -- use that tool for longer
                           commentary; this field is meant to stay short
                           (e.g. "re-run after formula v3 fix").

    Returns:
      The stored record as a dict (path, generating_script, params,
      sprint_item_id, decision_id, note, recorded_at, recorded_at_iso,
      content_hash), or ``{"error": ...}`` on failure.

    bd5b8d79 -- ``content_hash`` is a best-effort SHA-256 of ``path``'s
    on-disk bytes AT THE MOMENT this record is written, via
    :func:`fingerprint.script_content_hash` (the SAME hasher
    ``fingerprint.py`` already uses for its own script-staleness ledger --
    no new hash scheme is introduced here). ``None`` when ``path`` doesn't
    exist yet or isn't readable at record time. This is what lets a later
    reader (``provenance_status.get_provenance_status``) detect that a file
    has been relocated/regenerated since this record was made, by comparing
    against a freshly-computed hash at lookup time.

    6af1518d (requirement 3) -- once the record is successfully written,
    ``path`` is also registered for TARGETED, PROMPT indexing via
    ``outputs_local.register_priority_path``: rather than waiting for the
    ambient full-root walk to eventually reach ``path`` (which on a large
    tree could be many ``search_outputs`` calls away), a small, explicit,
    synchronous index update runs right here so the file is searchable via
    ``search_outputs(outputs_dir, ...)`` promptly. Best-effort: a failure to
    register never fails ``record_provenance`` itself -- the provenance
    record is the durable fact; fast indexing is a latency optimisation on
    top of it, not a correctness requirement.
    """
    if not outputs_dir or not str(outputs_dir).strip():
        return {"error": "outputs_dir is required"}
    if not path or not str(path).strip():
        return {"error": "path is required"}

    if generating_script is None:
        try:
            generating_script = outputs_local.file_fingerprint(path).generating_script
        except Exception:  # noqa: BLE001 -- best-effort fallback only
            generating_script = None

    try:
        content_hash = fingerprint.script_content_hash(path)
    except Exception:  # noqa: BLE001 -- best-effort snapshot only, never
        # blocks the (already-durable) provenance write on a hashing failure.
        content_hash = None

    now = time.time()
    record = ProvenanceRecord(
        path=path,
        generating_script=generating_script,
        params=dict(params) if params else {},
        sprint_item_id=sprint_item_id,
        decision_id=decision_id,
        note=note,
        recorded_at=now,
        recorded_at_iso=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        content_hash=content_hash,
    )

    key = _normalize_path(path)
    if not key:
        return {"error": f"could not normalize path {path!r}"}

    try:
        _write_ledger_entry(outputs_dir, key, record.to_dict())
    except (OSError, TypeError) as exc:
        return {"error": f"failed to write provenance record: {exc}"}

    try:
        outputs_local.register_priority_path(outputs_dir, path)
    except Exception:  # noqa: BLE001 -- never let the fast-path optimisation
        # break the (already-durable) provenance write it rides on top of.
        _log.debug(
            "record_provenance: register_priority_path failed for %r under %r",
            path, outputs_dir, exc_info=True,
        )

    return record.to_dict()


def get_provenance(outputs_dir: str, path: str) -> dict[str, Any] | None:
    """Look up the most recent reproducibility record for ``path``.

    Queryable WITHOUT opening/parsing the output file itself -- this reads
    only the lightweight JSON ledger, never the output's own bytes.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      path:         The output file to look up (normalized for matching --
                    back-slashes/forward-slashes and case differences on
                    Windows are handled the same way as elsewhere in this
                    package).

    Returns:
      The record dict for `path` (``path``, ``generating_script``,
      ``params``, ``sprint_item_id``, ``decision_id``, ``note``,
      ``recorded_at``, ``recorded_at_iso``, ``content_hash``), or None if
      nothing has ever been recorded for this path under this outputs_dir.

    Deliberately a PURE exact-match primitive -- bd5b8d79. A bare ``None``
    here is ambiguous ("never recorded" vs. "recorded, but this path was
    never even discovered by the outputs walker" vs. "not recorded, but a
    directory-level MERIDIAN_NOTES.md note covers it") and this function does
    NOT try to resolve that ambiguity by blending in outputs_local's index
    membership or its directory-annotation fallback -- that composition
    lives in :func:`meridian_outputs.provenance_status.get_provenance_status`,
    which calls this function as its first, most-authoritative tier and
    layers the richer status on top. Keeping this function narrow means its
    contract (exact key in, exact record out, nothing else) never drifts.
    """
    key = _normalize_path(path) if path else ""
    if not key:
        return None
    ledger = _read_ledger(outputs_dir)
    rec = ledger.get(key)
    return dict(rec) if rec is not None else None


def list_provenance(outputs_dir: str) -> list[dict[str, Any]]:
    """Return the reproducibility record for every distinct path ever
    recorded under ``outputs_dir``.

    Results are sorted by (normalized) path for deterministic output, matching
    this package's existing no-hidden-set/dict-iteration-order convention.

    Returns:
      A list of record dicts (same shape as :func:`get_provenance`'s return
      value), or ``[]`` if nothing has been recorded yet.
    """
    ledger = _read_ledger(outputs_dir)
    return [dict(ledger[key]) for key in sorted(ledger)]
