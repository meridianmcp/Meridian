"""Same-size file detection in meridian_outputs.outputs_local.

The size prefilter (e1fd4182) skips hashing a file whose size is unique when
it is analysed. These tests cover the follow-up fixes:

  - OutputsFtsIndex.rebuild counts paths deferred past the bounded analysis
    intake (self._pending_stale) in its size -> count map.
  - A cached row indexed without a hash is promoted back into the backlog,
    once, when a file of the same size appears later, so archival
    classification sees a real hash instead of None.
  - classify_canonical_archival compares sizes before hashing, and the public
    files_content_identical exposes the same pairwise rule.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import outputs_local as OL

try:
    import duckdb as _duckdb_probe  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


def _key(idx: OL.OutputsFtsIndex, name: str) -> str:
    """The _row_cache key for the file called ``name``."""
    return next(p for p in idx._row_cache if p.endswith(name))


class _CountingHasher:
    """Hasher that records every path it is asked to hash."""

    def __init__(self, *, fail_for: str | None = None) -> None:
        self.calls: list[str] = []
        self._fail_for = fail_for

    def __call__(self, path: str) -> str | None:
        self.calls.append(path)
        if self._fail_for is not None and path.endswith(self._fail_for):
            return None
        return OL._xxh3_file(path)

    def count(self, name: str) -> int:
        return sum(1 for p in self.calls if p.endswith(name))


class _ClassifySpy:
    """Wraps classify_canonical_archival and keeps every result it returns."""

    def __init__(self) -> None:
        self.results: list[dict[str, OL.ArchivalClassification]] = []
        self._real = OL.classify_canonical_archival

    def __call__(self, paths, **kwargs):
        out = self._real(paths, **kwargs)
        self.results.append(out)
        return out

    def last_for(self, name: str) -> OL.ArchivalClassification:
        latest = self.results[-1]
        return next(v for k, v in latest.items() if k.endswith(name))


# ---------------------------------------------------------------------------
# Archival pair where one side was first indexed alone (unique size)
# ---------------------------------------------------------------------------

class TestArchivalPairAfterUniqueSizeIndexing:
    @duckdb_required
    def test_canonical_indexed_alone_then_identical_old_twin_added(
        self, tmp_path: Path,
    ) -> None:
        content = b"a,b\n1,2\n3,4\n"
        (tmp_path / "run.csv").write_bytes(content)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx._row_cache[_key(idx, "run.csv")].sha256 is None  # premise

            (tmp_path / "run_old.csv").write_bytes(content)
            spy = _ClassifySpy()
            with patch.object(OL, "classify_canonical_archival", side_effect=spy):
                idx.rebuild()

            archival = idx._row_cache[_key(idx, "run_old.csv")]
            canonical = idx._row_cache[_key(idx, "run.csv")]
            assert canonical.sha256 is not None, "the canonical row was not backfilled"
            assert archival.is_archival is True
            assert archival.canonical_path == _key(idx, "run.csv")
            assert "SHA-256 identical" in spy.last_for("run_old.csv").reason
            assert idx.last_rebuild_metrics["same_size_promoted"] == 1
        finally:
            idx.close()

    @duckdb_required
    def test_old_twin_indexed_alone_then_identical_canonical_added(
        self, tmp_path: Path,
    ) -> None:
        content = b"a,b\n1,2\n3,4\n"
        (tmp_path / "run_old.csv").write_bytes(content)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx._row_cache[_key(idx, "run_old.csv")].sha256 is None  # premise
            assert idx._row_cache[_key(idx, "run_old.csv")].is_archival is False

            (tmp_path / "run.csv").write_bytes(content)
            spy = _ClassifySpy()
            with patch.object(OL, "classify_canonical_archival", side_effect=spy):
                idx.rebuild()

            archival = idx._row_cache[_key(idx, "run_old.csv")]
            assert archival.sha256 is not None
            assert archival.is_archival is True
            assert "SHA-256 identical" in spy.last_for("run_old.csv").reason
        finally:
            idx.close()

    @duckdb_required
    def test_rebuild_classification_uses_known_sizes_without_stat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """rebuild() passes the sizes it already holds, so the size-first
        check never stats a file from inside classification."""
        content = b"x,y\n5,6\n"
        (tmp_path / "res.csv").write_bytes(content)
        (tmp_path / "res_old.csv").write_bytes(content)
        (tmp_path / "other_old.csv").write_bytes(b"short")
        (tmp_path / "other.csv").write_bytes(b"a much longer body")

        def _no_stat(path: str) -> int | None:
            raise AssertionError(f"classification stat-ed {path!r}")

        monkeypatch.setattr(OL, "_stat_size_or_none", _no_stat)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            spy = _ClassifySpy()
            with patch.object(OL, "classify_canonical_archival", side_effect=spy):
                idx.rebuild()
            assert idx._row_cache[_key(idx, "res_old.csv")].is_archival is True
            assert idx._row_cache[_key(idx, "other_old.csv")].is_archival is False
            assert "sizes differ" in spy.last_for("other_old.csv").reason
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Size counts include paths deferred past the bounded analysis intake
# ---------------------------------------------------------------------------

class TestDeferredPathsCountTowardSizes:
    @duckdb_required
    def test_unique_stale_file_hashed_when_partner_is_deferred(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "a_first.bin").write_bytes(b"A" * 10)
        (tmp_path / "b_partner.bin").write_bytes(b"B" * 10)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        # One file per call is analysed; the walk itself stays unbounded,
        # so both files are discovered on the first call.
        monkeypatch.setattr(idx, "_adaptive_batch_limit", lambda: 1)
        try:
            idx.rebuild()
            assert idx.last_rebuild_metrics["analysis_backlog_deferred"] == 1
            assert any(p.endswith("b_partner.bin") for p in idx._pending_stale)
            first = idx._row_cache[_key(idx, "a_first.bin")]
            assert first.sha256 is not None, (
                "a file whose same-size partner was deferred past the intake "
                "slice was treated as unique-size and left unhashed"
            )
            assert idx.last_rebuild_metrics["files_hash_needed"] == 1
            assert idx.last_rebuild_metrics["files_hash_skipped"] == 0

            idx.rebuild()
            partner = idx._row_cache[_key(idx, "b_partner.bin")]
            assert partner.sha256 is not None
            assert not idx._pending_stale
        finally:
            idx.close()

    def test_known_size_counts_counts_pending_outside_all_paths(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx._row_cache["/t/cached"] = OL.OutputRow(
                path="/t/cached", content=None, mtime=1.0, sha256=None,
                size=7, generating_script=None,
            )
            idx._pending_stale["/t/listed"] = (2.0, 7)
            idx._pending_stale["/t/restored"] = (3.0, 7)
            idx._pending_stale["/t/unstatable"] = (None, None)
            all_paths = ["/t/cached", "/t/listed", "/t/unknown"]
            counts = idx._known_size_counts(all_paths, set(all_paths))
            assert counts == {7: 3}
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Promotion of unhashed rows: idempotent, bounded, gated
# ---------------------------------------------------------------------------

class TestSameSizePromotion:
    @duckdb_required
    def test_promotion_hashes_partner_once_and_is_idempotent(
        self, tmp_path: Path,
    ) -> None:
        hasher = _CountingHasher()
        (tmp_path / "solo.bin").write_bytes(b"S" * 12)
        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=hasher)
        try:
            idx.rebuild()
            assert hasher.count("solo.bin") == 0  # unique size: skipped

            (tmp_path / "later.bin").write_bytes(b"L" * 12)
            idx.rebuild()
            assert hasher.count("solo.bin") == 1
            assert hasher.count("later.bin") == 1
            assert idx.last_rebuild_metrics["same_size_promoted"] == 1
            assert idx._row_cache[_key(idx, "solo.bin")].sha256 is not None

            idx.rebuild()
            idx.rebuild()
            assert hasher.count("solo.bin") == 1, "the promoted partner was re-hashed"
            assert hasher.count("later.bin") == 1
            assert idx.last_rebuild_metrics["same_size_promoted"] == 0
            assert not idx._same_size_promoted
        finally:
            idx.close()

    @duckdb_required
    def test_unhashable_partner_is_not_requeued_every_call(
        self, tmp_path: Path,
    ) -> None:
        hasher = _CountingHasher(fail_for="solo.bin")
        (tmp_path / "solo.bin").write_bytes(b"S" * 12)
        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=hasher)
        try:
            idx.rebuild()
            (tmp_path / "later.bin").write_bytes(b"L" * 12)
            idx.rebuild()
            assert hasher.count("solo.bin") == 1
            assert idx._row_cache[_key(idx, "solo.bin")].sha256 is None

            # A later call with something pending runs the scan again; the
            # unchanged, unhashable row must not be queued a second time.
            (tmp_path / "third.bin").write_bytes(b"T" * 12)
            idx.rebuild()
            idx.rebuild()
            assert hasher.count("solo.bin") == 1
            assert hasher.count("third.bin") == 1
        finally:
            idx.close()

    @duckdb_required
    def test_unique_size_rows_are_never_promoted(self, tmp_path: Path) -> None:
        hasher = _CountingHasher()
        (tmp_path / "one.bin").write_bytes(b"1" * 3)
        (tmp_path / "two.bin").write_bytes(b"2" * 5)
        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=hasher)
        try:
            idx.rebuild()
            (tmp_path / "three.bin").write_bytes(b"3" * 9)
            idx.rebuild()
            idx.rebuild()
            assert hasher.calls == []
            assert idx.last_rebuild_metrics["same_size_promoted"] == 0
        finally:
            idx.close()

    @duckdb_required
    def test_promotion_is_bounded_by_analysis_limit_and_deterministic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        names = [f"f{i}.bin" for i in range(5)]
        for i, name in enumerate(names):
            (tmp_path / name).write_bytes(bytes([65 + i]) * 16)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            idx.rebuild()
            assert all(idx._row_cache[_key(idx, n)].sha256 for n in names)
            # Simulate rows written before this fix: same-size rows with no
            # hash, and a scan owed on an otherwise idle index.
            for n in names:
                idx._row_cache[_key(idx, n)].sha256 = None
            idx._same_size_scan_needed = True
            monkeypatch.setattr(idx, "_adaptive_batch_limit", lambda: 2)

            idx.rebuild()
            m = idx.last_rebuild_metrics
            assert m["same_size_promoted"] == 2
            assert m["same_size_promotion_deferred"] == 3
            hashed = sorted(n for n in names if idx._row_cache[_key(idx, n)].sha256)
            assert hashed == ["f0.bin", "f1.bin"]  # sorted order, first slots
            assert idx._same_size_scan_needed is True

            idx.rebuild()
            idx.rebuild()
            assert all(idx._row_cache[_key(idx, n)].sha256 for n in names)
            assert idx.last_rebuild_metrics["same_size_promotion_deferred"] == 0
            assert idx._same_size_scan_needed is False
        finally:
            idx.close()

    @duckdb_required
    def test_no_scan_on_an_idle_converged_index(self, tmp_path: Path) -> None:
        (tmp_path / "a.bin").write_bytes(b"a" * 8)
        (tmp_path / "b.bin").write_bytes(b"b" * 8)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()  # rehydration has not run at scan time: scan owed
            assert idx._same_size_scan_needed is True
            idx.rebuild()  # the owed scan runs, finds nothing
            assert idx._same_size_scan_needed is False

            calls: list[int] = []
            real = idx._known_size_counts

            def _spy(all_paths, path_set):
                calls.append(len(all_paths))
                return real(all_paths, path_set)

            idx._known_size_counts = _spy  # type: ignore[method-assign]
            idx.rebuild()
            assert calls == [], "an idle converged index paid for a size scan"
        finally:
            idx.close()

    @duckdb_required
    def test_restart_with_seeded_cache_backfills_unhashed_partner(
        self, tmp_path: Path,
    ) -> None:
        """A new instance seeded from a prior one (the restart-harness path)
        backfills a row the prior instance left unhashed, even though
        nothing is stale."""
        outputs = tmp_path / "out"
        outputs.mkdir()
        db_path = str(tmp_path / "index.duckdb")
        (outputs / "solo.bin").write_bytes(b"S" * 12)
        with patch.object(
            OL.OutputsFtsIndex, "_promote_unhashed_same_size_rows", return_value=0,
        ):
            idx1 = OL.OutputsFtsIndex(str(outputs), db_path=db_path)
            try:
                idx1.rebuild()
                (outputs / "later.bin").write_bytes(b"L" * 12)
                idx1.rebuild()
                # The pre-fix defect: the partner stays unhashed.
                assert idx1._row_cache[_key(idx1, "solo.bin")].sha256 is None
                seeded_rows = dict(idx1._row_cache)
                seeded_manifest = dict(idx1._manifest)
            finally:
                idx1.close()

        hasher = _CountingHasher()
        idx2 = OL.OutputsFtsIndex(
            str(outputs), db_path=db_path, hasher=hasher,
            initial_row_cache=seeded_rows, initial_manifest=seeded_manifest,
        )
        try:
            idx2.rebuild()
            assert hasher.count("later.bin") == 0  # not stale, not re-hashed
            assert hasher.count("solo.bin") == 1
            assert idx2._row_cache[_key(idx2, "solo.bin")].sha256 is not None
        finally:
            idx2.close()


# ---------------------------------------------------------------------------
# classify_canonical_archival: size first
# ---------------------------------------------------------------------------

class TestClassifyCanonicalArchivalSizeFirst:
    def test_no_hash_call_when_sizes_differ(self, tmp_path: Path) -> None:
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_bytes(b"a\n1")
        archival.write_bytes(b"a\n22")
        hasher = _CountingHasher()
        results = OL.classify_canonical_archival(
            [str(canonical), str(archival)], hasher=hasher,
        )
        rec = results[str(archival)]
        assert hasher.calls == []
        assert rec.is_archival is False
        assert rec.canonical_path == str(canonical)
        assert "content DIFFERS" in rec.reason
        assert "sizes differ" in rec.reason

    def test_same_size_still_hashes(self, tmp_path: Path) -> None:
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_bytes(b"a\n1")
        archival.write_bytes(b"a\n2")
        hasher = _CountingHasher()
        results = OL.classify_canonical_archival(
            [str(canonical), str(archival)], hasher=hasher,
        )
        assert sorted(hasher.calls) == sorted([str(canonical), str(archival)])
        assert results[str(archival)].is_archival is False
        assert "sizes differ" not in results[str(archival)].reason

    def test_stat_failure_falls_back_to_hashing(self, tmp_path: Path) -> None:
        canonical = str(tmp_path / "gone" / "run.csv")
        archival = str(tmp_path / "gone" / "run_old.csv")
        calls: list[str] = []

        def _same(path: str) -> str:
            calls.append(path)
            return "deadbeef"

        results = OL.classify_canonical_archival([canonical, archival], hasher=_same)
        assert results[archival].is_archival is True
        assert sorted(calls) == sorted([canonical, archival])

    def test_injected_sizer_is_used(self, tmp_path: Path) -> None:
        paths = [str(tmp_path / "run.csv"), str(tmp_path / "run_old.csv")]
        sizes = {paths[0]: 10, paths[1]: 11}
        hasher = _CountingHasher()
        results = OL.classify_canonical_archival(
            paths, hasher=hasher, sizer=sizes.get,
        )
        assert hasher.calls == []
        assert "sizes differ: 11 vs 10 bytes" in results[paths[1]].reason

    def test_default_sizer_is_stat(self) -> None:
        import inspect
        sig = inspect.signature(OL.classify_canonical_archival)
        assert sig.parameters["sizer"].default is None
        assert sig.parameters["hasher"].default is OL._xxh3_file


class TestFilesContentIdentical:
    def test_different_sizes_are_false_without_hashing(self, tmp_path: Path) -> None:
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"12345")
        b.write_bytes(b"123456")
        hasher = _CountingHasher()
        assert OL.files_content_identical(str(a), str(b), hasher=hasher) is False
        assert hasher.calls == []

    def test_identical_content_is_true(self, tmp_path: Path) -> None:
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"same bytes")
        b.write_bytes(b"same bytes")
        assert OL.files_content_identical(str(a), str(b)) is True

    def test_same_size_different_content_is_false(self, tmp_path: Path) -> None:
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"same-size-A")
        b.write_bytes(b"same-size-B")
        assert OL.files_content_identical(str(a), str(b)) is False

    def test_unavailable_hash_is_none(self, tmp_path: Path) -> None:
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"xx")
        b.write_bytes(b"yy")
        assert OL.files_content_identical(
            str(a), str(b), hasher=lambda _p: None,
        ) is None
        only_b_fails = _CountingHasher(fail_for="b.bin")
        assert OL.files_content_identical(
            str(a), str(b), hasher=only_b_fails,
        ) is None

    def test_missing_files_fall_back_to_hash(self, tmp_path: Path) -> None:
        missing_a = str(tmp_path / "missing_a.bin")
        missing_b = str(tmp_path / "missing_b.bin")
        assert OL.files_content_identical(missing_a, missing_b) is None
        assert OL.files_content_identical(
            missing_a, missing_b, hasher=lambda _p: "h",
        ) is True
