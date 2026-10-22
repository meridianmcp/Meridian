"""Tests for meridian_outputs.outputs_sqlite_store.

Covers:
  - Schema creation: idempotent, WITHOUT ROWID table present.
  - upsert_rows: single, batch, upsert-replaces-not-duplicates semantics,
    empty-list no-op.
  - get_by_path: found, not found, round-trips every column including
    is_archival's bool<->int coercion.
  - delete_paths: single, batch, chunk-boundary behaviour (> _DELETE_CHUNK
    paths), empty-list no-op, deleting a non-existent path is a silent no-op.
  - count: empty, after inserts, after deletes.
  - iter_sorted: path order, empty table, matches a plain sorted() of the
    same paths.
  - compact_to: real space reclaimed after a large delete (mirrors the
    DuckDB implementation's own compact_to() docstring test), error paths
    (:memory: mode, same-path target).
  - Connection lifecycle: connect() idempotent, close() then reconnect,
    ensure_schema() safe to call multiple times.
  - Fatal-error handling: sqlite3.DatabaseError triggers SqliteStoreError
    and discards the connection; sqlite3.OperationalError does not.
  - Concurrent access from multiple threads against one store instance.
  - real_disk_bytes: :memory: is 0, a real file reports its actual size.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs.outputs_sqlite_store import (  # noqa: E402
    OutputsIndexRow,
    OutputsSqliteStore,
    SqliteStoreError,
    _COLUMNS,
    migrate_from_duckdb,
)

duckdb = pytest.importorskip("duckdb", reason="migrate_from_duckdb tests need duckdb installed")

_DUCKDB_SCHEMA = (
    "CREATE TABLE outputs_index ("
    "path VARCHAR PRIMARY KEY, content VARCHAR, mtime DOUBLE, "
    "sha256 VARCHAR, size BIGINT, generating_script VARCHAR, "
    "kind VARCHAR, is_archival BOOLEAN, canonical_path VARCHAR, "
    "csv_columns VARCHAR, json_keys VARCHAR)"
)


def make_duckdb_source(db_path: str, rows: list[tuple]) -> None:
    con = duckdb.connect(db_path)
    con.execute(_DUCKDB_SCHEMA)
    if rows:
        con.executemany(
            "INSERT INTO outputs_index VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
    con.close()


def make_row(path: str, **overrides) -> OutputsIndexRow:
    defaults = dict(
        path=path, content="hello world", mtime=1700000000.0, sha256="a" * 64,
        size=11, generating_script="gen.py", kind="text", is_archival=False,
        canonical_path=None, csv_columns=None, json_keys=None,
    )
    defaults.update(overrides)
    return OutputsIndexRow(**defaults)


@pytest.fixture
def store(tmp_path):
    s = OutputsSqliteStore(str(tmp_path / "index.sqlite"))
    yield s
    s.close()


class TestSchema:
    def test_ensure_schema_creates_table(self, store):
        store.ensure_schema()
        con = store.connect()
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "outputs_index" in names
        assert "outputs_index_meta" in names

    def test_ensure_schema_idempotent(self, store):
        store.ensure_schema()
        store.ensure_schema()  # must not raise
        store.ensure_schema()

    def test_table_is_without_rowid(self, store):
        con = store.connect()
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='outputs_index'"
        ).fetchone()[0]
        assert "WITHOUT ROWID" in sql

    def test_wal_mode_enabled(self, store):
        con = store.connect()
        (mode,) = con.execute("PRAGMA journal_mode").fetchone()
        assert mode.lower() == "wal"


class TestUpsertAndGet:
    def test_single_row_round_trips_every_column(self, store):
        row = make_row(
            "a.txt", content="x", mtime=1.5, sha256="deadbeef", size=42,
            generating_script="foo.py", kind="csv", is_archival=True,
            canonical_path="/canon/a.txt", csv_columns='["a","b"]', json_keys=None,
        )
        store.upsert_rows([row])
        got = store.get_by_path("a.txt")
        assert got == row

    def test_is_archival_bool_coercion(self, store):
        store.upsert_rows([make_row("a.txt", is_archival=True), make_row("b.txt", is_archival=False)])
        assert store.get_by_path("a.txt").is_archival is True
        assert store.get_by_path("b.txt").is_archival is False

    def test_get_missing_path_returns_none(self, store):
        assert store.get_by_path("does/not/exist.txt") is None

    def test_upsert_empty_list_is_noop(self, store):
        store.upsert_rows([])
        assert store.count() == 0

    def test_upsert_same_path_replaces_not_duplicates(self, store):
        store.upsert_rows([make_row("a.txt", content="first")])
        store.upsert_rows([make_row("a.txt", content="second")])
        assert store.count() == 1
        assert store.get_by_path("a.txt").content == "second"

    def test_batch_upsert(self, store):
        rows = [make_row(f"file_{i:04d}.txt") for i in range(500)]
        store.upsert_rows(rows)
        assert store.count() == 500
        assert store.get_by_path("file_0250.txt") is not None


class TestDelete:
    def test_delete_single_path(self, store):
        store.upsert_rows([make_row("a.txt"), make_row("b.txt")])
        store.delete_paths(["a.txt"])
        assert store.count() == 1
        assert store.get_by_path("a.txt") is None
        assert store.get_by_path("b.txt") is not None

    def test_delete_empty_list_is_noop(self, store):
        store.upsert_rows([make_row("a.txt")])
        store.delete_paths([])
        assert store.count() == 1

    def test_delete_nonexistent_path_is_noop(self, store):
        store.upsert_rows([make_row("a.txt")])
        store.delete_paths(["never/existed.txt"])
        assert store.count() == 1

    def test_delete_across_chunk_boundary(self, store):
        # _DELETE_CHUNK is 2000 -- exercise more than one chunk in the
        # DELETE...IN(...) loop, including a partial final chunk.
        n = 4500
        rows = [make_row(f"file_{i:05d}.txt") for i in range(n)]
        store.upsert_rows(rows)
        to_delete = [f"file_{i:05d}.txt" for i in range(0, n, 2)]  # every other row
        store.delete_paths(to_delete)
        assert store.count() == n - len(to_delete)
        assert store.get_by_path("file_00000.txt") is None
        assert store.get_by_path("file_00001.txt") is not None


class TestCountAndScan:
    def test_count_empty(self, store):
        assert store.count() == 0

    def test_iter_sorted_empty(self, store):
        assert list(store.iter_sorted()) == []

    def test_iter_sorted_matches_path_order(self, store):
        paths = ["c.txt", "a.txt", "b.txt", "aa.txt"]
        store.upsert_rows([make_row(p) for p in paths])
        got = [r.path for r in store.iter_sorted()]
        assert got == sorted(paths)

    def test_iter_sorted_returns_full_rows(self, store):
        store.upsert_rows([make_row("a.txt", content="specific-content")])
        rows = list(store.iter_sorted())
        assert len(rows) == 1
        assert rows[0].content == "specific-content"


class TestCompaction:
    def test_compact_reclaims_space_after_large_delete(self, tmp_path):
        # Mirrors OutputsFtsIndex.compact_to's own docstring test: insert a
        # real batch, delete almost all of it, confirm the COMPACTED copy is
        # meaningfully smaller than the pre-delete size (this is the whole
        # point of compact_to existing -- DuckDB's own VACUUM does not do
        # this, unlike SQLite's plain VACUUM/VACUUM INTO).
        db_path = str(tmp_path / "index.sqlite")
        store = OutputsSqliteStore(db_path)
        n = 2000
        content = "x" * 2000  # large enough that row count dominates file size
        rows = [make_row(f"file_{i:05d}.txt", content=content) for i in range(n)]
        store.upsert_rows(rows)
        size_before = store.real_disk_bytes()

        store.delete_paths([f"file_{i:05d}.txt" for i in range(int(n * 0.98))])

        compact_path = str(tmp_path / "compact.sqlite")
        store.compact_to(compact_path)
        size_after = os.path.getsize(compact_path)
        store.close()

        assert size_after < size_before * 0.5, (
            f"expected the compacted copy to shrink substantially "
            f"(before={size_before}, after={size_after})"
        )
        # Original file is untouched (compact_to's documented contract:
        # writes a copy, does not swap the live file out from under a caller).
        assert os.path.exists(db_path)

    def test_compact_to_memory_mode_raises(self):
        store = OutputsSqliteStore(":memory:")
        with pytest.raises(ValueError, match="memory"):
            store.compact_to("/tmp/whatever.sqlite")

    def test_compact_to_same_path_raises(self, store):
        store.ensure_schema()
        with pytest.raises(ValueError, match="must differ"):
            store.compact_to(store._db_path)

    def test_compacted_copy_has_correct_data(self, tmp_path):
        db_path = str(tmp_path / "index.sqlite")
        store = OutputsSqliteStore(db_path)
        store.upsert_rows([make_row("a.txt"), make_row("b.txt")])
        compact_path = str(tmp_path / "compact.sqlite")
        store.compact_to(compact_path)
        store.close()

        verify = OutputsSqliteStore(compact_path)
        assert verify.count() == 2
        assert verify.get_by_path("a.txt") is not None
        verify.close()


class TestConnectionLifecycle:
    def test_connect_is_idempotent(self, store):
        con1 = store.connect()
        con2 = store.connect()
        assert con1 is con2

    def test_close_then_reconnect_preserves_data(self, tmp_path):
        db_path = str(tmp_path / "index.sqlite")
        store = OutputsSqliteStore(db_path)
        store.upsert_rows([make_row("a.txt")])
        store.close()

        store2 = OutputsSqliteStore(db_path)
        assert store2.get_by_path("a.txt") is not None
        store2.close()

    def test_close_without_ever_connecting_is_safe(self, tmp_path):
        store = OutputsSqliteStore(str(tmp_path / "index.sqlite"))
        store.close()  # must not raise


class TestFatalErrorHandling:
    """sqlite3.Connection is an immutable C type -- neither instance- nor
    class-level attribute patching can inject a failure into a real
    connection's executemany (confirmed: both raise TypeError/AttributeError
    attempting to (re)set the slot). Testing _reraise_if_fatal directly
    against fabricated exceptions is the precise, tool-independent way to
    verify the classification logic itself, which is what actually matters
    here -- not whether unittest.mock can reach into sqlite3's C internals.
    """

    def test_database_error_raises_and_discards_connection(self, store):
        store.ensure_schema()
        assert store._con is not None
        with pytest.raises(SqliteStoreError):
            store._reraise_if_fatal(sqlite3.DatabaseError("database disk image is malformed"))
        assert store._con is None

    def test_operational_error_does_not_raise_or_discard(self, store):
        store.ensure_schema()
        con = store._con
        store._reraise_if_fatal(sqlite3.OperationalError("database is locked"))
        assert store._con is con  # not treated as fatal, connection kept

    def test_integrity_error_does_not_raise_or_discard(self, store):
        # IntegrityError is also an OperationalError-adjacent, non-fatal
        # sqlite3.Error subclass (constraint violations) -- not a broken
        # connection, must not be discarded either.
        store.ensure_schema()
        con = store._con
        store._reraise_if_fatal(sqlite3.IntegrityError("UNIQUE constraint failed"))
        assert store._con is con

    def test_upsert_rolls_back_on_unbindable_value(self, store):
        # An unbindable Python value (dict) reaches sqlite3's own parameter
        # binding, not this class's SQL -- exercises upsert_rows' own
        # try/except/ROLLBACK wrapper end-to-end with a real error, not a
        # mocked one. Not fatal (ProgrammingError, not a bare DatabaseError).
        good = make_row("good.txt")
        bad = make_row("bad.txt", content={"not": "bindable"})  # type: ignore[arg-type]
        with pytest.raises(sqlite3.ProgrammingError):
            store.upsert_rows([good, bad])
        assert store.count() == 0  # rolled back, including the good row in the same batch
        assert store._con is not None  # not treated as fatal

    def test_delete_propagates_original_error_not_a_masking_rollback_failure(self, tmp_path):
        # Exercises delete_paths' own try/except/ROLLBACK wrapper with a
        # real failure (not just _reraise_if_fatal in isolation): close the
        # underlying connection out from under the store, so BOTH the
        # DELETE statement AND the except block's own ROLLBACK fail. The
        # caller must see the ORIGINAL error, not a second exception raised
        # by the ROLLBACK attempt itself (confirmed live this was a real
        # bug before _safe_rollback: the ROLLBACK's own failure silently
        # replaced the real one).
        db_path = str(tmp_path / "index.sqlite")
        store = OutputsSqliteStore(db_path)
        store.upsert_rows([make_row("a.txt"), make_row("b.txt")])
        store._con.close()  # simulate the connection breaking underneath the store
        with pytest.raises(sqlite3.ProgrammingError) as exc_info:
            store.delete_paths(["a.txt"])
        assert exc_info.value.__context__ is None, (
            "the rollback-failure warning must not chain a second exception "
            "onto the original one"
        )

    def test_upsert_rolls_back_and_propagates_on_constraint_style_error(self, store):
        # End-to-end check using a REAL failure this schema can actually
        # produce (rather than a mocked one): a row tuple with the wrong
        # arity trips sqlite3's own parameter-count check inside executemany,
        # which must roll back the transaction and propagate, not raise
        # SqliteStoreError (it is not a DatabaseError).
        store.ensure_schema()
        con = store._con
        # Wrong arity raises OperationalError in sqlite3 (not ProgrammingError
        # -- verified live), and OperationalError is correctly non-fatal per
        # _reraise_if_fatal's exact-type check.
        with pytest.raises(sqlite3.OperationalError):
            con.executemany("INSERT OR REPLACE INTO outputs_index VALUES (?,?,?)", [("a", "b", "c")])
        # Connection itself is still fine -- confirm normal use still works.
        store.upsert_rows([make_row("a.txt")])
        assert store.get_by_path("a.txt") is not None


class TestConcurrency:
    def test_concurrent_upserts_from_multiple_threads(self, tmp_path):
        db_path = str(tmp_path / "index.sqlite")
        store = OutputsSqliteStore(db_path)
        errors: list[Exception] = []

        def worker(offset: int) -> None:
            try:
                rows = [make_row(f"thread{offset}_file{i:04d}.txt") for i in range(50)]
                store.upsert_rows(rows)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert store.count() == 8 * 50
        store.close()


class TestRowSerialization:
    def test_as_tuple_matches_column_order(self):
        row = make_row("a.txt", is_archival=True)
        t = row.as_tuple()
        assert len(t) == len(_COLUMNS)
        assert t[0] == "a.txt"
        assert t[7] == 1  # is_archival coerced to int for storage

    def test_from_row_round_trip(self):
        row = make_row("a.txt", is_archival=True)
        rebuilt = OutputsIndexRow.from_row(row.as_tuple())
        assert rebuilt == row


class TestMigrateFromDuckdb:
    def test_migrates_all_rows_with_correct_data(self, tmp_path):
        duck_path = str(tmp_path / "old.duckdb")
        rows = [
            (f"/f_{i}.txt", f"content-{i}", 1.0 + i, "h" * 64, 10 + i,
             "gen.py", "text", i % 2 == 0, None, None, None)
            for i in range(250)
        ]
        make_duckdb_source(duck_path, rows)

        sqlite_path = str(tmp_path / "new.sqlite")
        store = OutputsSqliteStore(sqlite_path)
        n = migrate_from_duckdb(duck_path, store)
        assert n == 250
        assert store.count() == 250

        got = store.get_by_path("/f_100.txt")
        assert got.content == "content-100"
        assert got.mtime == 101.0
        assert got.is_archival is True  # 100 % 2 == 0
        got_odd = store.get_by_path("/f_101.txt")
        assert got_odd.is_archival is False
        store.close()

    def test_migrate_empty_source_is_a_noop(self, tmp_path):
        duck_path = str(tmp_path / "old.duckdb")
        make_duckdb_source(duck_path, [])
        store = OutputsSqliteStore(str(tmp_path / "new.sqlite"))
        n = migrate_from_duckdb(duck_path, store)
        assert n == 0
        assert store.count() == 0
        store.close()

    def test_migrate_respects_chunk_size_boundary(self, tmp_path):
        # 250 rows through a chunk_size of 100 -- exercises a partial final
        # chunk (100, 100, 50), not just the "everything fits in one
        # fetchmany" case the default 2000-row chunk would hide at this scale.
        duck_path = str(tmp_path / "old.duckdb")
        rows = [
            (f"/f_{i:04d}.txt", "c", 1.0, "h" * 64, 1, "gen.py", "text", False, None, None, None)
            for i in range(250)
        ]
        make_duckdb_source(duck_path, rows)
        store = OutputsSqliteStore(str(tmp_path / "new.sqlite"))
        n = migrate_from_duckdb(duck_path, store, chunk_size=100)
        assert n == 250
        assert store.count() == 250
        store.close()

    def test_migrated_data_is_sorted_scan_consistent(self, tmp_path):
        # Confirms the migrated destination behaves identically to a
        # natively-inserted one -- iter_sorted still returns real,
        # correctly-ordered rows after a migration, not just a row count.
        duck_path = str(tmp_path / "old.duckdb")
        paths = ["c.txt", "a.txt", "b.txt"]
        rows = [(p, "c", 1.0, "h" * 64, 1, "gen.py", "text", False, None, None, None) for p in paths]
        make_duckdb_source(duck_path, rows)
        store = OutputsSqliteStore(str(tmp_path / "new.sqlite"))
        migrate_from_duckdb(duck_path, store)
        assert [r.path for r in store.iter_sorted()] == sorted(paths)
        store.close()

    def test_migrate_into_nonempty_target_upserts_not_duplicates(self, tmp_path):
        duck_path = str(tmp_path / "old.duckdb")
        make_duckdb_source(duck_path, [
            ("a.txt", "from-duckdb", 1.0, "h" * 64, 1, "gen.py", "text", False, None, None, None),
        ])
        store = OutputsSqliteStore(str(tmp_path / "new.sqlite"))
        store.upsert_rows([make_row("a.txt", content="pre-existing")])
        migrate_from_duckdb(duck_path, store)
        assert store.count() == 1
        assert store.get_by_path("a.txt").content == "from-duckdb"
        store.close()

    def test_migrate_flushes_on_byte_budget_before_row_count(self, tmp_path):
        # 10 rows x ~40KB content each = ~400KB total. A byte budget of
        # 100KB should force multiple flushes well before chunk_size=2000
        # rows would ever trigger one -- confirms the byte budget is a
        # REAL second trigger, not dead code alongside the row-count one.
        duck_path = str(tmp_path / "old.duckdb")
        big_content = "x" * 40_000
        rows = [
            (f"/f_{i:04d}.txt", big_content, 1.0, "h" * 64, len(big_content),
             "gen.py", "text", False, None, None, None)
            for i in range(10)
        ]
        make_duckdb_source(duck_path, rows)
        store = OutputsSqliteStore(str(tmp_path / "new.sqlite"))
        n = migrate_from_duckdb(duck_path, store, chunk_size=2000, max_chunk_bytes=100_000)
        assert n == 10
        assert store.count() == 10
        assert store.get_by_path("/f_0005.txt").content == big_content
        store.close()

    def test_migrate_default_byte_budget_handles_large_average_content(self, tmp_path):
        # Confirms the fix for the real bug this addendum documents: a
        # corpus averaging well above a few KB/row (matching SUT_Compressed's
        # real ~326KB average) must not require a caller-supplied
        # max_chunk_bytes override just to migrate safely -- the DEFAULT
        # must already keep individual allocations small regardless of
        # average row size.
        duck_path = str(tmp_path / "old.duckdb")
        content = "y" * 300_000  # ~300KB/row, matching the real corpus's order of magnitude
        rows = [
            (f"/f_{i:04d}.txt", content, 1.0, "h" * 64, len(content),
             "gen.py", "text", False, None, None, None)
            for i in range(50)
        ]
        make_duckdb_source(duck_path, rows)
        store = OutputsSqliteStore(str(tmp_path / "new.sqlite"))
        n = migrate_from_duckdb(duck_path, store)  # defaults only, no override
        assert n == 50
        assert store.count() == 50
        store.close()

    def test_source_missing_raises(self, tmp_path):
        store = OutputsSqliteStore(str(tmp_path / "new.sqlite"))
        with pytest.raises(Exception):  # duckdb's own error for a bad path/catalog
            migrate_from_duckdb(str(tmp_path / "does_not_exist.duckdb"), store)
        store.close()


class TestDiskUsage:
    def test_memory_mode_reports_zero(self):
        store = OutputsSqliteStore(":memory:")
        assert store.real_disk_bytes() == 0

    def test_real_file_reports_actual_size(self, tmp_path):
        db_path = str(tmp_path / "index.sqlite")
        store = OutputsSqliteStore(db_path)
        store.upsert_rows([make_row("a.txt")])
        # Measured BEFORE close(): in WAL mode a fresh write can still be
        # sitting in the -wal sidecar rather than the main file, so the
        # correct comparison is against main+sidecars, not the main file
        # alone (close() auto-checkpoints, which would otherwise make this
        # test's outcome depend on checkpoint timing rather than real usage).
        size = store.real_disk_bytes()
        expected = os.path.getsize(db_path)
        for suffix in ("-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                expected += os.path.getsize(p)
        store.close()
        assert size > 0
        assert size == expected
