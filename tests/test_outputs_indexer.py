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
