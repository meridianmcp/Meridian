from __future__ import annotations

import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

from meridian import artifact_capture, artifact_store


PROJECT_ID = "project-1"


def _configured(tmp_path: Path, *, extensions=None, outputs_dir=None):
    project_root = tmp_path / "repo"
    output_root = project_root / "outputs"
    output_root.mkdir(parents=True)
    data_dir = tmp_path / "state"
    artifact_capture.configure_project(
        data_dir,
        project_id=PROJECT_ID,
        project_root=project_root,
        output_roots=[output_root],
        extensions=extensions,
        outputs_dir=outputs_dir,
    )
    return data_dir, project_root, output_root


def test_capture_is_disabled_until_selected_roots_are_configured(tmp_path):
    data_dir = tmp_path / "state"
    project_root = tmp_path / "repo"
    output_root = project_root / "outputs"
    output_root.mkdir(parents=True)
    source = output_root / "report.txt"
    source.write_text("approved output", encoding="utf-8")

    with pytest.raises(artifact_capture.ArtifactCaptureError, match="disabled"):
        artifact_capture.capture_file(data_dir, PROJECT_ID, source)


def test_capture_rejects_outside_root_and_symlink_escape(tmp_path):
    data_dir, _project_root, output_root = _configured(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not selected", encoding="utf-8")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="outside"):
        artifact_capture.capture_file(data_dir, PROJECT_ID, outside)

    escaped_link = output_root / "linked.txt"
    try:
        escaped_link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable for this Windows account")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="outside"):
        artifact_capture.capture_file(data_dir, PROJECT_ID, escaped_link)


def test_capture_fails_if_source_changes_during_open(tmp_path, monkeypatch):
    data_dir, _project_root, output_root = _configured(tmp_path)
    source = output_root / "report.txt"
    source.write_text("small", encoding="utf-8")
    real_open = artifact_capture.os.open
    changed = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal changed
        if Path(path) == source and not changed:
            changed = True
            source.write_text("the file changed before it could be read", encoding="utf-8")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifact_capture.os, "open", replace_before_open)
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="changed"):
        artifact_capture.capture_file(data_dir, PROJECT_ID, source)
    assert artifact_capture.list_occurrences(data_dir, PROJECT_ID) == []


def test_capture_redacts_text_and_preserves_allowlisted_binary(tmp_path):
    data_dir, _project_root, output_root = _configured(tmp_path)
    text_file = output_root / "report.txt"
    text_file.write_text("config: AWS_KEY=AKIAABCDEFGHIJKLMNOP done", encoding="utf-8")
    text_result = artifact_capture.capture_file(data_dir, PROJECT_ID, text_file)
    text_record = text_result["occurrence"]
    stored_text = artifact_store.get_artifact(str(data_dir), PROJECT_ID, text_record["content_hash"])
    assert text_record["redacted"] is True
    assert b"AKIAABCDEFGHIJKLMNOP" not in stored_text
    assert b"[REDACTED:" in stored_text

    binary_file = output_root / "figure.png"
    binary = b"\x89PNG\r\n\x1a\n\x00\xffbinary"
    binary_file.write_bytes(binary)
    artifact_capture.configure_project(
        data_dir,
        project_id=PROJECT_ID,
        project_root=output_root.parent,
        output_roots=[output_root],
        extensions=[".png"],
    )
    binary_result = artifact_capture.capture_file(data_dir, PROJECT_ID, binary_file)
    binary_record = binary_result["occurrence"]
    assert binary_record["redacted"] is False
    assert artifact_store.get_artifact(str(data_dir), PROJECT_ID, binary_record["content_hash"]) == binary
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="not enabled"):
        artifact_capture.capture_file(data_dir, PROJECT_ID, text_file)


def test_identical_bytes_keep_separate_run_lineage(tmp_path):
    data_dir, _project_root, output_root = _configured(tmp_path)
    source = output_root / "report.txt"
    source.write_text("same result", encoding="utf-8")
    first = artifact_capture.capture_file(
        data_dir, PROJECT_ID, source,
        source_event={"provider": "claude_code", "event_id": "event-1", "run_id": "run-1"},
    )["occurrence"]
    second = artifact_capture.capture_file(
        data_dir, PROJECT_ID, source,
        source_event={"provider": "codex_cli", "event_id": "event-2", "run_id": "run-2"},
    )["occurrence"]

    assert first["content_hash"] == second["content_hash"]
    assert first["occurrence_id"] != second["occurrence_id"]
    assert first["run_id"] == "run-1"
    assert second["run_id"] == "run-2"
    assert len(artifact_capture.list_occurrences(data_dir, PROJECT_ID)) == 2


def test_outputs_registry_hash_mismatch_is_recorded_without_losing_capture(tmp_path, monkeypatch):
    data_dir, project_root, output_root = _configured(tmp_path, outputs_dir=output_root_placeholder(tmp_path))
    source = output_root / "table.csv"
    source.write_text("x,y\n1,2\n", encoding="utf-8")
    register_calls = []
    package = types.ModuleType("meridian_outputs")
    package.__path__ = []
    registry = types.ModuleType("meridian_outputs.artifact_registry")

    def register_artifact(*args, **kwargs):
        register_calls.append((args, kwargs))
        return {"artifact_id": "artifact-1"}

    registry.register_artifact = register_artifact
    registry.verify_artifact_hash = lambda *args, **kwargs: {"verified": False, "reason": "hash_mismatch"}
    monkeypatch.setitem(sys.modules, "meridian_outputs", package)
    monkeypatch.setitem(sys.modules, "meridian_outputs.artifact_registry", registry)

    result = artifact_capture.capture_file(data_dir, PROJECT_ID, source)
    record = result["occurrence"]
    assert result["captured"] is True
    assert record["outputs_registry"] == {"status": "hash_mismatch", "artifact_id": "artifact-1", "verified": False}
    assert register_calls and record["source_sha256"] in register_calls[0][1]["expected_sha256"]


def output_root_placeholder(tmp_path: Path) -> Path:
    """Existing local directory used only to exercise the optional registry path."""
    path = tmp_path / "outputs-registry"
    path.mkdir()
    return path


def test_provider_hooks_capture_supported_files_and_deduplicate_replay(tmp_path):
    data_dir, project_root, output_root = _configured(tmp_path)
    claude_file = output_root / "claude.txt"
    claude_file.write_text("Claude output", encoding="utf-8")
    claude_event = {
        "hook_event_name": "PostToolUse",
        "session_id": "session-1",
        "tool_name": "Write",
        "tool_use_id": "call-1",
        "cwd": str(project_root),
        "tool_input": {"file_path": str(claude_file)},
    }
    first = artifact_capture.handle_hook_event(claude_event, data_dir=data_dir)
    replay = artifact_capture.handle_hook_event(claude_event, data_dir=data_dir)
    assert first["occurrence"]["provider"] == "claude_code"
    assert replay["deduplicated_event"] is True

    codex_file = output_root / "codex.txt"
    codex_file.write_text("Codex output", encoding="utf-8")
    codex_file_two = output_root / "codex-two.txt"
    codex_file_two.write_text("Second Codex output", encoding="utf-8")
    codex_event = {
        "hook_event_name": "PostToolUse",
        "session_id": "session-2",
        "turn_id": "turn-2",
        "tool_name": "apply_patch",
        "tool_use_id": "call-2",
        "cwd": str(project_root),
        "tool_input": {"command": "*** Begin Patch\n*** Add File: outputs/codex.txt\n+content\n*** Update File: outputs/codex-two.txt\n@@\n+updated\n*** End Patch"},
    }
    codex = artifact_capture.handle_hook_event(codex_event, data_dir=data_dir)
    assert codex["captured"] is True
    assert codex["captured_count"] == 2
    codex_records = [row["occurrence"] for row in codex["results"]]
    assert {row["provider"] for row in codex_records} == {"codex_cli"}
    assert {row["run_id"] for row in codex_records} == {"turn-2"}


def test_hook_without_supported_event_or_local_project_is_noop(tmp_path):
    data_dir, project_root, output_root = _configured(tmp_path)
    source = output_root / "report.txt"
    source.write_text("content", encoding="utf-8")
    assert artifact_capture.handle_hook_event({"hook_event_name": "Stop"}, data_dir=data_dir)["reason"] == "unsupported_event"
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(source)},
        "cwd": str(tmp_path),
    }
    assert artifact_capture.handle_hook_event(event, data_dir=data_dir)["reason"] == "project_not_configured"
    assert artifact_capture.hook_main(stdin=types.SimpleNamespace(read=lambda _limit: b"not-json"), data_dir=data_dir) == 0


def test_restore_is_hash_verified_and_rejects_outside_path_before_mkdir(tmp_path):
    data_dir, _project_root, output_root = _configured(tmp_path)
    source = output_root / "report.txt"
    source.write_text("restorable output", encoding="utf-8")
    captured = artifact_capture.capture_file(data_dir, PROJECT_ID, source)["occurrence"]

    destination = output_root / "restored" / "copy.txt"
    result = artifact_capture.restore_occurrence(data_dir, PROJECT_ID, captured["occurrence_id"], destination)
    assert result["restored"] is True and result["verified"] is True
    assert destination.read_text(encoding="utf-8") == "restorable output"
    assert artifact_capture.list_occurrences(data_dir, PROJECT_ID)[0]["restore_verifications"][-1]["verified"] is True

    outside_parent = tmp_path / "must-not-be-created"
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="outside"):
        artifact_capture.restore_occurrence(
            data_dir, PROJECT_ID, captured["occurrence_id"], outside_parent / "copy.txt",
        )
    assert not outside_parent.exists()


def test_export_unlink_and_confirmed_project_purge(tmp_path):
    data_dir, _project_root, output_root = _configured(tmp_path)
    source = output_root / "report.txt"
    source.write_text("exported output", encoding="utf-8")
    first = artifact_capture.capture_file(
        data_dir, PROJECT_ID, source, source_event={"event_id": "event-1"},
    )["occurrence"]
    export_path = tmp_path / "export.zip"
    export = artifact_capture.export_project(data_dir, PROJECT_ID, export_path)
    assert export["exported"] is True and export["blob_count"] == 1
    with zipfile.ZipFile(export_path) as archive:
        bundle = json.loads(archive.read("manifest.json"))
        assert bundle["occurrences"][0]["occurrence_id"] == first["occurrence_id"]
        assert f"blobs/{first['content_hash'].split(':', 1)[1]}" in archive.namelist()

    assert artifact_capture.unlink_occurrence(data_dir, PROJECT_ID, first["occurrence_id"]) is True
    assert artifact_store.get_artifact(str(data_dir), PROJECT_ID, first["content_hash"]) is not None
    second = artifact_capture.capture_file(
        data_dir, PROJECT_ID, source, source_event={"event_id": "event-2"},
    )["occurrence"]
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="confirm"):
        artifact_capture.purge_project(data_dir, PROJECT_ID, confirm_project_id="wrong-project")
    purged = artifact_capture.purge_project(data_dir, PROJECT_ID, confirm_project_id=PROJECT_ID)
    assert purged["purged"] is True and purged["occurrence_count"] == 1
    assert artifact_store.get_artifact(str(data_dir), PROJECT_ID, second["content_hash"]) is None
    states = {row["occurrence_id"]: row["retention_state"] for row in artifact_capture.list_occurrences(data_dir, PROJECT_ID)}
    assert states[first["occurrence_id"]] == "unlinked"
    assert states[second["occurrence_id"]] == "purged"


def test_artifacts_command_dispatches_from_cli_and_tray(monkeypatch):
    from meridian import __main__ as meridian_cli
    from meridian import tray_main

    monkeypatch.setattr(artifact_capture, "cli_main", lambda argv=None: 17)
    assert meridian_cli._dispatch_subcommand(["artifacts", "status"]) == 17
    monkeypatch.setattr(meridian_cli, "main", lambda argv=None: 23)
    assert tray_main.main(["artifacts", "status"]) == 23


def test_capture_rejects_secret_subproject_label(tmp_path):
    root = tmp_path / "repo"
    output_root = root / "outputs"
    output_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="pattern"):
        artifact_capture.configure_project(
            tmp_path / "state",
            project_id=PROJECT_ID,
            project_root=root,
            output_roots=[output_root],
            subproject="AWS_KEY=AKIAABCDEFGHIJKLMNOP",
        )


def test_local_state_resolution_and_unreadable_metadata(tmp_path, monkeypatch):
    state_dir = tmp_path / "configured-state"
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(state_dir))
    assert artifact_capture._data_dir() == state_dir.resolve()
    monkeypatch.delenv("MERIDIAN_DATA_DIR")
    fallback = tmp_path / "fallback-state"
    monkeypatch.setattr(artifact_capture, "default_state_dir", lambda: fallback)
    assert artifact_capture._data_dir() == fallback.resolve()

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{bad json", encoding="utf-8")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="malformed"):
        artifact_capture._read_json(malformed, limit=100)
    malformed.write_text("[]", encoding="utf-8")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="JSON object"):
        artifact_capture._read_json(malformed, limit=100)
    malformed.write_text(json.dumps({"x": "too large"}), encoding="utf-8")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="size limit"):
        artifact_capture._read_json(malformed, limit=2)


def test_configuration_rejects_invalid_paths_sizes_and_extensions(tmp_path):
    root = tmp_path / "repo"
    output_root = root / "outputs"
    output_root.mkdir(parents=True)
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="existing local directory"):
        artifact_capture.configure_project(state, project_id=PROJECT_ID, project_root=tmp_path / "missing", output_roots=[output_root])
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="inside"):
        artifact_capture.configure_project(state, project_id=PROJECT_ID, project_root=root, output_roots=[outside])
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="at least one"):
        artifact_capture.configure_project(state, project_id=PROJECT_ID, project_root=root, output_roots=[])
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="max_file_bytes"):
        artifact_capture.configure_project(state, project_id=PROJECT_ID, project_root=root, output_roots=[output_root], max_file_bytes=0)
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="extensions"):
        artifact_capture.configure_project(state, project_id=PROJECT_ID, project_root=root, output_roots=[output_root], extensions=["../pdf"])
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="extensions"):
        artifact_capture.configure_project(state, project_id=PROJECT_ID, project_root=root, output_roots=[output_root], extensions=[42])
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="Meridian Outputs directory"):
        artifact_capture.configure_project(state, project_id=PROJECT_ID, project_root=root, output_roots=[output_root], outputs_dir=tmp_path / "missing-outputs")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="safe, non-secret"):
        artifact_capture.configure_project(state, project_id="../bad", project_root=root, output_roots=[output_root])


def test_file_policy_rejects_sensitive_names_unsupported_extensions_and_size(tmp_path):
    data_dir, _project_root, output_root = _configured(tmp_path, extensions=["txt"])
    secret_file = output_root / "api_secret.txt"
    secret_file.write_text("safe-sized", encoding="utf-8")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="excluded"):
        artifact_capture.capture_file(data_dir, PROJECT_ID, secret_file)

    unsupported = output_root / "report.exe"
    unsupported.write_text("x", encoding="utf-8")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="not enabled"):
        artifact_capture.capture_file(data_dir, PROJECT_ID, unsupported)

    artifact_capture.configure_project(
        data_dir, project_id=PROJECT_ID, project_root=output_root.parent,
        output_roots=[output_root], extensions=["txt"], max_file_bytes=4,
    )
    too_large = output_root / "large.txt"
    too_large.write_text("12345", encoding="utf-8")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="exceeds"):
        artifact_capture.capture_file(data_dir, PROJECT_ID, too_large)


def test_stable_reader_rejects_missing_directory_and_open_failure(tmp_path, monkeypatch):
    missing = tmp_path / "missing.txt"
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="unavailable"):
        artifact_capture._read_stable_file(missing, 10)
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="regular"):
        artifact_capture._read_stable_file(directory, 10)
    source = tmp_path / "unreadable.txt"
    source.write_text("content", encoding="utf-8")
    monkeypatch.setattr(artifact_capture.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="could not read"):
        artifact_capture._read_stable_file(source, 100)


def test_git_ignore_probe_and_registry_unavailable_are_reported_locally(tmp_path, monkeypatch):
    data_dir, project_root, output_root = _configured(tmp_path, outputs_dir=output_root_placeholder(tmp_path))
    source = output_root / "report.txt"
    source.write_text("file output", encoding="utf-8")
    monkeypatch.setattr(artifact_capture.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    assert artifact_capture._git_ignored(project_root, source) is True
    monkeypatch.setattr(artifact_capture.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("git unavailable")))
    assert artifact_capture._git_ignored(project_root, source) is False

    real_import = __import__

    def no_outputs_registry(name, *args, **kwargs):
        if name == "meridian_outputs.artifact_registry":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_outputs_registry)
    result = artifact_capture.capture_file(data_dir, PROJECT_ID, source)
    assert result["captured"] is True
    assert result["occurrence"]["outputs_registry"]["status"] == "unavailable"


def test_hook_rejects_malformed_tool_shapes_without_capturing(tmp_path):
    data_dir, project_root, output_root = _configured(tmp_path)
    source = output_root / "report.txt"
    source.write_text("content", encoding="utf-8")
    assert artifact_capture.handle_hook_event(None, data_dir=data_dir)["reason"] == "invalid_event"
    assert artifact_capture.handle_hook_event({"hook_event_name": []}, data_dir=data_dir)["reason"] == "unsupported_event"
    assert artifact_capture.handle_hook_event({"hook_event_name": "PostToolUse", "tool_name": "Bash"}, data_dir=data_dir)["reason"] == "unsupported_tool"
    event = {
        "hook_event_name": "PostToolUse", "tool_name": "Write",
        "tool_input": {"file_path": str(source)}, "cwd": str(project_root),
    }
    result = artifact_capture.handle_hook_event(event, data_dir=data_dir)
    assert result["captured"] is True
    assert result["occurrence"]["provider"] == "claude_code"


def test_restore_and_export_fail_closed_when_occurrence_or_blob_is_invalid(tmp_path, monkeypatch):
    data_dir, _project_root, output_root = _configured(tmp_path)
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="invalid"):
        artifact_capture.restore_occurrence(data_dir, PROJECT_ID, "bad-id")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="not found"):
        artifact_capture.restore_occurrence(data_dir, PROJECT_ID, "0" * 32)

    source = output_root / "report.txt"
    source.write_text("restorable", encoding="utf-8")
    record = artifact_capture.capture_file(data_dir, PROJECT_ID, source)["occurrence"]
    existing = output_root / "existing.txt"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="exists"):
        artifact_capture.restore_occurrence(data_dir, PROJECT_ID, record["occurrence_id"], existing)
    assert existing.read_text(encoding="utf-8") == "keep"

    real_get = artifact_store.get_artifact
    monkeypatch.setattr(artifact_store, "get_artifact", lambda *args, **kwargs: b"tampered")
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="failed content-hash"):
        artifact_capture.restore_occurrence(data_dir, PROJECT_ID, record["occurrence_id"])
    with pytest.raises(artifact_capture.ArtifactCaptureError, match="export refused"):
        artifact_capture.export_project(data_dir, PROJECT_ID, tmp_path / "broken.zip")
    monkeypatch.setattr(artifact_store, "get_artifact", real_get)


def test_cli_commands_cover_local_capture_lifecycle(tmp_path, capsys):
    root = tmp_path / "repo"
    output_root = root / "outputs"
    output_root.mkdir(parents=True)
    data_dir = tmp_path / "state"
    source = output_root / "report.txt"
    source.write_text("cli output", encoding="utf-8")

    def run(*args):
        return artifact_capture.cli_main(["--data-dir", str(data_dir), *map(str, args)])

    assert run("configure", "--project-id", PROJECT_ID, "--root", root, "--output-root", output_root, "--extension", "txt") == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True
    assert run("status", "--project-id", PROJECT_ID) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True
    assert run("capture", "--project-id", PROJECT_ID, "--source", source, "--provider", "codex_cli", "--event-id", "cli-event", "--run-id", "cli-run") == 0
    captured = json.loads(capsys.readouterr().out)["occurrence"]
    assert run("list", "--project-id", PROJECT_ID) == 0
    assert json.loads(capsys.readouterr().out)["occurrences"][0]["occurrence_id"] == captured["occurrence_id"]

    destination = output_root / "copy.txt"
    assert run("restore", "--project-id", PROJECT_ID, "--occurrence-id", captured["occurrence_id"], "--destination", destination) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
    export_path = tmp_path / "cli-export.zip"
    assert run("export", "--project-id", PROJECT_ID, "--output", export_path) == 0
    assert json.loads(capsys.readouterr().out)["exported"] is True
    assert run("unlink", "--project-id", PROJECT_ID, "--occurrence-id", captured["occurrence_id"]) == 0
    assert json.loads(capsys.readouterr().out)["unlinked"] is True
    assert run("purge", "--project-id", PROJECT_ID, "--confirm-project", PROJECT_ID) == 0
    assert json.loads(capsys.readouterr().out)["purged"] is True
    assert run("disable", "--project-id", PROJECT_ID) == 0
    assert json.loads(capsys.readouterr().out)["disabled"] is True


def test_capture_status_reports_local_manifest_hash_and_count(tmp_path):
    data_dir, _project_root, output_root = _configured(tmp_path)
    source = output_root / "report.txt"
    source.write_text("tracked output", encoding="utf-8")

    before = artifact_capture.capture_status(data_dir, PROJECT_ID)
    assert before["manifest_status"] == "available"
    assert len(before["manifest_sha256"]) == 64
    assert before["manifest_occurrence_count"] == 0
    assert before["manifest_file_present"] is False

    artifact_capture.capture_file(data_dir, PROJECT_ID, source)
    after = artifact_capture.capture_status(data_dir, PROJECT_ID)
    assert after["manifest_status"] == "available"
    assert after["manifest_sha256"] != before["manifest_sha256"]
    assert after["manifest_occurrence_count"] == 1
    assert after["manifest_file_present"] is True
