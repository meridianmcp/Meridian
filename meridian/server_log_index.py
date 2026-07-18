"""BM25-searchable index over the server_logs ring-buffer — 222d54f8.

Architecture decision
---------------------
This module lives in core ``meridian/`` (not in the meridian-outputs extension)
because ``server_logs`` is a core-server concept: rows are written by the
application's own logging handler (``_MeridianDBLogHandler``) and the table is
defined in ``meridian/db/migrations.py``.  The DuckDB FTS machinery used here is
the same pattern already established in ``meridian/code_index.py`` (also core),
so keeping it core is consistent with that precedent.

The meridian-outputs extension's ``OutputsFtsIndex`` is the reference
implementation for the DuckDB FTS pattern (PRAGMA create_fts_index, Okapi BM25,
per-process in-memory flag + on-disk persistence).  We reuse that pattern
faithfully, simplified for the server_logs use-case:

- Source data: the ``server_logs`` SQLite/Postgres table (ring-buffer, up to
  2000 rows globally), NOT a filesystem tree.
- Incremental indexing: we track the set of ``id`` values already indexed and
  only write to DuckDB when new rows have arrived or ring-buffer eviction is
  detected (current row count dropped below our last-seen count).
- Persistence: the DuckDB sidecar lives in ``data_dir/server_log_index.duckdb``
  when ``data_dir`` is provided; falls back to ``:memory:`` otherwise (fine for
  tests and edge cases).
- Thread safety: a single ``threading.Lock`` serialises all DuckDB writes and
  reads (the DB is tiny -- max 2000 rows -- so one lock is fine).
- b1789c0d parity: we implement ``_fts_pending`` with the same lazy-build-on-
  next-search semantics so a cold index (no FTS yet, but rows already synced)
  never returns empty hits forever.

Ring-buffer eviction consistency
---------------------------------
``server_logs`` is capped at 2000 rows globally (``_SERVER_LOGS_RING_SIZE``).
When the ring overflows the oldest rows are deleted from the DB.  We detect this
by comparing the CURRENT visible row-count in the DB with the row-count we
recorded at the end of the LAST sync:

  - If current_count < last_synced_count: prune happened; do a FULL resync
    (DELETE all rows from the DuckDB table and reinsert from the DB).
  - Also delete orphaned ids (in DuckDB but not in current DB snapshot) --
    handles the edge case where count is the same but different rows evicted.
  - If no change: fast warm path (no DuckDB write, no FTS rebuild).

This is cheap: the ring-buffer is always tiny (max 2000 rows), so even a full
resync is fast today.  36a401fa: ``sync()`` nonetheless takes the same
deadline-skip + lazy-pending escape hatch as ``OutputsFtsIndex.rebuild()``
(b1789c0d) around its ``_rebuild_fts()`` call, so that invariant does not have
to hold forever for this module to stay safe -- see ``DEFAULT_SYNC_BUDGET_SECONDS``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

_log = logging.getLogger(__name__)

# 36a401fa — deadline budget for sync()'s _rebuild_fts() call, mirroring the
# b1789c0d fix in OutputsFtsIndex.rebuild().  server_logs is currently capped
# to a small ring-buffer (2000 rows globally, see _SERVER_LOGS_RING_SIZE in
# meridian/db/__init__.py), so _rebuild_fts() is fast today -- but the code
# below called it unconditionally on every sync() where rows changed, with NO
# deadline check at all.  That is the exact "monolithic full-rebuild, no
# budget, no cold-index escape hatch" shape that caused b1789c0d/de33589b on
# OutputsFtsIndex once its data source (66k files) outgrew what a synchronous
# rebuild could do inside the ~4min external MCP client timeout.  Even though
# server_logs is small *today*, nothing here enforced that invariant, so a
# future ring-size increase (or a differently-sized log store) would silently
# reintroduce the same failure.  Add the same deadline-skip + lazy-pending
# escape hatch defensively, before it is ever exercised at scale.
DEFAULT_SYNC_BUDGET_SECONDS = 20.0

# ---------------------------------------------------------------------------
# Module-level singleton cache (one index per db_path)
# ---------------------------------------------------------------------------

_index_lock = threading.Lock()
_index_cache: dict[str, "ServerLogFtsIndex"] = {}


def _get_index(db_path: str) -> "ServerLogFtsIndex":
    """Return (or create) the cached :class:`ServerLogFtsIndex` for *db_path*."""
    with _index_lock:
        idx = _index_cache.get(db_path)
        if idx is None:
            idx = ServerLogFtsIndex(db_path=db_path)
            _index_cache[db_path] = idx
        return idx


# ---------------------------------------------------------------------------
# ServerLogFtsIndex
# ---------------------------------------------------------------------------

class ServerLogFtsIndex:
    """Persistent DuckDB FTS index over the ``server_logs`` ring-buffer.

    Replicates the proven ``OutputsFtsIndex`` pattern from
    ``meridian/outputs_indexer.py`` (and its extension clone in
    ``extensions/meridian-outputs/meridian_outputs/outputs_local.py``):

    * Persistent on-disk DuckDB sidecar (falls back to ``:memory:``).
    * ``PRAGMA create_fts_index`` with Okapi BM25 + Porter stemmer.
    * Incremental sync: only new/evicted rows trigger a DB write.
    * ``_fts_pending`` flag (b1789c0d parity): when rows are in the table but
      FTS was not yet built, the next :meth:`search` call triggers a lazy FTS
      rebuild.
    """

    # DuckDB table name for the log rows we maintain locally.
    _TABLE = "server_logs_index"
    # DuckDB FTS schema name (DuckDB derives this from the table name).
    _FTS_SCHEMA = "fts_main_server_logs_index"

    def __init__(self, *, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._con: Any = None
        self._fts_built = False
        # b1789c0d parity: set when rows exist in the table but _rebuild_fts
        # was deferred.  search() checks this and triggers a lazy build.
        self._fts_pending = False
        # Track how many rows we last synced, for ring-buffer eviction detection.
        self._last_sync_count: int = 0
        # 36a401fa — set when the most recent sync() skipped _rebuild_fts()
        # because its deadline had already passed (mirrors
        # OutputsFtsIndex.last_rebuild_partial).
        self.last_sync_partial = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> Any:
        if self._con is not None:
            return self._con
        import duckdb  # noqa: PLC0415
        self._con = duckdb.connect(self._db_path)
        self._ensure_schema(self._con)
        # On a fresh process connecting to a PERSISTENT DuckDB sidecar, detect
        # an existing FTS schema so we don't rebuild from scratch (d9c76caa
        # follow-up pattern from OutputsFtsIndex._connect).
        try:
            existing = self._con.execute(
                "SELECT 1 FROM information_schema.schemata "
                f"WHERE schema_name = '{self._FTS_SCHEMA}'"
            ).fetchone()
            if existing is not None:
                self._fts_built = True
        except Exception:  # noqa: BLE001
            _log.debug(
                "ServerLogFtsIndex._connect: FTS schema probe failed",
                exc_info=True,
            )
        # Rehydrate _last_sync_count from an existing on-disk table so we don't
        # treat every row as "new" on process restart.
        try:
            row = self._con.execute(
                f"SELECT COUNT(*) FROM {self._TABLE}"
            ).fetchone()
            if row:
                self._last_sync_count = int(row[0] or 0)
        except Exception:  # noqa: BLE001
            _log.debug(
                "ServerLogFtsIndex._connect: rehydrate failed", exc_info=True,
            )
        return self._con

    def _ensure_schema(self, con: Any) -> None:
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {self._TABLE} ("
            "id TEXT PRIMARY KEY, "
            "level TEXT NOT NULL, "
            "logger TEXT NOT NULL, "
            "message TEXT NOT NULL, "
            "exc_text TEXT, "
            "recorded_at TEXT NOT NULL, "
            # Denormalized search body: level + logger + message + exc_text
            # concatenated so BM25 matches keywords in any of them.
            "search_body TEXT NOT NULL"
            ")"
        )

    def _rebuild_fts(self, con: Any) -> None:
        """Full FTS rebuild over the local table.  Caller must hold self._lock."""
        con.execute("INSTALL fts")
        con.execute("LOAD fts")
        con.execute(
            f"PRAGMA create_fts_index("
            f"'{self._TABLE}', 'id', 'search_body', "
            f"stemmer = 'porter', stopwords = 'none', overwrite = 1)"
        )
        self._fts_built = True
        self._fts_pending = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(
        self,
        rows: list[dict[str, Any]],
        *,
        max_seconds: float | None = DEFAULT_SYNC_BUDGET_SECONDS,
    ) -> int:
        """Sync *rows* (complete snapshot of ``server_logs``) into DuckDB.

        *rows* is the full current contents of the ``server_logs`` table as
        returned by :func:`meridian.db.get_server_logs` with no filter (newest
        first is fine; order is irrelevant to the sync).

        Returns the number of rows now in the DuckDB index after the sync.

        Algorithm
        ----------
        1. If current count < ``_last_sync_count``: ring-buffer was pruned.
           DELETE all rows from DuckDB and reinsert fresh (full resync).
        2. Find ids in the current snapshot NOT yet in DuckDB and INSERT them.
        3. Find ids in DuckDB NOT in the current snapshot and DELETE them
           (handles the edge case where count is the same but rows changed).
        4. Rebuild FTS if anything changed -- unless *max_seconds* has already
           elapsed (36a401fa / b1789c0d parity): row writes always complete
           (cheap, linear in row count with no FTS-build cost), but the
           potentially-expensive ``_rebuild_fts()`` call is skipped once the
           deadline passes, and ``_fts_pending`` is set so the next
           :meth:`search` call performs a lazy build with a fresh budget
           instead of blocking this call indefinitely.
        """
        deadline = (
            None if max_seconds is None else time.monotonic() + max_seconds
        )
        self.last_sync_partial = False
        with self._lock:
            try:
                con = self._connect()
                current_count = len(rows)
                current_ids = {r["id"] for r in rows}

                # Step 1: detect ring-buffer eviction via count drop.
                if current_count < self._last_sync_count:
                    con.execute(f"DELETE FROM {self._TABLE}")
                    self._last_sync_count = 0

                # Step 2 + 3: find new and orphaned ids.
                try:
                    existing = con.execute(
                        f"SELECT id FROM {self._TABLE}"
                    ).fetchall()
                    existing_ids: set[str] = {r[0] for r in existing}
                except Exception:  # noqa: BLE001
                    existing_ids = set()

                new_rows = [r for r in rows if r["id"] not in existing_ids]
                orphaned = existing_ids - current_ids

                # Delete orphaned ids (evicted from ring-buffer since last sync).
                for oid in orphaned:
                    con.execute(
                        f"DELETE FROM {self._TABLE} WHERE id = ?", [oid]
                    )

                # Insert new rows.
                if new_rows:
                    con.executemany(
                        f"INSERT INTO {self._TABLE} "
                        "(id, level, logger, message, exc_text, recorded_at, search_body) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            [
                                r["id"],
                                r.get("level", ""),
                                r.get("logger", ""),
                                r.get("message", ""),
                                r.get("exc_text"),
                                r.get("recorded_at", ""),
                                # search_body: concatenate all searchable fields.
                                " ".join(filter(None, [
                                    r.get("level", ""),
                                    r.get("logger", ""),
                                    r.get("message", ""),
                                    r.get("exc_text") or "",
                                ])),
                            ]
                            for r in new_rows
                        ],
                    )

                # 36a401fa — deadline check gates the potentially-expensive
                # _rebuild_fts() call the same way b1789c0d gates
                # OutputsFtsIndex._rebuild_fts(): the OLD code below called it
                # unconditionally whenever `changed` was True, with no escape
                # hatch if it ran long. Row writes above always happen (cheap);
                # only the FTS rebuild is deferred on deadline expiry.
                deadline_passed = (
                    deadline is not None and time.monotonic() > deadline
                )
                changed = bool(new_rows or orphaned)
                if changed:
                    if deadline_passed:
                        self._fts_pending = True
                        self.last_sync_partial = True
                    else:
                        self._rebuild_fts(con)
                elif not self._fts_built:
                    # Rows already in table (rehydrated on connect) but FTS not
                    # yet built.  Build it now if there are rows -- unless the
                    # deadline has already passed, in which case defer to the
                    # next search() call (same lazy-build escape hatch).
                    try:
                        n = con.execute(
                            f"SELECT COUNT(*) FROM {self._TABLE}"
                        ).fetchone()
                        if n and int(n[0]) > 0:
                            if deadline_passed:
                                self._fts_pending = True
                                self.last_sync_partial = True
                            else:
                                self._rebuild_fts(con)
                        else:
                            self._fts_pending = True
                    except Exception:  # noqa: BLE001
                        self._fts_pending = True

                self._last_sync_count = current_count
                return current_count
            except Exception:  # noqa: BLE001
                _log.debug("ServerLogFtsIndex.sync failed", exc_info=True)
                return 0

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        level: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """BM25 search over the FTS index.  Best-effort: errors yield [].

        *level* and *since* are post-BM25 filters applied in Python after BM25
        ranking (the index is small enough that this is cheaper than a compound
        FTS query).

        b1789c0d parity: if ``_fts_pending`` is True (rows in table but FTS was
        deferred), attempt a lazy FTS build now before searching.
        """
        q = (query or "").strip()
        if not q:
            return []
        safe_limit = max(1, int(limit))
        with self._lock:
            try:
                con = self._connect()
                # Lazy FTS build (b1789c0d parity).
                if not self._fts_built:
                    self._fts_pending = False
                    self._rebuild_fts(con)
                sql = (
                    f"SELECT id, level, logger, message, exc_text, recorded_at, "
                    f"{self._FTS_SCHEMA}.match_bm25(id, ?) AS bm25 "
                    f"FROM {self._TABLE} "
                    f"WHERE {self._FTS_SCHEMA}.match_bm25(id, ?) IS NOT NULL "
                    f"ORDER BY bm25 DESC "
                    # Fetch extra candidates so post-BM25 filters don't under-deliver.
                    f"LIMIT ?"
                )
                relation = con.execute(sql, [q, q, safe_limit * 4])
                columns = [c[0] for c in relation.description]
                fetched = relation.fetchall()
            except Exception:  # noqa: BLE001
                _log.debug("ServerLogFtsIndex.search failed", exc_info=True)
                return []
        hits: list[dict[str, Any]] = []
        for row in fetched:
            rec = dict(zip(columns, row))
            bm25 = rec.get("bm25")
            if bm25 is None:
                continue
            # Post-BM25 filters.
            if level and rec.get("level", "").upper() != level.upper():
                continue
            if since and (rec.get("recorded_at") or "") < since:
                continue
            hits.append({
                "id": rec["id"],
                "level": rec.get("level"),
                "logger": rec.get("logger"),
                "message": rec.get("message"),
                "exc_text": rec.get("exc_text"),
                "recorded_at": rec.get("recorded_at"),
                "score": float(bm25),
                "bm25": float(bm25),
            })
            if len(hits) >= safe_limit:
                break
        return hits

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                try:
                    self._con.close()
                except Exception:  # noqa: BLE001
                    pass
                self._con = None
                self._fts_built = False


# ---------------------------------------------------------------------------
# Module-level function called by the MCP handler
# ---------------------------------------------------------------------------

def search_server_logs(
    rows: list[dict[str, Any]],
    query: str,
    *,
    limit: int = 20,
    level: str | None = None,
    since: str | None = None,
    db_path: str = ":memory:",
    max_seconds: float | None = DEFAULT_SYNC_BUDGET_SECONDS,
) -> dict[str, Any]:
    """BM25 search over *rows* from the ``server_logs`` ring-buffer.

    *rows* is the complete current snapshot of ``server_logs`` (fetched from the
    DB by the async caller before invoking this function, so we don't need an
    async DB handle here).

    *max_seconds* (36a401fa) bounds the ``sync()`` call's FTS-rebuild cost --
    see :meth:`ServerLogFtsIndex.sync`.  On the (currently unreachable at
    today's 2000-row ring-buffer cap, but not structurally impossible) case
    where the deadline is hit, this returns ``partial=True`` and, if the FTS
    index itself is not yet built, ``fts_pending=True`` -- the caller should
    re-invoke to get a fully-built index and real hits, exactly the
    b1789c0d/de33589b contract used by ``search_outputs()``.

    Returns::

        {
            "query": str,
            "total_in_index": int,
            "count": int,
            "hits": [
                {
                    "id": str,
                    "level": str,
                    "logger": str,
                    "message": str,
                    "exc_text": str | None,
                    "recorded_at": str,
                    "score": float,
                    "bm25": float,
                }
            ],
            "partial": bool,       # only present when True
            "fts_pending": bool,   # only present when True
        }
    """
    result: dict[str, Any] = {
        "query": query,
        "total_in_index": 0,
        "count": 0,
        "hits": [],
    }
    if not query or not str(query).strip():
        result["error"] = "query is required"
        return result
    index = _get_index(db_path)
    total = index.sync(rows, max_seconds=max_seconds)
    result["total_in_index"] = total
    hits = index.search(query, limit=limit, level=level, since=since)
    result["hits"] = hits
    result["count"] = len(hits)
    if index.last_sync_partial:
        result["partial"] = True
    if index._fts_pending:
        result["fts_pending"] = True
        result["partial"] = True
    return result
