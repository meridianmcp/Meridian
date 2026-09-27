"""SQLite reference implementation of the ``outputs_index`` row-store role.

Scope, deliberately narrow: this module implements ONLY the persistent
metadata/index table contract that :class:`~meridian_outputs.outputs_local.
OutputsFtsIndex` currently backs with DuckDB -- schema, bulk upsert, point
lookup by path, delete, a path-sorted full scan, and space-reclaiming
compaction. It does NOT reimplement ``OutputsFtsIndex``'s other
~4,000 lines of surrounding logic (annotation management tied to Meridian
notes ingestion, legacy-path migration, hash-algo-version tracking,
ancestor-seeding for the periodic-restart harness, lock diagnostics,
Tantivy integration, the resumable file walk) -- none of that is specific
to which database backs the row store, and re-deriving it here would just
be an unreviewed duplicate of already-hardened, already-tested code.

Whether SQLite or DuckDB is actually faster here is CONTENT-SIZE DEPENDENT
-- this was tested twice, with opposite results, and both results are
real. At small per-row content (~3KB/row, up to 1M rows/~3GB, a synthetic
shape chosen before any real corpus was consulted), SQLite wins point
lookups by 30-40x and DuckDB's fixed per-query dispatch/planning overhead
dominates every operation. At content sizes matching this project's ACTUAL
real corpora (``SUT_Compressed``, ~326KB/file average; the full 466GiB
corpus, ~751KB/file average) -- tested directly, not assumed -- DuckDB
wins insert (1.5-6.3x across repeated runs) and point lookups (1.75-3.75x)
consistently, and a full sorted scan is a genuine toss-up (bounces both
directions run to run, never more than ~1.3x either way). The mechanism:
DuckDB's fixed per-query overhead, the dominant cost at small payloads,
gets outweighed by its columnar/compressed storage's efficiency at moving
hundreds of KB per row once payload size crosses some threshold -- real
corpora here are well past that threshold; the original small-content test
was well under it. **This module's row-store role should NOT be read as a
settled recommendation to replace DuckDB for the real corpora this project
actually indexes** -- for those, the evidence now points the other way.
It remains a real, tested, useful reference implementation, and DuckDB's
own ``VACUUM`` genuinely does not reclaim on-disk space at all regardless
of content size (confirmed live at both scales, still an open upstream
issue) where SQLite's native ``VACUUM INTO`` does the job in one statement
-- that specific advantage is content-size-independent and still holds.

Design choices specific to this module, not inherited from the DuckDB
implementation because they solve DuckDB-specific problems that do not
exist here:

- No memory_limit/preserve_insertion_order tuning -- both exist in
  ``OutputsFtsIndex`` to bound DuckDB's own vectorized-execution memory
  growth on large batches. SQLite's per-connection memory footprint for
  this row shape does not have that growth pattern; there is nothing
  analogous to bound.
- WAL journal mode + ``synchronous=NORMAL`` -- the standard, safe pairing
  for a single-writer/multi-reader embedded workload (this matches the
  access pattern ``OutputsFtsIndex`` already enforces via
  ``IndexFileLock``): WAL lets concurrent readers proceed without
  blocking on a writer, and NORMAL skips an fsync on every commit while
  still guaranteeing the database file itself cannot become corrupted by
  an application crash (only a full OS-level power loss can lose the
  most recent WAL-mode commits under NORMAL, which is the same tradeoff
  most production SQLite deployments accept).
- ``WITHOUT ROWID`` on the main table -- ``path`` is the only real access
  key (every query in the real workload is a lookup, insert, or scan
  keyed on it); a normal SQLite table would maintain both an implicit
  rowid B-tree AND a separate index over ``path`` for the PRIMARY KEY
  constraint. ``WITHOUT ROWID`` makes ``path`` the table's own clustered
  key, storing rows directly in that B-tree -- one structure instead of
  two, for a table that is never queried by rowid.
- Plain ``executemany`` in one transaction for bulk writes, not pyarrow --
  pyarrow's Arrow-registration path exists in the DuckDB implementation
  specifically to route around DuckDB's own slow parameter-bound insert
  path (confirmed via DuckDB's own maintainers: row-by-row insert is a
  documented anti-pattern for their engine). SQLite's row-at-a-time
  insert inside one transaction IS its fast path; there is no analogous
  workaround needed, and adding pyarrow here would just be a large,
  unnecessary dependency solving a problem this engine does not have.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

_log = logging.getLogger(__name__)

_COLUMNS: tuple[str, ...] = (
    "path", "content", "mtime", "sha256", "size", "generating_script",
    "kind", "is_archival", "canonical_path", "csv_columns", "json_keys",
)

_CREATE_OUTPUTS_INDEX = (
    "CREATE TABLE IF NOT EXISTS outputs_index ("
    "path TEXT PRIMARY KEY, content TEXT, mtime REAL, "
    "sha256 TEXT, size INTEGER, generating_script TEXT, "
    "kind TEXT, is_archival INTEGER, canonical_path TEXT, "
    "csv_columns TEXT, json_keys TEXT"
    ") WITHOUT ROWID"
)
_CREATE_META = (
    "CREATE TABLE IF NOT EXISTS outputs_index_meta ("
    "key TEXT PRIMARY KEY, value TEXT"
    ") WITHOUT ROWID"
)

_INSERT_SQL = f"INSERT OR REPLACE INTO outputs_index VALUES ({','.join('?' * len(_COLUMNS))})"
_SELECT_SQL = f"SELECT {','.join(_COLUMNS)} FROM outputs_index WHERE path = ?"
_SELECT_ALL_SORTED_SQL = f"SELECT {','.join(_COLUMNS)} FROM outputs_index ORDER BY path"

# Mirrors OutputsFtsIndex's own _WRITE_CHUNK_DEFAULT (outputs_local.py,
# 1bce8c41): bounds DELETE's parameter-list size per statement, independent
# of insert batching (executemany has no such limit -- SQLite streams bound
# parameters rather than building one combined VALUES list).
_DELETE_CHUNK = 2000

# SQLite's wal_autocheckpoint is a PAGE-count threshold (default 1000 pages,
# ~4MB at the default 4096-byte page size), unlike DuckDB's byte-size
# checkpoint_threshold (OutputsFtsIndex used '1GB'). 8000 pages here is
# deliberately smaller than a byte-equivalent 1GB would be: WAL-mode
# checkpoints in SQLite are cheap relative to DuckDB's, and this project's
# own prior perf work (2026-09-05 checkpoint_threshold fix) found that a
# checkpoint threshold that is too LARGE, not too small, is what causes a
# stop-the-world stall on this kind of resumable, many-short-calls workload.
_DEFAULT_WAL_AUTOCHECKPOINT_PAGES = 8000


_MIGRATION_DEFAULT_CHUNK = 2000
_MIGRATION_DEFAULT_MAX_CHUNK_BYTES = 32 * 1024 * 1024  # 32 MiB


def migrate_from_duckdb(
    duckdb_path: str,
    target: "OutputsSqliteStore",
    *,
    chunk_size: int = _MIGRATION_DEFAULT_CHUNK,
    max_chunk_bytes: int = _MIGRATION_DEFAULT_MAX_CHUNK_BYTES,
) -> int:
    """Copy every ``outputs_index`` row from an existing DuckDB-backed
    index into ``target``, a metadata-only row transfer -- NOT a rebuild.

    This is the practical answer to "what happens to an index that
    already exists": the two schemas are structurally identical (same 11
    columns, same primary key), so migrating is reading every row out of
    one engine and inserting it into the other, never re-walking the
    source directory tree, re-hashing file content, or re-running content
    analysis. For a large real corpus this matters a great deal --
    re-indexing 466 GiB from scratch and copying 466 GiB of already-
    computed metadata rows are entirely different costs, and this
    function is deliberately only the second one.

    Reads the source with DuckDB opened ``read_only=True`` (the source
    index keeps working normally throughout; nothing here requires
    downtime), fetching ONE ROW AT A TIME via the cursor's own
    ``fetchone`` and flushing to ``target`` once EITHER ``chunk_size``
    rows OR ``max_chunk_bytes`` of accumulated ``content`` have been
    buffered, whichever comes first.

    The byte budget exists because a fixed ROW-COUNT chunk size alone is
    not memory-safe across real corpora: this project's own real corpora
    range from a few KB/file (the controlled synthetic corpus) to
    hundreds of KB/file (``SUT_Compressed``, ~326 KB average) to closer to
    1 MB/file (the full 466 GiB corpus, ~751 KB average) -- confirmed
    live, this function's own earlier fixed-``chunk_size``-only design
    (``chunk_size=2000`` alone, no byte budget, using ``fetchmany`` rather
    than ``fetchone``) hit a genuine ``MemoryError``/``ArrowMemoryError``
    migrating a corpus at ``SUT_Compressed``'s real scale on a host under
    ordinary concurrent load, because 2,000 rows at ~326 KB each is a
    ~650 MB single fetch regardless of how much total memory is free --
    large contiguous allocations can fail under memory fragmentation even
    when aggregate free memory is large. Fetching one row at a time (not
    ``fetchmany``) and flushing on a BYTE budget, not just a row-count
    one, keeps each individual allocation small regardless of average
    row size, the same problem class this project's own adaptive
    Phase-1 batch sizing (``_adaptive_batch_limit``, ``outputs_local.py``)
    already exists to avoid on the indexing side.

    duckdb is imported lazily here, exactly like every other cross-engine
    dependency in this codebase (e.g. ``outputs_local.py``'s own lazy
    ``pyarrow``/``blake3`` imports) -- this module otherwise has no hard
    dependency on DuckDB at all.

    Returns the number of rows migrated. Raises whatever the source
    ``duckdb.connect`` or query raises if the source file is missing or
    unreadable; does not partially commit destination rows from a chunk
    whose read half succeeded but write half failed (each chunk's
    ``upsert_rows`` call is its own real transaction with its own
    rollback-on-failure, per that method's own contract).
    """
    import duckdb  # noqa: PLC0415 -- lazy, see docstring

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        cursor = con.execute(f"SELECT {','.join(_COLUMNS)} FROM outputs_index ORDER BY path")
        total = 0
        buffer: list[OutputsIndexRow] = []
        buffer_bytes = 0
        while True:
            row = cursor.fetchone()
            if row is None:
                break
            parsed = OutputsIndexRow.from_row(row)
            buffer.append(parsed)
            buffer_bytes += len(parsed.content) if parsed.content else 0
            if len(buffer) >= chunk_size or buffer_bytes >= max_chunk_bytes:
                target.upsert_rows(buffer)
                total += len(buffer)
                buffer = []
                buffer_bytes = 0
        if buffer:
            target.upsert_rows(buffer)
            total += len(buffer)
        return total
    finally:
        con.close()


class SqliteStoreError(Exception):
    """Raised when the underlying connection is fatally broken (SQLite's
    analogue of DuckDB's ``FatalException``: the connection must be
    discarded and a fresh one opened, not retried in place)."""


@dataclass(frozen=True, slots=True)
class OutputsIndexRow:
    """One ``outputs_index`` row. Deliberately independent of
    ``outputs_local.OutputRow`` -- this module has no import dependency on
    ``outputs_local.py`` at all, so it can be tested and reasoned about in
    complete isolation. A future integration layer adapts between the two.
    """
    path: str
    content: str | None
    mtime: float | None
    sha256: str | None
    size: int | None
    generating_script: str | None
    kind: str
    is_archival: bool
    canonical_path: str | None
    csv_columns: str | None
    json_keys: str | None

    def as_tuple(self) -> tuple:
        return (
            self.path, self.content, self.mtime, self.sha256, self.size,
            self.generating_script, self.kind, int(self.is_archival),
            self.canonical_path, self.csv_columns, self.json_keys,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple) -> "OutputsIndexRow":
        return cls(
            path=row[0], content=row[1], mtime=row[2], sha256=row[3],
            size=row[4], generating_script=row[5], kind=row[6],
            is_archival=bool(row[7]), canonical_path=row[8],
            csv_columns=row[9], json_keys=row[10],
        )


class OutputsSqliteStore:
    """SQLite-backed row store for one ``outputs_index`` table.

    Not thread-safe on its own -- exactly like ``OutputsFtsIndex``, callers
    are expected to serialise writers externally (that project's
    ``IndexFileLock``); this class only guards against a single Python
    process handing the same connection object to two threads at once,
    via ``self._lock``, matching ``OutputsFtsIndex``'s own
    ``self._read_lock`` role.
    """

    def __init__(
        self,
        db_path: str,
        *,
        wal_autocheckpoint_pages: int = _DEFAULT_WAL_AUTOCHECKPOINT_PAGES,
    ) -> None:
        self._db_path = db_path
        self._wal_autocheckpoint_pages = wal_autocheckpoint_pages
        self._con: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        # Double-checked locking: the fast path (already connected) avoids
        # taking the lock on every call, but creating the connection AND
        # creating the schema must BOTH happen inside the same lock hold,
        # not just connection creation -- confirmed live, in two stages,
        # under pytest-cov's tracing slowdown (which widened an otherwise
        # narrow race enough to reproduce it reliably):
        #   1. Without any lock here, concurrent first-access threads could
        #      each see self._con as None and each create their OWN
        #      separate sqlite3.Connection against the same db_path,
        #      genuinely colliding at the SQLite level ("database is
        #      locked") -- a per-instance RLock guarding only the
        #      transaction bodies in upsert_rows/delete_paths can't prevent
        #      that, since the race is in connection creation itself.
        #   2. Fixed #1 by locking connection creation, but originally
        #      still called self.ensure_schema() AFTER releasing the lock:
        #      a second thread could then acquire the lock, see self._con
        #      already set, return immediately, and try an INSERT before
        #      the first thread's ensure_schema() had actually created the
        #      table ("no such table: outputs_index"). Schema creation
        #      must complete before the lock is released, not after.
        if self._con is not None:
            return self._con
        with self._lock:
            if self._con is not None:
                return self._con
            if self._db_path != ":memory:":
                parent = os.path.dirname(self._db_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            # check_same_thread=False: this class serialises all real access
            # itself via self._lock (an RLock), exactly like OutputsFtsIndex
            # serialises its own connection via IndexFileLock/self._read_lock --
            # sqlite3's default same-thread guard is a blanket safety check with
            # no awareness of that external serialisation, and would otherwise
            # reject the (safe, lock-protected) multi-threaded access this
            # class is explicitly designed to support.
            con = sqlite3.connect(self._db_path, isolation_level=None, check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute(f"PRAGMA wal_autocheckpoint={self._wal_autocheckpoint_pages}")
            con.execute(_CREATE_OUTPUTS_INDEX)
            con.execute(_CREATE_META)
            # Assigned last, still inside the lock: no other thread may
            # observe self._con as non-None until the connection is fully
            # configured AND the schema exists.
            self._con = con
        return con

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None

    def ensure_schema(self) -> None:
        """Schema creation is now folded into connect() itself (see its
        docstring: it must complete before any other thread can observe a
        usable connection). This method exists as an explicit, named
        no-op-if-already-connected call for callers that want to force
        schema creation without caring about the returned connection."""
        self.connect()

    def _reraise_if_fatal(self, exc: sqlite3.Error) -> None:
        """SQLite's equivalent of DuckDB's FatalException check
        (``outputs_local.py``'s own reconnect-on-fatal-error path,
        ``duckdb.FatalException`` at rebuild()'s checkpoint-persist call
        site): a genuinely broken connection (real corruption, e.g.
        "database disk image is malformed") must be discarded, never
        retried in place.

        This is deliberately an EXACT type check, not ``isinstance``:
        every one of sqlite3's specific, well-understood error classes
        (``OperationalError`` for "database is locked"/"database is busy",
        ``IntegrityError`` for a constraint violation, ``ProgrammingError``
        for a malformed call, etc.) is a SUBCLASS of ``DatabaseError`` --
        an ``isinstance(exc, sqlite3.DatabaseError)`` check matches nearly
        every real sqlite3 error, not just corruption (confirmed live: an
        earlier version of this check wrongly discarded the connection on
        a plain ``IntegrityError``). CPython's sqlite3 module raises the
        bare ``DatabaseError`` class itself, with no more specific
        subclass, precisely for the corruption-class conditions that have
        no dedicated subclass -- that bare-class case is what this method
        actually needs to catch. None of those specific subclasses are
        fatal here: this project already serialises writers via
        ``IndexFileLock`` at the application level, so e.g. a lock
        conflict reaching SQLite at all indicates that external
        serialisation was bypassed, which is a caller bug to surface, not
        something this class should silently retry around.
        """
        if type(exc) is sqlite3.DatabaseError:
            self.close()
            raise SqliteStoreError(f"fatal SQLite error, connection discarded: {exc}") from exc

    @staticmethod
    def _safe_rollback(con: sqlite3.Connection) -> None:
        """Best-effort ROLLBACK from inside an except block. Deliberately
        swallows its OWN failure (logged, not raised): if the connection is
        already broken, ROLLBACK failing too is expected, not new
        information -- and letting it raise from here would replace the
        real original exception with this secondary one, masking the
        actual root cause from the caller (confirmed live: closing the
        connection out from under this class made BEGIN IMMEDIATE and
        ROLLBACK fail with the identical message, and an unguarded ROLLBACK
        call meant the ROLLBACK's own exception -- not the one that
        actually explains what went wrong -- was what callers saw)."""
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error as rollback_exc:
            _log.warning(
                "OutputsSqliteStore: ROLLBACK itself failed after another "
                "error -- connection is likely already broken: %s", rollback_exc,
            )

    def upsert_rows(self, rows: Sequence[OutputsIndexRow]) -> None:
        if not rows:
            return
        con = self.connect()
        with self._lock:
            try:
                con.execute("BEGIN IMMEDIATE")
                con.executemany(_INSERT_SQL, (r.as_tuple() for r in rows))
                con.execute("COMMIT")
            except sqlite3.Error as exc:
                self._safe_rollback(con)
                self._reraise_if_fatal(exc)
                raise

    def delete_paths(self, paths: Sequence[str]) -> None:
        if not paths:
            return
        con = self.connect()
        with self._lock:
            try:
                con.execute("BEGIN IMMEDIATE")
                for i in range(0, len(paths), _DELETE_CHUNK):
                    chunk = paths[i:i + _DELETE_CHUNK]
                    placeholders = ",".join("?" for _ in chunk)
                    con.execute(f"DELETE FROM outputs_index WHERE path IN ({placeholders})", chunk)
                con.execute("COMMIT")
            except sqlite3.Error as exc:
                self._safe_rollback(con)
                self._reraise_if_fatal(exc)
                raise

    def get_by_path(self, path: str) -> OutputsIndexRow | None:
        con = self.connect()
        with self._lock:
            row = con.execute(_SELECT_SQL, (path,)).fetchone()
        return OutputsIndexRow.from_row(row) if row is not None else None

    def count(self) -> int:
        con = self.connect()
        with self._lock:
            (n,) = con.execute("SELECT COUNT(*) FROM outputs_index").fetchone()
        return n

    def iter_sorted(self) -> Iterator[OutputsIndexRow]:
        """Path-ordered full scan -- the migration/compaction access
        pattern. Native SQLite index order on the ``WITHOUT ROWID``
        clustered key, no separate sort step."""
        con = self.connect()
        with self._lock:
            rows = con.execute(_SELECT_ALL_SORTED_SQL).fetchall()
        for row in rows:
            yield OutputsIndexRow.from_row(row)

    def compact_to(self, new_db_path: str) -> None:
        """Reclaim deleted-row space into a fresh file at ``new_db_path``.

        One statement (SQLite's native ``VACUUM INTO``) where the DuckDB
        implementation needs ``ATTACH`` + resolving the source catalog
        name via ``PRAGMA database_list`` + ``COPY FROM DATABASE`` --
        because DuckDB's own ``VACUUM`` does not reclaim space at all
        (confirmed live, still open upstream). SQLite's ``VACUUM`` reclaims
        space in place already; ``VACUUM INTO`` is used here anyway so the
        caller gets the same "write a compacted copy, leave the original
        untouched" contract ``OutputsFtsIndex.compact_to`` already
        promises (crash-safety: swapping a live database file out from
        under a concurrent reader needs the caller's own quiesce/verify/
        rename sequencing, not attempted here, matching that method's own
        documented scope).
        """
        if self._db_path == ":memory:":
            raise ValueError("compact_to: nothing to compact in ':memory:' mode")
        target_norm = os.path.normcase(os.path.abspath(new_db_path))
        source_norm = os.path.normcase(os.path.abspath(self._db_path))
        if target_norm == source_norm:
            raise ValueError("compact_to: new_db_path must differ from this store's own db_path")
        con = self.connect()
        parent = os.path.dirname(new_db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._lock:
            con.execute(f"VACUUM INTO '{new_db_path}'")

    def real_disk_bytes(self) -> int:
        """Actual space used on disk, right now.

        In WAL mode (this class's default), a fresh write lives in the
        ``-wal`` sidecar file until SQLite auto-checkpoints it into the
        main file (at ``wal_autocheckpoint_pages``, or on close) --
        reading only ``Path(db_path).stat().st_size`` would silently
        undercount everything sitting in an un-checkpointed WAL, and the
        reported number would swing based on checkpoint timing rather
        than reflecting real usage. Summing the main file and its ``-wal``/
        ``-shm`` sidecars (whichever exist) gives a checkpoint-timing-
        independent answer. This is also why this method takes no
        DuckDB-vs-LMDB-style "apparent vs real" distinction the way a
        sparse-file-backed engine's equivalent call needs to -- none of
        these three files are ever sparse.
        """
        if self._db_path == ":memory:":
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(self._db_path + suffix)
            if p.exists():
                total += p.stat().st_size
        return total
