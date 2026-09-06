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
from meridian_outputs import research_evidence as RE

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
# annotate._read_ledger caching (code-review fix, fa600e42) -- added to
# mitigate a real O(N*M) regression an adversarial review caught:
# get_provenance_status's new relocation-detection tier calls
# annotate.list_provenance (a full ledger read+parse) on every lookup that
# doesn't hit an exact record, including from real batch/loop callers.
# ---------------------------------------------------------------------------

class TestLedgerReadCache:
    def test_repeated_reads_of_unchanged_ledger_return_consistent_data(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "a.csv"
        f.write_text("x,y\n1,1\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f), generating_script="train.py")

        first = AN.list_provenance(str(tmp_path))
        second = AN.list_provenance(str(tmp_path))
        assert first == second
        assert len(first) == 1

    def test_write_after_read_is_visible_on_next_read(
        self, tmp_path: Path,
    ) -> None:
        """The cache must never serve a stale ledger after a real write --
        it is keyed on the ledger file's own (mtime, size), not a fixed
        TTL, so a subsequent write is always observed on the next read."""
        f1 = tmp_path / "a.csv"
        f1.write_text("x,y\n1,1\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f1))
        assert len(AN.list_provenance(str(tmp_path))) == 1

        f2 = tmp_path / "b.csv"
        f2.write_text("x,y\n2,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f2))
        assert len(AN.list_provenance(str(tmp_path))) == 2

    def test_get_provenance_sees_own_immediately_preceding_write(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "a.csv"
        f.write_text("x,y\n1,1\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f), generating_script="v1.py")
        assert AN.get_provenance(str(tmp_path), str(f))["generating_script"] == "v1.py"

        AN.record_provenance(str(tmp_path), str(f), generating_script="v2.py")
        assert AN.get_provenance(str(tmp_path), str(f))["generating_script"] == "v2.py"

    def test_different_outputs_dirs_never_share_a_cache_entry(
        self, tmp_path: Path,
    ) -> None:
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        fa = dir_a / "out.csv"
        fa.write_text("x\n1\n", encoding="utf-8")
        AN.record_provenance(str(dir_a), str(fa), generating_script="a.py")
        fb = dir_b / "out.csv"
        fb.write_text("x\n1\n", encoding="utf-8")
        AN.record_provenance(str(dir_b), str(fb), generating_script="b.py")

        assert AN.get_provenance(str(dir_a), str(fa))["generating_script"] == "a.py"
        assert AN.get_provenance(str(dir_b), str(fb))["generating_script"] == "b.py"


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
# Relocation via content-hash match (sprint item fa600e42)
# ---------------------------------------------------------------------------

class TestRelocated:
    @duckdb_required
    def test_single_hash_match_at_different_path_is_relocated(
        self, tmp_path: Path,
    ) -> None:
        old = tmp_path / "run_1" / "metrics.csv"
        old.parent.mkdir()
        old.write_text("epoch,accuracy\n1,0.9\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(old), generating_script="train.py")

        new = tmp_path / "run_1_moved" / "metrics.csv"
        new.parent.mkdir()
        new.write_text("epoch,accuracy\n1,0.9\n", encoding="utf-8")  # identical content
        old.unlink()

        status = PS.get_provenance_status(str(tmp_path), str(new))
        assert status["provenance_type"] == PS.RELOCATED
        assert status["record"]["path"] == str(old)
        assert status["directory_note"] is None
        assert status["staleness"]["exists_on_disk"] is True
        assert status["staleness"]["stale"] is False
        assert repr(str(old)) in status["staleness"]["reason"]
        assert status["inconclusive"] is False

    @duckdb_required
    def test_exact_record_for_this_path_wins_over_relocation_match(
        self, tmp_path: Path,
    ) -> None:
        # Two files can legitimately share identical content -- an exact
        # record for THIS path must still take priority over treating it as
        # "relocated from" some other twin.
        twin_a = tmp_path / "a.csv"
        twin_a.write_text("x,y\n1,1\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(twin_a))
        twin_b = tmp_path / "b.csv"
        twin_b.write_text("x,y\n1,1\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(twin_b))

        status = PS.get_provenance_status(str(tmp_path), str(twin_b))
        assert status["provenance_type"] == PS.EXACT
        assert status["record"]["path"] == str(twin_b)

    def test_no_relocation_match_when_candidate_file_missing(
        self, tmp_path: Path,
    ) -> None:
        old = tmp_path / "gone.csv"
        old.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(old))
        old.unlink()

        # A path that never existed on disk has no content to hash, so it
        # can never be confirmed "relocated" -- falls through to unknown.
        phantom = tmp_path / "phantom.csv"
        status = PS.get_provenance_status(str(tmp_path), str(phantom))
        assert status["provenance_type"] == PS.UNKNOWN

    @duckdb_required
    def test_resolver_state_maps_relocated_to_verified_authoritative(
        self, tmp_path: Path,
    ) -> None:
        old = tmp_path / "before.csv"
        old.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(old))
        new = tmp_path / "after.csv"
        new.write_text("a,b\n1,2\n", encoding="utf-8")
        old.unlink()

        status = PS.get_provenance_status(str(tmp_path), str(new))
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.resolver.status is RE.ResolverStatus.VERIFIED
        assert rec.partial is False
        assert rec.is_authoritative is True
        assert rec.attributes["provenance_type"] == PS.RELOCATED


# ---------------------------------------------------------------------------
# Ambiguous: content hash matches more than one prior record (fa600e42)
# ---------------------------------------------------------------------------

class TestAmbiguous:
    @duckdb_required
    def test_two_hash_matches_is_ambiguous(self, tmp_path: Path) -> None:
        content = "epoch,accuracy\n1,0.9\n"
        first = tmp_path / "run_a" / "metrics.csv"
        first.parent.mkdir()
        first.write_text(content, encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(first))

        second = tmp_path / "run_b" / "metrics.csv"
        second.parent.mkdir()
        second.write_text(content, encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(second))

        candidate = tmp_path / "run_c" / "metrics.csv"
        candidate.parent.mkdir()
        candidate.write_text(content, encoding="utf-8")  # matches BOTH

        status = PS.get_provenance_status(str(tmp_path), str(candidate))
        assert status["provenance_type"] == PS.AMBIGUOUS
        assert status["record"] is None
        assert status["directory_note"] is None
        assert status["staleness"] is None
        assert status["inconclusive"] is False
        candidate_paths = {c["path"] for c in status["candidates"]}
        assert candidate_paths == {str(first), str(second)}

    @duckdb_required
    def test_resolver_state_maps_ambiguous_to_partial_ambiguous(
        self, tmp_path: Path,
    ) -> None:
        content = "a,b\n1,2\n"
        first = tmp_path / "x.csv"
        first.write_text(content, encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(first))
        second = tmp_path / "y.csv"
        second.write_text(content, encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(second))
        candidate = tmp_path / "z.csv"
        candidate.write_text(content, encoding="utf-8")

        status = PS.get_provenance_status(str(tmp_path), str(candidate))
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS
        assert rec.partial is True
        assert rec.partial_reason
        assert "2" in rec.partial_reason
        assert rec.is_authoritative is False

    @duckdb_required
    def test_candidates_preserved_losslessly_in_evidence_record(
        self, tmp_path: Path,
    ) -> None:
        """Code-review fix (fa600e42): evidence_record_from_provenance_status
        promises every field get_provenance_status returns is preserved
        losslessly in attributes -- AMBIGUOUS's candidates list was being
        silently dropped, contradicting that guarantee."""
        content = "a,b\n1,2\n"
        first = tmp_path / "x.csv"
        first.write_text(content, encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(first))
        second = tmp_path / "y.csv"
        second.write_text(content, encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(second))
        candidate = tmp_path / "z.csv"
        candidate.write_text(content, encoding="utf-8")

        status = PS.get_provenance_status(str(tmp_path), str(candidate))
        rec = PS.evidence_record_from_provenance_status(status)

        assert rec.attributes["candidates"] == status["candidates"]
        candidate_paths = {c["path"] for c in rec.attributes["candidates"]}
        assert candidate_paths == {str(first), str(second)}

    @duckdb_required
    def test_candidates_absent_for_non_ambiguous_types(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "results.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))

        status = PS.get_provenance_status(str(tmp_path), str(f))
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.attributes["candidates"] is None


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
        read as confirmed absence. (Since item 3f758063, a brand-new,
        NEVER-rebuilt index is ALSO never confidently "converged" -- see
        test_never_walked_index_makes_unknown_inconclusive below for that
        distinct, zero-evidence scenario; see test_converged_index_makes_
        unknown_confident for the genuine-convergence baseline instead.)
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
    def test_never_walked_index_makes_unknown_inconclusive(
        self, tmp_path: Path,
    ) -> None:
        """3f758063 -- a genuinely brand-new index that has never once been
        asked to walk `outputs_dir` at all (no rebuild()/search_outputs()
        call preceded this lookup) must answer "unknown" as inconclusive,
        never as confirmed absence -- the real hosted/local mismatch this
        item was opened to close: a caller trusting `get_provenance_status`
        as authoritative on its very first call against a real, non-empty
        outputs tree that simply hadn't been walked yet."""
        real_output = tmp_path / "already_here.csv"
        real_output.write_text("a,b\n1,2\n", encoding="utf-8")
        never = tmp_path / "genuinely_absent.csv"  # never created on disk

        # No rebuild()/search_outputs() call at all before this lookup.
        status = PS.get_provenance_status(str(tmp_path), str(never))
        assert status["provenance_type"] == PS.UNKNOWN
        assert status["convergence"]["converged"] is False
        assert status["convergence"]["never_walked"] is True
        assert status["inconclusive"] is True

        # The REAL, already-on-disk file must be equally inconclusive, not
        # silently reported as a confirmed miss either.
        status_real = PS.get_provenance_status(str(tmp_path), str(real_output))
        assert status_real["provenance_type"] == PS.UNKNOWN
        assert status_real["inconclusive"] is True

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


# ---------------------------------------------------------------------------
# Typed research-evidence bridge (item 0ea8fd3c)
# ---------------------------------------------------------------------------

class TestEvidenceRecordFromProvenanceStatus:
    @duckdb_required
    def test_exact_unchanged_maps_to_verified_non_partial(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "results.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f), generating_script="train.py")

        status = PS.get_provenance_status(str(tmp_path), str(f))
        rec = PS.evidence_record_from_provenance_status(status)

        assert rec.identity.kind is RE.EvidenceKind.OUTPUT
        assert rec.identity.id == str(f)
        assert rec.identity.locator == str(f)
        assert rec.resolver.status is RE.ResolverStatus.VERIFIED
        assert rec.partial is False
        assert rec.is_authoritative is True
        assert rec.attributes["provenance_type"] == PS.EXACT

    @duckdb_required
    def test_content_changed_since_recording_maps_to_stale(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "overwritten.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))
        f.write_text("a,b\n999,999\n", encoding="utf-8")

        status = PS.get_provenance_status(str(tmp_path), str(f))
        rec = PS.evidence_record_from_provenance_status(status)

        assert rec.resolver.status is RE.ResolverStatus.STALE
        assert rec.partial is False  # the record itself is exact, just stale
        assert rec.is_authoritative is False

    @duckdb_required
    def test_no_content_hash_captured_maps_to_ambiguous(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "no_hash.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))
        # Sanity: a real record was captured with a content hash. Simulate
        # "no hash was ever captured" the same way the underlying staleness
        # helper distinguishes it -- by making the recorded hash look absent.
        status = PS.get_provenance_status(str(tmp_path), str(f))
        status["staleness"]["recorded_content_hash"] = None
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS

    @duckdb_required
    def test_script_stale_maps_to_stale(self, tmp_path: Path) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        output = tmp_path / "predictions.csv"
        output.write_text("id,pred\n1,0.5\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(output), generating_script=str(script))
        FP.tag_output(str(output), str(tmp_path), script_path=str(script))
        script.write_text("print('v2 fixed')\n", encoding="utf-8")

        status = PS.get_provenance_status(str(tmp_path), str(output))
        assert status["provenance_type"] == PS.STALE_BY_SCRIPT  # sanity
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.resolver.status is RE.ResolverStatus.STALE

    @duckdb_required
    def test_directory_fallback_maps_to_degraded(self, tmp_path: Path) -> None:
        sub = tmp_path / "run_1"
        sub.mkdir()
        (sub / OL.MERIDIAN_NOTES_FILENAME).write_text("a note", encoding="utf-8")
        target = sub / "width_hist.csv"
        target.write_text("bin,count\n1,5\n", encoding="utf-8")
        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()

        status = PS.get_provenance_status(str(tmp_path), str(target))
        assert status["provenance_type"] == PS.DIRECTORY_FALLBACK  # sanity
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.resolver.status is RE.ResolverStatus.DEGRADED
        assert rec.partial is False

    @duckdb_required
    def test_unregistered_maps_to_held_and_partial(self, tmp_path: Path) -> None:
        f = tmp_path / "orphaned.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        OL.register_priority_path(str(tmp_path), str(f))

        status = PS.get_provenance_status(str(tmp_path), str(f))
        assert status["provenance_type"] == PS.UNREGISTERED  # sanity
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.resolver.status is RE.ResolverStatus.HELD
        assert rec.partial is True
        assert rec.partial_reason
        assert rec.is_authoritative is False

    @duckdb_required
    def test_unknown_confident_maps_to_unavailable_and_partial(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "some_other_output.csv").write_text("x\n", encoding="utf-8")
        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()

        never = tmp_path / "genuinely_absent.csv"  # never created on disk
        status = PS.get_provenance_status(str(tmp_path), str(never))
        assert status["provenance_type"] == PS.UNKNOWN  # sanity
        assert status["inconclusive"] is False  # sanity: converged index

        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.partial is True
        assert rec.resolver.status is RE.ResolverStatus.UNAVAILABLE

    @duckdb_required
    def test_unknown_inconclusive_maps_to_ambiguous(self, tmp_path: Path) -> None:
        for i in range(25):
            (tmp_path / f"file_{i:03d}.csv").write_text(f"{i}\n", encoding="utf-8")
        never = tmp_path / "genuinely_absent.csv"

        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild(max_seconds=0)
        assert idx.get_convergence_state().converged is False  # sanity

        status = PS.get_provenance_status(str(tmp_path), str(never))
        assert status["provenance_type"] == PS.UNKNOWN
        assert status["inconclusive"] is True  # sanity
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.resolver.status is RE.ResolverStatus.AMBIGUOUS
        assert rec.partial is True

    def test_error_status_raises_envelope_validation_error(self) -> None:
        with pytest.raises(RE.EnvelopeValidationError):
            PS.evidence_record_from_provenance_status({"error": "outputs_dir is required"})

    @duckdb_required
    def test_archival_identity_surfaced_as_external_id_and_hash(
        self, tmp_path: Path,
    ) -> None:
        canonical = tmp_path / "results.csv"
        canonical.write_text("a,b\n1,2\n", encoding="utf-8")
        archival = tmp_path / "results_old.csv"
        archival.write_text("a,b\n1,2\n", encoding="utf-8")
        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()

        status = PS.get_provenance_status(str(tmp_path), str(archival))
        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.identity.external_ids.get("canonical_path") == str(canonical)
        assert rec.attributes["archival"]["is_archival"] is True

    @duckdb_required
    def test_manifest_status_preserved_in_attributes(self, tmp_path: Path) -> None:
        target = tmp_path / "out.csv"
        target.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(target))
        status = PS.get_manifest_backed_provenance_status(str(tmp_path), str(target), None)

        rec = PS.evidence_record_from_provenance_status(status)
        assert rec.attributes["manifest_status"] == status["manifest_status"]

    @duckdb_required
    def test_record_is_json_serializable_end_to_end(self, tmp_path: Path) -> None:
        """The whole point of the bridge -- what comes out must be a fully
        valid, round-trippable research_evidence record, not just a
        constructible one."""
        f = tmp_path / "results.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))
        status = PS.get_provenance_status(str(tmp_path), str(f))
        rec = PS.evidence_record_from_provenance_status(status)

        env = RE.build_envelope(records=[rec], envelope_id="e", generated_at="t")
        payload = RE.serialize_provenance_envelope(env, format="json")
        restored = RE.parse_provenance_envelope(payload, format="json")
        assert restored == env


class TestBuildProvenanceEnvelope:
    @duckdb_required
    def test_builds_one_record_per_path(self, tmp_path: Path) -> None:
        a = tmp_path / "a.csv"
        a.write_text("a\n1\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(a))
        b = tmp_path / "b.csv"
        b.write_text("b\n2\n", encoding="utf-8")  # never recorded -> unknown

        env = PS.build_provenance_envelope(str(tmp_path), [str(a), str(b)])
        assert len(env.records) == 2
        ids = {r.identity.id for r in env.records}
        assert ids == {str(a), str(b)}

    def test_empty_paths_yields_empty_non_partial_envelope(
        self, tmp_path: Path,
    ) -> None:
        env = PS.build_provenance_envelope(str(tmp_path), [])
        assert env.records == []
        assert env.partial is False

    @duckdb_required
    def test_any_partial_record_marks_envelope_partial(
        self, tmp_path: Path,
    ) -> None:
        recorded = tmp_path / "recorded.csv"
        recorded.write_text("a\n1\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(recorded))
        never = tmp_path / "never_seen.csv"
        never.write_text("a\n2\n", encoding="utf-8")

        env = PS.build_provenance_envelope(str(tmp_path), [str(recorded), str(never)])
        assert env.partial is True
        assert "partial" in (env.partial_reason or "").lower()

    def test_missing_outputs_dir_raises(self) -> None:
        with pytest.raises(RE.EnvelopeValidationError):
            PS.build_provenance_envelope("", ["/some/file.csv"])

    def test_bad_path_raises_rather_than_silently_dropping(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(RE.EnvelopeValidationError):
            PS.build_provenance_envelope(str(tmp_path), [""])

    @duckdb_required
    def test_envelope_round_trips_losslessly(self, tmp_path: Path) -> None:
        f = tmp_path / "results.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        AN.record_provenance(str(tmp_path), str(f))

        env = PS.build_provenance_envelope(
            str(tmp_path), [str(f)],
            envelope_id="fixed-env", generated_at="2026-08-21T00:00:00+00:00",
        )
        payload = RE.serialize_provenance_envelope(env, format="xml")
        restored = RE.parse_provenance_envelope(payload, format="xml")
        assert restored == env
