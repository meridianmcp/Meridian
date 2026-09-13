"""First-class derived-artifact cache and fast variant rendering.

Sprint item 19917525 (v0.2.6-75D).

Prior state (confirmed by investigation)
-----------------------------------------
This package already has three separate, narrow caching/ledger concepts,
none of which is a general-purpose cache for a COMPUTED (derived) artifact
keyed off a source file plus a rendering recipe:

  1. ``outputs_local``'s ``OutputsFtsIndex`` -- a persistent DuckDB+Tantivy
     search index over the outputs tree itself. Its own internal
     ``_index_cache`` is an in-process LRU of live *index instances*, not a
     cache of rendered/derived bytes, and ``get_cache_quota_status`` only
     reports aggregate disk usage under ``.meridian-outputs-cache/`` -- it
     has no concept of one derived artifact, its source, or its own
     eviction/invalidation lifecycle.
  2. ``fingerprint``'s ``fingerprint_ledger.json`` -- tracks a generating
     SCRIPT's content hash at tag time, for staleness classification. It
     stores no artifact bytes at all.
  3. ``annotate``'s ``provenance_ledger.json`` -- tracks reproducibility
     metadata (params, sprint/decision ids, a best-effort content hash) for
     one output path. Also stores no artifact bytes, and is keyed by path
     alone -- one path, one record -- with no notion of "this same source
     rendered N different ways" (e.g. a thumbnail vs. a full-res PNG vs. a
     CSV-to-Parquet conversion of the same source file).

What this module adds
----------------------
A genuinely persisted, keyed cache from ``(source_path, variant, params)``
to derived-artifact BYTES, with real eviction and real invalidation -- not
an in-memory dict that forgets everything on process exit:

  - **Cache key** (:func:`compute_cache_key`): a stable SHA-256 over the
    source path (normalized via ``outputs_local._normalize_output_path``,
    the SAME equality semantics the FTS index already uses -- no second,
    subtly different path-equality scheme), the variant name, and the
    caller's rendering params (canonical JSON, sorted keys).
  - **Persistence** (:func:`put_cached_variant`): derived bytes are written
    to their own file under
    ``<outputs_dir>/.meridian-outputs-cache/derived/artifacts/<key><ext>``,
    and a JSON manifest entry (same atomic tmp-file + ``os.replace`` pattern
    ``fingerprint.py``/``annotate.py`` already use for their own ledgers) is
    written recording the source's signature and content hash AT WRITE
    TIME. Survives a process restart -- this is a real on-disk cache, not a
    module-level dict.
  - **Fast-path invalidation** (:func:`get_cached_variant`): a lookup first
    compares the source file's CHEAP signature (size + mtime_ns, one
    ``os.stat`` call -- no read, no hash) against what was recorded at write
    time. An unchanged signature is trusted as a HIT with no further I/O --
    this is the actual "fast" in "fast variant rendering": a repeat request
    for the same source+variant+params skips re-rendering AND skips
    re-hashing. Only when the cheap signature disagrees does this fall back
    to a real content-hash comparison (reusing
    :func:`fingerprint.script_content_hash`, the SAME hasher
    ``fingerprint.py`` already uses for its own staleness ledger) --
    catching the case where a copy/restore touched mtime without changing
    bytes, so that case is still correctly served from cache rather than
    needlessly invalidated. A source file that no longer exists, or whose
    content hash has genuinely changed, is treated as a confirmed MISS
    (mirrors ``fingerprint.check_staleness``'s own "script no longer
    readable/present" => stale rule) -- this cache never serves bytes it
    cannot verify against the CURRENT source.
  - **Real eviction** (:func:`evict_to_budget`): true LRU by
    ``last_accessed_at`` (updated on every hit), removing the oldest entries
    -- and deleting their on-disk artifact files -- until both an optional
    byte budget and an optional file-count budget are satisfied. Lives under
    the same ``.meridian-outputs-cache/`` convention
    :func:`outputs_local.get_cache_quota_status` already walks, so this
    cache's disk usage is visible there for free, with no separate quota
    plumbing.
  - **Staleness-ledger integration** (:func:`invalidate_stale_sources`):
    composes with :mod:`fingerprint`'s EXISTING script-staleness ledger
    rather than duplicating it -- when a generating script is found to have
    changed (``fingerprint.check_staleness``), every derived variant cached
    for its previously-tagged outputs is proactively purged, so a stale
    upstream script cannot leave a downstream rendered variant looking
    fresh just because the OUTPUT file's own bytes never changed.
  - **A real, checkable convergence state** (:func:`get_convergence_state`):
    unlike ``OutputsFtsIndex.get_convergence_state`` (which answers "has the
    background WALK finished"), this cache never walks -- it is a keyed,
    on-demand cache, not a full-tree indexer. "Converged" here means the
    on-disk manifest is genuinely readable AND every artifact file it
    references actually exists on disk -- i.e. the persisted cache is
    self-consistent, not merely present. A caller (e.g. a benchmark/
    qualification harness) can call this to confirm the cache it just wrote
    is real and internally consistent, rather than trusting a bare "done"
    signal.

NO hosted call is made anywhere in this module -- fully local, matching the
rest of the ``meridian_outputs`` package.

Known, stated limitation -- not a silent gap
----------------------------------------------
Write serialization here is an in-process ``threading.Lock`` (mirrors
``fingerprint.py``'s own ``_write_lock`` scope and rationale: a lower-stakes,
keyed sidecar cache, not the FTS index itself, so cross-process locking is
not warranted here). Two independent PROCESSES writing the SAME cache key
concurrently can race on the manifest (last write wins, matching the
existing ledgers' own concurrency contract) -- this is the same limitation
``fingerprint``/``annotate`` already carry, not a new one introduced here.
A caller that needs strict single-writer discipline across processes (e.g.
a benchmark comparing wall-clock write throughput) should use one cache
owner per fresh cache path, exactly as this item's own qualification
follow-up (28448a5c) already requires -- ``owner`` is accepted on the write
path purely as an audit trail for that discipline, not as an enforced lock.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import outputs_local
from .fingerprint import script_content_hash

_log = logging.getLogger(__name__)

_CACHE_DIRNAME = ".meridian-outputs-cache"
_DERIVED_SUBDIR = "derived"
_ARTIFACTS_SUBDIR = "artifacts"
_MANIFEST_FILENAME = "manifest.json"

#: Bumped whenever DerivedArtifactEntry's on-disk shape changes in a way a
#: reader needs to know about. Purely observational today (nothing yet
#: branches on it) -- reserved the same way fingerprint.py's own
#: _DIGEST_VERSION is, so a future format change has somewhere to record
#: itself without guessing from field presence alone.
_SCHEMA_VERSION = 1

# Guards read-modify-write of the on-disk manifest. A threading.Lock (not
# cross-process) mirrors fingerprint.py's own _write_lock scope -- see this
# module's docstring ("Known, stated limitation") for the exact rationale.
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cache location (reuses the existing .meridian-outputs-cache convention)
# ---------------------------------------------------------------------------

def _derived_root(outputs_dir: str) -> str:
    """The ``derived/`` subdirectory of the shared ``.meridian-outputs-cache``
    convention outputs_local's FTS index, fingerprint.py's ledger, and
    annotate.py's ledger all already live under -- created (and its PARENT
    gitignored, matching fingerprint.py's exact pattern) on first use."""
    cache_dir = os.path.join(outputs_dir, _CACHE_DIRNAME)
    root = os.path.join(cache_dir, _DERIVED_SUBDIR)
    try:
        os.makedirs(os.path.join(root, _ARTIFACTS_SUBDIR), exist_ok=True)
        outputs_local.ensure_gitignored(cache_dir)
    except OSError:
        _log.debug(
            "derived_cache: could not create/gitignore cache dir %r",
            root, exc_info=True,
        )
    return root


def _manifest_path(outputs_dir: str) -> str:
    return os.path.join(_derived_root(outputs_dir), _MANIFEST_FILENAME)


def _artifact_rel_path(key: str, file_extension: str) -> str:
    """Path to one artifact's bytes, relative to ``outputs_dir`` -- stored in
    the manifest in this relative form (not absolute) so the cache directory
    remains portable if ``outputs_dir`` itself is ever moved/copied intact."""
    ext = file_extension if file_extension.startswith(".") else f".{file_extension}"
    return os.path.join(_CACHE_DIRNAME, _DERIVED_SUBDIR, _ARTIFACTS_SUBDIR, f"{key}{ext}")


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def compute_cache_key(
    source_path: str, variant: str, params: dict[str, Any] | None = None,
) -> str:
    """Deterministic SHA-256 cache key for one ``(source_path, variant,
    params)`` combination.

    Reuses ``outputs_local._normalize_output_path`` for the source-path
    component -- the SAME case/slash-insensitive equality semantics the FTS
    index already uses for path matching, rather than a second, potentially
    diverging normalization scheme. ``params`` is canonicalized via sorted-
    key JSON so key order never affects the resulting key.
    """
    normalized_source = outputs_local._normalize_output_path(source_path) or (
        os.path.abspath(str(source_path)) if source_path else ""
    )
    canonical = json.dumps(
        {
            "source_path": normalized_source,
            "variant": str(variant),
            "params": params or {},
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Source signature -- the cheap (stat-only) fast path, verified by content
# hash only when the cheap signature disagrees.
# ---------------------------------------------------------------------------

def _source_signature(source_path: str) -> tuple[int | None, int | None]:
    """(size, mtime_ns) for ``source_path``, or (None, None) if it cannot be
    stat'd right now (missing/unreadable) -- the same fail-soft contract
    ``fingerprint.script_content_hash`` already uses for its own callers."""
    try:
        st = os.stat(source_path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (None, None)


# ---------------------------------------------------------------------------
# Persisted entry shape
# ---------------------------------------------------------------------------

@dataclass
class DerivedArtifactEntry:
    """One persisted derived-artifact cache entry (a manifest row)."""

    key: str
    source_path: str
    variant: str
    params: dict[str, Any]
    content_type: str | None
    artifact_path: str  # relative to outputs_dir
    size_bytes: int
    source_size: int | None
    source_mtime_ns: int | None
    source_content_hash: str | None
    schema_version: int
    created_at: float
    created_at_iso: str
    last_accessed_at: float
    last_accessed_at_iso: str
    hit_count: int = 0
    owner: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DerivedArtifactEntry":
        """Tolerant of a manifest row written by an earlier schema version
        that lacks a field this dataclass later added -- mirrors
        fingerprint.py's own old/new ledger-row compatibility convention
        (see its ``_digest_hex`` docstring) rather than raising on a
        perfectly valid, just-older, row."""
        known = {f: data.get(f) for f in cls.__dataclass_fields__ if f in data}
        known.setdefault("hit_count", 0)
        known.setdefault("owner", None)
        known.setdefault("schema_version", _SCHEMA_VERSION)
        return cls(**known)


# ---------------------------------------------------------------------------
# Manifest read/write (same atomic tmp+os.replace pattern as fingerprint.py
# / annotate.py's own ledgers)
# ---------------------------------------------------------------------------

def _read_manifest(outputs_dir: str) -> dict[str, dict[str, Any]]:
    path = _manifest_path(outputs_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(outputs_dir: str, manifest: dict[str, dict[str, Any]]) -> None:
    path = _manifest_path(outputs_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic on both POSIX and Windows


# ---------------------------------------------------------------------------
# Lookup (fast path) / put (persist) / invalidate / evict
# ---------------------------------------------------------------------------

def get_cached_variant(
    outputs_dir: str, source_path: str, variant: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Look up one cached derived variant, verifying it against the
    source's CURRENT on-disk state before ever returning it as a hit.

    Returns ``{"hit": bool, "reason": str, "data": bytes | None,
    "entry": dict | None, "key": str}``. ``reason`` is one of:
      - ``"fresh"``            -- cheap (size, mtime_ns) signature matches
                                   what was recorded at write time. No hash
                                   computed -- this is the fast path.
      - ``"fresh_by_hash"``    -- the cheap signature changed (e.g. mtime
                                   touched by a copy/restore) but the
                                   source's CURRENT content hash still
                                   matches what was recorded -- still a
                                   genuine hit, just one stat+hash more
                                   expensive than the fast path.
      - ``"no_entry"``         -- nothing cached for this key yet.
      - ``"source_changed"``   -- the source's content hash no longer
                                   matches -- a confirmed, real MISS.
      - ``"source_missing"``   -- the source file no longer exists --
                                   treated as a MISS (this cache never
                                   serves bytes it cannot verify), mirroring
                                   ``fingerprint.check_staleness``'s own
                                   "script no longer readable" => stale rule.
      - ``"artifact_missing"`` -- the manifest row exists but its artifact
                                   file is gone from disk (e.g. manually
                                   deleted, or a prior eviction that didn't
                                   update the manifest) -- a MISS; the stale
                                   manifest row is dropped as a side effect.

    A HIT (``"fresh"``/``"fresh_by_hash"``) updates ``last_accessed_at`` and
    increments ``hit_count`` -- the bookkeeping :func:`evict_to_budget`'s LRU
    ordering depends on.
    """
    key = compute_cache_key(source_path, variant, params)
    manifest = _read_manifest(outputs_dir)
    row = manifest.get(key)
    if row is None:
        return {"hit": False, "reason": "no_entry", "data": None, "entry": None, "key": key}

    entry = DerivedArtifactEntry.from_dict(row)
    artifact_full_path = os.path.join(outputs_dir, entry.artifact_path)

    size, mtime_ns = _source_signature(source_path)
    if size is None:
        return _drop_and_report(outputs_dir, key, "source_missing")

    hit_reason: str | None = None
    if size == entry.source_size and mtime_ns == entry.source_mtime_ns:
        hit_reason = "fresh"
    else:
        current_hash = script_content_hash(source_path)
        if current_hash is not None and current_hash == entry.source_content_hash:
            hit_reason = "fresh_by_hash"
        else:
            return _drop_and_report(outputs_dir, key, "source_changed")

    try:
        with open(artifact_full_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return _drop_and_report(outputs_dir, key, "artifact_missing")

    _record_hit(outputs_dir, key)
    entry.last_accessed_at = time.time()
    entry.hit_count += 1
    return {"hit": True, "reason": hit_reason, "data": data, "entry": entry.to_dict(), "key": key}


def _drop_and_report(outputs_dir: str, key: str, reason: str) -> dict[str, Any]:
    """A verification failure (stale/missing source or artifact) both
    reports the miss AND proactively drops the now-known-bad manifest row --
    so a subsequent lookup doesn't pay the same verification cost again, and
    :func:`get_cache_stats`/:func:`get_convergence_state` never count a
    known-invalid entry as live."""
    with _write_lock:
        manifest = _read_manifest(outputs_dir)
        row = manifest.pop(key, None)
        if row is not None:
            artifact_full_path = os.path.join(outputs_dir, row.get("artifact_path", ""))
            try:
                os.remove(artifact_full_path)
            except OSError:
                pass
            _write_manifest(outputs_dir, manifest)
    return {"hit": False, "reason": reason, "data": None, "entry": None, "key": key}


def _record_hit(outputs_dir: str, key: str) -> None:
    with _write_lock:
        manifest = _read_manifest(outputs_dir)
        row = manifest.get(key)
        if row is None:
            return
        row["last_accessed_at"] = time.time()
        row["last_accessed_at_iso"] = datetime.now(timezone.utc).isoformat()
        row["hit_count"] = int(row.get("hit_count", 0)) + 1
        _write_manifest(outputs_dir, manifest)


def put_cached_variant(
    outputs_dir: str, source_path: str, variant: str, data: bytes,
    *,
    params: dict[str, Any] | None = None,
    content_type: str | None = None,
    file_extension: str = ".bin",
    owner: str | None = None,
) -> DerivedArtifactEntry:
    """Persist ``data`` (the rendered derived artifact) to the cache,
    recording the source's signature and content hash AT THIS MOMENT.

    Args:
      outputs_dir:     Root outputs directory (the cache lives under
                        ``<outputs_dir>/.meridian-outputs-cache/derived/``).
      source_path:      The file this derived artifact was rendered from.
      variant:          Short variant/recipe name (e.g. "thumbnail_256",
                        "parquet", "png_preview") -- part of the cache key,
                        so the same source can have many independently
                        cached variants.
      data:             The derived artifact's bytes.
      params:           Rendering parameters that also distinguish this
                        variant (e.g. ``{"width": 256}``) -- part of the
                        cache key.
      content_type:     Optional MIME-ish label stored for callers'
                        convenience (not interpreted here).
      file_extension:   Extension for the on-disk artifact file (default
                        ``.bin``).
      owner:            Optional caller identity, recorded for audit only
                        (e.g. a benchmark harness's single-cache-owner
                        discipline) -- never enforced as a lock here; see
                        this module's docstring.

    Returns:
      The :class:`DerivedArtifactEntry` written to the manifest.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"put_cached_variant: data must be bytes, got {type(data).__name__}")
    data = bytes(data)
    key = compute_cache_key(source_path, variant, params)
    size, mtime_ns = _source_signature(source_path)
    source_hash = script_content_hash(source_path)

    artifact_rel = _artifact_rel_path(key, file_extension)
    artifact_full = os.path.join(outputs_dir, artifact_rel)
    os.makedirs(os.path.dirname(artifact_full), exist_ok=True)
    tmp = artifact_full + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, artifact_full)  # atomic on both POSIX and Windows

    now = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    entry = DerivedArtifactEntry(
        key=key,
        source_path=source_path,
        variant=str(variant),
        params=params or {},
        content_type=content_type,
        artifact_path=artifact_rel,
        size_bytes=len(data),
        source_size=size,
        source_mtime_ns=mtime_ns,
        source_content_hash=source_hash,
        schema_version=_SCHEMA_VERSION,
        created_at=now,
        created_at_iso=now_iso,
        last_accessed_at=now,
        last_accessed_at_iso=now_iso,
        hit_count=0,
        owner=owner,
    )
    with _write_lock:
        manifest = _read_manifest(outputs_dir)
        manifest[key] = entry.to_dict()
        _write_manifest(outputs_dir, manifest)
    return entry


def invalidate_variant(
    outputs_dir: str, source_path: str, variant: str | None = None,
    params: dict[str, Any] | None = None,
) -> list[str]:
    """Explicitly purge cached variant(s) for ``source_path``.

    ``variant=None`` invalidates EVERY cached variant for this source path
    (every key whose recorded ``source_path`` -- normalized the same way
    :func:`compute_cache_key` does -- matches), regardless of ``params``.
    A specific ``variant`` (with optional ``params``) invalidates only that
    exact key. Returns the list of manifest keys actually removed (empty if
    nothing matched -- never an error).
    """
    normalized_source = outputs_local._normalize_output_path(source_path) or (
        os.path.abspath(str(source_path)) if source_path else ""
    )
    removed: list[str] = []
    with _write_lock:
        manifest = _read_manifest(outputs_dir)
        if variant is not None:
            candidate_keys = [compute_cache_key(source_path, variant, params)]
        else:
            candidate_keys = [
                k for k, row in manifest.items()
                if (
                    outputs_local._normalize_output_path(row.get("source_path", ""))
                    == normalized_source
                )
            ]
        for key in candidate_keys:
            row = manifest.pop(key, None)
            if row is None:
                continue
            removed.append(key)
            artifact_full = os.path.join(outputs_dir, row.get("artifact_path", ""))
            try:
                os.remove(artifact_full)
            except OSError:
                pass
        if removed:
            _write_manifest(outputs_dir, manifest)
    return removed


def invalidate_stale_sources(outputs_dir: str) -> dict[str, Any]:
    """Compose with :mod:`fingerprint`'s EXISTING script-staleness ledger:
    for every output ``fingerprint.check_staleness`` currently reports as
    stale (its generating script's content has changed since the output was
    tagged), proactively purge every derived variant cached for it.

    This closes the gap the module docstring calls out: an unchanged OUTPUT
    file's bytes alone can never reveal that the SCRIPT which produced it
    has since changed -- but a derived variant rendered from that output
    (e.g. a thumbnail) would otherwise keep being served from cache forever,
    even after the upstream bug is found and fixed. Read-only with respect
    to the fingerprint ledger itself -- this only READS
    ``check_staleness()``'s result and acts on THIS cache's own manifest.

    Returns ``{"stale_sources_checked": int, "invalidated_keys": [...]}``.
    """
    from . import fingerprint as _fingerprint  # noqa: PLC0415 (avoid import cycle at module load)

    stale_results = _fingerprint.check_staleness(outputs_dir)
    invalidated: list[str] = []
    stale_paths = [r.path for r in stale_results if r.is_stale]
    for path in stale_paths:
        invalidated.extend(invalidate_variant(outputs_dir, path))
    return {"stale_sources_checked": len(stale_paths), "invalidated_keys": invalidated}


def evict_to_budget(
    outputs_dir: str, *, max_bytes: int | None = None, max_files: int | None = None,
) -> dict[str, Any]:
    """Real LRU eviction: remove the oldest-accessed entries (and delete
    their on-disk artifact files) until both budgets are satisfied.

    A budget of ``None`` is treated as unbounded for that dimension (mirrors
    :func:`outputs_local.get_cache_quota_status`'s own convention). Passing
    neither budget is a no-op (nothing to evict against). Never raises: an
    artifact file that is already gone is skipped, not treated as an error.

    Returns ``{"evicted_count", "evicted_keys", "freed_bytes",
    "remaining_count", "remaining_bytes"}``.
    """
    with _write_lock:
        manifest = _read_manifest(outputs_dir)
        entries = sorted(
            manifest.items(), key=lambda kv: kv[1].get("last_accessed_at", 0.0),
        )
        total_bytes = sum(int(row.get("size_bytes", 0) or 0) for _, row in manifest.items())
        total_files = len(manifest)

        evicted_keys: list[str] = []
        freed_bytes = 0
        idx = 0
        while idx < len(entries) and (
            (max_bytes is not None and total_bytes > max_bytes)
            or (max_files is not None and total_files > max_files)
        ):
            key, row = entries[idx]
            idx += 1
            artifact_full = os.path.join(outputs_dir, row.get("artifact_path", ""))
            try:
                os.remove(artifact_full)
            except OSError:
                pass
            size = int(row.get("size_bytes", 0) or 0)
            freed_bytes += size
            total_bytes -= size
            total_files -= 1
            evicted_keys.append(key)

        for key in evicted_keys:
            manifest.pop(key, None)
        if evicted_keys:
            _write_manifest(outputs_dir, manifest)

    return {
        "evicted_count": len(evicted_keys),
        "evicted_keys": evicted_keys,
        "freed_bytes": freed_bytes,
        "remaining_count": total_files,
        "remaining_bytes": total_bytes,
    }


# ---------------------------------------------------------------------------
# Fast variant rendering -- the primary Python-level entry point
# ---------------------------------------------------------------------------

def get_or_render_variant(
    outputs_dir: str, source_path: str, variant: str,
    render_fn: Callable[..., bytes],
    *,
    params: dict[str, Any] | None = None,
    content_type: str | None = None,
    file_extension: str = ".bin",
    owner: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """The primary "fast variant rendering" entry point: return cached bytes
    for ``(source_path, variant, params)`` when a verified-fresh cache entry
    exists, otherwise call ``render_fn(source_path, **params)`` exactly once
    and persist its result before returning.

    ``render_fn`` is never called on a cache hit -- this is what makes
    repeat rendering of the same variant fast: the expensive work
    (recompressing an image, converting a format, re-running a transform)
    happens once per distinct ``(source, variant, params)`` combination,
    and every subsequent call for the same combination pays only a stat
    (and, rarely, a hash) instead.

    Args:
      render_fn:  ``render_fn(source_path, **params) -> bytes``. Must
                  return ``bytes``/``bytearray`` -- anything else raises
                  ``TypeError`` (fail loud on a caller bug rather than
                  silently caching a wrong type).
      force:      Skip the cache lookup and always re-render + re-persist
                  (e.g. a caller that knows the render recipe itself
                  changed, which this cache -- keyed only off the SOURCE
                  file's content -- cannot detect on its own).

    Returns ``{"data": bytes, "entry": dict, "cache_hit": bool, "key": str,
    "reason": str}`` -- ``reason`` is the same lookup-outcome string
    :func:`get_cached_variant` documents (``"fresh"``/``"fresh_by_hash"``
    on a hit, or whatever miss reason triggered the render on a miss).
    """
    if not force:
        lookup = get_cached_variant(outputs_dir, source_path, variant, params)
        if lookup["hit"]:
            return {
                "data": lookup["data"], "entry": lookup["entry"],
                "cache_hit": True, "key": lookup["key"], "reason": lookup["reason"],
            }
        miss_reason = lookup["reason"]
    else:
        miss_reason = "forced"

    data = render_fn(source_path, **(params or {}))
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            f"get_or_render_variant: render_fn must return bytes, got {type(data).__name__}"
        )
    entry = put_cached_variant(
        outputs_dir, source_path, variant, bytes(data),
        params=params, content_type=content_type,
        file_extension=file_extension, owner=owner,
    )
    return {
        "data": bytes(data), "entry": entry.to_dict(),
        "cache_hit": False, "key": entry.key, "reason": miss_reason,
    }


# ---------------------------------------------------------------------------
# Stats + convergence
# ---------------------------------------------------------------------------

def get_cache_stats(outputs_dir: str) -> dict[str, Any]:
    """Aggregate, read-only summary of this ``outputs_dir``'s derived-
    artifact cache -- entry/byte counts, hit totals, and the age range of
    what is currently persisted. Never raises: an unreadable/missing
    manifest reports an empty cache, not an error."""
    manifest = _read_manifest(outputs_dir)
    entry_count = len(manifest)
    total_bytes = sum(int(row.get("size_bytes", 0) or 0) for row in manifest.values())
    total_hits = sum(int(row.get("hit_count", 0) or 0) for row in manifest.values())
    created_ats = [row.get("created_at") for row in manifest.values() if row.get("created_at") is not None]
    variants = sorted({str(row.get("variant")) for row in manifest.values()})
    return {
        "outputs_dir": outputs_dir,
        "entry_count": entry_count,
        "total_bytes": total_bytes,
        "total_hits": total_hits,
        "distinct_variants": variants,
        "oldest_created_at": min(created_ats) if created_ats else None,
        "newest_created_at": max(created_ats) if created_ats else None,
    }


@dataclass(frozen=True)
class DerivedCacheConvergenceState:
    """A single, explicit snapshot of how self-consistent (converged) the
    on-disk derived-artifact cache is, right now. See
    :func:`get_convergence_state`'s docstring for what "converged" means
    for this cache specifically -- it is NOT the same question
    ``OutputsFtsIndex.get_convergence_state`` answers for the (walk-based)
    search index."""

    outputs_dir: str
    converged: bool
    manifest_readable: bool
    entry_count: int
    total_bytes: int
    missing_artifact_keys: "list[str]"
    quota: dict[str, Any]
    last_write_at: float | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_convergence_state(
    outputs_dir: str, *, max_bytes: int | None = None, max_files: int | None = None,
) -> DerivedCacheConvergenceState:
    """A real, checkable answer to "is the persisted derived-artifact cache
    valid right now" -- not a bare boolean asserted by the writer.

    This cache has no background walk (unlike ``OutputsFtsIndex``, which
    converges once a full-tree pass completes) -- it answers point lookups
    on demand. "Converged" here means: the manifest file is present and
    parses as valid JSON, AND every artifact file it references actually
    exists on disk. A caller (e.g. the 28448a5c qualification rerun) can
    call this against a fresh cache path after a write pass to confirm the
    cache it just built is real and self-consistent, rather than trusting
    an in-process "the writer said it finished" signal alone.

    Never raises: a missing/corrupt manifest is reported via
    ``manifest_readable=False`` and ``converged=False``, not an exception.
    """
    manifest_path = _manifest_path(outputs_dir)
    manifest_readable = True
    manifest: dict[str, dict[str, Any]] = {}
    reason: str | None = None

    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            manifest = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError) as exc:
            manifest_readable = False
            manifest = {}
            reason = f"manifest unreadable: {exc}"
    # A never-used cache (no manifest file yet) is a legitimate, converged
    # empty state -- there is nothing to be inconsistent about yet.

    missing_artifact_keys: list[str] = []
    total_bytes = 0
    last_write_at: float | None = None
    for key, row in manifest.items():
        total_bytes += int(row.get("size_bytes", 0) or 0)
        created_at = row.get("created_at")
        if created_at is not None and (last_write_at is None or created_at > last_write_at):
            last_write_at = created_at
        artifact_rel = row.get("artifact_path")
        if not artifact_rel or not os.path.isfile(os.path.join(outputs_dir, artifact_rel)):
            missing_artifact_keys.append(key)

    if missing_artifact_keys and reason is None:
        reason = (
            f"{len(missing_artifact_keys)} manifest entr"
            f"{'y' if len(missing_artifact_keys) == 1 else 'ies'} "
            "reference missing artifact file(s)"
        )

    converged = manifest_readable and not missing_artifact_keys
    quota = outputs_local.get_cache_quota_status(
        outputs_dir, max_bytes=max_bytes, max_files=max_files,
    )
    return DerivedCacheConvergenceState(
        outputs_dir=outputs_dir,
        converged=converged,
        manifest_readable=manifest_readable,
        entry_count=len(manifest),
        total_bytes=total_bytes,
        missing_artifact_keys=missing_artifact_keys,
        quota=quota,
        last_write_at=last_write_at,
        reason=reason,
    )
