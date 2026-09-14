"""Tests for meridian/temp_artifacts.py (sprint item c4c74141, source
proposal a08defbc-481f-4354-915d-462a254e5e75).

Covers:
  - script_content_hash -- fail-soft SHA-256, same algorithm/shape as
    meridian_outputs.fingerprint.script_content_hash.
  - default_registry_root -- project-local + gitignored by default; falls
    back EXPLICITLY (never silently) to a machine-local temp dir when the
    project-local candidate resolves under a OneDrive-synced location.
  - is_local_absolute_path / to_shared_safe_pointer / assert_shared_safe --
    the local-manifest-vs-shared-Meridian-state path boundary.
  - inspect_registry / quarantine_corrupt_registry /
    scan_for_incomplete_writes -- corrupt/partial manifest DETECTION and
    PRESERVATION (never a silent overwrite/discard), restart-safety.
  - register_artifact / get_artifact / list_artifacts -- versioned,
    atomic writes.
  - supersede_artifact / retire_artifact -- lifecycle transitions that
    never delete an entry.
  - check_artifact / check_all -- present_current / missing / changed /
    superseded / retired / unreadable / never_verified; read-only, never
    executes or deletes the checked script.
  - cross_reference_outputs_fingerprint -- soft-import composition point
    (this test environment does not have meridian_outputs importable,
    which is itself proof the "no hard dependency" contract holds --
    extensions/meridian-outputs/tests/test_temp_artifacts_reconciliation.py
    covers the case where it IS importable).
  - A small CLI smoke test.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from meridian import temp_artifacts as TA


def _onedrive_env_cleared(monkeypatch) -> None:
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# script_content_hash
# ---------------------------------------------------------------------------

class TestScriptContentHash:
    def test_hashes_real_file(self, tmp_path):
        p = tmp_path / "s.py"
        p.write_text("x = 1\n", encoding="utf-8")
        expected = hashlib.sha256(p.read_bytes()).hexdigest()
        assert TA.script_content_hash(str(p)) == expected

    def test_missing_file_returns_none(self, tmp_path):
        assert TA.script_content_hash(str(tmp_path / "nope.py")) is None

    def test_empty_or_none_path_returns_none(self):
        assert TA.script_content_hash("") is None
        assert TA.script_content_hash(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# default_registry_root
# ---------------------------------------------------------------------------

class TestDefaultRegistryRoot:
    def test_normal_project_local_root_created_and_gitignored(self, tmp_path, monkeypatch):
        _onedrive_env_cleared(monkeypatch)
        info = TA.default_registry_root(str(tmp_path))
        assert info["used_fallback"] is False
        assert info["reason"] is None
        assert os.path.normcase(info["root"]) == os.path.normcase(str(tmp_path / ".meridian-temp-artifacts"))
        assert os.path.isdir(info["root"])
        gi = tmp_path / ".gitignore"
        assert gi.is_file()
        assert "/.meridian-temp-artifacts/" in gi.read_text(encoding="utf-8")

    def test_gitignore_entry_appended_not_duplicated(self, tmp_path, monkeypatch):
        _onedrive_env_cleared(monkeypatch)
        TA.default_registry_root(str(tmp_path))
        TA.default_registry_root(str(tmp_path))
        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert content.count("/.meridian-temp-artifacts/") == 1

    def test_onedrive_project_falls_back_explicitly(self, tmp_path, monkeypatch):
        onedrive_root = tmp_path / "OneDriveRoot"
        onedrive_root.mkdir()
        monkeypatch.setenv("OneDrive", str(onedrive_root))
        monkeypatch.delenv("OneDriveCommercial", raising=False)
        monkeypatch.delenv("OneDriveConsumer", raising=False)
        project = onedrive_root / "MyProject"
        project.mkdir()

        info = TA.default_registry_root(str(project))

        assert info["used_fallback"] is True
        assert info["reason"] is not None and "OneDrive" in info["reason"]
        assert not os.path.normcase(info["root"]).startswith(os.path.normcase(str(onedrive_root)))
        assert os.path.isdir(info["root"])

    def test_defaults_to_cwd_when_no_project_root_given(self, tmp_path, monkeypatch):
        _onedrive_env_cleared(monkeypatch)
        monkeypatch.chdir(tmp_path)
        info = TA.default_registry_root()
        assert os.path.normcase(info["project_root"]) == os.path.normcase(str(tmp_path))

    def test_always_surfaces_root_explicitly(self, tmp_path, monkeypatch):
        """The contract this item calls out by name: never a silent,
        undiscoverable default -- the resolved root must always come back
        to the caller."""
        _onedrive_env_cleared(monkeypatch)
        info = TA.default_registry_root(str(tmp_path))
        assert isinstance(info["root"], str) and info["root"]


# ---------------------------------------------------------------------------
# Local-vs-shared path boundary.
# ---------------------------------------------------------------------------

class TestSharedSafeBoundary:
    def test_windows_drive_path_is_local_absolute(self):
        assert TA.is_local_absolute_path(r"C:\Users\alice\scratch\fix.py") is True

    def test_posix_home_path_is_local_absolute(self):
        assert TA.is_local_absolute_path("/home/alice/scratch/fix.py") is True

    def test_relative_path_is_not_local_absolute(self):
        assert TA.is_local_absolute_path("scripts/fix.py") is False

    def test_empty_is_not_local_absolute(self):
        assert TA.is_local_absolute_path("") is False

    def test_to_shared_safe_pointer_project_relative(self, tmp_path):
        project = tmp_path / "repo"
        script = project / "scripts" / "fix.py"
        script.parent.mkdir(parents=True)
        script.write_text("x = 1\n", encoding="utf-8")

        result = TA.to_shared_safe_pointer(str(script), project_root=str(project))

        assert result["portable"] is True
        assert result["pointer"] == "scripts/fix.py"
        assert result["reason"] is None

    def test_to_shared_safe_pointer_redacts_outside_project(self, tmp_path):
        outside = tmp_path / "elsewhere" / "fix.py"
        outside.parent.mkdir(parents=True)
        outside.write_text("x = 1\n", encoding="utf-8")
        content_hash = TA.script_content_hash(str(outside))

        result = TA.to_shared_safe_pointer(
            str(outside), project_root=str(tmp_path / "repo"), content_hash=content_hash,
        )

        assert result["portable"] is False
        assert result["pointer"].startswith("<redacted-local-path:fix.py>")
        assert content_hash in result["pointer"]
        assert str(outside) not in result["pointer"]

    def test_to_shared_safe_pointer_relative_input_passthrough(self):
        result = TA.to_shared_safe_pointer("scripts/fix.py")
        assert result == {"pointer": "scripts/fix.py", "portable": True, "reason": None}

    def test_assert_shared_safe_raises_on_absolute_path(self):
        with pytest.raises(TA.TempArtifactError):
            TA.assert_shared_safe(r"C:\Users\alice\scratch\fix.py")

    def test_assert_shared_safe_allows_portable_pointer(self):
        TA.assert_shared_safe("scripts/fix.py")  # must not raise
        TA.assert_shared_safe("<redacted-local-path:fix.py>#sha256:abc123")  # must not raise


# ---------------------------------------------------------------------------
# Registry file I/O: atomic writes, corrupt-manifest detection+preservation.
# ---------------------------------------------------------------------------

class TestRegistryFileIntegrity:
    def test_inspect_registry_missing_file(self, tmp_path):
        status = TA.inspect_registry(str(tmp_path))
        assert status == {
            "path": str(tmp_path / "temp_artifact_registry.json"),
            "exists": False, "corrupt": False, "reason": None, "entry_count": 0,
        }

    def test_register_then_inspect_reports_valid(self, tmp_path):
        root = str(tmp_path)
        TA.register_artifact(root, name="a", script_path=str(tmp_path / "a.py"))
        status = TA.inspect_registry(root)
        assert status["exists"] is True
        assert status["corrupt"] is False
        assert status["entry_count"] == 1

    def test_register_write_is_atomic_no_leftover_tmp(self, tmp_path):
        root = str(tmp_path)
        TA.register_artifact(root, name="a", script_path=str(tmp_path / "a.py"))
        assert not os.path.isfile(os.path.join(root, "temp_artifact_registry.json.tmp"))

    def test_corrupt_json_is_detected_not_overwritten(self, tmp_path):
        registry_file = tmp_path / "temp_artifact_registry.json"
        registry_file.write_text("{not valid json!!", encoding="utf-8")

        status = TA.inspect_registry(str(tmp_path))
        assert status["corrupt"] is True

        with pytest.raises(TA.CorruptManifestError):
            TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        with pytest.raises(TA.CorruptManifestError):
            TA.check_all(str(tmp_path))

        # The corrupt file itself must be byte-for-byte UNTOUCHED by either
        # of the failed calls above.
        assert registry_file.read_text(encoding="utf-8") == "{not valid json!!"

    def test_missing_entries_key_is_treated_as_corrupt(self, tmp_path):
        (tmp_path / "temp_artifact_registry.json").write_text(
            json.dumps({"schema_version": 1}), encoding="utf-8",
        )
        status = TA.inspect_registry(str(tmp_path))
        assert status["corrupt"] is True
        assert "entries" in status["reason"]

    def test_quarantine_corrupt_registry_moves_it_aside_and_allows_fresh_write(self, tmp_path):
        registry_file = tmp_path / "temp_artifact_registry.json"
        registry_file.write_text("not json", encoding="utf-8")

        result = TA.quarantine_corrupt_registry(str(tmp_path))

        assert not registry_file.exists()
        quarantined = Path(result["quarantined_path"])
        assert quarantined.is_file()
        assert quarantined.read_text(encoding="utf-8") == "not json"

        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        assert entry["lifecycle"] == TA.ACTIVE
        assert TA.inspect_registry(str(tmp_path))["corrupt"] is False

    def test_quarantine_refuses_when_not_corrupt(self, tmp_path):
        TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        with pytest.raises(TA.TempArtifactError):
            TA.quarantine_corrupt_registry(str(tmp_path))

    def test_quarantine_refuses_when_missing(self, tmp_path):
        with pytest.raises(TA.TempArtifactError):
            TA.quarantine_corrupt_registry(str(tmp_path))

    def test_scan_for_incomplete_writes_detects_leftover_tmp_without_deleting(self, tmp_path):
        root = str(tmp_path)
        TA.register_artifact(root, name="a", script_path=str(tmp_path / "a.py"))
        tmp_sibling = tmp_path / "temp_artifact_registry.json.tmp"
        tmp_sibling.write_text("{partial", encoding="utf-8")

        result = TA.scan_for_incomplete_writes(root)
        assert result["incomplete_write_detected"] is True
        assert tmp_sibling.is_file()  # never auto-deleted

        # The COMMITTED registry is unaffected by the stray tmp fossil.
        assert TA.inspect_registry(root)["corrupt"] is False
        assert len(TA.list_artifacts(root)) == 1

    def test_scan_for_incomplete_writes_clean_state(self, tmp_path):
        result = TA.scan_for_incomplete_writes(str(tmp_path))
        assert result["incomplete_write_detected"] is False
        assert result["note"] is None

    def test_registry_version_increments_across_writes(self, tmp_path):
        root = str(tmp_path)
        TA.register_artifact(root, name="a", script_path=str(tmp_path / "a.py"))
        with open(os.path.join(root, "temp_artifact_registry.json"), encoding="utf-8") as fh:
            v1 = json.load(fh)["registry_version"]
        TA.register_artifact(root, name="b", script_path=str(tmp_path / "b.py"))
        with open(os.path.join(root, "temp_artifact_registry.json"), encoding="utf-8") as fh:
            v2 = json.load(fh)["registry_version"]
        assert v2 == v1 + 1


# ---------------------------------------------------------------------------
# register_artifact / get_artifact / list_artifacts
# ---------------------------------------------------------------------------

class TestRegisterAndList:
    def test_register_minimal(self, tmp_path):
        script = tmp_path / "fix.py"
        script.write_text("print('hi')\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path / "registry"), name="fix", script_path=str(script))

        assert entry["name"] == "fix"
        assert entry["lifecycle"] == TA.ACTIVE
        assert entry["version"] == 1
        assert entry["script_sha256"] == TA.script_content_hash(str(script))
        assert entry["environment"]["python_version"]
        assert entry["environment"]["platform"]
        assert entry["created_at"]
        assert entry["last_checked_at"] is None

    def test_register_missing_name_raises(self, tmp_path):
        with pytest.raises(TA.TempArtifactError):
            TA.register_artifact(str(tmp_path), name="", script_path=str(tmp_path / "a.py"))

    def test_register_missing_script_path_raises(self, tmp_path):
        with pytest.raises(TA.TempArtifactError):
            TA.register_artifact(str(tmp_path), name="a", script_path="")

    def test_register_script_not_yet_existing_records_none_hash(self, tmp_path):
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "not_yet.py"))
        assert entry["script_sha256"] is None

    def test_register_inputs_normalized_from_bare_paths(self, tmp_path):
        input_file = tmp_path / "in.csv"
        input_file.write_text("a,b\n", encoding="utf-8")
        entry = TA.register_artifact(
            str(tmp_path), name="a", script_path=str(tmp_path / "a.py"), inputs=[str(input_file)],
        )
        assert entry["inputs"] == [
            {"path": str(input_file), "fingerprint": TA.script_content_hash(str(input_file))}
        ]

    def test_register_inputs_precomputed_fingerprint_preserved(self, tmp_path):
        entry = TA.register_artifact(
            str(tmp_path), name="a", script_path=str(tmp_path / "a.py"),
            inputs=[{"path": "some/logical/ref.csv", "fingerprint": "deadbeef"}],
        )
        assert entry["inputs"] == [{"path": "some/logical/ref.csv", "fingerprint": "deadbeef"}]

    def test_register_outputs_command_owner_expiry_notes_stored_verbatim(self, tmp_path):
        entry = TA.register_artifact(
            str(tmp_path), name="a", script_path=str(tmp_path / "a.py"),
            command="python a.py --x=1", outputs=["out/a.csv"], owner="adam",
            expiry_condition="until the v3 formula lands", notes="impromptu fix",
        )
        assert entry["command"] == "python a.py --x=1"
        assert entry["outputs"] == ["out/a.csv"]
        assert entry["owner"] == "adam"
        assert entry["expiry_condition"] == "until the v3 formula lands"
        assert entry["notes"] == "impromptu fix"

    def test_get_artifact_roundtrip(self, tmp_path):
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        assert TA.get_artifact(str(tmp_path), entry["artifact_id"]) == entry

    def test_get_artifact_unknown_returns_none(self, tmp_path):
        assert TA.get_artifact(str(tmp_path), "nope") is None

    def test_list_artifacts_sorted_newest_first(self, tmp_path):
        e1 = TA.register_artifact(str(tmp_path), name="first", script_path=str(tmp_path / "a.py"))
        time.sleep(0.01)
        e2 = TA.register_artifact(str(tmp_path), name="second", script_path=str(tmp_path / "b.py"))
        rows = TA.list_artifacts(str(tmp_path))
        assert [r["artifact_id"] for r in rows] == [e2["artifact_id"], e1["artifact_id"]]

    def test_list_artifacts_filter_by_lifecycle(self, tmp_path):
        e1 = TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        TA.register_artifact(str(tmp_path), name="b", script_path=str(tmp_path / "b.py"))
        TA.supersede_artifact(str(tmp_path), e1["artifact_id"])
        active_only = TA.list_artifacts(str(tmp_path), lifecycle=TA.ACTIVE)
        assert [r["name"] for r in active_only] == ["b"]


# ---------------------------------------------------------------------------
# supersede_artifact / retire_artifact -- never delete.
# ---------------------------------------------------------------------------

class TestLifecycleTransitions:
    def test_supersede_marks_lifecycle_and_bumps_version(self, tmp_path):
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        updated = TA.supersede_artifact(
            str(tmp_path), entry["artifact_id"], replaced_by="new-id", reason="formula v3",
        )
        assert updated["lifecycle"] == TA.SUPERSEDED
        assert updated["replaced_by"] == "new-id"
        assert updated["version"] == 2
        assert "formula v3" in updated["notes"]
        assert TA.get_artifact(str(tmp_path), entry["artifact_id"])["lifecycle"] == TA.SUPERSEDED

    def test_retire_marks_lifecycle(self, tmp_path):
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        updated = TA.retire_artifact(str(tmp_path), entry["artifact_id"], reason="no longer needed")
        assert updated["lifecycle"] == TA.RETIRED
        assert updated["version"] == 2

    def test_transition_unknown_id_raises(self, tmp_path):
        with pytest.raises(TA.TempArtifactError):
            TA.supersede_artifact(str(tmp_path), "nope")

    def test_entry_never_removed_by_lifecycle_change(self, tmp_path):
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        TA.retire_artifact(str(tmp_path), entry["artifact_id"])
        assert TA.inspect_registry(str(tmp_path))["entry_count"] == 1


# ---------------------------------------------------------------------------
# check_artifact / check_all -- read-only, never deletes/executes.
# ---------------------------------------------------------------------------

class TestCheckArtifact:
    def test_present_current(self, tmp_path):
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))
        result = TA.check_artifact(str(tmp_path), entry["artifact_id"])
        assert result["status"] == TA.STATUS_PRESENT_CURRENT

    def test_missing(self, tmp_path):
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))
        script.unlink()
        result = TA.check_artifact(str(tmp_path), entry["artifact_id"])
        assert result["status"] == TA.STATUS_MISSING

    def test_changed(self, tmp_path):
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))
        script.write_text("x = 2\n", encoding="utf-8")
        result = TA.check_artifact(str(tmp_path), entry["artifact_id"])
        assert result["status"] == TA.STATUS_CHANGED

    def test_never_verified(self, tmp_path):
        script = tmp_path / "a.py"
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))
        assert entry["script_sha256"] is None
        script.write_text("x = 1\n", encoding="utf-8")  # created AFTER registration
        result = TA.check_artifact(str(tmp_path), entry["artifact_id"])
        assert result["status"] == TA.STATUS_NEVER_VERIFIED

    def test_unreadable(self, tmp_path, monkeypatch):
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))

        # script_content_hash's own documented contract is "None on any
        # OSError" (e.g. permission denied) -- injecting that return value
        # directly exercises the exact same branch a real unreadable file
        # would hit, without depending on platform-specific ACL behavior.
        monkeypatch.setattr(TA, "script_content_hash", lambda path: None)
        result = TA.check_artifact(str(tmp_path), entry["artifact_id"])
        assert result["status"] == TA.STATUS_UNREADABLE

    def test_superseded_wins_even_if_script_still_present_and_unchanged(self, tmp_path):
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))
        TA.supersede_artifact(str(tmp_path), entry["artifact_id"], replaced_by="new-id")
        result = TA.check_artifact(str(tmp_path), entry["artifact_id"])
        assert result["status"] == TA.STATUS_SUPERSEDED
        assert "new-id" in result["detail"]

    def test_retired_wins_even_if_script_changed(self, tmp_path):
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))
        TA.retire_artifact(str(tmp_path), entry["artifact_id"])
        script.write_text("x = 2\n", encoding="utf-8")
        result = TA.check_artifact(str(tmp_path), entry["artifact_id"])
        assert result["status"] == TA.STATUS_RETIRED

    def test_check_unknown_id_raises(self, tmp_path):
        with pytest.raises(TA.TempArtifactError):
            TA.check_artifact(str(tmp_path), "nope")

    def test_touch_records_last_checked_at(self, tmp_path):
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))
        assert entry["last_checked_at"] is None
        TA.check_artifact(str(tmp_path), entry["artifact_id"], touch=True)
        refreshed = TA.get_artifact(str(tmp_path), entry["artifact_id"])
        assert refreshed["last_checked_at"] is not None
        assert refreshed["last_checked_at_epoch"] is not None

    def test_touch_false_does_not_mutate_registry(self, tmp_path):
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))
        TA.check_artifact(str(tmp_path), entry["artifact_id"], touch=False)
        refreshed = TA.get_artifact(str(tmp_path), entry["artifact_id"])
        assert refreshed["last_checked_at"] is None

    def test_never_executes_or_deletes_script(self, tmp_path, monkeypatch):
        """Read-only diagnostic contract: check_artifact must never shell
        out or remove the script it's checking."""
        script = tmp_path / "a.py"
        script.write_text("x = 1\n", encoding="utf-8")
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(script))

        def _boom(*_a, **_k):
            raise AssertionError("check_artifact must never execute or delete a script")

        monkeypatch.setattr("subprocess.run", _boom, raising=False)
        monkeypatch.setattr("subprocess.Popen", _boom, raising=False)
        monkeypatch.setattr("os.remove", _boom, raising=False)
        monkeypatch.setattr("os.unlink", _boom, raising=False)

        TA.check_artifact(str(tmp_path), entry["artifact_id"])
        assert script.is_file()  # untouched

    def test_check_all(self, tmp_path):
        s1 = tmp_path / "a.py"
        s1.write_text("1\n", encoding="utf-8")
        s2 = tmp_path / "b.py"  # deliberately never created -> missing
        e1 = TA.register_artifact(str(tmp_path), name="a", script_path=str(s1))
        e2 = TA.register_artifact(str(tmp_path), name="b", script_path=str(s2))

        results = TA.check_all(str(tmp_path))
        by_id = {r["artifact_id"]: r for r in results}
        assert by_id[e1["artifact_id"]]["status"] == TA.STATUS_PRESENT_CURRENT
        assert by_id[e2["artifact_id"]]["status"] == TA.STATUS_MISSING


# ---------------------------------------------------------------------------
# cross_reference_outputs_fingerprint -- soft-import composition.
# ---------------------------------------------------------------------------

class TestCrossReferenceSoftDependency:
    def test_unavailable_when_meridian_outputs_not_importable(self, tmp_path, monkeypatch):
        """This test environment (tests/ -- the main pixi suite) does not
        have meridian_outputs on sys.path: extensions/meridian-outputs is
        never pip-installed into the main pixi env (its own tests import it
        straight off sys.path instead, per pixi.toml's 52cbe5d8 notes).
        Confirming this degrades gracefully -- never an ImportError bubbling
        up -- is itself proof that meridian core has no hard dependency on
        that optionally-installed extension. See
        extensions/meridian-outputs/tests/test_temp_artifacts_reconciliation.py
        for the case where meridian_outputs IS importable.

        Several other files in this same suite (e.g. test_mde5_evidence_lossless.py,
        test_research_evidence_envelope.py) do an unscoped, process-lifetime
        `sys.path.insert(0, ".../extensions/meridian-outputs")` at module import
        time so THEIR OWN tests can `import meridian_outputs`. In a full xdist
        run that can land this test on the same worker process, making the
        real ambient import succeed and this assertion depend on test
        scheduling order rather than on the code path under test. Force the
        unavailable case deterministically: a `None` entry in `sys.modules`
        makes Python's import machinery raise ImportError immediately,
        before consulting sys.path at all.
        """
        monkeypatch.setitem(sys.modules, "meridian_outputs", None)
        entry = TA.register_artifact(str(tmp_path), name="a", script_path=str(tmp_path / "a.py"))
        result = TA.cross_reference_outputs_fingerprint(
            str(tmp_path), entry["artifact_id"], str(tmp_path / "outputs"),
        )
        assert result["available"] is False
        assert "not installed" in result["reason"]
        assert result["agrees"] is None
        assert result["temp_artifacts_script_sha256"] == entry["script_sha256"]

    def test_unknown_artifact_id_raises(self, tmp_path):
        with pytest.raises(TA.TempArtifactError):
            TA.cross_reference_outputs_fingerprint(str(tmp_path), "nope", str(tmp_path))


# ---------------------------------------------------------------------------
# CLI smoke test.
# ---------------------------------------------------------------------------

class TestCli:
    def test_register_check_list_supersede_roundtrip(self, tmp_path, capsys):
        script = tmp_path / "fix.py"
        script.write_text("x = 1\n", encoding="utf-8")
        root = str(tmp_path / "registry")

        assert TA._cli(["--root", root, "register", "--name", "fix", "--script", str(script)]) == 0
        registered = json.loads(capsys.readouterr().out)
        artifact_id = registered["artifact_id"]

        assert TA._cli(["--root", root, "check", artifact_id]) == 0
        checked = json.loads(capsys.readouterr().out)
        assert checked["status"] == TA.STATUS_PRESENT_CURRENT

        assert TA._cli(["--root", root, "list"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 1

        assert TA._cli(["--root", root, "supersede", artifact_id, "--reason", "done"]) == 0
        superseded = json.loads(capsys.readouterr().out)
        assert superseded["lifecycle"] == TA.SUPERSEDED

    def test_cli_default_root_prints_notice_to_stderr(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _onedrive_env_cleared(monkeypatch)
        rc = TA._cli(["register", "--name", "fix", "--script", str(tmp_path / "fix.py")])
        assert rc == 0
        captured = capsys.readouterr()
        assert "using default registry root" in captured.err
        assert os.path.isdir(str(tmp_path / ".meridian-temp-artifacts"))

    def test_cli_inspect_missing_registry(self, tmp_path, capsys):
        root = str(tmp_path / "registry")
        assert TA._cli(["--root", root, "inspect"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["exists"] is False
