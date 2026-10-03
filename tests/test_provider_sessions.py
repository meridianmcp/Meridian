from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from meridian import provider_sessions as ps


def _catalog(tmp_path, monkeypatch, *, scan_limit=100, limit=200):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    monkeypatch.setattr(ps.shutil, "which", lambda executable: f"/fake/{executable}")
    result = ps.catalog_local_sessions(
        claude_config_dir=claude_root,
        codex_home=codex_root,
        limit=limit,
        scan_limit=scan_limit,
        clock=lambda: 1_791_000_000,
    )
    return result, claude_root, codex_root


def test_catalog_lists_claude_and_codex_by_metadata_only(tmp_path, monkeypatch):
    claude_id = str(uuid.uuid4())
    codex_id = str(uuid.uuid4())
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    claude_file = claude_root / "projects" / "project-alias" / f"{claude_id}.jsonl"
    codex_file = codex_root / "sessions" / "2026" / "10" / "03" / f"rollout-2026-10-03T00-00-00-{codex_id}.jsonl"
    claude_file.parent.mkdir(parents=True)
    codex_file.parent.mkdir(parents=True)
    claude_file.write_text('{"message":"PRIVATE_CLAUDE_CONTENT"}\n', encoding="utf-8")
    codex_file.write_text('{"message":"PRIVATE_CODEX_CONTENT"}\n', encoding="utf-8")
    monkeypatch.setattr(ps.shutil, "which", lambda executable: f"/fake/{executable}")

    report = ps.catalog_local_sessions(claude_config_dir=claude_root, codex_home=codex_root)

    assert {row["provider"] for row in report["sessions"]} == {"claude_code", "codex_cli"}
    assert {row["session_id"].lower() for row in report["sessions"]} == {claude_id.lower(), codex_id.lower()}
    assert "PRIVATE_CLAUDE_CONTENT" not in json.dumps(report)
    assert "PRIVATE_CODEX_CONTENT" not in json.dumps(report)
    assert all(row["recovery_outcomes"]["read"] is False for row in report["sessions"])
    assert all(row["recovery_outcomes"]["provider_native_resume"] == "candidate_unverified" for row in report["sessions"])
    assert {row["provider"]: row["status"] for row in report["not_cataloged_surfaces"]}["codex_app"] == "app_server_or_host_required"
    assert {row["provider"]: row["status"] for row in report["not_cataloged_surfaces"]}["chatgpt"] == "provider_ui_or_user_export"


def test_catalog_bounds_scan_and_result_count(tmp_path, monkeypatch):
    claude_root = tmp_path / "claude"
    project = claude_root / "projects" / "project"
    project.mkdir(parents=True)
    for _ in range(4):
        (project / f"{uuid.uuid4()}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(ps.shutil, "which", lambda _executable: "/fake/runtime")

    report = ps.catalog_local_sessions(
        claude_config_dir=claude_root,
        codex_home=tmp_path / "missing-codex",
        limit=1,
        scan_limit=3,
    )

    assert report["returned_count"] == 1
    assert report["complete"] is False
    assert report["providers"][0]["truncated"] is True


def test_catalog_rejects_symlinked_session_files(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    project = root / "projects" / "project"
    project.mkdir(parents=True)
    target = tmp_path / "outside.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    link = project / f"{uuid.uuid4()}.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    monkeypatch.setattr(ps.shutil, "which", lambda _executable: "/fake/runtime")

    report = ps.catalog_local_sessions(claude_config_dir=root, codex_home=tmp_path / "codex")

    assert report["sessions"] == []


def test_hash_selected_jsonl_range_returns_hash_not_content(tmp_path, monkeypatch):
    session_id = str(uuid.uuid4())
    session_file = tmp_path / "claude" / "projects" / "p" / f"{session_id}.jsonl"
    session_file.parent.mkdir(parents=True)
    selected = b'{"message":"selected"}\n{"message":"range"}\n'
    session_file.write_bytes(b'{"message":"first"}\n' + selected + b'{"message":"last"}\n')
    monkeypatch.setattr(ps.shutil, "which", lambda _executable: "/fake/runtime")
    catalog = ps.catalog_local_sessions(claude_config_dir=tmp_path / "claude", codex_home=tmp_path / "codex")
    session = ps.ProviderSession(**{
        **{key: value for key, value in catalog["sessions"][0].items() if key != "recovery_outcomes"},
        "resume_argv": tuple(catalog["sessions"][0]["resume_argv"]),
    })

    result = ps.hash_selected_jsonl_lines(session, 2, 3)

    assert result["sha256"] == hashlib.sha256(selected).hexdigest()
    assert result["selected_bytes"] == len(selected)
    assert result["content_returned"] is False


def test_hash_selected_range_detects_source_change(tmp_path, monkeypatch):
    session_id = str(uuid.uuid4())
    session_file = tmp_path / "claude" / "projects" / "p" / f"{session_id}.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(ps.shutil, "which", lambda _executable: "/fake/runtime")
    row = ps.catalog_local_sessions(claude_config_dir=tmp_path / "claude", codex_home=tmp_path / "codex")["sessions"][0]
    session = ps.ProviderSession(**{
        **{key: value for key, value in row.items() if key != "recovery_outcomes"},
        "resume_argv": tuple(row["resume_argv"]),
    })
    session_file.write_text('{"new":"data"}\n', encoding="utf-8")

    with pytest.raises(ps.ProviderSessionError, match="changed since cataloging"):
        ps.hash_selected_jsonl_lines(session, 1, 1)


def test_hash_selected_range_rejects_replaced_session_symlink(tmp_path, monkeypatch):
    session = _selected_session(tmp_path, monkeypatch)
    path = Path(session.source_path)
    content = path.read_bytes()
    path.unlink()
    target = tmp_path / "outside.jsonl"
    target.write_bytes(content)
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ps.ProviderSessionError, match="regular file|changed"):
        ps.hash_selected_jsonl_lines(session, 1, 1)


def _selected_session(tmp_path, monkeypatch):
    session_id = str(uuid.uuid4())
    path = tmp_path / "claude" / "projects" / "p" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"role":"user"}\n{"role":"assistant"}\n', encoding="utf-8")
    monkeypatch.setattr(ps.shutil, "which", lambda _executable: "/fake/runtime")
    row = ps.catalog_local_sessions(claude_config_dir=tmp_path / "claude", codex_home=tmp_path / "codex")["sessions"][0]
    return ps.ProviderSession(**{
        **{key: value for key, value in row.items() if key != "recovery_outcomes"},
        "resume_argv": tuple(row["resume_argv"]),
    })


def _live_recovery(project_id="project-1", version="v1"):
    return {
        "recovery": {"project_id": project_id, "sprint_version": version},
        "continuation": {
            "board": {
                "version_filter": version,
                "item_count": 1,
                "items": [{"id": "item-1", "title": "Continue task", "status": "in_progress"}],
                "revision_hash": "a" * 64,
                "blocker_summary": "none",
            },
            "active_file_claims": [{"file_path": "src/module.py", "mode": "write", "expires_at": "2030-01-01"}],
        },
    }


def test_pack_links_local_range_live_board_and_git_state(tmp_path, monkeypatch):
    session = _selected_session(tmp_path, monkeypatch)
    selected_range = ps.hash_selected_jsonl_lines(session, 1, 2)
    monkeypatch.setattr(ps, "_git_state", lambda _repo: {"repo_root": "C:/repo", "git_head": "abc123", "worktree_state": "dirty", "changed_paths": ["a.py"]})

    pack = ps.build_local_context_pack(
        session,
        project_id="project-1",
        sprint_version="v1",
        task_state={
            "objective": {"text": "Finish recovery catalog", "source_refs": ["meridian-live-board"]},
            "current_step": "Inspect selected provider range",
            "next_action": {"text": "Run the provider recovery tests", "source_refs": ["provider-range-0"]},
            "recent_transcript": {"text": "The selected turns describe the remaining catalog hardening.", "source_refs": ["provider-range-0"]},
            "command_error_ledger": [{"command": "pytest focused suite", "result": "pass", "source_refs": ["provider-range-0"]}],
        },
        meridian_recovery=_live_recovery(),
        transcript_range=selected_range,
        repo_root=tmp_path,
        clock=lambda: 1_791_000_000,
    )

    assert pack["coverage"]["complete"] is True
    assert pack["recovery_outcomes"]["reconstructable"] == "ready"
    assert pack["recovery_outcomes"]["restorable"] == "not_proven"
    assert pack["verified_state"]["meridian"]["items"][0]["id"] == "item-1"
    assert pack["verified_state"]["git_head"] == "abc123"
    assert pack["source"]["range"]["sha256"] == selected_range["sha256"]
    assert pack["task"]["recent_transcript"]["source_refs"] == ["provider-range-0"]
    assert pack["task"]["command_error_ledger"][0]["result"] == "pass"
    assert pack["integrity"]["canonical_sha256"] == ps._canonical_hash(pack)


def test_pack_marks_missing_live_board_and_range_as_incomplete(tmp_path, monkeypatch):
    session = _selected_session(tmp_path, monkeypatch)
    pack = ps.build_local_context_pack(
        session,
        project_id="project-1",
        sprint_version="v1",
        task_state={"objective": "Resume", "next_action": "Check the live board"},
    )

    assert pack["coverage"]["complete"] is False
    assert pack["recovery_outcomes"]["reconstructable"] == "incomplete"
    assert pack["verified_state"]["meridian"]["available"] is False


def test_pack_rejects_cross_project_recovery_state(tmp_path, monkeypatch):
    session = _selected_session(tmp_path, monkeypatch)
    with pytest.raises(ps.ProviderSessionError, match="different project"):
        ps.build_local_context_pack(
            session,
            project_id="project-1",
            sprint_version="v1",
            task_state={"objective": "Resume", "next_action": "Check board"},
            meridian_recovery=_live_recovery(project_id="other-project"),
        )


def test_pack_redacts_secret_shaped_summary(monkeypatch, tmp_path):
    session = _selected_session(tmp_path, monkeypatch)

    def reject_secret(text, *, context):
        if "SECRET" in text:
            raise ValueError("secret found")

    monkeypatch.setattr(ps, "check_for_secrets", reject_secret)
    with pytest.raises(ps.ProviderSessionError, match="secret found"):
        ps.build_local_context_pack(
            session,
            project_id="project-1",
            sprint_version="v1",
            task_state={"objective": "SECRET", "next_action": "Continue"},
        )


def test_pack_rejects_unknown_task_source_reference(tmp_path, monkeypatch):
    session = _selected_session(tmp_path, monkeypatch)
    with pytest.raises(ps.ProviderSessionError, match="unknown source ref"):
        ps.build_local_context_pack(
            session,
            project_id="project-1",
            sprint_version="v1",
            task_state={
                "objective": {"text": "Resume", "source_refs": ["missing-ref"]},
                "next_action": "Continue",
            },
        )


def test_pack_rejects_task_context_over_limit(tmp_path, monkeypatch):
    session = _selected_session(tmp_path, monkeypatch)
    with pytest.raises(ps.ProviderSessionError, match="32-entry limit"):
        ps.build_local_context_pack(
            session,
            project_id="project-1",
            sprint_version="v1",
            task_state={
                "objective": "Resume",
                "next_action": "Continue",
                "constraints": ["bounded"] * 33,
            },
        )


def test_pack_validates_artifact_status_text(tmp_path, monkeypatch):
    session = _selected_session(tmp_path, monkeypatch)
    with pytest.raises(ps.ProviderSessionError, match="artifact_refs\\[0\\]\\.status must be a string"):
        ps.build_local_context_pack(
            session,
            project_id="project-1",
            sprint_version="v1",
            task_state={"objective": "Resume", "next_action": "Continue"},
            artifact_refs=[{"uri": "meridian://artifact/1", "status": {"unsafe": "shape"}}],
        )


def test_local_context_pack_roundtrip_and_tamper_detection(tmp_path, monkeypatch):
    session = _selected_session(tmp_path, monkeypatch)
    pack = ps.build_local_context_pack(
        session,
        project_id="project-1",
        sprint_version="v1",
        task_state={"objective": "Resume", "next_action": "Review local state"},
    )

    path = ps.write_local_context_pack(tmp_path / "state", pack)

    assert ps.read_local_context_pack(path) == pack
    path.write_text(path.read_text(encoding="utf-8").replace("Review local state", "tampered state"), encoding="utf-8")
    assert ps.read_local_context_pack(path) is None


def test_cli_catalog_is_json_and_does_not_read_transcript_content(tmp_path, monkeypatch, capsys):
    root = tmp_path / "claude"
    transcript = root / "projects" / "p" / f"{uuid.uuid4()}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"text":"MUST_NOT_APPEAR"}\n', encoding="utf-8")
    monkeypatch.setattr(ps.shutil, "which", lambda _executable: "/fake/runtime")

    rc = ps.cli_main(["catalog", "--claude-root", str(root), "--codex-home", str(tmp_path / "codex")])
    output = capsys.readouterr().out

    assert rc == 0
    assert "MUST_NOT_APPEAR" not in output
    assert json.loads(output)["sessions"]


def test_shared_cli_dispatches_local_recovery_command(monkeypatch):
    from meridian import __main__ as cli

    monkeypatch.setattr(ps, "cli_main", lambda argv: 29)

    assert cli._dispatch_subcommand(["recovery", "catalog"]) == 29


def test_tray_executable_dispatches_local_recovery_command(monkeypatch):
    from meridian import __main__ as cli
    from meridian import tray_main

    monkeypatch.setattr(cli, "main", lambda argv: 31)

    assert tray_main.main(["recovery", "catalog"]) == 31
