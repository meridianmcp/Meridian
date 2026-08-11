"""Tests for meridian_outputs.provenance.bind_artifact_provenance
(sprint item 6d02f343).

Covers the artifact-manifest join point: given a document's own structural
figure/table/equation artifact list (each carrying whatever canonical_path/
expected_sha256 the caller already knows), classify each artifact against
meridian-outputs' per-file provenance:

  - resolved       -- exact match (hash confirmed, or no hash requested), or
                       an unambiguous relocation-tolerant basename match.
  - hash_mismatch   -- exact match whose recorded hash differs from
                       expected_sha256.
  - orphaned        -- nothing resolvable at all (no exact/basename match,
                       no directory-level fallback either).
  - unresolved      -- weaker evidence only: no canonical_path recorded, an
                       ambiguous multi-candidate basename match, a basename
                       match that cannot confirm a requested hash, or ONLY
                       non-authoritative directory-level fallback evidence.

Also covers the aggregate counts/all_clear shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import outputs_local as OL
from meridian_outputs import provenance as PV

try:
    import duckdb as _duckdb_probe  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


# ---------------------------------------------------------------------------
# No canonical_path recorded on the artifact at all
# ---------------------------------------------------------------------------

class TestNoCanonicalPath:
    def test_missing_canonical_path_is_unresolved(self, tmp_path: Path) -> None:
        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{"artifact_id": "fig-1", "kind": "figure", "canonical_path": None}],
        )
        assert result["all_clear"] is False
        assert result["counts"] == {
            PV.RESOLVED: 0, PV.ORPHANED: 0, PV.HASH_MISMATCH: 0, PV.UNRESOLVED: 1,
        }
        binding = result["bindings"][0]
        assert binding["artifact_id"] == "fig-1"
        assert binding["kind"] == "figure"
        assert binding["status"] == PV.UNRESOLVED
        assert binding["reason"]

    def test_empty_artifact_list_is_all_clear(self, tmp_path: Path) -> None:
        result = PV.bind_artifact_provenance(str(tmp_path), [])
        assert result == {
            "bindings": [],
            "counts": {
                PV.RESOLVED: 0, PV.ORPHANED: 0, PV.HASH_MISMATCH: 0, PV.UNRESOLVED: 0,
            },
            "all_clear": True,
        }


# ---------------------------------------------------------------------------
# Exact-tier resolution
# ---------------------------------------------------------------------------

class TestExactTier:
    @duckdb_required
    def test_exact_match_no_expected_hash_is_resolved(self, tmp_path: Path) -> None:
        f = tmp_path / "loss_curve.png"
        f.write_bytes(b"fake-png-bytes")

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{"artifact_id": "fig-1", "kind": "figure", "canonical_path": str(f)}],
        )
        assert result["all_clear"] is True
        binding = result["bindings"][0]
        assert binding["status"] == PV.RESOLVED
        assert binding["match_type"] == "exact"
        assert binding["evidence"] == "meridian_outputs_exact"
        assert binding["reason"] is None

    @duckdb_required
    def test_exact_match_no_hash_on_file_cannot_confirm_is_unresolved(
        self, tmp_path: Path,
    ) -> None:
        """The outputs walker's size-prefilter (e1fd4182) skips hashing a
        file whose size is unique in the tree -- so an exact match can
        legitimately carry no hash at all. Requesting a hash confirmation
        against a record with none on file must fail closed (unresolved),
        never silently pass as "resolved"."""
        f = tmp_path / "loss_curve.png"
        f.write_bytes(b"fake-png-bytes-of-a-unique-size")

        exact = PV.resolve_figure_output(str(tmp_path), str(f))
        assert exact is not None
        assert exact["sha256"] is None  # sanity: confirms the no-hash premise

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-1", "kind": "figure",
                "canonical_path": str(f), "expected_sha256": "some-hash",
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.UNRESOLVED
        assert binding["match_type"] == "exact"
        assert binding["resolved_sha256"] is None

    @duckdb_required
    def test_exact_match_matching_expected_hash_is_resolved(
        self, tmp_path: Path,
    ) -> None:
        # The size-prefilter only skips hashing when a file's size is
        # UNIQUE in the tree -- two same-size siblings force a real hash to
        # be computed for both, so this exercises the real-hash path.
        f = tmp_path / "loss_curve.png"
        f.write_bytes(b"fake-bytes-AAAA")
        sibling = tmp_path / "sibling_same_size.png"
        sibling.write_bytes(b"fake-bytes-BBBB")

        exact = PV.resolve_figure_output(str(tmp_path), str(f))
        assert exact is not None
        recorded_hash = exact["sha256"]
        assert recorded_hash

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-1", "kind": "figure",
                "canonical_path": str(f), "expected_sha256": recorded_hash,
            }],
        )
        assert result["all_clear"] is True
        assert result["bindings"][0]["status"] == PV.RESOLVED
        assert result["bindings"][0]["resolved_sha256"] == recorded_hash

    @duckdb_required
    def test_exact_match_mismatched_expected_hash_is_hash_mismatch(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "loss_curve.png"
        f.write_bytes(b"fake-bytes-AAAA")
        sibling = tmp_path / "sibling_same_size.png"
        sibling.write_bytes(b"fake-bytes-BBBB")

        exact = PV.resolve_figure_output(str(tmp_path), str(f))
        assert exact is not None
        assert exact["sha256"]  # sanity: confirms the real-hash premise

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-1", "kind": "figure",
                "canonical_path": str(f),
                "expected_sha256": "deadbeef-not-the-real-hash",
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.HASH_MISMATCH
        assert binding["match_type"] == "exact"
        assert binding["resolved_sha256"]
        assert binding["resolved_sha256"] != "deadbeef-not-the-real-hash"
        assert "hash" in binding["reason"]


# ---------------------------------------------------------------------------
# Basename-fallback tier
# ---------------------------------------------------------------------------

class TestBasenameTier:
    @duckdb_required
    def test_relocated_unambiguous_basename_match_is_not_authoritative(
        self, tmp_path: Path,
    ) -> None:
        real_dir = tmp_path / "outputs_run_17"
        real_dir.mkdir()
        (real_dir / "width_hist.png").write_bytes(b"fake-png-bytes")
        relocated_path = str(tmp_path / "docs_media" / "width_hist.png")

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-2", "kind": "figure",
                "canonical_path": relocated_path,
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.UNRESOLVED
        assert binding["match_type"] == "basename"
        assert binding["evidence"] == "meridian_outputs_basename_non_authoritative"
        assert binding["reason"]
        assert binding["resolved_sha256"] is None

    @duckdb_required
    def test_ambiguous_basename_match_is_unresolved(self, tmp_path: Path) -> None:
        (tmp_path / "run_a").mkdir()
        (tmp_path / "run_a" / "width_hist.png").write_bytes(b"aaaa")
        (tmp_path / "run_b").mkdir()
        (tmp_path / "run_b" / "width_hist.png").write_bytes(b"bbbb")
        relocated_path = str(tmp_path / "docs_media" / "width_hist.png")

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-3", "kind": "figure",
                "canonical_path": relocated_path,
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.UNRESOLVED
        assert binding["evidence"] == "meridian_outputs_basename_ambiguous"

    @duckdb_required
    def test_basename_match_with_expected_hash_cannot_confirm_is_unresolved(
        self, tmp_path: Path,
    ) -> None:
        real_dir = tmp_path / "outputs_run_17"
        real_dir.mkdir()
        (real_dir / "width_hist.png").write_bytes(b"fake-png-bytes")
        relocated_path = str(tmp_path / "docs_media" / "width_hist.png")

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-4", "kind": "figure",
                "canonical_path": relocated_path,
                "expected_sha256": "some-expected-hash",
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.UNRESOLVED
        assert binding["evidence"] == "meridian_outputs_basename_non_authoritative"


# ---------------------------------------------------------------------------
# No meridian-outputs record at all: orphaned, or directory-fallback-only
# ---------------------------------------------------------------------------

class TestNoAuthoritativeRecord:
    def test_completely_unresolvable_path_is_orphaned(self, tmp_path: Path) -> None:
        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-5", "kind": "figure",
                "canonical_path": str(tmp_path / "never_existed.png"),
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.ORPHANED
        assert binding["evidence"] == "none"

    @duckdb_required
    def test_directory_fallback_only_is_unresolved_not_resolved(
        self, tmp_path: Path,
    ) -> None:
        sub = tmp_path / "run_17"
        sub.mkdir()
        (sub / OL.MERIDIAN_NOTES_FILENAME).write_text(
            "This run: PCA on, BFS off.", encoding="utf-8",
        )
        target = sub / "width_hist.png"
        target.write_bytes(b"fake-png-bytes")

        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()

        # sanity: no exact/basename resolution for a completely different
        # path that merely lives under the same annotated directory.
        missing = sub / "not_the_indexed_file.png"
        assert PV.resolve_figure_output(str(tmp_path), str(missing)) is None

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-6", "kind": "figure",
                "canonical_path": str(missing),
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.UNRESOLVED
        assert binding["evidence"] == "directory_fallback"


# ---------------------------------------------------------------------------
# Fail-closed on incomplete evidence (item 3f758063)
#
# Prior state (confirmed by direct reproduction before this fix): the
# "nothing in meridian-outputs' authoritative index at all" fallback branch
# in _bind_one_artifact called provenance_status.get_provenance_status and
# read its `provenance_type`, but never consulted the `inconclusive` field
# that same call already computes. An artifact whose canonical output
# genuinely exists but simply had not been reached yet by a still-
# converging walk (a real possibility any time resolve_figure_output's own
# forced rebuild() runs out of budget on a large/cold outputs_dir -- see
# that function's own docstring) was classified ORPHANED -- a confident
# "this artifact is orphaned from any known provenance" verdict -- with
# nothing distinguishing it from a genuinely, confirmedly absent artifact.
# A caller gating a write on `status`/`all_clear` would then reject or
# quarantine a perfectly valid, not-yet-indexed artifact.
# ---------------------------------------------------------------------------

class TestIncompleteEvidenceFailsClosed:
    @duckdb_required
    def test_unconverged_index_reports_unresolved_not_orphaned(
        self, tmp_path: Path,
    ) -> None:
        """Simulates a still-mid-walk index (real completed pass, then a
        confirmed-stale backlog entry injected and rebuild() frozen so the
        simulated backlog survives resolve_figure_output's own forced
        rebuild() calls -- same deterministic technique
        test_degraded_labeling.py already uses for this exact reason,
        rather than racing a tiny time budget against a slow hasher)."""
        real_output = tmp_path / "already_here.csv"
        real_output.write_text("a,b\n1,2\n", encoding="utf-8")

        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()
        idx.rebuild = lambda max_seconds=None: len(idx._row_cache)
        idx._pending_stale["/still/not/discovered/output.csv"] = (None, None)
        idx.last_rebuild_partial = True

        never_written = str(tmp_path / "not_yet_produced.csv")
        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-7", "kind": "figure",
                "canonical_path": never_written,
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.UNRESOLVED
        assert binding["evidence"] == "index_not_converged"
        assert binding["status"] != PV.ORPHANED
        assert binding["reason"]

    @duckdb_required
    def test_converged_index_still_reports_orphaned(self, tmp_path: Path) -> None:
        """Fail-closed cuts both ways: once the index HAS genuinely
        converged, a real absence must still come back ORPHANED, not get
        swept into UNRESOLVED just because this item added a new check."""
        (tmp_path / "unrelated.csv").write_text("x\n", encoding="utf-8")
        idx = OL._get_cached_index(str(tmp_path))
        idx.rebuild()
        assert idx.get_convergence_state().converged is True  # sanity

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [{
                "artifact_id": "fig-8", "kind": "figure",
                "canonical_path": str(tmp_path / "truly_missing.csv"),
            }],
        )
        assert result["all_clear"] is False
        binding = result["bindings"][0]
        assert binding["status"] == PV.ORPHANED
        assert binding["evidence"] == "none"


# ---------------------------------------------------------------------------
# Aggregate counts / all_clear across a mixed manifest
# ---------------------------------------------------------------------------

class TestAggregateManifest:
    @duckdb_required
    def test_mixed_manifest_counts_and_all_clear(self, tmp_path: Path) -> None:
        good = tmp_path / "good.png"
        good.write_bytes(b"good-bytes")
        orphan_path = str(tmp_path / "no_such_file.png")

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [
                {"artifact_id": "fig-1", "kind": "figure", "canonical_path": str(good)},
                {"artifact_id": "fig-2", "kind": "figure", "canonical_path": orphan_path},
                {"artifact_id": "tbl-1", "kind": "table", "canonical_path": None},
            ],
        )
        assert result["all_clear"] is False
        assert result["counts"] == {
            PV.RESOLVED: 1, PV.ORPHANED: 1, PV.HASH_MISMATCH: 0, PV.UNRESOLVED: 1,
        }
        assert [b["artifact_id"] for b in result["bindings"]] == [
            "fig-1", "fig-2", "tbl-1",
        ]

    @duckdb_required
    def test_all_resolved_manifest_is_all_clear(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.png"
        f1.write_bytes(b"aaa")
        f2 = tmp_path / "b.png"
        f2.write_bytes(b"bbb")

        result = PV.bind_artifact_provenance(
            str(tmp_path),
            [
                {"artifact_id": "fig-1", "kind": "figure", "canonical_path": str(f1)},
                {"artifact_id": "tbl-1", "kind": "table", "canonical_path": str(f2)},
            ],
        )
        assert result["all_clear"] is True
        assert result["counts"][PV.RESOLVED] == 2
