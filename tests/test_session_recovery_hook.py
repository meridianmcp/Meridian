from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from meridian import session_recovery as recovery


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / ".claude" / "hooks" / "session_recovery_hook.py"
SPEC = importlib.util.spec_from_file_location("session_recovery_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def _registration_input() -> dict:
    return {
        "project_id": "project-1",
        "session_id": "meridian-session-1",
        "transport": "remote_control",
        "client_type": "claude-code",
        "local_identity": {
            "local_session_id": "untrusted-model-value",
            "bridge_id": "bridge-private-1",
            "environment_id": "environment-private-1",
            "local_transcript_path": "C:/Users/alice/.claude/private.jsonl",
            "argv": ["claude", "--resume", "untrusted-model-value"],
        },
    }


def test_pretool_persists_identity_locally_and_redacts_hosted_arguments(tmp_path):
    response = hook.handle_hook_payload(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__meridian__register_session_recovery",
            "session_id": "claude-provider-session-1",
            "tool_input": _registration_input(),
        },
        data_dir=tmp_path,
    )

    updated = response["hookSpecificOutput"]["updatedInput"]
    assert "local_identity" not in updated
    assert updated["verified_resumable"] is True
    assert len(updated["local_ref_id"]) == 32
    serialized = json.dumps(response)
    for secret_local_value in (
        "bridge-private-1",
        "environment-private-1",
        "private.jsonl",
        "untrusted-model-value",
    ):
        assert secret_local_value not in serialized

    snapshot_path = recovery.client_local_recovery_snapshot_path(tmp_path)
    assert snapshot_path.exists()
    snapshot = recovery.read_client_local_recovery_snapshot(tmp_path)
    record = snapshot["records"][updated["local_ref_id"]]
    assert record["provider_session_id"] == "claude-provider-session-1"
    assert record["local_identity"]["local_session_id"] == "claude-provider-session-1"
    assert record["local_identity"]["bridge_id"] == "bridge-private-1"
    assert record["resume_recipe"]


@pytest.mark.parametrize(
    ("session_id_present", "session_id"),
    [
        (False, None),
        (True, ""),
        (True, " \t "),
        (True, 123),
    ],
)
def test_pretool_refuses_local_identity_without_active_host_session_id(
    tmp_path, session_id_present, session_id
):
    tool_input = _registration_input()
    tool_input["local_identity"]["local_session_id"] = "forged-other-host-session"
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__meridian__register_session_recovery",
        "tool_input": tool_input,
    }
    if session_id_present:
        payload["session_id"] = session_id

    response = hook.handle_hook_payload(payload, data_dir=tmp_path)

    hook_output = response["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "deny"
    assert "updatedInput" not in hook_output
    assert not recovery.client_local_recovery_snapshot_path(tmp_path).exists()


def test_subagent_start_stop_are_local_and_recovery_context_is_session_bound(tmp_path):
    start = {
        "hook_event_name": "SubagentStart",
        "session_id": "claude-provider-session-1",
        "agent_id": "agent-42",
        "agent_type": "Workflow",
        "transcript_path": "C:/Users/alice/main.jsonl",
        "agent_transcript_path": "C:/Users/alice/agent.jsonl",
    }
    assert recovery.record_client_local_agent_lifecycle(tmp_path, start)["bound"] is False

    updated = recovery.prepare_client_local_registration(
        tmp_path,
        _registration_input(),
        provider_session_id="claude-provider-session-1",
    )
    local_ref_id = updated["local_ref_id"]
    stop = {
        "hook_event_name": "SubagentStop",
        "session_id": "claude-provider-session-1",
        "agent_id": "agent-42",
        "agent_type": "Workflow",
        "last_assistant_message": "This private final response must not be stored.",
        "agent_transcript_path": "C:/Users/alice/agent.jsonl",
    }
    assert recovery.record_client_local_agent_lifecycle(tmp_path, stop)["bound"] is True

    snapshot = recovery.read_client_local_recovery_snapshot(tmp_path)
    encoded = json.dumps(snapshot)
    assert "private final response" not in encoded
    assert "agent.jsonl" not in encoded
    agent = snapshot["records"][local_ref_id]["agents"]["agent-42"]
    assert agent["status"] == "completed"
    assert agent["agent_type"] == "Workflow"

    response = hook.handle_hook_payload(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "claude-provider-session-1",
            "tool_name": "mcp__meridian__get_session_recovery",
            "tool_input": {"project_id": "project-1", "session_id": "meridian-session-1"},
            "tool_response": {
                "structuredContent": {
                    "recovery": {
                        "local_ref_id": local_ref_id,
                        "meridian_session_id": "meridian-session-1",
                    }
                }
            },
        },
        data_dir=tmp_path,
    )
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "resume_recipe" in context
    assert "agent-42" in context
    assert "completed" in context
    assert "private final response" not in context
    assert "agent.jsonl" not in context


def test_posttool_refuses_local_context_for_a_different_session(tmp_path):
    updated = recovery.prepare_client_local_registration(
        tmp_path,
        _registration_input(),
        provider_session_id="claude-provider-session-1",
    )
    response = hook.handle_hook_payload(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "claude-provider-session-1",
            "tool_name": "mcp__meridian__get_session_recovery",
            "tool_input": {"session_id": "another-meridian-session"},
            "tool_response": {
                "recovery": {
                    "local_ref_id": updated["local_ref_id"],
                    "meridian_session_id": "meridian-session-1",
                }
            },
        },
        data_dir=tmp_path,
    )
    assert response is None


def test_posttool_refuses_local_context_for_different_active_host_session(tmp_path):
    updated = recovery.prepare_client_local_registration(
        tmp_path,
        _registration_input(),
        provider_session_id="claude-provider-session-a",
    )
    response = hook.handle_hook_payload(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "claude-provider-session-b",
            "tool_name": "mcp__meridian__get_session_recovery",
            "tool_input": {"session_id": "meridian-session-1"},
            "tool_response": {
                "recovery": {
                    "local_ref_id": updated["local_ref_id"],
                    "meridian_session_id": "meridian-session-1",
                }
            },
        },
        data_dir=tmp_path,
    )
    assert response is None


def test_posttool_suppresses_local_context_without_active_host_session_id(tmp_path):
    updated = recovery.prepare_client_local_registration(
        tmp_path,
        _registration_input(),
        provider_session_id="claude-provider-session-a",
    )
    response = hook.handle_hook_payload(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__meridian__get_session_recovery",
            "tool_input": {"session_id": "meridian-session-1"},
            "tool_response": {
                "recovery": {
                    "local_ref_id": updated["local_ref_id"],
                    "meridian_session_id": "meridian-session-1",
                }
            },
        },
        data_dir=tmp_path,
    )
    assert response is None


def test_settings_register_local_recovery_for_registration_and_subagents():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = settings["hooks"]
    pre = [entry for entry in hooks["PreToolUse"] if "register_session_recovery" in entry["matcher"]]
    post = [entry for entry in hooks["PostToolUse"] if "get_session_recovery" in entry["matcher"]]
    assert pre and post
    assert "SubagentStart" in hooks
    assert "SubagentStop" in hooks
    assert any("session_recovery_hook.py" in handler["command"] for entry in hooks["SubagentStart"] for handler in entry["hooks"])
    assert any("session_recovery_hook.py" in handler["command"] for entry in hooks["SubagentStop"] for handler in entry["hooks"])


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"])
def test_hook_ignores_unrelated_tool_and_event(event, tmp_path):
    assert hook.handle_hook_payload(
        {"hook_event_name": event, "tool_name": "Read", "tool_input": {}},
        data_dir=tmp_path,
    ) is None


def test_client_local_snapshot_handles_missing_corrupt_and_invalid_schema(tmp_path):
    import pytest

    snapshot_path = recovery.client_local_recovery_snapshot_path(tmp_path)
    assert recovery.read_client_local_recovery_snapshot(tmp_path) == {
        "schema_version": recovery.CLIENT_LOCAL_RECOVERY_SCHEMA_VERSION,
        "records": {},
        "pending_agents": {},
    }

    snapshot_path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        recovery.read_client_local_recovery_snapshot(tmp_path)

    snapshot_path.write_text(
        json.dumps({"schema_version": 99, "records": {}, "pending_agents": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid caller-local recovery snapshot"):
        recovery.read_client_local_recovery_snapshot(tmp_path)


def test_client_local_snapshot_atomic_write_failure_removes_temporary_file(tmp_path, monkeypatch):
    import pytest

    snapshot_path = recovery.client_local_recovery_snapshot_path(tmp_path)

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(recovery.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        recovery._write_client_local_snapshot_unlocked(snapshot_path, {"safe": True})

    assert not snapshot_path.exists()
    assert list(snapshot_path.parent.glob("*.tmp")) == []


def test_client_local_registration_rejects_invalid_identity_and_skips_empty_identity(tmp_path):
    import pytest

    assert recovery.prepare_client_local_registration(
        tmp_path, {"session_id": "meridian-session"}, provider_session_id=None
    ) is None

    with pytest.raises(ValueError, match="local_identity must be an object"):
        recovery.prepare_client_local_registration(
            tmp_path,
            {"local_identity": "provider-private"},
            provider_session_id=None,
        )

    invalid_argv = _registration_input()
    invalid_argv["local_identity"]["argv"] = "not-an-argv-list"
    with pytest.raises(ValueError, match="argv must be a bounded list"):
        recovery.prepare_client_local_registration(
            tmp_path, invalid_argv, provider_session_id=None
        )

    with pytest.raises(ValueError, match="control characters"):
        recovery.prepare_client_local_registration(
            tmp_path,
            _registration_input(),
            provider_session_id="provider-session\nforged",
        )


def test_client_local_data_dir_override_and_record_trimming(tmp_path, monkeypatch):
    monkeypatch.setenv("MERIDIAN_SESSION_RECOVERY_STATE_DIR", f"{tmp_path}/custom")
    assert recovery.default_client_local_recovery_data_dir() == tmp_path / "custom"

    rows = {
        "old": {"updated_at": "2026-01-01"},
        "missing": None,
        "new": {"updated_at": "2026-03-01"},
    }
    assert recovery._trim_local_rows(rows, 2) == {
        "old": {"updated_at": "2026-01-01"},
        "new": {"updated_at": "2026-03-01"},
    }
    assert recovery._trim_local_rows(rows, 3) is rows


def test_subagent_lifecycle_ignores_incomplete_events_and_preserves_start_time(tmp_path):
    assert recovery.record_client_local_agent_lifecycle(
        tmp_path,
        {"hook_event_name": "PreToolUse", "session_id": "provider-session", "agent_id": "a"},
    ) is None
    assert recovery.record_client_local_agent_lifecycle(
        tmp_path,
        {"hook_event_name": "SubagentStart", "session_id": " ", "agent_id": "a"},
    ) is None
    assert recovery.record_client_local_agent_lifecycle(
        tmp_path,
        {"hook_event_name": "SubagentStop", "session_id": "provider-session", "agent_id": " "},
    ) is None

    start = {
        "hook_event_name": "SubagentStart",
        "session_id": "provider-session",
        "agent_id": "agent-1",
        "agent_type": " ",
    }
    recovery.record_client_local_agent_lifecycle(tmp_path, start)
    first_start = recovery.read_client_local_recovery_snapshot(tmp_path)["pending_agents"][
        "provider-session"
    ]["agent-1"]["started_at"]
    recovery.record_client_local_agent_lifecycle(tmp_path, start)
    stopped = recovery.record_client_local_agent_lifecycle(
        tmp_path,
        {**start, "hook_event_name": "SubagentStop", "agent_type": None},
    )
    pending = recovery.read_client_local_recovery_snapshot(tmp_path)["pending_agents"][
        "provider-session"
    ]["agent-1"]
    assert stopped == {"ok": True, "bound": False, "status": "completed"}
    assert pending["started_at"] == first_start
    assert pending["agent_type"] == "unknown"


def test_client_local_recovery_context_rejects_unparseable_or_unmatched_results(tmp_path):
    assert recovery.client_local_recovery_context(
        tmp_path, "not-json", expected_session_id="meridian-session"
    ) is None
    assert recovery.client_local_recovery_context(
        tmp_path,
        {"recovery": {"local_ref_id": "ref", "meridian_session_id": "another-session"}},
        expected_session_id="meridian-session",
    ) is None
    assert recovery.client_local_recovery_context(
        tmp_path,
        {"recovery": {"local_ref_id": "missing", "meridian_session_id": "meridian-session"}},
        expected_session_id="meridian-session",
    ) is None
    assert recovery._extract_recovery_record({"text": "invalid json"}) is None
    assert recovery._extract_recovery_record([None, {"recovery": {"local_ref_id": "ref"}}]) == {
        "local_ref_id": "ref"
    }
