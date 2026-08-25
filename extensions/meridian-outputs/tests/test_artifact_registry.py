"""Tests for meridian_outputs.artifact_registry (sprint item e1c979e3).

Covers:
  - compute_artifact_id: deterministic, relocation-safe, fails closed with no
    portable anchor.
  - register_artifact: create/update semantics, redacted local-path storage,
    hash-mismatch refusal (fresh-hash and on-file-hash), lifecycle validation.
  - resolve_artifact: id lookup, content-hash resolution, exact local-path
    resolution, explicit ambiguous outcomes (never a basename guess),
    hash_mismatch, orphaned vs unresolved distinction.
  - verify_artifact_hash: never silently "verified" with nothing to compare.
  - lifecycle transitions, source<->artifact edges (both directions).
  - reconcile_legacy_outputs: dry-run preview vs real registration, already-
    registered detection, ambiguous/unanchored entries never registered.
  - strip_local_metadata: absolute paths never leak into the shareable view.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import artifact_registry as AR
from meridian_outputs import annotate


def _write(path: Path, content: str = "hello") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# compute_artifact_id
# ---------------------------------------------------------------------------

class TestComputeArtifactId:
    def test_deterministic_same_inputs_same_id(self) -> None:
        a = AR.compute_artifact_id("figure", content_hash="abc123", generator="gen.py")
        b = AR.compute_artifact_id("figure", content_hash="abc123", generator="gen.py")
        assert a == b

    def test_relocation_safe_no_path_input_possible(self) -> None:
        # The function signature has no path parameter at all -- the id is
        # structurally incapable of depending on where the file sits.
        a = AR.compute_artifact_id("figure", content_hash="abc123")
        b = AR.compute_artifact_id("figure", content_hash="abc123")
        assert a == b

    def test_different_content_hash_different_id(self) -> None:
        a = AR.compute_artifact_id("figure", content_hash="abc123")
        b = AR.compute_artifact_id("figure", content_hash="def456")
        assert a != b

    def test_different_kind_different_id(self) -> None:
        a = AR.compute_artifact_id("figure", content_hash="abc123")
        b = AR.compute_artifact_id("table", content_hash="abc123")
        assert a != b

    def test_no_anchor_raises(self) -> None:
        with pytest.raises(AR.RegistryError):
            AR.compute_artifact_id("figure")

    def test_missing_kind_raises(self) -> None:
        with pytest.raises(AR.RegistryError):
            AR.compute_artifact_id("", content_hash="abc123")

    def test_generator_alone_is_a_valid_anchor(self) -> None:
        assert AR.compute_artifact_id("output", generator="run.py")

    def test_source_locator_alone_is_a_valid_anchor(self) -> None:
        assert AR.compute_artifact_id("output", source_locator="sprint-item-123")


# ---------------------------------------------------------------------------
# register_artifact
# ---------------------------------------------------------------------------

class TestRegisterArtifact:
    def test_missing_outputs_dir_raises(self) -> None:
        with pytest.raises(AR.RegistryError):
            AR.register_artifact("", "figure", generator="g.py")

    def test_missing_kind_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AR.RegistryError):
            AR.register_artifact(str(tmp_path), "", generator="g.py")

    def test_invalid_lifecycle_state_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AR.RegistryError):
            AR.register_artifact(
                str(tmp_path), "figure", generator="g.py", lifecycle_state="on_fire",
            )

    def test_no_portable_anchor_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AR.RegistryError):
            AR.register_artifact(str(tmp_path), "figure")

    def test_basic_registration_by_generator(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        assert rec["created"] is True
        assert rec["kind"] == "figure"
        assert rec["generator"] == "plot.py"
        assert rec["lifecycle_state"] == AR.ACTIVE
        assert rec["artifact_id"]
        assert rec["local_paths"] == []

    def test_registration_with_real_file_computes_hash(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "fig1.png", "binary-ish content")
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
        )
        assert rec["content_hash"]
        assert rec["hash_verified"] is True
        assert len(rec["local_paths"]) == 1
        assert rec["local_paths"][0]["local_only_path"] == fig
        assert rec["local_paths"][0]["basename"] == "fig1.png"

    def test_local_path_stored_only_as_redacted_metadata(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "sensitive" / "fig1.png", "content")
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
        )
        # artifact_id must not encode the path in any recoverable way -- it's
        # a uuid5 derived only from kind/hash/generator/source_locator.
        assert fig not in rec["artifact_id"]
        # the raw path IS present, but only inside local_paths (the
        # explicitly redacted bucket) -- confirmed removed by
        # strip_local_metadata below.
        clean = AR.strip_local_metadata(rec)
        assert "local_only_path" not in clean["local_paths"][0]
        assert clean["local_paths"][0]["basename"] == "fig1.png"

    def test_reregistration_same_identity_updates_not_duplicates(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "fig1.png", "same-content")
        first = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
        )
        second = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
            metadata={"note": "second pass"},
        )
        assert first["artifact_id"] == second["artifact_id"]
        assert second["created"] is False
        assert second["metadata"]["note"] == "second pass"
        assert len(second["local_paths"]) == 1  # same path -> updated sighting, not duplicated

    def test_relocation_same_content_same_id_new_local_path_recorded(self, tmp_path: Path) -> None:
        original = _write(tmp_path / "run1" / "fig1.png", "identical-bytes")
        moved = _write(tmp_path / "archive" / "fig1_copy.png", "identical-bytes")

        first = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=original, generator="plot.py",
        )
        second = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=moved, generator="plot.py",
        )
        assert first["artifact_id"] == second["artifact_id"]
        assert len(second["local_paths"]) == 2

    def test_expected_sha256_mismatch_against_fresh_hash_refuses(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "fig1.png", "actual-content")
        with pytest.raises(AR.RegistryError):
            AR.register_artifact(
                str(tmp_path), "figure", canonical_path=fig,
                expected_sha256="0" * 64, generator="plot.py",
            )

    def test_mutated_content_at_same_path_mints_a_new_identity(self, tmp_path: Path) -> None:
        # Because content_hash is itself baked into the artifact_id seed,
        # editing a file in place produces a NEW id on re-registration rather
        # than silently overwriting the old identity's hash -- confirms the
        # id scheme can never let a mutated-content re-registration corrupt
        # a previously-registered identity's on-file hash.
        path = tmp_path / "fig.png"
        _write(path, "content-A")
        first = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=str(path), generator="plot.py",
        )
        _write(path, "content-B")
        second = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=str(path), generator="plot.py",
        )
        assert first["artifact_id"] != second["artifact_id"]
        # The original record's own hash is untouched.
        original = AR.get_artifact(str(tmp_path), first["artifact_id"])
        assert original["content_hash"] != second["content_hash"]


# ---------------------------------------------------------------------------
# resolve_artifact
# ---------------------------------------------------------------------------

class TestResolveArtifact:
    def test_no_args_is_unresolved(self, tmp_path: Path) -> None:
        result = AR.resolve_artifact(str(tmp_path))
        assert result["status"] == AR.UNRESOLVED

    def test_unknown_artifact_id_is_orphaned(self, tmp_path: Path) -> None:
        result = AR.resolve_artifact(str(tmp_path), artifact_id="not-a-real-id")
        assert result["status"] == AR.ORPHANED
        assert result["record"] is None

    def test_resolve_by_artifact_id(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        result = AR.resolve_artifact(str(tmp_path), artifact_id=rec["artifact_id"])
        assert result["status"] == AR.RESOLVED
        assert result["evidence"] == "artifact_id"
        assert result["record"]["artifact_id"] == rec["artifact_id"]

    def test_resolve_by_artifact_id_hash_mismatch(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "fig.png", "real-content")
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
        )
        result = AR.resolve_artifact(
            str(tmp_path), artifact_id=rec["artifact_id"], expected_sha256="0" * 64,
        )
        assert result["status"] == AR.HASH_MISMATCH

    def test_resolve_by_content_hash(self, tmp_path: Path) -> None:
        original = _write(tmp_path / "run1" / "fig.png", "shared-bytes")
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=original, generator="plot.py",
        )
        # Query from a DIFFERENT path with identical content -- never
        # registered at that path before, so this can only resolve via
        # content hash, never via basename.
        moved = _write(tmp_path / "elsewhere" / "renamed.png", "shared-bytes")
        result = AR.resolve_artifact(str(tmp_path), canonical_path=moved)
        assert result["status"] == AR.RESOLVED
        assert result["evidence"] == "content_hash"
        assert result["artifact_id"] == rec["artifact_id"]

    def test_resolve_by_exact_local_path_sighting(self, tmp_path: Path) -> None:
        # Register with a generator anchor only (no file on disk at register
        # time), then resolve by the exact same local path once it exists.
        placeholder = tmp_path / "fig.png"
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=str(placeholder), generator="plot.py",
        )
        result = AR.resolve_artifact(str(tmp_path), canonical_path=str(placeholder))
        assert result["status"] == AR.RESOLVED
        assert result["evidence"] == "local_path_exact"
        assert result["artifact_id"] == rec["artifact_id"]

    def test_ambiguous_content_hash_lists_every_candidate(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path / "f1.png", "same-bytes")
        f2 = _write(tmp_path / "f2.png", "same-bytes")
        r1 = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=f1, generator="gen-a.py",
        )
        r2 = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=f2, generator="gen-b.py",
        )
        assert r1["artifact_id"] != r2["artifact_id"]

        probe = _write(tmp_path / "probe.png", "same-bytes")
        result = AR.resolve_artifact(str(tmp_path), canonical_path=probe)
        assert result["status"] == AR.AMBIGUOUS
        assert result["record"] is None
        assert sorted(result["candidates"]) == sorted([r1["artifact_id"], r2["artifact_id"]])

    def test_never_falls_back_to_basename_guess(self, tmp_path: Path) -> None:
        # Register fig.png with real, unique content.
        original = _write(tmp_path / "run1" / "fig.png", "unique-bytes-A")
        AR.register_artifact(
            str(tmp_path), "figure", canonical_path=original, generator="plot.py",
        )
        # Query a DIFFERENT fig.png (same basename, different content, never
        # sighted at this exact path before) -- a basename-fuzzy resolver
        # would incorrectly match this to the same artifact; this one must not.
        different = _write(tmp_path / "run2" / "fig.png", "totally-different-bytes-B")
        result = AR.resolve_artifact(str(tmp_path), canonical_path=different)
        assert result["status"] == AR.UNRESOLVED

    def test_unresolved_path_never_seen(self, tmp_path: Path) -> None:
        never_seen = _write(tmp_path / "ghost.png", "nobody-registered-this")
        result = AR.resolve_artifact(str(tmp_path), canonical_path=never_seen)
        assert result["status"] == AR.UNRESOLVED
        assert result["record"] is None


# ---------------------------------------------------------------------------
# verify_artifact_hash
# ---------------------------------------------------------------------------

class TestVerifyArtifactHash:
    def test_unknown_artifact_id(self, tmp_path: Path) -> None:
        result = AR.verify_artifact_hash(str(tmp_path), "nope")
        assert result["verified"] is False

    def test_unreadable_path_never_reports_verified(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        result = AR.verify_artifact_hash(str(tmp_path), rec["artifact_id"], path=str(tmp_path / "x.png"))
        # x.png doesn't exist -> current_hash can't be computed -> never
        # silently reported as verified.
        assert result["verified"] is False
        assert result["current_hash"] is None

    def test_verified_true_when_content_unchanged(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "fig.png", "stable-content")
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
        )
        result = AR.verify_artifact_hash(str(tmp_path), rec["artifact_id"])
        assert result["verified"] is True
        assert result["current_hash"] == result["registered_hash"]

    def test_verified_false_when_content_changed(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "fig.png", "original-content")
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
        )
        _write(tmp_path / "fig.png", "mutated-content")
        result = AR.verify_artifact_hash(str(tmp_path), rec["artifact_id"])
        assert result["verified"] is False
        assert result["current_hash"] != result["registered_hash"]

    def test_no_path_available_at_all(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        result = AR.verify_artifact_hash(str(tmp_path), rec["artifact_id"])
        assert result["verified"] is False
        assert result["path"] is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_set_lifecycle_state(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        updated = AR.set_lifecycle_state(
            str(tmp_path), rec["artifact_id"], AR.QUARANTINED, reason="stale content",
        )
        assert updated["lifecycle_state"] == AR.QUARANTINED
        assert updated["lifecycle_history"][-1]["from"] == AR.ACTIVE
        assert updated["lifecycle_history"][-1]["to"] == AR.QUARANTINED
        assert updated["lifecycle_history"][-1]["reason"] == "stale content"

    def test_invalid_lifecycle_state_raises(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        with pytest.raises(AR.RegistryError):
            AR.set_lifecycle_state(str(tmp_path), rec["artifact_id"], "bogus")

    def test_unknown_artifact_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AR.RegistryError):
            AR.set_lifecycle_state(str(tmp_path), "nope", AR.DEPRECATED)

    def test_list_artifacts_filters(self, tmp_path: Path) -> None:
        fig = AR.register_artifact(str(tmp_path), "figure", generator="g1.py")
        AR.register_artifact(str(tmp_path), "table", generator="g2.py")
        AR.set_lifecycle_state(str(tmp_path), fig["artifact_id"], AR.QUARANTINED)

        all_rows = AR.list_artifacts(str(tmp_path))
        assert len(all_rows) == 2
        figures = AR.list_artifacts(str(tmp_path), kind="figure")
        assert len(figures) == 1
        quarantined = AR.list_artifacts(str(tmp_path), lifecycle_state=AR.QUARANTINED)
        assert len(quarantined) == 1


# ---------------------------------------------------------------------------
# Source <-> artifact edges
# ---------------------------------------------------------------------------

class TestSourceEdges:
    def test_bind_edge_requires_registered_artifact(self, tmp_path: Path) -> None:
        with pytest.raises(AR.RegistryError):
            AR.bind_source_edge(str(tmp_path), "not-registered", "data.csv")

    def test_bind_edge_requires_source_locator(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        with pytest.raises(AR.RegistryError):
            AR.bind_source_edge(str(tmp_path), rec["artifact_id"], "")

    def test_bind_and_query_both_directions(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        edge = AR.bind_source_edge(
            str(tmp_path), rec["artifact_id"], "data/raw.csv", relation="derived_from",
        )
        assert edge["artifact_id"] == rec["artifact_id"]
        assert edge["source_locator"] == "data/raw.csv"

        from_artifact = AR.get_artifact_sources(str(tmp_path), rec["artifact_id"])
        assert len(from_artifact) == 1
        assert from_artifact[0]["source_locator"] == "data/raw.csv"

        from_source = AR.get_source_artifacts(str(tmp_path), "data/raw.csv")
        assert len(from_source) == 1
        assert from_source[0]["artifact_id"] == rec["artifact_id"]

    def test_idempotent_rebind_updates_metadata_not_duplicate(self, tmp_path: Path) -> None:
        rec = AR.register_artifact(str(tmp_path), "figure", generator="plot.py")
        AR.bind_source_edge(str(tmp_path), rec["artifact_id"], "data/raw.csv")
        AR.bind_source_edge(
            str(tmp_path), rec["artifact_id"], "data/raw.csv", metadata={"note": "v2"},
        )
        edges = AR.get_artifact_sources(str(tmp_path), rec["artifact_id"])
        assert len(edges) == 1
        assert edges[0]["metadata"]["note"] == "v2"


# ---------------------------------------------------------------------------
# reconcile_legacy_outputs
# ---------------------------------------------------------------------------

class TestReconcileLegacyOutputs:
    def test_empty_entries_empty_report(self, tmp_path: Path) -> None:
        report = AR.reconcile_legacy_outputs(str(tmp_path), [])
        assert report["scanned"] == 0
        assert report["would_register"] == []

    def test_dry_run_previews_without_writing(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "legacy.png", "legacy-bytes")
        entries = [{"kind": "figure", "canonical_path": fig, "generator": "old_script.py"}]
        report = AR.reconcile_legacy_outputs(str(tmp_path), entries, dry_run=True)
        assert report["dry_run"] is True
        assert len(report["would_register"]) == 1
        # Nothing was actually written.
        assert AR.list_artifacts(str(tmp_path)) == []

    def test_real_run_registers_and_is_idempotent(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "legacy.png", "legacy-bytes")
        entries = [{"kind": "figure", "canonical_path": fig, "generator": "old_script.py"}]
        report = AR.reconcile_legacy_outputs(str(tmp_path), entries, dry_run=False)
        assert len(report["registered"]) == 1
        assert len(AR.list_artifacts(str(tmp_path))) == 1

        # Running again finds it already registered, doesn't duplicate.
        report2 = AR.reconcile_legacy_outputs(str(tmp_path), entries, dry_run=False)
        assert report2["already_registered"] == report["registered"]
        assert report2["registered"] == []
        assert len(AR.list_artifacts(str(tmp_path))) == 1

    def test_unanchored_entry_is_skipped_never_registered(self, tmp_path: Path) -> None:
        entries = [{"kind": "figure", "canonical_path": None}]
        report = AR.reconcile_legacy_outputs(str(tmp_path), entries, dry_run=False)
        assert len(report["skipped_unanchored"]) == 1
        assert report["registered"] == []
        assert AR.list_artifacts(str(tmp_path)) == []

    def test_ambiguous_entry_never_registered(self, tmp_path: Path) -> None:
        f1 = _write(tmp_path / "f1.png", "dup-bytes")
        f2 = _write(tmp_path / "f2.png", "dup-bytes")
        AR.register_artifact(str(tmp_path), "figure", canonical_path=f1, generator="gen-a.py")
        AR.register_artifact(str(tmp_path), "figure", canonical_path=f2, generator="gen-b.py")

        probe = _write(tmp_path / "probe.png", "dup-bytes")
        entries = [{"kind": "figure", "canonical_path": probe, "generator": "gen-c.py"}]
        report = AR.reconcile_legacy_outputs(str(tmp_path), entries, dry_run=False)
        assert len(report["ambiguous"]) == 1
        assert report["registered"] == []

    def test_defaults_to_provenance_ledger_when_no_entries_given(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "legacy.png", "legacy-bytes")
        annotate.record_provenance(str(tmp_path), fig, generating_script="old_script.py")
        report = AR.reconcile_legacy_outputs(str(tmp_path), dry_run=True)
        assert report["scanned"] == 1
        assert len(report["would_register"]) == 1


# ---------------------------------------------------------------------------
# strip_local_metadata
# ---------------------------------------------------------------------------

class TestStripLocalMetadata:
    def test_removes_local_only_path_keeps_basename(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "dir" / "fig.png", "content")
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
        )
        clean = AR.strip_local_metadata(rec)
        assert all("local_only_path" not in e for e in clean["local_paths"])
        assert clean["local_paths"][0]["basename"] == "fig.png"
        assert clean["artifact_id"] == rec["artifact_id"]

    def test_does_not_mutate_input(self, tmp_path: Path) -> None:
        fig = _write(tmp_path / "fig.png", "content")
        rec = AR.register_artifact(
            str(tmp_path), "figure", canonical_path=fig, generator="plot.py",
        )
        AR.strip_local_metadata(rec)
        assert "local_only_path" in rec["local_paths"][0]
