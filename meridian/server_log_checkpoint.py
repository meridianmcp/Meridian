"""Lightweight positional/checkpoint index for the server_logs ring-buffer.

b241a437 -- byte-offset / line-number 'table of contents' for fast seeking
through large log windows.

Architecture
------------
The server_logs ring-buffer (max 2000 rows) is stored in a SQLite/Postgres
table, so there are no raw byte offsets.  The equivalent positional primitive
is a mapping of::

    timestamp_bucket (minute-granularity ISO string)
        -> {first_id, last_id, count, min_recorded_at, max_recorded_at}

...built from the in-memory snapshot that is already fetched for search
and get_server_logs calls.  This lets a caller seeking near a specific
timestamp skip most rows by narrowing the DB query to a tight ``since=``
/ ``before=`` window, rather than scanning backwards through 2000 rows.

Design properties
-----------------
* **Complementary to FTS (222d54f8)**: does not overlap BM25 search.
  Positional navigation (where in the log?) vs. semantic ranking (what text?).
* **Pure in-memory**: no DuckDB sidecar, no disk I/O.  The checkpoint
  is rebuilt from the DB snapshot on each sync call (sub-millisecond for
  <=2000 rows; no background thread needed).
* **Granularity is configurable** (default: 1-minute buckets).  For very
  sparse logs, coarser buckets (e.g. 5 minutes) collapse to fewer entries;
  for dense logs the bucket count is still bounded by ring_size/1
  (<= 2000 unique minute-strings for 2000 rows).
* **Fallback-safe**: callers that don't hold a checkpoint yet (fresh process,
  empty ring-buffer) receive an empty index and fall back to the full
  ``get_server_logs`` scan automatically.
* **Thread-safe**: read-only after build; the module-level singleton is
  replaced atomically.

Seeking idiom (for MCP callers)
---------------------------------
1. Call ``get_server_log_checkpoint`` to retrieve the index.
2. Pick the bucket just before the target timestamp to get a tight
   ``since=`` hint.
3. Call ``get_server_logs(since=<hint>)`` -- the DB query hits only rows
   at or after that timestamp, skipping everything older.

This eliminates the O(N) backwards scan for the common case of
"show me logs around 2026-07-17 03:00:00" in a full 2000-row ring.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default bucket granularity: minute (first 16 chars of ISO timestamp
#: "YYYY-MM-DD HH:MM").  Reducing to 5-minute or hourly granularity
#: is done by slicing [:13] or [:10] instead.
_DEFAULT_BUCKET_CHARS = 16  # "YYYY-MM-DD HH:MM"


# ---------------------------------------------------------------------------
# ServerLogCheckpointIndex
# ---------------------------------------------------------------------------


class ServerLogCheckpointIndex:
    """In-memory checkpoint index for the ``server_logs`` ring-buffer.

    Maps each timestamp bucket to a summary entry so a caller can jump
    near a target timestamp without a full table scan.

    Each bucket entry has the shape::

        {
            "bucket":          "2026-07-17 03:00",  # minute bucket key
            "count":           int,                  # rows in this bucket
            "min_recorded_at": str,                  # earliest timestamp in bucket
            "max_recorded_at": str,                  # latest timestamp in bucket
            "first_id":        str,                  # id of oldest row in bucket
            "last_id":         str,                  # id of newest row in bucket
        }

    The index is ordered **oldest-first** so callers can binary-search by
    bucket key (ISO strings sort lexicographically).
    """

    def __init__(self, bucket_chars: int = _DEFAULT_BUCKET_CHARS) -> None:
        self._bucket_chars = bucket_chars
        self._lock = threading.Lock()
        # Ordered list of bucket entries, oldest-first.
        self._buckets: list[dict[str, Any]] = []
        # Snapshot metadata
        self._total_rows: int = 0
        self._min_ts: str | None = None
        self._max_ts: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, rows: list[dict[str, Any]]) -> None:
        """(Re)build the checkpoint index from *rows*.

        *rows* is the complete current snapshot of ``server_logs`` as
        returned by :func:`meridian.db.get_server_logs` with no filter
        (any ordering is fine; we sort internally).

        O(N) in len(rows); for N <= 2000 this is always sub-millisecond.
        """
        bc = self._bucket_chars
        # Group by bucket key.
        bucket_map: dict[str, dict[str, Any]] = {}
        for r in rows:
            ts = (r.get("recorded_at") or "")[:bc]
            if not ts:
                continue
            if ts not in bucket_map:
                bucket_map[ts] = {
                    "bucket": ts,
                    "count": 0,
                    "min_recorded_at": r.get("recorded_at", ""),
                    "max_recorded_at": r.get("recorded_at", ""),
                    "first_id": r.get("id", ""),
                    "last_id": r.get("id", ""),
                }
            entry = bucket_map[ts]
            entry["count"] += 1
            row_ts = r.get("recorded_at", "")
            if row_ts < entry["min_recorded_at"]:
                entry["min_recorded_at"] = row_ts
                entry["first_id"] = r.get("id", "")
            if row_ts > entry["max_recorded_at"]:
                entry["max_recorded_at"] = row_ts
                entry["last_id"] = r.get("id", "")

        # Sort buckets oldest-first (ISO strings sort lexicographically).
        sorted_buckets = sorted(bucket_map.values(), key=lambda b: b["bucket"])

        all_ts = [r.get("recorded_at", "") for r in rows if r.get("recorded_at")]
        with self._lock:
            self._buckets = sorted_buckets
            self._total_rows = len(rows)
            self._min_ts = min(all_ts) if all_ts else None
            self._max_ts = max(all_ts) if all_ts else None

    def as_dict(self) -> dict[str, Any]:
        """Return the full checkpoint index as a JSON-serialisable dict."""
        with self._lock:
            return {
                "total_rows": self._total_rows,
                "bucket_granularity_chars": self._bucket_chars,
                "bucket_granularity_label": _bucket_label(self._bucket_chars),
                "min_recorded_at": self._min_ts,
                "max_recorded_at": self._max_ts,
                "bucket_count": len(self._buckets),
                "buckets": list(self._buckets),  # shallow copy
            }

    def seek_hint(self, target_ts: str) -> str | None:
        """Return the best ``since=`` hint for a caller seeking near *target_ts*.

        Finds the bucket whose key is at or just before *target_ts*, then
        returns its ``min_recorded_at`` -- the earliest timestamp in that
        bucket, which is a safe lower bound for a ``since=`` DB query.

        Returns ``None`` if the index is empty or *target_ts* is before the
        oldest bucket (caller should do a full scan).

        This is O(B) where B = number of buckets (<= total_rows <= 2000).
        """
        target_bucket = (target_ts or "")[:self._bucket_chars]
        if not target_bucket:
            return None
        with self._lock:
            buckets = self._buckets  # stable reference under lock
        # Linear scan (B <= 2000; binary search would be premature).
        best: str | None = None
        for entry in buckets:
            if entry["bucket"] <= target_bucket:
                best = entry["min_recorded_at"]
            else:
                break
        return best

    def stats(self) -> dict[str, Any]:
        """Summary statistics for the index (for diagnostics)."""
        with self._lock:
            return {
                "total_rows": self._total_rows,
                "bucket_count": len(self._buckets),
                "min_recorded_at": self._min_ts,
                "max_recorded_at": self._max_ts,
            }


# ---------------------------------------------------------------------------
# Module-level singleton (one per process)
# ---------------------------------------------------------------------------

_checkpoint_lock = threading.Lock()
_checkpoint_index: ServerLogCheckpointIndex | None = None


def _get_or_create_index() -> ServerLogCheckpointIndex:
    global _checkpoint_index  # noqa: PLW0603
    with _checkpoint_lock:
        if _checkpoint_index is None:
            _checkpoint_index = ServerLogCheckpointIndex()
        return _checkpoint_index


def build_checkpoint(rows: list[dict[str, Any]]) -> ServerLogCheckpointIndex:
    """Build (or rebuild) the module-level checkpoint index from *rows*.

    Called by the MCP handlers after fetching the log snapshot so the
    index is always up-to-date with the current ring-buffer state.

    Returns the index object (caller may call ``.as_dict()`` on it).
    """
    idx = _get_or_create_index()
    try:
        idx.build(rows)
    except Exception:  # noqa: BLE001
        _log.debug("server_log_checkpoint.build_checkpoint failed", exc_info=True)
    return idx


def get_checkpoint_dict() -> dict[str, Any]:
    """Return the current checkpoint index as a dict.

    Returns an empty/zero dict if the index was never built (fresh process,
    no log rows yet).
    """
    idx = _get_or_create_index()
    try:
        return idx.as_dict()
    except Exception:  # noqa: BLE001
        _log.debug("server_log_checkpoint.get_checkpoint_dict failed", exc_info=True)
        return {"total_rows": 0, "bucket_count": 0, "buckets": []}


def seek_hint_for(target_ts: str) -> str | None:
    """Convenience wrapper: return the ``since=`` hint for *target_ts*.

    Returns ``None`` if the index is empty or the target is before all
    known rows (caller falls back to a full scan).
    """
    idx = _get_or_create_index()
    try:
        return idx.seek_hint(target_ts)
    except Exception:  # noqa: BLE001
        _log.debug("server_log_checkpoint.seek_hint_for failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bucket_label(chars: int) -> str:
    """Human-readable label for a bucket granularity expressed as char-count."""
    labels = {
        7: "monthly",
        10: "daily",
        13: "hourly",
        16: "minute",
        19: "second",
    }
    return labels.get(chars, f"{chars}-char prefix")
