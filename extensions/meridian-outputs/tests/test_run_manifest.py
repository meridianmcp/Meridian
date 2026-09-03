"""Tests for meridian_outputs.run_manifest (sprint item 37ce5537).

Covers:
  - start_run_manifest: required-arg validation, deterministic identity hash,
    idempotent re-call, immutability refusal on a genuine identity change,
    input-path hashing (missing path -> None, real content -> real sha256).
  - finalize_run_manifest: requires a prior start; fail-closed exact output
    binding (a missing output downgrades "complete" -> "partial"
    automatically); unknown artifact ids degrade the same way; manifest_hash
    never changes across start -> finalize.
  - check_run_manifest_immutable: the four explicit branches.
  - get_run_manifest / list_run_manifests.
  - run_manifest_to_evidence_record: RUN-kind, resolver-status mapping per
    phase, partial flag/reason.
  - build_run_manifest_envelope + JSON/XML round trip + canonical hash
    stability.
  - capture_git_state: injectable run() seam, degrade-to-None on failure.
  - _snapshot_bounds: never raises even when outputs_local internals change
    shape.
  - end-to-end fixture-tree smoke test (the local MCP path, no tunnel).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import artifact_registry as AR
from meridian_outputs import fingerprint as FP
from meridian_outputs import research_evidence as RE
from meridian_outputs import run_manifest as RM


def _write(path: Path, content: str = "hello") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# start_run_manifest
# ---------------------------------------------------------------------------

class TestStartRunManifest:
    def test_missing_outputs_dir_raises(self) -> None:
        with pytest.raises(RM.RunManifestError):
            RM.start_run_manifest("", run_id="r1", command_name="build")

    def test_missing_run_id_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RM.RunManifestError):
            RM.start_run_manifest(str(tmp_path), run_id="", command_name="build")

    def test_missing_command_name_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RM.RunManifestError):
            RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="")

    def test_negative_expected_count_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RM.RunManifestError):
            RM.start_run_manifest(
                str(tmp_path), run_id="r1", command_name="build",
                expected_counts={"images": -1},
            )

    def test_non_int_expected_count_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RM.RunManifestError):
            RM.start_run_manifest(
                str(tmp_path), run_id="r1", command_name="build",
                expected_counts={"images": "many"},
            )

    def test_basic_record_shape(self, tmp_path: Path) -> None:
        rec = RM.start_run_manifest(
            str(tmp_path), run_id="r1", command_name="build_outputs",
            project_id="proj-a", version="v1", command_args={"seed": 42},
        )
        assert rec["run_id"] == "r1"
        assert rec["phase"] == "in_progress"
        assert rec["scope"] == {
            "project_id": "proj-a", "version": "v1", "sprint_item_id": None,
        }
        assert rec["command_identity"] == {
            "tool_name": "build_outputs", "args": {"seed": 42},
        }
        assert rec["package_identity"]["name"] == "meridian_outputs"
        assert rec["output_identity"] is None
        assert rec["artifact_ids"] == []
        assert rec["unknown_artifact_ids"] == []
        assert rec["manifest_hash"]
        assert rec["created_at"] and rec["updated_at"]

    def test_deterministic_hash_same_inputs(self, tmp_path: Path) -> None:
        a = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        b = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        assert a["manifest_hash"] == b["manifest_hash"]
        assert a["created_at"] == b["created_at"]  # persisted record, unchanged

    def test_independent_run_ids_are_independent(self, tmp_path: Path) -> None:
        a = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        b = RM.start_run_manifest(str(tmp_path), run_id="r2", command_name="build")
        assert a["run_id"] != b["run_id"]
        assert RM.get_run_manifest(str(tmp_path), "r1") is not None
        assert RM.get_run_manifest(str(tmp_path), "r2") is not None

    def test_same_run_id_different_identity_refused(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build", version="v1")
        with pytest.raises(RM.RunManifestError):
            RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build", version="v2")

    def test_input_path_hashing(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "input.csv", "a,b\n1,2\n")
        missing = str(tmp_path / "does_not_exist.csv")
        rec = RM.start_run_manifest(
            str(tmp_path), run_id="r1", command_name="build",
            input_paths=[p, missing],
        )
        hashes = rec["input_identity"]["file_hashes"]
        assert hashes[missing] is None
        assert hashes[p] == FP.script_content_hash(p)
        assert rec["input_identity"]["file_set_hash"]

    def test_ledger_persisted_on_disk(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        ledger_path = tmp_path / ".meridian-outputs-cache" / "run_manifest_ledger.json"
        assert ledger_path.is_file()

    def test_bounds_snapshot_present(self, tmp_path: Path) -> None:
        rec = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        bounds = rec["bounds"]
        for key in (
            "max_workers", "max_batch", "adaptive_max_batch",
            "adaptive_max_fts_seconds", "adaptive_max_write_seconds",
            "tantivy_heap_bytes", "duckdb_memory_limit_bytes",
        ):
            assert key in bounds

    def test_ledger_locations_reference_sibling_ledgers(self, tmp_path: Path) -> None:
        rec = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        locs = rec["ledger_locations"]
        cache_dir = tmp_path / ".meridian-outputs-cache"
        assert locs["run_manifest"] == str(cache_dir / "run_manifest_ledger.json")
        assert locs["provenance_ledger"] == str(cache_dir / "provenance_ledger.json")
        assert locs["fingerprint_ledger"] == str(cache_dir / "fingerprint_ledger.json")
        assert locs["artifact_registry"] == str(cache_dir / "artifact_registry.json")


# ---------------------------------------------------------------------------
# finalize_run_manifest
# ---------------------------------------------------------------------------

class TestFinalizeRunManifest:
    def test_finalize_without_start_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RM.RunManifestError):
            RM.finalize_run_manifest(str(tmp_path), "never-started")

    def test_invalid_status_raises(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        with pytest.raises(RM.RunManifestError):
            RM.finalize_run_manifest(str(tmp_path), "r1", status="bogus")

    def test_clean_complete(self, tmp_path: Path) -> None:
        p = _write(tmp_path / "out.csv", "x\n1\n")
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec = RM.finalize_run_manifest(str(tmp_path), "r1", output_paths=[p], status="complete")
        assert rec["phase"] == "complete"
        assert rec["output_identity"]["missing_or_unreadable"] == []
        assert rec["status_reason"] is None

    def test_missing_output_downgrades_to_partial(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "ghost.csv")
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec = RM.finalize_run_manifest(str(tmp_path), "r1", output_paths=[missing], status="complete")
        assert rec["phase"] == "partial"
        assert missing in rec["output_identity"]["missing_or_unreadable"]
        assert "missing/unreadable" in rec["status_reason"]

    def test_unknown_artifact_id_downgrades_to_partial(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec = RM.finalize_run_manifest(
            str(tmp_path), "r1", artifact_ids=["does-not-exist"], status="complete",
        )
        assert rec["phase"] == "partial"
        assert rec["unknown_artifact_ids"] == ["does-not-exist"]
        assert "not found in the registry" in rec["status_reason"]

    def test_known_artifact_id_accepted(self, tmp_path: Path) -> None:
        art = AR.register_artifact(str(tmp_path), "output", generator="gen.py", source_locator="src")
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec = RM.finalize_run_manifest(
            str(tmp_path), "r1", artifact_ids=[art["artifact_id"]], status="complete",
        )
        assert rec["phase"] == "complete"
        assert rec["artifact_ids"] == [art["artifact_id"]]
        assert rec["unknown_artifact_ids"] == []

    def test_failed_status_preserved_not_auto_downgraded(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec = RM.finalize_run_manifest(str(tmp_path), "r1", status="failed", reason="OOM")
        assert rec["phase"] == "failed"
        assert rec["status_reason"] == "OOM"

    def test_manifest_hash_unchanged_across_finalize(self, tmp_path: Path) -> None:
        started = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        finished = RM.finalize_run_manifest(str(tmp_path), "r1", status="complete")
        assert started["manifest_hash"] == finished["manifest_hash"]

    def test_finalize_persists_and_is_gettable(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        RM.finalize_run_manifest(str(tmp_path), "r1", status="complete")
        rec = RM.get_run_manifest(str(tmp_path), "r1")
        assert rec is not None
        assert rec["phase"] == "complete"


# ---------------------------------------------------------------------------
# check_run_manifest_immutable
# ---------------------------------------------------------------------------

class TestCheckRunManifestImmutable:
    def test_no_existing_is_ok(self) -> None:
        ok, reason = RM.check_run_manifest_immutable(
            None, {"run_id": "r1", "scope": {}, "manifest_hash": "h1"},
        )
        assert ok and reason is None

    def test_same_hash_is_ok(self) -> None:
        m = {"run_id": "r1", "scope": {"project_id": "p", "version": "v"}, "manifest_hash": "h1"}
        ok, reason = RM.check_run_manifest_immutable(m, dict(m))
        assert ok and reason is None

    def test_different_run_id_refused(self) -> None:
        existing = {"run_id": "r1", "scope": {}, "manifest_hash": "h1"}
        new = {"run_id": "r2", "scope": {}, "manifest_hash": "h1"}
        ok, reason = RM.check_run_manifest_immutable(existing, new)
        assert not ok and reason

    def test_different_hash_same_run_refused(self) -> None:
        existing = {"run_id": "r1", "scope": {"project_id": "p", "version": "v"}, "manifest_hash": "h1"}
        new = {"run_id": "r1", "scope": {"project_id": "p", "version": "v"}, "manifest_hash": "h2"}
        ok, reason = RM.check_run_manifest_immutable(existing, new)
        assert not ok and "DIFFERENT hash" in reason

    def test_different_scope_same_run_refused(self) -> None:
        existing = {"run_id": "r1", "scope": {"project_id": "p", "version": "v1"}, "manifest_hash": "h1"}
        new = {"run_id": "r1", "scope": {"project_id": "p", "version": "v2"}, "manifest_hash": "h1"}
        ok, reason = RM.check_run_manifest_immutable(existing, new)
        assert not ok and reason


# ---------------------------------------------------------------------------
# get_run_manifest / list_run_manifests
# ---------------------------------------------------------------------------

class TestGetListRunManifests:
    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        assert RM.get_run_manifest(str(tmp_path), "nope") is None

    def test_get_empty_run_id_returns_none(self, tmp_path: Path) -> None:
        assert RM.get_run_manifest(str(tmp_path), "") is None

    def test_list_empty(self, tmp_path: Path) -> None:
        assert RM.list_run_manifests(str(tmp_path)) == []

    def test_list_sorted_by_run_id(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r2", command_name="build")
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        ids = [r["run_id"] for r in RM.list_run_manifests(str(tmp_path))]
        assert ids == sorted(ids)
        assert ids == ["r1", "r2"]


# ---------------------------------------------------------------------------
# RUN-kind EvidenceRecord bridge
# ---------------------------------------------------------------------------

class TestRunManifestToEvidenceRecord:
    def test_fresh_in_progress_is_pending_retry_and_partial(self, tmp_path: Path) -> None:
        """PROV-CANONICAL (7d9b8251): a JUST-started (fresh updated_at) run
        is genuinely live -- PENDING_RETRY, not UNAVAILABLE (which is now
        reserved for a STALE in_progress receipt; see the next test)."""
        rec = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        evidence = RM.run_manifest_to_evidence_record(rec)
        assert evidence.identity.kind == RE.EvidenceKind.RUN
        assert evidence.identity.id == "run:r1"
        assert evidence.resolver.status == RE.ResolverStatus.PENDING_RETRY
        assert evidence.partial is True
        assert not evidence.is_authoritative

    def test_stale_in_progress_is_unavailable_and_partial(self, tmp_path: Path) -> None:
        """PROV-CANONICAL (7d9b8251): an in_progress receipt whose
        updated_at is long in the past (crashed/orphaned run, never
        finalized) is UNAVAILABLE -- distinct from a fresh one."""
        rec = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec["updated_at"] = "2020-01-01T00:00:00+00:00"
        evidence = RM.run_manifest_to_evidence_record(rec)
        assert evidence.resolver.status == RE.ResolverStatus.UNAVAILABLE
        assert evidence.partial is True

    def test_in_progress_with_unparseable_timestamp_fails_closed_to_unavailable(
        self, tmp_path: Path,
    ) -> None:
        """No parseable liveness signal -> treated as NOT alive (fail
        closed), never optimistically assumed to be PENDING_RETRY."""
        rec = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec["updated_at"] = "not-a-timestamp"
        rec["created_at"] = "also-not-a-timestamp"
        evidence = RM.run_manifest_to_evidence_record(rec)
        assert evidence.resolver.status == RE.ResolverStatus.UNAVAILABLE

    def test_clean_complete_is_verified_and_not_partial(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec = RM.finalize_run_manifest(str(tmp_path), "r1", status="complete")
        evidence = RM.run_manifest_to_evidence_record(rec)
        assert evidence.resolver.status == RE.ResolverStatus.VERIFIED
        assert evidence.partial is False
        assert evidence.is_authoritative

    def test_failed_is_failed_status(self, tmp_path: Path) -> None:
        """PROV-CANONICAL (7d9b8251): a reported-failed run is the new
        first-class ResolverStatus.FAILED, not DEGRADED (which pre-dates
        this item and conflated "terminally failed" with "usable but
        imperfect")."""
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec = RM.finalize_run_manifest(str(tmp_path), "r1", status="failed", reason="boom")
        evidence = RM.run_manifest_to_evidence_record(rec)
        assert evidence.resolver.status == RE.ResolverStatus.FAILED
        assert evidence.partial is True
        assert evidence.partial_reason == "boom"

    def test_partial_from_missing_output_is_ambiguous(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        rec = RM.finalize_run_manifest(
            str(tmp_path), "r1",
            output_paths=[str(tmp_path / "ghost.csv")], status="complete",
        )
        evidence = RM.run_manifest_to_evidence_record(rec)
        assert evidence.resolver.status == RE.ResolverStatus.AMBIGUOUS
        assert evidence.partial is True

    def test_attributes_carry_full_manifest(self, tmp_path: Path) -> None:
        rec = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        evidence = RM.run_manifest_to_evidence_record(rec)
        assert evidence.attributes["run_id"] == "r1"
        assert evidence.attributes["manifest_hash"] == rec["manifest_hash"]

    def test_hash_carried_as_evidence_hash(self, tmp_path: Path) -> None:
        rec = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        evidence = RM.run_manifest_to_evidence_record(rec)
        assert evidence.hashes[0].algorithm == "sha256"
        assert evidence.hashes[0].value == rec["manifest_hash"]


# ---------------------------------------------------------------------------
# build_run_manifest_envelope + JSON/XML round trip
# ---------------------------------------------------------------------------

class TestBuildRunManifestEnvelope:
    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RM.RunManifestError):
            RM.build_run_manifest_envelope(str(tmp_path), "nope")

    def test_envelope_has_one_run_record(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        env = RM.build_run_manifest_envelope(str(tmp_path), "r1")
        assert len(env.records) == 1
        assert env.records[0].identity.kind == RE.EvidenceKind.RUN

    def test_json_round_trip_lossless(self, tmp_path: Path) -> None:
        RM.start_run_manifest(
            str(tmp_path), run_id="r1", command_name="build", command_args={"a": 1},
        )
        RM.finalize_run_manifest(str(tmp_path), "r1", status="complete")
        env = RM.build_run_manifest_envelope(str(tmp_path), "r1")
        payload = RE.serialize_provenance_envelope(env, format="json")
        round_tripped = RE.parse_provenance_envelope(payload, format="json")
        assert RE.envelope_to_dict(round_tripped) == RE.envelope_to_dict(env)

    def test_xml_round_trip_lossless(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        env = RM.build_run_manifest_envelope(str(tmp_path), "r1")
        payload = RE.serialize_provenance_envelope(env, format="xml")
        round_tripped = RE.parse_provenance_envelope(payload, format="xml")
        assert RE.envelope_to_dict(round_tripped) == RE.envelope_to_dict(env)

    def test_canonical_hash_stable_for_identical_envelope(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        env = RM.build_run_manifest_envelope(
            str(tmp_path), "r1", envelope_id="fixed-id",
            generated_at="2026-01-01T00:00:00+00:00",
        )
        h1 = RE.canonical_envelope_hash(env)
        env2 = RM.build_run_manifest_envelope(
            str(tmp_path), "r1", envelope_id="fixed-id",
            generated_at="2026-01-01T00:00:00+00:00",
        )
        h2 = RE.canonical_envelope_hash(env2)
        assert h1 == h2

    def test_markdown_projection_flags_non_authoritative(self, tmp_path: Path) -> None:
        RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        env = RM.build_run_manifest_envelope(str(tmp_path), "r1")
        md = env.to_markdown()
        # A fresh in_progress run is PENDING_RETRY (see
        # TestRunManifestToEvidenceRecord.test_fresh_in_progress_is_pending_retry_and_partial)
        # -- still non-authoritative, so still flagged in the projection.
        assert "PENDING_RETRY" in md


# ---------------------------------------------------------------------------
# capture_git_state
# ---------------------------------------------------------------------------

class TestCaptureGitState:
    def test_success(self, tmp_path: Path) -> None:
        def fake_run(argv: list[str]) -> "str | None":
            if argv[:2] == ["git", "rev-parse"]:
                return "abc123\n"
            if argv[:2] == ["git", "status"]:
                return " M file1.py\n?? file2.py\n"
            return None
        state = RM.capture_git_state(str(tmp_path), run=fake_run)
        assert state["head"] == "abc123"
        assert state["dirty_files"] == ["file1.py", "file2.py"]

    def test_failure_degrades_to_none(self, tmp_path: Path) -> None:
        state = RM.capture_git_state(str(tmp_path), run=lambda argv: None)
        assert state == {"head": None, "dirty_files": []}


# ---------------------------------------------------------------------------
# Bounds snapshot never raises
# ---------------------------------------------------------------------------

class TestSnapshotBounds:
    def test_never_raises_on_internal_shape_change(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from meridian_outputs import outputs_local as OL
        monkeypatch.delattr(OL, "_resolve_max_workers", raising=False)
        rec = RM.start_run_manifest(str(tmp_path), run_id="r1", command_name="build")
        assert rec["bounds"]["max_workers"] is None


# ---------------------------------------------------------------------------
# End-to-end fixture-tree smoke test -- local MCP path, no tunnel required
# ---------------------------------------------------------------------------

class TestFixtureTreeSmoke:
    def test_full_lifecycle_against_a_real_fixture_tree(self, tmp_path: Path) -> None:
        script = _write(tmp_path / "scripts" / "train.py", "print('hi')\n")
        input_csv = _write(tmp_path / "inputs" / "data.csv", "a,b\n1,2\n")
        output_csv = _write(tmp_path / "outputs" / "result.csv", "x\n42\n")

        art = AR.register_artifact(
            str(tmp_path), "output", canonical_path=output_csv,
            generator=script, source_locator="result.csv",
        )

        started = RM.start_run_manifest(
            str(tmp_path), run_id="run-42", command_name="build_outputs",
            project_id="proj-a", version="v1", repo_dir=str(tmp_path),
            input_paths=[input_csv], expected_counts={"rows": 1},
            _git_runner=lambda argv: None,  # not a real git repo
        )
        assert started["phase"] == "in_progress"

        finished = RM.finalize_run_manifest(
            str(tmp_path), "run-42", output_paths=[output_csv],
            artifact_ids=[art["artifact_id"]], status="complete",
        )
        assert finished["phase"] == "complete"
        assert finished["manifest_hash"] == started["manifest_hash"]
        assert finished["artifact_ids"] == [art["artifact_id"]]

        env = RM.build_run_manifest_envelope(str(tmp_path), "run-42")
        assert env.records[0].is_authoritative

        payload = RE.serialize_provenance_envelope(env, format="json")
        parsed = RE.parse_provenance_envelope(payload, format="json")
        assert RE.envelope_to_dict(parsed) == RE.envelope_to_dict(env)

        # Interruption / resumability: a fresh process re-reading the ledger
        # sees the SAME finalized record without redoing any work.
        reread = RM.get_run_manifest(str(tmp_path), "run-42")
        assert reread == finished
