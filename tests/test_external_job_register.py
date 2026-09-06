"""Tests for sprint item 88277b63 — external-job continuity."""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import external_job_register as model
from meridian.db import external_jobs as job_db
import meridian.mcp_tools as mcp_tools
import meridian.server as server


async def _session(db, prefix: str):
    project = await db_module.create_project(db, prefix)
    session = await db_module.register_session(db, project["id"], f"{prefix}-session")
    return project, session


def test_external_job_validation_rejects_secrets_and_shared_absolute_paths():
    with pytest.raises(ValueError, match="Refusing to persist"):
        model.validate_job_fields(
            status="running",
            resume_hint="resume with TOKEN=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
    with pytest.raises(ValueError, match="machine-local absolute paths"):
        model.validate_job_fields(status="running", check_hint="read C:\\Users\\adam\\pod.log")
    with pytest.raises(ValueError, match="machine-local absolute paths"):
        model.validate_job_identity(
            job_key="build", provider="ssh", external_id="C:\\Users\\adam\\pod.id"
        )


def test_external_job_validation_and_snapshot_failures_are_structured(tmp_path):
    with pytest.raises(ValueError, match="status must be one of"):
        model.validate_external_status("not-a-status")
    with pytest.raises(ValueError, match="phase must be a string"):
        model.validate_job_fields(status="running", phase=42)
    with pytest.raises(ValueError, match="metadata must be an object"):
        model.validate_metadata(["not", "an", "object"])
    with pytest.raises(ValueError, match="non-JSON value"):
        model.validate_metadata({"bad": object()})
    with pytest.raises(ValueError, match="exceeds 50000"):
        model.validate_metadata({"large": ["x" * 8_000] * 7})

    data_dir_file = tmp_path / "data-dir-file"
    data_dir_file.write_text("not a directory", encoding="utf-8")
    failure = model.write_local_status_snapshot(data_dir_file, "project-1", [])
    assert failure["ok"] is False
    assert model.read_local_status_snapshot(tmp_path, "missing-project") is None


def test_external_job_snapshot_is_atomic_and_readable(tmp_path):
    result = model.write_local_status_snapshot(
        tmp_path,
        "project-1",
        [{"job_key": "build", "status": "running"}],
    )
    assert result["ok"] is True
    assert model.external_job_snapshot_path(tmp_path, "project-1").exists()
    payload = model.read_local_status_snapshot(tmp_path, "project-1")
    assert payload["schema_version"] == 1
    assert payload["jobs"][0]["job_key"] == "build"


@pytest.mark.asyncio
async def test_register_update_history_and_terminal_guard(db):
    project, session = await _session(db, "external-register-lifecycle")
    job = await job_db.register_external_job(
        db,
        project["id"],
        session["id"],
        job_key="gps-slam-build",
        provider="runpod",
        external_id="pod-123",
        phase="compile",
        check_hint="query pod status",
        resume_hint="resume from the next safe build step",
        metadata={"host": "gpu-01", "port": 2222},
    )
    assert job["status"] == "running"
    assert job["metadata"] == {"host": "gpu-01", "port": 2222}

    observed = await job_db.update_external_job(
        db,
        project["id"],
        session["id"],
        job_key="gps-slam-build",
        phase="compile",
        detail="compiler still active",
    )
    assert observed["last_observed_at"]
    full = await job_db.get_external_job(
        db, project["id"], job_key="gps-slam-build", include_history=True
    )
    assert len(full["history"]) == 2
    assert full["history"][0]["event_kind"] == "registered"

    completed = await job_db.complete_external_job(
        db, project["id"], session["id"], job_key="gps-slam-build", detail="verified"
    )
    assert completed["status"] == "succeeded"
    with pytest.raises(ValueError, match="cannot transition"):
        await job_db.update_external_job(
            db, project["id"], session["id"], job_key="gps-slam-build", status="running"
        )


@pytest.mark.asyncio
async def test_register_is_idempotent_for_same_identity_and_rejects_replacement(db):
    project, session = await _session(db, "external-register-idempotency")
    kwargs = dict(
        job_key="build", provider="ssh", external_id="host-job-7", status="queued"
    )
    first = await job_db.register_external_job(db, project["id"], session["id"], **kwargs)
    second = await job_db.register_external_job(db, project["id"], session["id"], **kwargs)
    assert first["id"] == second["id"]
    with pytest.raises(ValueError, match="different external job"):
        await job_db.register_external_job(
            db, project["id"], session["id"], **{**kwargs, "external_id": "host-job-8"}
        )
    rows = await job_db.list_external_jobs(db, project["id"], include_terminal=True)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_project_and_session_scope_are_enforced(db):
    project, session = await _session(db, "external-register-scope-a")
    other_project, other_session = await _session(db, "external-register-scope-b")
    await job_db.register_external_job(
        db, project["id"], session["id"], job_key="job", provider="ci", external_id="42"
    )
    assert await job_db.get_external_job(db, other_project["id"], job_key="job") is None
    with pytest.raises(ValueError, match="does not belong"):
        await job_db.update_external_job(
            db, project["id"], other_session["id"], job_key="job", phase="wrong project"
        )


@pytest.mark.asyncio
async def test_mcp_register_list_and_brief_expose_recovery_state(db, tmp_path):
    project, session = await _session(db, "external-register-mcp")
    result = await server._dispatch_mcp_tool(
        "register_external_job",
        {
            "project_id": project["id"],
            "session_id": session["id"],
            "job_key": "gps-slam-build",
            "provider": "runpod",
            "external_id": "pod-55",
            "phase": "upload",
            "check_hint": "check transfer process",
            "resume_hint": "continue the tarball upload",
        },
        db,
        str(tmp_path),
    )
    assert result["job"]["external_id"] == "pod-55"
    assert result["task_log"]["description"].startswith("External job registered:")
    assert result["local_snapshot"]["ok"] is True

    listed = await server._dispatch_mcp_tool(
        "list_external_jobs", {"project_id": project["id"]}, db, str(tmp_path)
    )
    assert listed["count"] == 1
    assert listed["local_snapshot"]["exists"] is True

    brief = await server._dispatch_mcp_tool(
        "get_session_brief",
        {"project_id": project["id"], "session_id": session["id"], "role": "executor"},
        db,
        str(tmp_path),
    )
    assert '<external_jobs count="1">' in brief["text"]
    assert "continue the tarball upload" in brief["text"]


def test_mcp_tools_advertise_external_job_surface():
    names = {tool["name"] for tool in mcp_tools._MCP_TOOLS_LIST}
    assert {
        "register_external_job", "update_external_job", "get_external_job",
        "list_external_jobs", "complete_external_job",
    } <= names
    read_only = {name for name in mcp_tools._READ_ONLY_TOOLS}
    assert {"get_external_job", "list_external_jobs"} <= read_only


@pytest.mark.asyncio
async def test_stdio_transport_dispatches_external_job_tools(db, tmp_path, monkeypatch):
    """Claude Desktop's stdio entrypoint must execute the advertised tools."""
    import mcp.types as mcp_types
    import meridian.server as server_module

    project, session = await _session(db, "external-register-stdio")

    async def _return_db(*_args, **_kwargs):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    server_instance, _run_stdio = server_module.build_mcp_server()
    call_handler = server_instance.request_handlers[mcp_types.CallToolRequest]
    response = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="register_external_job",
                arguments={
                    "project_id": project["id"],
                    "session_id": session["id"],
                    "job_key": "gps-slam-build",
                    "provider": "runpod",
                    "external_id": "pod-stdio-1",
                    "phase": "compile",
                    "check_hint": "query pod status",
                    "resume_hint": "resume from the next safe build step",
                },
            )
        )
    )
    payload = json.loads(response.root.content[0].text)
    assert payload["job"]["external_id"] == "pod-stdio-1"
    assert payload["local_snapshot"]["ok"] is True
