"""Tests for OutputsFtsIndex's DuckDB block size (new index files only).

DuckDB's own default block size is 262144 bytes. OutputsFtsIndex creates a
new index file at DUCKDB_NEW_FILE_BLOCK_SIZE (16384) unless the caller passes
``duckdb_block_size`` explicitly. Covered here:
  - a new file gets the default, or the explicitly requested supported size;
  - an unsupported size raises ValueError before anything is created;
  - an existing file keeps its block size when reopened with a different
    setting (DuckDB records the size in the file header);
  - the read connection and the write connection agree on the size of a file
    whichever of them creates it, including when both are open at once;
  - search results for the same corpus are identical at 16384 and 262144.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meridian_outputs import outputs_local as OL

duckdb = pytest.importorskip("duckdb")


def _block_size_via(con: Any) -> int:
    """Read the block size DuckDB reports for an open connection."""
    cur = con.execute("PRAGMA database_size")
    cols = [d[0] for d in cur.description]
    row = dict(zip(cols, cur.fetchone()))
    return int(row["block_size"])


def _block_size_on_disk(db_path: str) -> int:
    """Open the file on its own and read the block size from its header."""
    con = duckdb.connect(db_path)
    try:
        return _block_size_via(con)
    finally:
        con.close()


def _make_outputs_dir(tmp_path: Path) -> Path:
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    return outputs_dir


class TestNewFileBlockSize:
    def test_default_constant_is_16384(self) -> None:
        assert OL.DUCKDB_NEW_FILE_BLOCK_SIZE == 16384

    def test_new_file_created_with_default_has_block_size_16384(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "index.duckdb"
        idx = OL.OutputsFtsIndex(str(_make_outputs_dir(tmp_path)), db_path=str(db_path))
        try:
            idx._connect()
        finally:
            idx.close()
        assert _block_size_on_disk(str(db_path)) == 16384

    def test_none_argument_means_the_default(self, tmp_path: Path) -> None:
        db_path = tmp_path / "index.duckdb"
        idx = OL.OutputsFtsIndex(
            str(_make_outputs_dir(tmp_path)), db_path=str(db_path),
            duckdb_block_size=None,
        )
        try:
            idx._connect()
        finally:
            idx.close()
        assert _block_size_on_disk(str(db_path)) == 16384

    def test_new_file_created_with_262144_has_block_size_262144(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "index.duckdb"
        idx = OL.OutputsFtsIndex(
            str(_make_outputs_dir(tmp_path)), db_path=str(db_path),
            duckdb_block_size=262144,
        )
        try:
            idx._connect()
        finally:
            idx.close()
        assert _block_size_on_disk(str(db_path)) == 262144

    def test_read_connection_creating_file_agrees_with_write_connection(
        self, tmp_path: Path,
    ) -> None:
        """Read-first creation: _read_connect() creates the file, then the
        write connection opens the same file while the read connection is
        still open. Both must succeed and report the same block size."""
        db_path = tmp_path / "index.duckdb"
        idx = OL.OutputsFtsIndex(str(_make_outputs_dir(tmp_path)), db_path=str(db_path))
        try:
            read_con = idx._read_connect()
            write_con = idx._connect()
            assert read_con is not write_con
            assert _block_size_via(read_con) == 16384
            assert _block_size_via(write_con) == 16384
        finally:
            idx.close()
        assert _block_size_on_disk(str(db_path)) == 16384

    def test_read_connection_creating_file_honours_explicit_size(
        self, tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "index.duckdb"
        idx = OL.OutputsFtsIndex(
            str(_make_outputs_dir(tmp_path)), db_path=str(db_path),
            duckdb_block_size=262144,
        )
        try:
            read_con = idx._read_connect()
            write_con = idx._connect()
            assert _block_size_via(read_con) == 262144
            assert _block_size_via(write_con) == 262144
        finally:
            idx.close()
        assert _block_size_on_disk(str(db_path)) == 262144


class TestBlockSizeValidation:
    @pytest.mark.parametrize(
        "bad", [0, -16384, 4096, 8192, 131071, 524288, 16384.0, "16384", True],
    )
    def test_unsupported_block_size_raises_value_error(
        self, tmp_path: Path, bad: Any,
    ) -> None:
        db_path = tmp_path / "index.duckdb"
        with pytest.raises(ValueError, match="duckdb_block_size"):
            OL.OutputsFtsIndex(
                str(_make_outputs_dir(tmp_path)), db_path=str(db_path),
                duckdb_block_size=bad,
            )
        assert not db_path.exists(), "a rejected size must not create the file"

    @pytest.mark.parametrize(
        "size", [16384, 32768, 65536, 131072, 262144],
    )
    def test_every_supported_block_size_is_accepted(
        self, tmp_path: Path, size: int,
    ) -> None:
        db_path = tmp_path / "index.duckdb"
        idx = OL.OutputsFtsIndex(
            str(_make_outputs_dir(tmp_path)), db_path=str(db_path),
            duckdb_block_size=size,
        )
        try:
            assert idx._duckdb_block_size == size
        finally:
            idx.close()


class TestExistingFileKeepsBlockSize:
    @pytest.mark.parametrize(
        ("created_with", "reopened_with", "expected"),
        [
            (16384, 262144, 16384),
            (262144, None, 262144),
            (262144, 16384, 262144),
        ],
    )
    def test_existing_file_keeps_its_block_size(
        self, tmp_path: Path, created_with: int, reopened_with: int | None,
        expected: int,
    ) -> None:
        outputs_dir = _make_outputs_dir(tmp_path)
        db_path = tmp_path / "index.duckdb"
        first = OL.OutputsFtsIndex(
            str(outputs_dir), db_path=str(db_path), duckdb_block_size=created_with,
        )
        try:
            first._connect()
        finally:
            first.close()
        assert _block_size_on_disk(str(db_path)) == created_with

        second = OL.OutputsFtsIndex(
            str(outputs_dir), db_path=str(db_path), duckdb_block_size=reopened_with,
        )
        try:
            assert _block_size_via(second._connect()) == expected
            assert _block_size_via(second._read_connect()) == expected
        finally:
            second.close()
        assert _block_size_on_disk(str(db_path)) == expected


CORPUS = {
    "alpha.txt": "the quick brown fox jumps over the lazy dog",
    "beta.txt": "pack my box with five dozen liquor jugs",
    "gamma.csv": "name,score\nmeridian,42\nindexer,7\n",
    "delta.md": "block size sixteen kilobytes for new files\n",
    "epsilon.txt": "quarterly revenue grew while costs stayed flat",
    "zeta.txt": "fox and hound and fox again, fox everywhere",
    "eta.json": '{"metric": "latency", "p95_ms": 120}',
    "theta.txt": "lazy afternoon dog naps by the river",
}

QUERIES = ["fox", "dog", "revenue", "block size", "latency", "meridian"]


class TestSearchEquivalenceAcrossBlockSizes:
    def test_same_corpus_gives_same_ordered_paths_at_16k_and_256k(
        self, tmp_path: Path,
    ) -> None:
        outputs_dir = _make_outputs_dir(tmp_path)
        for name, text in CORPUS.items():
            (outputs_dir / name).write_text(text, encoding="utf-8")

        small_db = tmp_path / "small" / "index.duckdb"
        large_db = tmp_path / "large" / "index.duckdb"
        small_db.parent.mkdir()
        large_db.parent.mkdir()

        small = OL.OutputsFtsIndex(
            str(outputs_dir), db_path=str(small_db), duckdb_block_size=16384,
        )
        large = OL.OutputsFtsIndex(
            str(outputs_dir), db_path=str(large_db), duckdb_block_size=262144,
        )
        try:
            assert small.rebuild() > 0
            assert large.rebuild() > 0

            for query in QUERIES:
                small_paths = [h["path"] for h in small.search(query, limit=10)]
                large_paths = [h["path"] for h in large.search(query, limit=10)]
                assert small_paths, f"query {query!r} must hit the corpus"
                assert small_paths == large_paths, (
                    f"query {query!r}: 16384 gave {small_paths}, "
                    f"262144 gave {large_paths}"
                )
        finally:
            small.close()
            large.close()

        # Guard: the two indexes really are different block sizes, so the
        # equality above compares two genuinely distinct on-disk layouts.
        assert _block_size_on_disk(str(small_db)) == 16384
        assert _block_size_on_disk(str(large_db)) == 262144
