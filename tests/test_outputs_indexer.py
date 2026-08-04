"""Coverage for the recursive outputs indexer (06df6ab3 Part 2).

Exercises:

* index_csv / index_json — the unified DuckDB glob index across a directory
  tree (real DuckDB, real files in tmp_path; filename column traces rows back
  to source), plus the injectable sql_runner seam and the guarded-failure path.
* npy_metadata — shape/dtype/size/mtime via a real numpy .npy file (mmap'd, no
  full-array content), plus a corrupt-file error path.
* OutputsIndexer.handle_event — npy_index maintenance on created/modified/
  deleted, driven directly (no real filesystem-event delivery needed).
* OutputsIndexer.start()/stop() — ONE real end-to-end watchdog Observer test
  (native OS filesystem events, polled with a bounded timeout).
"""
from __future__ import annotations

import json
import os
import time

import pytest

from meridian import outputs_indexer as oi


# ---------------------------------------------------------------------------
# index_csv / index_json — real DuckDB over tmp_path
# ---------------------------------------------------------------------------

def test_index_csv_unified_glob_across_nested_directories(tmp_path):
    nested = tmp_path / "run1"
    nested.mkdir()
    (tmp_path / "a.csv").write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    (nested / "b.csv").write_text("x,y\n5,6\n", encoding="utf-8")

    rows = oi.index_csv(str(tmp_path))
    assert len(rows) == 3
    assert all("filename" in r for r in rows)
    files = {os.path.basename(r["filename"]) for r in rows}
    assert files == {"a.csv", "b.csv"}
    values = sorted((r["x"], r["y"]) for r in rows)
    assert values == [(1, 2), (3, 4), (5, 6)]


def test_index_json_unified_glob_across_nested_directories(tmp_path):
    nested = tmp_path / "run1"
    nested.mkdir()
    (tmp_path / "a.json").write_text(json.dumps({"x": 1, "y": 2}), encoding="utf-8")
    (nested / "b.json").write_text(json.dumps({"x": 3, "y": 4}), encoding="utf-8")

    rows = oi.index_json(str(tmp_path))
    assert len(rows) == 2
    assert all("filename" in r for r in rows)
    values = sorted((r["x"], r["y"]) for r in rows)
    assert values == [(1, 2), (3, 4)]


def test_index_csv_empty_directory_returns_empty_list(tmp_path):
    assert oi.index_csv(str(tmp_path)) == []
    assert oi.index_json(str(tmp_path)) == []


def test_index_csv_uses_injected_sql_runner_seam(tmp_path):
    calls = []

    class _FakeRelation:
        columns = ["a", "b"]

        def fetchall(self):
            return [(1, 2), (3, 4)]

    def _runner(sql):
        calls.append(sql)
        return _FakeRelation()

    rows = oi.index_csv(str(tmp_path), sql_runner=_runner)
    assert rows == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    assert len(calls) == 1
    assert "read_csv" in calls[0]
    assert "filename=true" in calls[0]


def test_index_csv_guards_against_query_failure(tmp_path):
    def _boom(_sql):
        raise RuntimeError("duckdb exploded")

    assert oi.index_csv(str(tmp_path), sql_runner=_boom) == []
    assert oi.index_json(str(tmp_path), sql_runner=_boom) == []


def test_glob_pattern_uses_forward_slashes(tmp_path):
    pattern = oi._glob_pattern(str(tmp_path), "csv")
    assert "\\" not in pattern
    assert pattern.endswith("/**/*.csv")


# ---------------------------------------------------------------------------
# npy_metadata — shape/dtype/size/mtime only, never full-array content
# ---------------------------------------------------------------------------

def test_npy_metadata_reports_shape_dtype_size_and_mtime(tmp_path):
    numpy = pytest.importorskip("numpy")
    path = tmp_path / "arr.npy"
    numpy.save(str(path), numpy.zeros((3, 4), dtype="float32"))

    meta = oi.npy_metadata(str(path))
    assert meta.shape == (3, 4)
    assert meta.dtype == "float32"
    assert meta.size_bytes == os.path.getsize(str(path))
    assert meta.modified_at == pytest.approx(os.path.getmtime(str(path)))
    assert meta.error is None

    d = meta.to_dict()
    assert d["shape"] == (3, 4)
    assert d["dtype"] == "float32"


def test_npy_metadata_corrupt_file_still_reports_size_and_error(tmp_path):
    path = tmp_path / "bad.npy"
    path.write_bytes(b"not a real npy file")

    meta = oi.npy_metadata(str(path))
    assert meta.shape is None
    assert meta.dtype is None
    assert meta.size_bytes == len(b"not a real npy file")
    assert meta.modified_at is not None
    assert meta.error is not None


def test_npy_metadata_missing_file_returns_all_none_with_error(tmp_path):
    meta = oi.npy_metadata(str(tmp_path / "missing.npy"))
    assert meta.shape is None
    assert meta.dtype is None
    assert meta.size_bytes is None
    assert meta.modified_at is None
    assert meta.error is not None


# ---------------------------------------------------------------------------
# OutputsIndexer.handle_event — npy index maintenance (no real fs events)
# ---------------------------------------------------------------------------

def test_handle_event_maintains_npy_index_on_created_modified_deleted(tmp_path):
    numpy = pytest.importorskip("numpy")
    idx = oi.OutputsIndexer(str(tmp_path))
    path = str(tmp_path / "arr.npy")
    numpy.save(path, numpy.ones((2, 2)))

    idx.handle_event("created", path)
    assert path in idx.npy_index
    assert idx.npy_index[path].shape == (2, 2)

    numpy.save(path, numpy.ones((5, 5)))
    idx.handle_event("modified", path)
    assert idx.npy_index[path].shape == (5, 5)

    idx.handle_event("deleted", path)
    assert path not in idx.npy_index

    assert idx.events == [("created", path), ("modified", path), ("deleted", path)]


def test_handle_event_ignores_non_npy_files_for_the_metadata_index(tmp_path):
    idx = oi.OutputsIndexer(str(tmp_path))
    csv_path = str(tmp_path / "a.csv")
    idx.handle_event("created", csv_path)
    assert idx.npy_index == {}
    assert idx.events == [("created", csv_path)]


def test_refresh_npy_index_walks_existing_files(tmp_path):
    numpy = pytest.importorskip("numpy")
    nested = tmp_path / "nested"
    nested.mkdir()
    numpy.save(str(tmp_path / "a.npy"), numpy.zeros(3))
    numpy.save(str(nested / "b.npy"), numpy.zeros(4))
    (tmp_path / "ignored.csv").write_text("x\n1\n", encoding="utf-8")

    idx = oi.OutputsIndexer(str(tmp_path))
    idx.refresh_npy_index()
    assert set(idx.npy_index.keys()) == {
        str(tmp_path / "a.npy"), str(nested / "b.npy"),
    }
    assert idx.npy_index[str(tmp_path / "a.npy")].shape == (3,)
    assert idx.npy_index[str(nested / "b.npy")].shape == (4,)


# ---------------------------------------------------------------------------
# OutputsIndexer.start()/stop() — one real end-to-end watchdog test
# ---------------------------------------------------------------------------

def test_outputs_indexer_start_stop_delivers_real_filesystem_events(tmp_path):
    pytest.importorskip("watchdog")
    idx = oi.OutputsIndexer(str(tmp_path))
    idx.start()
    try:
        assert idx.running is True
        p = os.path.join(str(tmp_path), "x.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("a,b\n1,2\n")

        deadline = time.time() + 10
        while time.time() < deadline and not idx.events:
            time.sleep(0.1)
        assert idx.events, "expected at least one watchdog event to be delivered"
        assert idx.events[0][1] == p
    finally:
        idx.stop()
    assert idx.running is False

    # start() is idempotent — calling it again after stop() works cleanly.
    idx.start()
    try:
        assert idx.running is True
    finally:
        idx.stop()


def test_outputs_indexer_start_creates_missing_outputs_dir(tmp_path):
    pytest.importorskip("watchdog")
    target = tmp_path / "does_not_exist_yet"
    idx = oi.OutputsIndexer(str(target))
    idx.start()
    try:
        assert target.is_dir()
    finally:
        idx.stop()


# ---------------------------------------------------------------------------
# OutputsFtsIndex / search_outputs — large-tree convergence authoritative
# (5b897ad3): a cold/large tree can legitimately take several rebuild() calls
# to finish. Before this fix, `search_outputs()` here only ever exposed a
# bare `partial` boolean (no pending count, no error surfaced, no loud
# zero-hit warning) — the exact "is this genuinely empty, or just not
# finished yet" ambiguity already fixed for the sibling
# extensions/meridian-outputs package (see its ConvergenceState /
# zero_hits_warning contract). These tests use small synthetic tmp_path
# trees with an artificially SLOWED per-file analysis step (monkeypatching
# oi.file_fingerprint to sleep) — never a real/large on-disk tree, and every
# OutputsFtsIndex here uses the class default `:memory:` DuckDB connection,
# so nothing is ever persisted to (or rebuilt from) any real cache on disk.
# ---------------------------------------------------------------------------

try:
    import duckdb  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


class TestOutputsFtsIndexConvergence:
    @staticmethod
    def _install_slow_fingerprint(monkeypatch, delay: float) -> None:
        real_fp = oi.file_fingerprint

        def slow_fp(path):
            time.sleep(delay)
            return real_fp(path)

        monkeypatch.setattr(oi, "file_fingerprint", slow_fp)

    @duckdb_required
    def test_healthy_rebuild_leaves_pending_count_at_zero(self, tmp_path):
        (tmp_path / "a.csv").write_text("col\n1\n", encoding="utf-8")
        idx = oi.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx.last_rebuild_partial is False
            assert idx.last_pending_count == 0
            assert idx.last_db_write_error is None
        finally:
            idx.close()

    @duckdb_required
    def test_partial_rebuild_reports_real_pending_backlog(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A tight budget that cuts the stale-file loop short must leave a
        REAL, non-zero pending_count — not just the bare partial=True flag —
        so a caller can tell how much work is still queued."""
        n = 20
        for i in range(n):
            (tmp_path / f"r{i:03d}.csv").write_text(
                f"col\nvalue={i}\n", encoding="utf-8",
            )
        self._install_slow_fingerprint(monkeypatch, 0.02)

        idx = oi.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild(max_seconds=0.05)
            assert idx.last_rebuild_partial is True
            assert idx.last_pending_count > 0
            assert idx.last_pending_count < n, (
                "test setup didn't actually leave a partial backlog -- "
                f"pending={idx.last_pending_count}"
            )

            # Repeated calls with a generous budget must converge: every
            # stale file eventually processed, backlog drained to zero.
            for _ in range(20):
                idx.rebuild(max_seconds=2.0)
                if idx.last_pending_count == 0 and not idx.last_rebuild_partial:
                    break
            assert idx.last_rebuild_partial is False
            assert idx.last_pending_count == 0
            assert len(idx._row_cache) == n
        finally:
            idx.close()

    @duckdb_required
    def test_search_outputs_surfaces_pending_count_and_zero_hits_warning(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The module-level search_outputs() (the real MCP-tool-facing entry
        point) must surface the same signals, and a zero-hit result on an
        unfinished index must carry zero_hits_warning -- never look identical
        to a confirmed miss."""
        n = 15
        for i in range(n):
            (tmp_path / f"s{i:03d}.csv").write_text(
                f"col\nvalue={i}\n", encoding="utf-8",
            )
        self._install_slow_fingerprint(monkeypatch, 0.02)
        key = oi._cache_key(str(tmp_path))
        try:
            result = oi.search_outputs(
                str(tmp_path), "no-such-term-zzz", max_seconds=0.05,
            )
            assert result["partial"] is True
            assert result["pending_count"] > 0
            assert result["hits"] == []
            assert "zero_hits_warning" in result, (
                "a zero-hit result on an unfinished index must be loudly "
                "flagged as non-authoritative -- got "
                f"{result}"
            )
        finally:
            with oi._index_cache_lock:
                evicted = oi._index_cache.pop(key, None)
            if evicted is not None:
                evicted.close()

    @duckdb_required
    def test_search_outputs_no_warning_once_fully_converged(
        self, tmp_path,
    ) -> None:
        """Once the index is genuinely fully converged (no slow-down, no
        budget pressure), a real zero-hit search must NOT carry
        zero_hits_warning/partial/pending_count -- those fields exist to flag
        an INCOMPLETE index, not to editorialize a confirmed miss."""
        (tmp_path / "t.csv").write_text("col\n1\n", encoding="utf-8")
        key = oi._cache_key(str(tmp_path))
        try:
            result = oi.search_outputs(str(tmp_path), "no-such-term-zzz")
            assert result["hits"] == []
            assert "partial" not in result
            assert "pending_count" not in result
            assert "zero_hits_warning" not in result
        finally:
            with oi._index_cache_lock:
                evicted = oi._index_cache.pop(key, None)
            if evicted is not None:
                evicted.close()

    @duckdb_required
    def test_rebuild_surfaces_db_write_error_instead_of_silent_debug_log(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Before this fix, a Phase-2 DB-write failure was swallowed at DEBUG
        level only, while total_indexed/row_cache kept looking like a
        healthy, growing index -- a real persistence failure was
        indistinguishable from success. last_db_write_error must now surface
        it, and clear on a subsequent successful call."""
        (tmp_path / "a.csv").write_text("term_one,1\n", encoding="utf-8")

        idx = oi.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx.last_db_write_error is None

            def _boom(self, con):  # noqa: ANN001 -- matches _ensure_schema's signature
                raise RuntimeError("simulated disk-full / connection failure")

            monkeypatch.setattr(oi.OutputsFtsIndex, "_ensure_schema", _boom)
            total_indexed = idx.rebuild()

            # The misleading part of the original bug: the in-memory count
            # still looks like a healthy, progressing index...
            assert total_indexed >= 1
            assert len(idx._row_cache) >= 1
            # ...but the write genuinely failed, and that must now be visible.
            assert idx.last_db_write_error is not None
            assert "simulated disk-full" in idx.last_db_write_error

            # A subsequent successful call clears the error (per-call
            # semantics -- last_db_write_error reflects only the most recent
            # rebuild() call).
            monkeypatch.undo()
            idx.rebuild()
            assert idx.last_db_write_error is None
        finally:
            idx.close()

    @duckdb_required
    def test_search_outputs_surfaces_db_write_error_in_result_dict(
        self, tmp_path, monkeypatch,
    ) -> None:
        (tmp_path / "b.csv").write_text("term_two,1\n", encoding="utf-8")

        def _boom(self, con):  # noqa: ANN001
            raise RuntimeError("simulated write failure")

        monkeypatch.setattr(oi.OutputsFtsIndex, "_ensure_schema", _boom)
        key = oi._cache_key(str(tmp_path))
        try:
            result = oi.search_outputs(str(tmp_path), "term_two")
            assert result["total_indexed"] >= 1, (
                "the in-memory count still looks like a healthy index -- "
                "this is exactly the deceptive state db_write_error exists "
                "to make visible"
            )
            assert result.get("db_write_error") is not None
            assert "simulated write failure" in result["db_write_error"]
        finally:
            with oi._index_cache_lock:
                evicted = oi._index_cache.pop(key, None)
            if evicted is not None:
                evicted.close()
