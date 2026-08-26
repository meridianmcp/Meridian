"""Tests for meridian/local_resilience.py (c7ef8ff7, MDE-9 P1).

Covers:
  - is_onedrive_path / assert_disk_only_prestage_path -- OneDrive must never
    receive temporary or draft artifacts.
  - check_local_quota / enforce_local_quota -- quota exhaustion degrades
    visibly (an explicit allowed=False / exceeded=True, never silent).
  - start_temp_run / complete_temp_run / fail_temp_run / get_temp_run /
    list_temp_runs -- durable temp-run manifests, atomic ledger.
  - scan_interrupted_runs -- restart scavenging detection (read-only).
  - resolve_interrupted_run -- deterministic resume-vs-quarantine + owned-
    process cleanup, both recorded as auditable receipts.
  - terminate_owned_process -- identity-checked, never a sweep.
  - reap_stale_render_tempdirs -- crash-recovery reaper for render_gate.py's
    disposable temp directories.
  - list_cleanup_receipts -- auditable trail.
  - summarize_for_capability_manifest -- no local paths ever leak into
    shared capability-manifest state.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from meridian import capability_manifest, local_resilience as lr


# ---------------------------------------------------------------------------
# is_onedrive_path / assert_disk_only_prestage_path
# ---------------------------------------------------------------------------

class TestOneDriveDetection:
    def test_empty_path_is_not_onedrive(self) -> None:
        assert lr.is_onedrive_path("") is False

    def test_plain_local_path_is_not_onedrive(self, tmp_path: Path) -> None:
        assert lr.is_onedrive_path(str(tmp_path / "drafts" / "x.docx")) is False

    def test_env_var_root_match(self, monkeypatch, tmp_path: Path) -> None:
        onedrive_root = tmp_path / "OneDriveRoot"
        onedrive_root.mkdir()
        monkeypatch.setenv("OneDrive", str(onedrive_root))
        target = str(onedrive_root / "Documents" / "draft.docx")
        assert lr.is_onedrive_path(target) is True

    def test_env_var_root_exact_match(self, monkeypatch, tmp_path: Path) -> None:
        onedrive_root = tmp_path / "OneDriveRoot"
        onedrive_root.mkdir()
        monkeypatch.setenv("OneDrive", str(onedrive_root))
        assert lr.is_onedrive_path(str(onedrive_root)) is True

    def test_path_segment_fallback_signal(self, monkeypatch) -> None:
        for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            monkeypatch.delenv(var, raising=False)
        assert lr.is_onedrive_path(r"C:\Users\alice\OneDrive - Acme\drafts\x.docx") is True

    def test_path_segment_named_similarly_but_not_onedrive_is_not_flagged(self, monkeypatch) -> None:
        for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            monkeypatch.delenv(var, raising=False)
        assert lr.is_onedrive_path(r"C:\Projects\OneDriveExporter\out.docx") is False

    def test_assert_disk_only_prestage_path_allows_local(self, tmp_path: Path) -> None:
        result = lr.assert_disk_only_prestage_path(str(tmp_path / "draft.docx"))
        assert result["allowed"] is True
        assert result["reason"] is None

    def test_assert_disk_only_prestage_path_refuses_onedrive(self, monkeypatch, tmp_path: Path) -> None:
        onedrive_root = tmp_path / "OneDriveRoot"
        onedrive_root.mkdir()
        monkeypatch.setenv("OneDrive", str(onedrive_root))
        result = lr.assert_disk_only_prestage_path(str(onedrive_root / "draft.docx"))
        assert result["allowed"] is False
        assert "OneDrive" in result["reason"]

    def test_assert_disk_only_prestage_path_refuses_empty(self) -> None:
        result = lr.assert_disk_only_prestage_path("")
        assert result["allowed"] is False


# ---------------------------------------------------------------------------
# check_local_quota / enforce_local_quota
# ---------------------------------------------------------------------------

class TestLocalQuota:
    def test_missing_root_is_not_exceeded(self, tmp_path: Path) -> None:
        result = lr.check_local_quota(str(tmp_path / "does-not-exist"), max_bytes=1000)
        assert result["exists"] is False
        assert result["used_bytes"] == 0
        assert result["exceeded"] is False

    def test_under_budget_not_exceeded(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_bytes(b"x" * 100)
        result = lr.check_local_quota(str(tmp_path), max_bytes=1000)
        assert result["used_bytes"] == 100
        assert result["exceeded"] is False
        assert result["reason"] is None

    def test_over_byte_budget_exceeded_with_visible_reason(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_bytes(b"x" * 2000)
        result = lr.check_local_quota(str(tmp_path), max_bytes=1000)
        assert result["exceeded"] is True
        assert result["reason"] is not None
        assert "exceeded" in result["reason"]

    def test_over_file_count_budget_exceeded(self, tmp_path: Path) -> None:
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_bytes(b"x")
        result = lr.check_local_quota(str(tmp_path), max_bytes=10_000_000, max_files=3)
        assert result["exceeded"] is True
        assert result["used_files"] == 5

    def test_nested_directories_are_summed(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "f.txt").write_bytes(b"x" * 50)
        (tmp_path / "g.txt").write_bytes(b"x" * 50)
        result = lr.check_local_quota(str(tmp_path), max_bytes=1000)
        assert result["used_bytes"] == 100
        assert result["used_files"] == 2

    def test_enforce_local_quota_allowed_true_under_budget(self, tmp_path: Path) -> None:
        result = lr.enforce_local_quota(str(tmp_path), max_bytes=1000)
        assert result["allowed"] is True

    def test_enforce_local_quota_allowed_false_over_budget_degrades_visibly(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_bytes(b"x" * 2000)
        result = lr.enforce_local_quota(str(tmp_path), max_bytes=1000)
        assert result["allowed"] is False
        assert result["exceeded"] is True
        assert result["reason"]


# ---------------------------------------------------------------------------
# Temp-run manifests
# ---------------------------------------------------------------------------

class TestTempRunLifecycle:
    def test_start_temp_run_requires_kind(self, tmp_path: Path) -> None:
        with pytest.raises(lr.LocalResilienceError):
            lr.start_temp_run(str(tmp_path), kind="", owner_pid=os.getpid(), resumable=True)

    def test_start_temp_run_requires_valid_pid(self, tmp_path: Path) -> None:
        with pytest.raises(lr.LocalResilienceError):
            lr.start_temp_run(str(tmp_path), kind="render", owner_pid="not-a-pid", resumable=True)

    def test_start_temp_run_basic_shape(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(
            str(tmp_path), kind="word_com_render", owner_pid=1234, resumable=True,
            process_name="WINWORD.EXE", temp_paths=["/tmp/x.pdf"], metadata={"note": "test"},
        )
        assert run["run_id"]
        assert run["kind"] == "word_com_render"
        assert run["owner_pid"] == 1234
        assert run["resumable"] is True
        assert run["process_name"] == "WINWORD.EXE"
        assert run["temp_paths"] == ["/tmp/x.pdf"]
        assert run["metadata"] == {"note": "test"}
        assert run["status"] == lr.RUN_STARTED
        assert run["ended_at"] is None

    def test_complete_temp_run_updates_status(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        updated = lr.complete_temp_run(str(tmp_path), run["run_id"], detail="all good")
        assert updated["status"] == lr.RUN_COMPLETED
        assert updated["ended_at"] is not None
        assert updated["end_detail"] == "all good"

    def test_fail_temp_run_updates_status(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        updated = lr.fail_temp_run(str(tmp_path), run["run_id"], detail="broke")
        assert updated["status"] == lr.RUN_FAILED

    def test_complete_unknown_run_raises(self, tmp_path: Path) -> None:
        with pytest.raises(lr.LocalResilienceError):
            lr.complete_temp_run(str(tmp_path), "nope")

    def test_get_temp_run_roundtrip(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        fetched = lr.get_temp_run(str(tmp_path), run["run_id"])
        assert fetched["run_id"] == run["run_id"]

    def test_get_temp_run_missing_is_none(self, tmp_path: Path) -> None:
        assert lr.get_temp_run(str(tmp_path), "nope") is None

    def test_list_temp_runs_filters_by_status(self, tmp_path: Path) -> None:
        r1 = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        r2 = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=2, resumable=True)
        lr.complete_temp_run(str(tmp_path), r1["run_id"])

        all_runs = lr.list_temp_runs(str(tmp_path))
        assert len(all_runs) == 2
        started_only = lr.list_temp_runs(str(tmp_path), status=lr.RUN_STARTED)
        assert len(started_only) == 1
        assert started_only[0]["run_id"] == r2["run_id"]

    def test_ledger_persists_across_independent_reads(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        # Simulate a fresh process reading the ledger.
        reloaded = lr.list_temp_runs(str(tmp_path))
        assert len(reloaded) == 1
        assert reloaded[0]["run_id"] == run["run_id"]


# ---------------------------------------------------------------------------
# scan_interrupted_runs -- restart scavenging detection
# ---------------------------------------------------------------------------

class TestScanInterruptedRuns:
    def test_no_runs_nothing_interrupted(self, tmp_path: Path) -> None:
        result = lr.scan_interrupted_runs(str(tmp_path))
        assert result["checked"] == 0
        assert result["interrupted"] == []

    def test_dead_pid_run_is_interrupted(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=999999, resumable=True)
        result = lr.scan_interrupted_runs(str(tmp_path), pid_alive=lambda pid: False)
        assert result["checked"] == 1
        assert len(result["interrupted"]) == 1
        assert result["interrupted"][0]["run_id"] == run["run_id"]

    def test_live_pid_run_is_not_interrupted(self, tmp_path: Path) -> None:
        lr.start_temp_run(str(tmp_path), kind="k", owner_pid=os.getpid(), resumable=True)
        result = lr.scan_interrupted_runs(str(tmp_path), pid_alive=lambda pid: True)
        assert result["interrupted"] == []

    def test_completed_run_is_never_interrupted_even_if_pid_dead(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        lr.complete_temp_run(str(tmp_path), run["run_id"])
        result = lr.scan_interrupted_runs(str(tmp_path), pid_alive=lambda pid: False)
        assert result["interrupted"] == []

    def test_default_pid_alive_uses_real_os_kill(self, tmp_path: Path) -> None:
        # A pid this unlikely to exist should be reported dead by the REAL
        # (non-injected) liveness check.
        lr.start_temp_run(str(tmp_path), kind="k", owner_pid=999999999, resumable=True)
        result = lr.scan_interrupted_runs(str(tmp_path))
        assert len(result["interrupted"]) == 1


# ---------------------------------------------------------------------------
# terminate_owned_process
# ---------------------------------------------------------------------------

class TestTerminateOwnedProcess:
    def test_already_gone_pid_is_a_noop(self) -> None:
        result = lr.terminate_owned_process(999999999)
        assert result["terminated"] is False
        assert result["reason"] == "already_gone"

    def test_identity_mismatch_refuses_and_never_kills(self) -> None:
        result = lr.terminate_owned_process(
            os.getpid(), expected_name="WINWORD.EXE",
            process_name_for_pid=lambda pid: "python.exe",
        )
        assert result["terminated"] is False
        assert result["reason"] == "identity_mismatch"

    def test_identity_unknown_when_lookup_raises(self) -> None:
        def _boom(pid: int) -> str:
            raise RuntimeError("psutil not available")

        result = lr.terminate_owned_process(
            os.getpid(), expected_name="WINWORD.EXE", process_name_for_pid=_boom,
        )
        assert result["terminated"] is False
        assert result["reason"] == "identity_unknown"

    def test_no_name_lookup_supplied_falls_back_to_pid_only_and_terminates_a_real_child(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            # Give the child a moment to actually start.
            time.sleep(0.3)
            result = lr.terminate_owned_process(proc.pid, expected_name="python")
            assert result["terminated"] is True
            proc.wait(timeout=10)
            assert proc.returncode is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_matching_name_terminates_a_real_child(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            time.sleep(0.3)
            result = lr.terminate_owned_process(
                proc.pid, expected_name="python",
                process_name_for_pid=lambda pid: "python.exe",
            )
            assert result["terminated"] is True
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# resolve_interrupted_run
# ---------------------------------------------------------------------------

class TestResolveInterruptedRun:
    def test_unknown_run_raises(self, tmp_path: Path) -> None:
        with pytest.raises(lr.LocalResilienceError):
            lr.resolve_interrupted_run(str(tmp_path), "nope")

    def test_non_started_run_raises(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        lr.complete_temp_run(str(tmp_path), run["run_id"])
        with pytest.raises(lr.LocalResilienceError):
            lr.resolve_interrupted_run(str(tmp_path), run["run_id"])

    def test_resumable_run_resolves_as_resume(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="render", owner_pid=999999999, resumable=True)
        result = lr.resolve_interrupted_run(str(tmp_path), run["run_id"])
        assert result["action"] == "resume"
        assert result["quarantine_result"] is None
        assert result["run"]["status"] == lr.RUN_RESOLVED_RESUME

    def test_non_resumable_run_with_no_ownership_check_skips_quarantine_fail_closed(self, tmp_path: Path) -> None:
        temp_file = tmp_path / "draft.docx"
        temp_file.write_bytes(b"draft content")
        run = lr.start_temp_run(
            str(tmp_path), kind="docx_draft_write", owner_pid=999999999, resumable=False,
            temp_paths=[str(temp_file)],
        )
        result = lr.resolve_interrupted_run(str(tmp_path), run["run_id"])
        assert result["action"] == "quarantine"
        assert result["quarantine_result"]["reason"] == "quarantine_skipped_no_ownership_check"
        assert temp_file.exists()  # never touched -- fail closed
        assert result["run"]["status"] == lr.RUN_RESOLVED_QUARANTINE

    def test_non_resumable_run_no_temp_paths(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=999999999, resumable=False)
        result = lr.resolve_interrupted_run(str(tmp_path), run["run_id"])
        assert result["quarantine_result"]["reason"] == "no_temp_paths_recorded"

    def test_non_resumable_run_with_ownership_check_quarantines(self, tmp_path: Path) -> None:
        temp_file = tmp_path / "draft.docx"
        temp_file.write_bytes(b"draft content")
        archive_root = tmp_path / "archive"
        run = lr.start_temp_run(
            str(tmp_path), kind="docx_draft_write", owner_pid=999999999, resumable=False,
            temp_paths=[str(temp_file)],
        )

        result = lr.resolve_interrupted_run(
            str(tmp_path), run["run_id"],
            quarantine_root=str(archive_root),
            ownership_check=lambda path: {"eligible": True, "reason": "known temp draft"},
        )

        assert result["quarantine_result"]["moved_count"] == 1
        assert not temp_file.exists()  # moved into the archive
        assert result["run"]["status"] == lr.RUN_RESOLVED_QUARANTINE

    def test_process_cleanup_attempted_when_process_name_recorded(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(
            str(tmp_path), kind="word_com_render", owner_pid=999999999, resumable=True,
            process_name="WINWORD.EXE",
        )
        result = lr.resolve_interrupted_run(str(tmp_path), run["run_id"])
        assert result["process_cleanup"] is not None
        assert result["process_cleanup"]["reason"] == "already_gone"

    def test_no_process_cleanup_when_no_process_name_recorded(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="outputs_temp_write", owner_pid=999999999, resumable=True)
        result = lr.resolve_interrupted_run(str(tmp_path), run["run_id"])
        assert result["process_cleanup"] is None

    def test_resolving_twice_refuses_the_second_time(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=999999999, resumable=True)
        lr.resolve_interrupted_run(str(tmp_path), run["run_id"])
        with pytest.raises(lr.LocalResilienceError):
            lr.resolve_interrupted_run(str(tmp_path), run["run_id"])


# ---------------------------------------------------------------------------
# reap_stale_render_tempdirs
# ---------------------------------------------------------------------------

class TestReapStaleRenderTempdirs:
    def test_missing_temp_root_reports_skip_not_crash(self, tmp_path: Path) -> None:
        result = lr.reap_stale_render_tempdirs(str(tmp_path / "nope"))
        assert result["removed"] == []
        assert result["skipped"]

    def test_removes_old_matching_dirs_leaves_recent_ones(self, tmp_path: Path) -> None:
        old_dir = tmp_path / "meridian_render_gate_old123"
        old_dir.mkdir()
        recent_dir = tmp_path / "meridian_render_gate_recent456"
        recent_dir.mkdir()
        unrelated_dir = tmp_path / "some_other_tempdir"
        unrelated_dir.mkdir()

        now = time.time()
        old_time = now - 7200  # 2 hours old
        os.utime(old_dir, (old_time, old_time))
        os.utime(recent_dir, (now, now))

        result = lr.reap_stale_render_tempdirs(str(tmp_path), max_age_seconds=3600.0, now=now)

        assert str(old_dir) in result["removed"]
        assert not old_dir.exists()
        assert recent_dir.exists()
        assert unrelated_dir.exists()
        assert result["scanned"] == 2  # only prefix-matching dirs counted

    def test_custom_prefix(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "custom_prefix_abc"
        custom_dir.mkdir()
        old_time = time.time() - 7200
        os.utime(custom_dir, (old_time, old_time))

        result = lr.reap_stale_render_tempdirs(
            str(tmp_path), prefix="custom_prefix_", max_age_seconds=3600.0,
        )
        assert not custom_dir.exists()


# ---------------------------------------------------------------------------
# list_cleanup_receipts -- auditable trail
# ---------------------------------------------------------------------------

class TestCleanupReceipts:
    def test_receipts_recorded_for_start_and_complete(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        lr.complete_temp_run(str(tmp_path), run["run_id"], detail="done")

        receipts = lr.list_cleanup_receipts(str(tmp_path))
        actions = [r["action"] for r in receipts]
        assert "started" in actions
        assert lr.RUN_COMPLETED in actions

    def test_receipts_filterable_by_run_id(self, tmp_path: Path) -> None:
        r1 = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=1, resumable=True)
        r2 = lr.start_temp_run(str(tmp_path), kind="k", owner_pid=2, resumable=True)

        receipts_r1 = lr.list_cleanup_receipts(str(tmp_path), run_id=r1["run_id"])
        assert all(r["run_id"] == r1["run_id"] for r in receipts_r1)
        assert len(receipts_r1) >= 1

    def test_empty_ledger_returns_empty_list(self, tmp_path: Path) -> None:
        assert lr.list_cleanup_receipts(str(tmp_path)) == []

    def test_resolve_interrupted_run_appends_receipts(self, tmp_path: Path) -> None:
        run = lr.start_temp_run(
            str(tmp_path), kind="word_com_render", owner_pid=999999999, resumable=True,
            process_name="WINWORD.EXE",
        )
        lr.resolve_interrupted_run(str(tmp_path), run["run_id"])
        receipts = lr.list_cleanup_receipts(str(tmp_path), run_id=run["run_id"])
        actions = [r["action"] for r in receipts]
        assert "process_cleanup" in actions
        assert lr.RUN_RESOLVED_RESUME in actions


# ---------------------------------------------------------------------------
# summarize_for_capability_manifest -- no local paths ever leak
# ---------------------------------------------------------------------------

class TestSummarizeForCapabilityManifest:
    def test_returns_a_valid_normalized_capability(self) -> None:
        summary = lr.summarize_for_capability_manifest()
        assert summary["id"] == "local_resilience"
        assert summary["availability_policy"] == "optional"
        assert summary["required_tools"]

    def test_contains_no_local_path_shaped_strings(self) -> None:
        summary = lr.summarize_for_capability_manifest()
        # Re-validated through the REAL validator -- would raise if a path
        # had snuck in; also assert directly for defense-in-depth.
        capability_manifest.normalize_capability(summary)
        serialized = str(summary)
        assert "C:\\" not in serialized
        assert "/home/" not in serialized
        assert "/Users/" not in serialized

    def test_takes_no_local_path_parameters_at_all(self) -> None:
        import inspect

        sig = inspect.signature(lr.summarize_for_capability_manifest)
        assert "manifest_dir" not in sig.parameters
        assert "temp_root" not in sig.parameters

    def test_custom_verification_command(self) -> None:
        summary = lr.summarize_for_capability_manifest(verification_command="pytest tests/test_c7ef8ff7_local_resilience.py")
        assert summary["verification_command"]
