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
    a long-lived project's provenance store doesn't grow unbounded. A fresh
    call makes its record current and moves the prior one into that entry's
    bounded ``history`` (newest 50 kept, older ones counted in
    ``history_dropped``), in the same atomic replace.
  - Locking: every write runs inside ``ledger_io.ledger_transaction``, which
    holds ``outputs_local.IndexFileLock`` on the ledger file (cross-process,
    not just cross-thread), writes a unique fsynced temporary file and
    replaces the ledger atomically. A corrupt ledger is quarantined to
    ``provenance_ledger.json.corrupt-<utc>`` instead of being read as empty.
    Readers stay lock-free and strict: they raise
    ``ledger_io.LedgerCorruptError`` rather than report an empty ledger.
  - Reuse over duplication: falls back to ``outputs_local.file_fingerprint``
    for a best-effort ``generating_script`` hint when the caller doesn't
    supply one explicitly, the same helper ``fingerprint.py`` builds on, so
    both modules read the SAME underlying signal instead of inventing a
    second heuristic.

NO hosted call is made anywhere in this module -- fully local, matching the
rest of the ``meridian_outputs`` package.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import environment, fingerprint, ledger_io, outputs_local
from .path_security import (
    relative_output_path,
    relative_reference,
    _require_within,
    resolve_reference,
)

_log = logging.getLogger(__name__)

__all__ = [
    "ProvenanceRecord",
    "record_provenance",
    "get_provenance",
    "get_provenance_history",
    "list_provenance",
]

_CACHE_DIRNAME = ".meridian-outputs-cache"
_LEDGER_FILENAME = "provenance_ledger.json"

# Version history. Every re-record moves the superseded record (without its
# own history) into the entry's ``history`` list, oldest first, in the same
# atomic ledger replace that writes the new record -- current record and
# history can never disagree on disk. The list keeps the newest
# _HISTORY_LIMIT superseded records; older ones are dropped and counted in
# ``history_dropped`` so version numbers stay stable. get_provenance and
# list_provenance hide both keys unless include_history=True;
# get_provenance_history is the public view.
_HISTORY_KEY = "history"
_HISTORY_DROPPED_KEY = "history_dropped"
_HISTORY_KEYS = (_HISTORY_KEY, _HISTORY_DROPPED_KEY)
_HISTORY_LIMIT = 50

# Guards ONLY the _ledger_read_cache dict below. Ledger writes are serialised
# by ledger_io.ledger_transaction (outputs_local.IndexFileLock on the ledger
# file, cross-process). That lock's in-process part is a plain, non-reentrant
# threading.Lock, so nothing called inside a transaction may take it again:
# the writer reads through _load_ledger_unlocked, which only ever holds this
# RLock for the few instructions that touch the cache dict.
#
# fa600e42 -- RLock, not Lock: kept reentrant so a caller that already holds
# it (none today) can never deadlock on the cache, the hang fa600e42 hit when
# the cache was first added under a plain Lock.
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


def _cache_dir(outputs_dir: str, *, create: bool = True) -> str:
    """``<outputs_dir>/.meridian-outputs-cache`` -- created + gitignored on
    first write. Never raises; falls back to the (uncreated) path on failure
    so callers degrade to a no-op read/write rather than crashing.
    ``create=False`` (every read path) only computes the path: a read never
    creates the directory or edits a ``.gitignore``."""
    cache_dir = os.path.join(outputs_dir, _CACHE_DIRNAME)
    if not create:
        return cache_dir
    try:
        os.makedirs(cache_dir, exist_ok=True)
        outputs_local.ensure_gitignored(cache_dir)
    except OSError:
        pass
    return cache_dir


def _ledger_path(outputs_dir: str, *, create: bool = True) -> str:
    return os.path.join(_cache_dir(outputs_dir, create=create), _LEDGER_FILENAME)


# fa600e42 -- path -> (stat signature, parsed ledger dict). Avoids re-reading
# and re-JSON-parsing an UNCHANGED ledger file on every call: originally a
# non-issue (this ledger had no caller that read it in a loop), but
# provenance_status.get_provenance_status's new relocation-detection tier
# now calls list_provenance() (which goes through this function) on every
# lookup that doesn't hit an exact record, including from real batch/loop
# callers (bind_artifact_provenance, build_provenance_envelope) that can
# call it once per path against a project with a large accumulated ledger
# -- an O(N) re-parse of the same unchanged file for an N-path batch,
# without this cache.
#
# The signature is (st_mtime_ns, st_size, st_ino). mtime+size alone served a
# stale ledger after a same-size rewrite inside one mtime tick; every ledger
# write is an atomic replace of a NEW file, so st_ino changes on each write
# even when size and mtime do not. An in-place rewrite that keeps size,
# mtime and inode is not detected (that would need a content hash); no
# writer in this package rewrites the ledger in place.
_LedgerSig = tuple[int, int, int]
_ledger_read_cache: dict[str, tuple[_LedgerSig, dict[str, dict[str, Any]]]] = {}


def _ledger_sig(st: os.stat_result) -> _LedgerSig:
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _load_ledger_unlocked(path: str) -> dict[str, dict[str, Any]]:
    """Strict, cache-aware read of the ledger at ``path``.

    Takes no ledger lock (only ``_write_lock`` around the cache dict), so it
    is safe both for lock-free readers and inside
    ``ledger_io.ledger_transaction``, whose lock is not reentrant. Returns
    ``{}`` only for a missing ledger; raises ``ledger_io.LedgerCorruptError``
    for a corrupt one. The parse is cached only when the file's signature is
    the same before and after reading, so a concurrent replace can never pin
    the new bytes under the old signature.
    """
    try:
        before = _ledger_sig(os.stat(path))
    except FileNotFoundError:
        return {}
    with _write_lock:
        cached = _ledger_read_cache.get(path)
        if cached is not None and cached[0] == before:
            return dict(cached[1])
    data = ledger_io.read_ledger_strict(path)
    try:
        after = _ledger_sig(os.stat(path))
    except OSError:
        after = None
    if after == before:
        with _write_lock:
            _ledger_read_cache[path] = (before, data)
    return dict(data)


def _read_ledger(outputs_dir: str) -> dict[str, dict[str, Any]]:
    # A missing cache directory reads as a missing ledger ({}), never created.
    return _load_ledger_unlocked(_ledger_path(outputs_dir, create=False))


def _history_parts(entry: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """(superseded snapshots oldest-first, dropped count) of a ledger entry.
    A legacy entry written before history existed has neither key: ([], 0)."""
    raw = entry.get(_HISTORY_KEY)
    history = [dict(h) for h in raw if isinstance(h, dict)] if isinstance(raw, list) else []
    dropped = entry.get(_HISTORY_DROPPED_KEY)
    if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
        dropped = 0
    return history, dropped


def _without_history(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in _HISTORY_KEYS}


def _supersede(
    prior: dict[str, Any] | None, record: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Attach the bounded history to ``record``: ``prior`` (minus its own
    history) becomes the newest snapshot. Returns (entry to store, 1-based
    version of ``record``). Never mutates ``prior``, which may be shared with
    the read cache."""
    history, dropped = _history_parts(prior) if prior is not None else ([], 0)
    if prior is not None:
        history.append(_without_history(prior))
    overflow = len(history) - _HISTORY_LIMIT
    if overflow > 0:
        history = history[overflow:]
        dropped += overflow
    stored = _without_history(record)
    stored[_HISTORY_KEY] = history
    stored[_HISTORY_DROPPED_KEY] = dropped
    return stored, dropped + len(history) + 1


def _write_ledger_entry(
    outputs_dir: str, key: str, record: dict[str, Any],
) -> tuple[str | None, int]:
    """Upsert ``record`` under ``key`` inside one cross-process transaction,
    moving any existing record for ``key`` into its bounded history.

    Returns ``(quarantine_path, version)``: the path a corrupt ledger was
    copied to first (else ``None``) and the new record's 1-based version.
    Raises ``outputs_local.IndexLockAcquireError`` when another process holds
    the ledger lock past its timeout.
    """
    path = _ledger_path(outputs_dir)
    with ledger_io.ledger_transaction(path) as txn:
        ledger = txn.load(_load_ledger_unlocked)
        # An update also scrubs legacy absolute-path rows that were written
        # before this module started storing relative references.
        portable: dict[str, dict[str, Any]] = {}
        for old_key, old_record in ledger.items():
            if not isinstance(old_record, dict):
                continue
            entry = dict(old_record)
            old_path = entry.get("path") or old_key
            safe_path = (
                relative_reference(outputs_dir, old_path)
                if isinstance(old_path, str) and os.path.isabs(old_path)
                else str(old_path).replace("\\", "/")
            )
            if not safe_path:
                continue
            entry["path"] = safe_path
            script = entry.get("generating_script")
            if isinstance(script, str) and os.path.isabs(script):
                entry["generating_script"] = (
                    relative_reference(outputs_dir, script) or os.path.basename(script)
                )
            safe_key = os.path.normcase(os.path.normpath(safe_path)).replace("\\", "/")
            portable[safe_key] = entry
        ledger = portable
        # A legacy absolute-path row was re-keyed above, so it is found here
        # and becomes history[0] under the relative key.
        ledger[key], version = _supersede(ledger.get(key), record)
        txn.commit(ledger)  # unique tmp + fsync + atomic os.replace
        return txn.quarantined_path, version


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
    # SHA-256 of the generating script's content at record time (the output's own hash is content_hash).
    # None when the script cannot be resolved, or on a ledger entry written before this field existed.
    script_content_hash: str | None = None
    # 3f6b8715 -- W1-M Experiment Registry: optional link to a
    # meridian.db.experiments run row. Defaulted so every existing caller
    # (and every ledger entry written before this field existed) is
    # unaffected -- old JSON simply lacks the key, and reading code here
    # returns the raw dict as-is rather than reconstructing this dataclass,
    # so there is no forward-compat parsing step that could choke on it.
    experiment_run_id: str | None = None
    # 93a8b26b -- optional opaque Zotero item keys cited by this output.
    # Resolution stays with consumers that already have Zotero access.
    cited_zotero_keys: list[str] | None = None
    # Optional opaque link to the run_manifest run that produced this output
    # (run_manifest.start_run_manifest's run_id). Not validated here.
    run_id: str | None = None
    # environment.capture_environment() at record time: interpreter,
    # platform, this package's version and the direct runtime dependencies'
    # installed versions. Absent (None) on ledger rows written before it
    # existed; readers return those rows unchanged.
    reproduction: dict[str, Any] | None = None

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
    experiment_run_id: str | None = None,
    cited_zotero_keys: list[str] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Attach lightweight reproducibility metadata to one output file.

    Fully local -- no hosted call, no DB engine. Upserts one entry (keyed by
    normalized path) into ``<outputs_dir>/.meridian-outputs-cache/
    provenance_ledger.json``; calling this again for the same path (e.g.
    after re-running the generating script with different params) makes the
    new record current and moves the previous one into that path's bounded
    version history (the newest 50 superseded records; see
    :func:`get_provenance_history`) -- no separate update step needed. Every
    call creates a version, even with identical content. Paths
    in the persistent ledger are relative to ``outputs_dir``; the read APIs
    resolve them for callers and continue reading legacy absolute-path rows.

    Concurrency: writers in different processes are serialised by the
    ledger's cross-process lock, so no record is lost. When two processes
    record the same path, the last writer's record is current and the
    other's is the version before it (last writer wins the current slot).

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
      experiment_run_id:   Optional (3f6b8715) — links this provenance
                           record to a meridian.db.experiments run id (the
                           W1-M Experiment Registry). Purely additive:
                           omitting it (the default) is byte-for-byte
                           identical to this function's behavior before the
                           field existed -- no validation against a live
                           experiment run is performed here (this module has
                           no DB engine access at all), it is stored as an
                           opaque string exactly like sprint_item_id/
                           decision_id already are.
      cited_zotero_keys:   Optional (93a8b26b) Zotero item keys cited by this
                           output. Stored as an opaque list; this standalone
                           package does not resolve keys or make hosted calls.
      run_id:              Optional ``run_manifest`` run id that produced
                           this output, stored as an opaque string (not
                           checked against the run-manifest ledger), so the
                           per-output record joins to the run's manifest.

    Every record also carries ``reproduction``: the
    :func:`environment.capture_environment` snapshot (interpreter version,
    platform, this package's version, and the installed versions of the
    direct runtime dependencies), captured once per process. The command
    line is deliberately not recorded (it can hold secrets or local paths).

    Returns:
      The stored record as a dict (path, generating_script, params,
      sprint_item_id, decision_id, note, recorded_at, recorded_at_iso,
      content_hash, script_content_hash, experiment_run_id,
      cited_zotero_keys, run_id, reproduction) plus ``version``
      (1-based count of records ever written for this path since history
      began, including this one), or
      ``{"error": ...}`` on failure. When the existing ledger was corrupt,
      its bytes are first copied to ``provenance_ledger.json.corrupt-<utc>``
      and the result carries ``ledger_quarantined`` (that file's basename).
      Raises ``outputs_local.IndexLockAcquireError`` when another process
      holds the ledger lock past its timeout (fail closed, nothing written).

    bd5b8d79 -- ``content_hash`` is a best-effort SHA-256 of ``path``'s
    on-disk bytes AT THE MOMENT this record is written, via
    :func:`fingerprint.script_content_hash` (the SAME hasher
    ``fingerprint.py`` already uses for its own script-staleness ledger --
    no new hash scheme is introduced here). ``None`` when ``path`` doesn't
    exist yet or isn't readable at record time. This is what lets a later
    reader (``provenance_status.get_provenance_status``) detect that a file
    has been relocated/regenerated since this record was made, by comparing
    against a freshly-computed hash at lookup time.

    W1-A -- when ``path`` is relative, it is resolved against ``outputs_dir``
    before hashing (``os.path.join(outputs_dir, path)``); an already-absolute
    ``path`` is used as-is. ``script_content_hash`` opens the given path
    directly, and a relative path otherwise resolves against the calling
    process's current working directory -- not ``outputs_dir`` -- which
    previously made ``content_hash`` come back ``None`` for every caller that
    (reasonably) passed a path relative to the outputs tree.

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

    try:
        resolved_path = _require_within(outputs_dir, path)
        stored_path = relative_output_path(outputs_dir, resolved_path)
    except ValueError as exc:
        return {"error": str(exc)}

    if generating_script is None:
        try:
            generating_script = outputs_local.file_fingerprint(resolved_path).generating_script
        except Exception:  # noqa: BLE001 -- best-effort fallback only
            generating_script = None

    # Hash the canonical output path that passed the realpath containment
    # check above. Relative inputs have already been resolved against the root.
    try:
        content_hash = fingerprint.script_content_hash(resolved_path, outputs_dir=outputs_dir)
    except Exception:  # noqa: BLE001 -- best-effort snapshot only, never
        # blocks the (already-durable) provenance write on a hashing failure.
        content_hash = None

    # The generating script's content, not the output's. An edited script is a staleness signal for the output
    # it produced, and the record keeps the hash it had when the output was recorded. Resolved the way tag_output
    # resolves a script (the outputs directory, then the working directory); a script that cannot be found leaves
    # the field None and the record is still written.
    script_hash: str | None = None
    if isinstance(generating_script, str) and generating_script:
        script_file = fingerprint._resolve_script_path(generating_script, search_root=outputs_dir)
        if script_file is not None:
            script_hash = fingerprint.script_content_hash(script_file)

    now = time.time()
    record = ProvenanceRecord(
        path=resolved_path,
        generating_script=generating_script,
        params=dict(params) if params else {},
        sprint_item_id=sprint_item_id,
        decision_id=decision_id,
        note=note,
        recorded_at=now,
        recorded_at_iso=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        content_hash=content_hash,
        script_content_hash=script_hash,
        experiment_run_id=experiment_run_id,
        cited_zotero_keys=cited_zotero_keys,
        run_id=run_id,
        reproduction=environment.capture_environment(),
    )

    key = os.path.normcase(os.path.normpath(stored_path)).replace("\\", "/")
    if not key:
        return {"error": "could not normalize path"}

    stored_record = record.to_dict()
    stored_record["path"] = stored_path
    if isinstance(generating_script, str) and os.path.isabs(generating_script):
        stored_record["generating_script"] = (
            relative_reference(outputs_dir, generating_script)
            or os.path.basename(generating_script)
        )

    # outputs_local.IndexLockAcquireError (another process holds the ledger
    # lock past its timeout) deliberately propagates: fail closed, never a
    # silently dropped record.
    try:
        quarantined, version = _write_ledger_entry(outputs_dir, key, stored_record)
    except (OSError, TypeError, ledger_io.LedgerCorruptError) as exc:
        return {"error": f"failed to write provenance record: {exc}"}

    try:
        outputs_local.register_priority_path(outputs_dir, resolved_path)
    except Exception:  # noqa: BLE001 -- never let the fast-path optimisation
        # break the (already-durable) provenance write it rides on top of.
        _log.debug(
            "record_provenance: register_priority_path failed for %r under %r",
            path, outputs_dir, exc_info=True,
        )

    result = record.to_dict()
    result["version"] = version
    if quarantined:
        # The prior ledger was corrupt: its bytes were copied aside before
        # this write replaced it. Basename only -- no machine-local path.
        result["ledger_quarantined"] = os.path.basename(quarantined)
    return result


def _resolved_path_copy(outputs_dir: str, record: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``record`` whose relative ``path`` is resolved to absolute --
    the public API's shape, while on-disk records stay portable."""
    out = dict(record)
    path = out.get("path")
    if isinstance(path, str) and not os.path.isabs(path):
        out["path"] = resolve_reference(outputs_dir, path)
    return out


def _public_record(
    outputs_dir: str, record: dict[str, Any], *, include_history: bool,
) -> dict[str, Any]:
    """The caller-facing copy of one ledger entry: absolute ``path`` and,
    unless ``include_history``, no ``history``/``history_dropped`` keys, so
    get_provenance and list_provenance keep their pre-history shape."""
    if not include_history:
        return _resolved_path_copy(outputs_dir, _without_history(record))
    out = _resolved_path_copy(outputs_dir, record)
    history, dropped = _history_parts(record)
    out[_HISTORY_KEY] = [_resolved_path_copy(outputs_dir, h) for h in history]
    out[_HISTORY_DROPPED_KEY] = dropped
    return out


def get_provenance(
    outputs_dir: str, path: str, *, include_history: bool = False,
) -> dict[str, Any] | None:
    """Look up the most recent reproducibility record for ``path``.

    Queryable WITHOUT opening/parsing the output file itself -- this reads
    only the lightweight JSON ledger, never the output's own bytes.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      path:         The output file to look up (normalized for matching --
                    back-slashes/forward-slashes and case differences on
                    Windows are handled the same way as elsewhere in this
                    package).
      include_history: Also return the record's ``history`` (superseded
                    records, oldest first) and ``history_dropped`` keys.
                    Off by default so the record keeps its long-standing
                    shape; :func:`get_provenance_history` is the versioned
                    view.

    Returns:
      The record dict for `path` (``path``, ``generating_script``,
      ``params``, ``sprint_item_id``, ``decision_id``, ``note``,
      ``recorded_at``, ``recorded_at_iso``, ``content_hash``,
      ``experiment_run_id``, ``cited_zotero_keys``, ``run_id``,
      ``reproduction``), or None if nothing has ever been recorded for this
      path under this outputs_dir. A row written before a field existed is
      returned as stored, without that key.

    Raises ``ledger_io.LedgerCorruptError`` when the ledger exists but is not
    a JSON object: a damaged ledger is never reported as "nothing recorded".

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
    if not path:
        return None
    try:
        relative_path = relative_output_path(outputs_dir, path)
        key = os.path.normcase(os.path.normpath(relative_path)).replace("\\", "/")
    except ValueError:
        return None
    if not key:
        return None
    ledger = _read_ledger(outputs_dir)
    rec = ledger.get(key)
    if rec is None:
        # Read provenance ledgers written before relative-path storage.
        legacy_key = _normalize_path(path)
        rec = ledger.get(legacy_key) if legacy_key else None
    if not isinstance(rec, dict):
        return None
    # Preserve the public API's absolute-path result while keeping new
    # on-disk records portable and free of machine-local roots.
    return _public_record(outputs_dir, rec, include_history=include_history)


def get_provenance_history(outputs_dir: str, path: str) -> dict[str, Any] | None:
    """Every retained provenance record for ``path``, oldest first.

    Each re-record of a path moves its previous record into a bounded
    history inside the same ledger entry (one atomic write), so this reads
    one ledger entry and never opens the output itself.

    Returns ``None`` when nothing was ever recorded for ``path`` (or it is
    outside ``outputs_dir``), else::

        {"path": <absolute path>,
         "versions": [<record>, ...],   # oldest first; the last is current
         "current_version": <int>,
         "history_dropped": <int>,      # superseded records past the cap
         "history_limit": 50}

    Every record in ``versions`` has the :func:`get_provenance` shape plus
    ``version`` (1-based, stable across the cap: the oldest retained
    record's version is ``history_dropped + 1``) and ``is_current``. A
    legacy record written before history existed is version 1. Raises
    ``ledger_io.LedgerCorruptError`` for a corrupt ledger.
    """
    rec = get_provenance(outputs_dir, path, include_history=True)
    if rec is None:
        return None
    history, dropped = _history_parts(rec)
    versions: list[dict[str, Any]] = []
    for offset, snapshot in enumerate(history):
        entry = _without_history(snapshot)
        entry["version"] = dropped + offset + 1
        entry["is_current"] = False
        versions.append(entry)
    current = _without_history(rec)
    current["version"] = dropped + len(history) + 1
    current["is_current"] = True
    versions.append(current)
    return {
        "path": current.get("path"),
        "versions": versions,
        "current_version": current["version"],
        "history_dropped": dropped,
        "history_limit": _HISTORY_LIMIT,
    }


def list_provenance(
    outputs_dir: str, *, include_history: bool = False,
) -> list[dict[str, Any]]:
    """Return the reproducibility record for every distinct path ever
    recorded under ``outputs_dir``.

    Results are sorted by (normalized) path for deterministic output, matching
    this package's existing no-hidden-set/dict-iteration-order convention.

    Returns:
      A list of record dicts (same shape as :func:`get_provenance`'s return
      value; ``include_history`` works the same way), or ``[]`` if nothing
      has been recorded yet. Raises ``ledger_io.LedgerCorruptError`` for a
      corrupt ledger, as :func:`get_provenance` does.
    """
    ledger = _read_ledger(outputs_dir)
    return [
        _public_record(outputs_dir, ledger[key], include_history=include_history)
        for key in sorted(ledger)
        if isinstance(ledger[key], dict)
    ]
