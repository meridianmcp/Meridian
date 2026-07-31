"""Tests for meridian_outputs.provenance_status (sprint item bd5b8d79).

Covers:
  - Exact hit: a recorded path returns the exact record with authoritative
    ("exact") status.
  - Missing exact record but indexed/known path: "unregistered", not a bare
    None/absence.
  - Directory-note fallback: a MERIDIAN_NOTES.md-covered path with no exact
    record surfaces the directory annotation under "directory_fallback",
    distinguishable in shape from an exact hit.
  - Unknown path: never discovered by the walker at all, distinguished from
    "unregistered" purely by provenance_type (identical None-shaped fields).
  - Relocated/changed paths: existence and content-hash mismatches are
    flagged as stale, not silently returned as fresh.
  - Restart persistence: both the exact-record tier and the
    indexed-but-unregistered tier survive a simulated process restart (cache
    eviction + reconnect to the same on-disk state), per this codebase's own
    "cache eviction == process restart" testing convention.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate as AN
from meridian_outputs import outputs_local as OL
from meridian_outputs import provenance_status as PS

try:
    import duckdb as _duckdb_probe  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


def _simulate_restart(outputs_dir: str) -> None:
    """Evict + close the cached OutputsFtsIndex for ``outputs_dir``, forcing
    the next lookup to open a BRAND NEW instance bound to the same on-disk
    db_path -- this codebase's own established process-restart simulation
    (see TestCachedIndexPersistence.test_index_survives_cache_eviction in
    test_outputs_local.py). annotate.py's ledger has no in-memory cache at
    all (it re-reads the JSON file on every call), so only the
    OutputsFtsIndex side needs explicit eviction here.
    """
    key = OL._cache_key(outputs_dir)
    with OL._index_cache_lock:
        idx = OL._index_cache.pop(key, None)
    if idx is not None:
        idx.close()


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

class TestArgumentValidation:
    def test_missing_outputs_dir(self) -> None:
        result = PS.get_provenance_status("", "/some/file.csv")
        assert "error" in result

    def test_missing_path(self, tmp_path: Path) -> None:
        result = PS.get_provenance_status(str(tmp_path), "")
        assert "error" in result


# ---------------------------------------------------------------------------
# Exact hit
# ---------------------------------------------------------------------------

class TestExactHit:
    @duckdb_required
    def test_exact_record_reports_authoritative_status(self, tmp_path: Path) -> None:
        f = tmp_path / "results.csv"
        f.write_text("epoch,accuracy\n1,0.9\n", encoding="utf-8")
        recorded = AN.record_provenance(
            str(tmp_path), str(f), generating_script="train.py",
            params={"lr": 0.01},
        )
        assert "error" not in recorded

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["provenance_type"] == PS.EXACT
        assert status["record"]["path"] == str(f)
        assert status["record"]["generating_script"] == "train.py"
        assert status["directory_note"] is None
        assert status["staleness"]["exists_on_disk"] is True
        assert status["staleness"]["stale"] is False


# ---------------------------------------------------------------------------
# Missing exact record, but indexed/known
# ---------------------------------------------------------------------------

class TestUnregistered:
    @duckdb_required
    def test_indexed_path_without_provenance_is_unregistered_not_none(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "orphaned_output.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        OL.register_priority_path(str(tmp_path), str(f))
        assert AN.get_provenance(str(tmp_path), str(f)) is None  # sanity

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["provenance_type"] == PS.UNREGISTERED
        assert status["record"] is None
        assert status["directory_note"] is None
        assert status["staleness"] is None

    def test_never_discovered_path_is_unknown(self, tmp_path: Path) -> None:
        f = tmp_path / "totally_untouched.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["provenance_type"] == PS.UNKNOWN
        assert status["record"] is None
        assert status["directory_note"] is None
        assert status["staleness"] is None

    @duckdb_required
    def test_unregistered_and_unknown_share_shape_differ_only_by_type(
        self, tmp_path: Path,
    ) -> None:
        indexed = tmp_path / "indexed.csv"
        indexed.write_text("a,b\n1,2\n", encoding="utf-8")
        OL.register_priority_path(str(tmp_path), str(indexed))
        never = tmp_path / "never.csv"
        never.write_text("a,b\n3,4\n", encoding="utf-8")

        s_indexed = PS.get_provenance_status(str(tmp_path), str(indexed))
        s_never = PS.get_provenance_status(str(tmp_path), str(never))
        assert s_indexed["provenance_type"] != s_never["provenance_type"]
        for key in ("record", "directory_note", "staleness"):
            assert s_indexed[key] is None
            assert s_never[key] is None


# ---------------------------------------------------------------------------
# Directory-note fallback
# ---------------------------------------------------------------------------

class TestDirectoryFallback:
    @duckdb_required
    def test_meridian_notes_covered_path_surfaces_as_directory_fallback(
        self, tmp_path: Path,
    ) -> None:
        sub = tmp_path / "run_17"
        sub.mkdir()
        (sub / OL.MERIDIAN_NOTES_FILENAME).write_text(
            "This run: PCA on, BFS off, overwritten 5x.", encoding="utf-8",
        )
        target = sub / "width_hist.csv"
        target.write_text("bin,count\n1,5\n2,9\n", encoding="utf-8")

        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()
        assert AN.get_provenance(str(tmp_path), str(target)) is None  # sanity

        status = PS.get_provenance_status(str(tmp_path), str(target))
        assert status["provenance_type"] == PS.DIRECTORY_FALLBACK
        assert status["record"] is None
        assert status["directory_note"] is not None
        assert status["directory_note"]["source"] == OL.MERIDIAN_NOTES_FILENAME
        assert "PCA on" in status["directory_note"]["note"]
        assert status["staleness"] is None

    @duckdb_required
    def test_directory_fallback_distinguishable_from_exact(
        self, tmp_path: Path,
    ) -> None:
        sub = tmp_path / "run_18"
        sub.mkdir()
        (sub / OL.MERIDIAN_NOTES_FILENAME).write_text("dir note", encoding="utf-8")
        fallback_target = sub / "no_exact_record.csv"
        fallback_target.write_text("a,b\n1,2\n", encoding="utf-8")
        exact_target = sub / "has_exact_record.csv"
        exact_target.write_text("a,b\n3,4\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(exact_target))

        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()

        fallback_status = PS.get_provenance_status(str(tmp_path), str(fallback_target))
        exact_status = PS.get_provenance_status(str(tmp_path), str(exact_target))

        assert fallback_status["provenance_type"] == PS.DIRECTORY_FALLBACK
        assert exact_status["provenance_type"] == PS.EXACT
        assert fallback_status["provenance_type"] != exact_status["provenance_type"]
        assert fallback_status["record"] is None
        assert exact_status["record"] is not None
        assert fallback_status["directory_note"] is not None
        assert exact_status["directory_note"] is None


# ---------------------------------------------------------------------------
# Relocated / stale paths
# ---------------------------------------------------------------------------

class TestStaleness:
    @duckdb_required
    def test_deleted_path_flagged_stale(self, tmp_path: Path) -> None:
        f = tmp_path / "will_be_deleted.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))
        f.unlink()

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["provenance_type"] == PS.EXACT
        assert status["staleness"]["exists_on_disk"] is False
        assert status["staleness"]["stale"] is True
        assert "relocated or deleted" in status["staleness"]["reason"]

    @duckdb_required
    def test_content_changed_since_recording_flagged_stale(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "will_be_overwritten.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        recorded = AN.record_provenance(str(tmp_path), str(f))
        assert recorded["content_hash"] is not None

        f.write_text("a,b\n999,999\n", encoding="utf-8")

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["staleness"]["exists_on_disk"] is True
        assert status["staleness"]["stale"] is True
        assert status["staleness"]["current_content_hash"] != (
            status["staleness"]["recorded_content_hash"]
        )

    @duckdb_required
    def test_unchanged_content_not_flagged_stale(self, tmp_path: Path) -> None:
        f = tmp_path / "untouched_after_recording.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["staleness"]["stale"] is False
        assert status["staleness"]["current_content_hash"] == (
            status["staleness"]["recorded_content_hash"]
        )


# ---------------------------------------------------------------------------
# Restart persistence
# ---------------------------------------------------------------------------

class TestRestartPersistence:
    @duckdb_required
    def test_exact_record_survives_restart(self, tmp_path: Path) -> None:
        f = tmp_path / "durable_output.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f), generating_script="run.py")

        before = PS.get_provenance_status(str(tmp_path), str(f))
        assert before["provenance_type"] == PS.EXACT

        _simulate_restart(str(tmp_path))

        after = PS.get_provenance_status(str(tmp_path), str(f))
        assert after["provenance_type"] == PS.EXACT
        assert after["record"]["generating_script"] == "run.py"
        assert after["staleness"]["stale"] is False

    @duckdb_required
    def test_unregistered_status_survives_restart(self, tmp_path: Path) -> None:
        """The 'known but unregistered' distinction depends on
        outputs_local's persisted index (not annotate's ledger, which never
        saw this path at all) -- this must hold after a REAL reconnect to
        the on-disk DuckDB file, not just within one warm in-memory cache
        (interacts with item 6b5ecdc5's walk-state persistence)."""
        f = tmp_path / "indexed_but_never_recorded.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        OL.register_priority_path(str(tmp_path), str(f))

        before = PS.get_provenance_status(str(tmp_path), str(f))
        assert before["provenance_type"] == PS.UNREGISTERED

        _simulate_restart(str(tmp_path))

        after = PS.get_provenance_status(str(tmp_path), str(f))
        assert after["provenance_type"] == PS.UNREGISTERED
