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

Sprint item d3374b0e additionally covers (hash- and convergence-awareness):
  - stale_by_script: an exact record whose independently fingerprint-tagged
    generating script has since changed content is promoted to
    "stale_by_script", distinct from generic output-content staleness.
  - archival identity: is_archival/canonical_path/sha256 surfaced from the
    indexed row, on branches that never previously carried it.
  - inconclusive: an unconverged index answering "unknown" is flagged
    inconclusive; a converged one confirming genuine absence is not; the
    "unregistered" branch is never inconclusive regardless of convergence.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate as AN
from meridian_outputs import fingerprint as FP
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


# ---------------------------------------------------------------------------
# stale_by_script (item d3374b0e)
# ---------------------------------------------------------------------------

class TestStaleByScript:
    @duckdb_required
    def test_script_changed_since_tagging_promotes_to_stale_by_script(
        self, tmp_path: Path,
    ) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('v1 -- has a bug')\n", encoding="utf-8")
        output = tmp_path / "predictions.csv"
        output.write_text("id,pred\n1,0.5\n", encoding="utf-8")

        recorded = AN.record_provenance(
            str(tmp_path), str(output), generating_script=str(script),
        )
        assert "error" not in recorded
        FP.tag_output(str(output), str(tmp_path), script_path=str(script))

        # The bug is fixed -- script content changes, output is untouched.
        script.write_text("print('v2 -- bug fixed')\n", encoding="utf-8")

        status = PS.get_provenance_status(str(tmp_path), str(output))
        assert status["provenance_type"] == PS.STALE_BY_SCRIPT
        assert status["script_staleness"]["is_stale"] is True
        assert "script content changed" in status["script_staleness"]["reason"]
        # The output file itself is unchanged -- generic staleness stays False.
        assert status["staleness"]["stale"] is False
        # record/directory_note keep the same shape as a plain EXACT hit.
        assert status["record"] is not None
        assert status["directory_note"] is None
        assert status["inconclusive"] is False

    @duckdb_required
    def test_unchanged_script_stays_exact(self, tmp_path: Path) -> None:
        script = tmp_path / "stable.py"
        script.write_text("print('unchanged')\n", encoding="utf-8")
        output = tmp_path / "stable_output.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")

        AN.record_provenance(str(tmp_path), str(output), generating_script=str(script))
        FP.tag_output(str(output), str(tmp_path), script_path=str(script))

        status = PS.get_provenance_status(str(tmp_path), str(output))
        assert status["provenance_type"] == PS.EXACT
        assert status["script_staleness"]["is_stale"] is False

    @duckdb_required
    def test_never_fingerprint_tagged_has_no_script_staleness(
        self, tmp_path: Path,
    ) -> None:
        """An exact record that was never ALSO fingerprint-tagged must not
        be promoted -- script_staleness is None (nothing to compare), not
        falsely "not stale"."""
        output = tmp_path / "untagged.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(output))

        status = PS.get_provenance_status(str(tmp_path), str(output))
        assert status["provenance_type"] == PS.EXACT
        assert status["script_staleness"] is None

    @duckdb_required
    def test_output_content_mismatch_alone_does_not_trigger_stale_by_script(
        self, tmp_path: Path,
    ) -> None:
        """A generic output-content mismatch (the file itself changed) is a
        DIFFERENT signal (staleness.stale) from stale_by_script (the
        generating SCRIPT changed) -- they must not be conflated."""
        output = tmp_path / "overwritten.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(output))

        output.write_text("a,b\n999,999\n", encoding="utf-8")

        status = PS.get_provenance_status(str(tmp_path), str(output))
        assert status["provenance_type"] == PS.EXACT  # not stale_by_script
        assert status["staleness"]["stale"] is True
        assert status["script_staleness"] is None  # never fingerprint-tagged


# ---------------------------------------------------------------------------
# Canonical/archival identity (item d3374b0e)
# ---------------------------------------------------------------------------

class TestArchivalIdentity:
    @duckdb_required
    def test_archival_copy_surfaces_identity_even_when_unregistered(
        self, tmp_path: Path,
    ) -> None:
        canonical = tmp_path / "results.csv"
        canonical.write_text("a,b\n1,2\n", encoding="utf-8")
        archival = tmp_path / "results_old.csv"
        archival.write_text("a,b\n1,2\n", encoding="utf-8")

        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()

        status = PS.get_provenance_status(str(tmp_path), str(archival))
        assert status["provenance_type"] == PS.UNREGISTERED  # no exact record
        assert status["archival"] is not None
        assert status["archival"]["is_archival"] is True
        assert status["archival"]["canonical_path"] == str(canonical)

    @duckdb_required
    def test_never_indexed_path_has_no_archival_identity(
        self, tmp_path: Path,
    ) -> None:
        never = tmp_path / "never_old.csv"
        status = PS.get_provenance_status(str(tmp_path), str(never))
        assert status["archival"] is None


# ---------------------------------------------------------------------------
# inconclusive / convergence (item d3374b0e)
# ---------------------------------------------------------------------------

class TestConvergenceAwareness:
    @duckdb_required
    def test_unconverged_index_makes_unknown_inconclusive(
        self, tmp_path: Path,
    ) -> None:
        """A rebuild() call bounded to an immediately-expired deadline, over
        enough files that one drain() can't finish the walk in a single
        pass, leaves the walk genuinely IN PROGRESS (not converged) -- an
        "unknown" answer read against that state must say so, never be
        read as confirmed absence. (A brand-new, NEVER-rebuilt index is
        trivially "converged" by this system's own design -- no walk has
        started, so none can be "in progress" -- so that scenario alone
        does not exercise this path; see test_converged_index_makes_
        unknown_confident for that baseline instead.)
        """
        for i in range(25):
            (tmp_path / f"file_{i:03d}.csv").write_text(f"{i}\n", encoding="utf-8")
        never = tmp_path / "genuinely_absent.csv"  # never created on disk

        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild(max_seconds=0)
        assert idx.get_convergence_state().converged is False  # sanity

        status = PS.get_provenance_status(str(tmp_path), str(never))
        assert status["provenance_type"] == PS.UNKNOWN
        assert status["convergence"] is not None
        assert status["convergence"]["converged"] is False
        assert status["inconclusive"] is True

    @duckdb_required
    def test_converged_index_makes_unknown_confident(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "some_other_output.csv").write_text("x\n", encoding="utf-8")
        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()

        never = tmp_path / "genuinely_absent.csv"  # never created on disk
        status = PS.get_provenance_status(str(tmp_path), str(never))
        assert status["provenance_type"] == PS.UNKNOWN
        assert status["convergence"]["converged"] is True
        assert status["inconclusive"] is False

    @duckdb_required
    def test_unregistered_is_never_inconclusive_regardless_of_convergence(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "priority_registered.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        OL.register_priority_path(str(tmp_path), str(f))

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["provenance_type"] == PS.UNREGISTERED
        # Discovering the path IS a positive fact -- inconclusive is False
        # here regardless of whether the REST of the tree has converged.
        assert status["inconclusive"] is False

    @duckdb_required
    def test_exact_and_directory_fallback_are_never_inconclusive(
        self, tmp_path: Path,
    ) -> None:
        exact_target = tmp_path / "recorded.csv"
        exact_target.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(exact_target))

        sub = tmp_path / "run_9"
        sub.mkdir()
        (sub / OL.MERIDIAN_NOTES_FILENAME).write_text("note", encoding="utf-8")
        fallback_target = sub / "no_record.csv"
        fallback_target.write_text("a,b\n3,4\n", encoding="utf-8")

        exact_status = PS.get_provenance_status(str(tmp_path), str(exact_target))
        fallback_status = PS.get_provenance_status(str(tmp_path), str(fallback_target))
        assert exact_status["inconclusive"] is False
        assert fallback_status["inconclusive"] is False


# ---------------------------------------------------------------------------
# 3b3020ac — execution-manifest-backed provenance status
# (get_manifest_backed_provenance_status), a thin adapter layered on top of
# get_provenance_status that consumes
# meridian.executor_contract.aggregate_worker_completions()'s fail-closed
# aggregation (duck-typed here — this package never imports
# meridian.executor_contract; see the adapter's own module-section
# docstring for why).
# ---------------------------------------------------------------------------

def _ok_manifest_aggregation(worker_records):
    return {
        "ok": True, "status": "complete", "is_full_production": True,
        "worker_records": worker_records,
    }


class TestManifestBackedProvenanceStatus:
    def test_missing_outputs_dir_returns_error_unchanged(self) -> None:
        result = PS.get_manifest_backed_provenance_status("", "/some/file.csv", None)
        assert "error" in result
        assert "manifest_status" not in result

    @duckdb_required
    def test_no_aggregation_supplied_is_not_verified(self, tmp_path: Path) -> None:
        target = tmp_path / "out.csv"
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        result = PS.get_manifest_backed_provenance_status(str(tmp_path), str(target), None)
        assert result["manifest_status"]["manifest_verified"] is False
        assert result["manifest_status"]["reason"]

    @duckdb_required
    def test_not_ok_aggregation_is_not_verified(self, tmp_path: Path) -> None:
        target = tmp_path / "out.csv"
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        aggregation = {"ok": False, "status": "failed", "worker_records": {}}
        result = PS.get_manifest_backed_provenance_status(str(tmp_path), str(target), aggregation)
        assert result["manifest_status"]["manifest_verified"] is False
        assert "not ok" in result["manifest_status"]["reason"] or "status=" in result["manifest_status"]["reason"]

    @duckdb_required
    def test_matching_recorded_hash_verifies_true(self, tmp_path: Path) -> None:
        target = tmp_path / "out.csv"
        content = b"a,b\n1,2\n"
        target.write_bytes(content)
        import hashlib
        content_hash = hashlib.sha256(content).hexdigest()
        aggregation = _ok_manifest_aggregation({"w1": {"output_hashes": {str(target): content_hash}}})

        result = PS.get_manifest_backed_provenance_status(str(tmp_path), str(target), aggregation)
        assert result["manifest_status"]["manifest_verified"] is True
        assert result["manifest_status"]["reason"] is None
        assert result["manifest_status"]["recorded_output_hash"] == content_hash
        assert result["manifest_status"]["current_content_hash"] == content_hash

    @duckdb_required
    def test_path_not_among_recorded_outputs_is_not_verified(self, tmp_path: Path) -> None:
        target = tmp_path / "out.csv"
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        aggregation = _ok_manifest_aggregation({"w1": {"output_hashes": {}}})

        result = PS.get_manifest_backed_provenance_status(str(tmp_path), str(target), aggregation)
        assert result["manifest_status"]["manifest_verified"] is False
        assert "not among" in result["manifest_status"]["reason"]

    @duckdb_required
    def test_stale_content_hash_is_not_verified(self, tmp_path: Path) -> None:
        """The file on disk has changed since the manifest-backed run
        recorded its output hash — a stale content match must never be
        reported as verified."""
        target = tmp_path / "out.csv"
        target.write_text("current content\n", encoding="utf-8")
        stale_hash = "0" * 64
        aggregation = _ok_manifest_aggregation({"w1": {"output_hashes": {str(target): stale_hash}}})

        result = PS.get_manifest_backed_provenance_status(str(tmp_path), str(target), aggregation)
        assert result["manifest_status"]["manifest_verified"] is False
        assert result["manifest_status"]["recorded_output_hash"] == stale_hash
        assert result["manifest_status"]["current_content_hash"] != stale_hash

    @duckdb_required
    def test_base_provenance_fields_are_preserved(self, tmp_path: Path) -> None:
        """The adapter's manifest_status key is purely ADDITIVE — every
        field get_provenance_status already returns stays intact."""
        target = tmp_path / "out.csv"
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(target))

        base = PS.get_provenance_status(str(tmp_path), str(target))
        result = PS.get_manifest_backed_provenance_status(str(tmp_path), str(target), None)
        for key in base:
            assert result[key] == base[key]
        assert "manifest_status" in result
