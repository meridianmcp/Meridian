"""Tests for sprint item 88277b63 — external-job continuity."""
from __future__ import annotations

import json
import uuid

import pytest

from meridian import db as db_module
from meridian import external_job_register as model
from meridian.db import external_jobs as job_db
from meridian.db import migrations as db_migrations
from meridian import pg_adapter
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
async def test_register_update_history_and_terminal_guard(db, monkeypatch):
    project, session = await _session(db, "external-register-lifecycle")
    # Windows clocks can return the same microsecond for nearby DB operations.
    # Make the tie deterministic and ensure random UUID order cannot reorder history.
    monkeypatch.setattr(model, "utcnow_iso", lambda: "2026-01-01T00:00:00.000000+00:00")
    ids = iter(
        (
            uuid.UUID("10000000-0000-0000-0000-000000000000"),
            uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            uuid.UUID("00000000-0000-0000-0000-000000000000"),
            uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        )
    )
    monkeypatch.setattr(job_db.uuid, "uuid4", lambda: next(ids))
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
async def test_external_job_event_order_migration_backfills_and_tracks_inserts(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "legacy-external-events.db")
    try:
        await db.execute(
            "CREATE TABLE external_job_events ("
            "id TEXT PRIMARY KEY, external_job_id TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        timestamp = "2026-01-01T00:00:00.000000+00:00"
        await db.execute(
            "INSERT INTO external_job_events (id, external_job_id, created_at) VALUES (?, ?, ?)",
            ("z-first", "job", timestamp),
        )
        await db.execute(
            "INSERT INTO external_job_events (id, external_job_id, created_at) VALUES (?, ?, ?)",
            ("a-second", "job", timestamp),
        )
        await db_migrations._migrate_external_job_register(db)
        # Re-running the migration must replace (not preserve) the old trigger
        # and must not reset the durable counter.
        await db_migrations._migrate_external_job_register(db)

        async with db.execute(
            "SELECT id, event_order FROM external_job_events ORDER BY event_order ASC"
        ) as cur:
            existing = await cur.fetchall()
        assert [row[0] for row in existing] == ["z-first", "a-second"]
        assert [row[1] for row in existing] == [1, 2]

        await db.execute(
            "INSERT INTO external_job_events (id, external_job_id, created_at) VALUES (?, ?, ?)",
            ("m-third", "job", timestamp),
        )
        await db.execute("DELETE FROM external_job_events WHERE id = 'm-third'")
        await db.execute(
            "INSERT INTO external_job_events (id, external_job_id, created_at) VALUES (?, ?, ?)",
            ("n-fourth", "job", timestamp),
        )
        async with db.execute(
            "SELECT id, event_order FROM external_job_events ORDER BY event_order ASC"
        ) as cur:
            all_ids = await cur.fetchall()
        assert [row[0] for row in all_ids] == ["z-first", "a-second", "n-fourth"]
        assert [row[1] for row in all_ids] == [1, 2, 4]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_external_job_event_order_migration_handles_partial_backfill(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "partial-external-events.db")
    try:
        await db.execute(
            "CREATE TABLE external_job_events ("
            "id TEXT PRIMARY KEY, external_job_id TEXT NOT NULL, "
            "created_at TEXT NOT NULL, event_order INTEGER)"
        )
        timestamp = "2026-01-01T00:00:00.000000+00:00"
        await db.executemany(
            "INSERT INTO external_job_events "
            "(id, external_job_id, created_at, event_order) VALUES (?, ?, ?, ?)",
            [
                ("legacy-first", "job", timestamp, None),
                ("already-numbered", "job", timestamp, 50),
                ("legacy-second", "job", timestamp, None),
            ],
        )
        # Simulate the earlier migration trigger so this rollout must replace
        # its rowid-derived behavior without modifying existing rows.
        await db.execute(
            """CREATE TRIGGER trg_external_job_events_event_order
            AFTER INSERT ON external_job_events
            FOR EACH ROW WHEN NEW.event_order IS NULL
            BEGIN
                UPDATE external_job_events SET event_order = NEW.rowid
                WHERE rowid = NEW.rowid;
            END"""
        )

        await db_migrations._migrate_external_job_register(db)
        async with db.execute(
            "SELECT id, event_order FROM external_job_events ORDER BY event_order ASC"
        ) as cur:
            rows = await cur.fetchall()
        assert rows == [
            ("already-numbered", 50),
            ("legacy-first", 51),
            ("legacy-second", 52),
        ]

        await db.execute(
            "INSERT INTO external_job_events (id, external_job_id, created_at) VALUES (?, ?, ?)",
            ("new-event", "job", timestamp),
        )
        async with db.execute(
            "SELECT event_order FROM external_job_events WHERE id = 'new-event'"
        ) as cur:
            assert (await cur.fetchone())[0] == 53
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_postgres_external_job_event_order_migration_adds_sequence():
    class ScriptCapture:
        script = ""

        async def executescript(self, script):
            self.script = script

    connection = ScriptCapture()
    await pg_adapter._migrate_pg_external_job_register(connection)

    assert "pg_advisory_xact_lock(5062994, 88277)" in connection.script
    assert (
        "CREATE SEQUENCE IF NOT EXISTS external_job_events_event_order_seq"
        in connection.script
    )
    assert "event_order BIGINT NOT NULL" in connection.script
    assert "ADD COLUMN IF NOT EXISTS event_order BIGINT" in connection.script
    assert "ADD COLUMN IF NOT EXISTS event_order BIGSERIAL" not in connection.script
    assert "ALTER COLUMN event_order SET DEFAULT" in connection.script
    assert "ALTER SEQUENCE external_job_events_event_order_seq" in connection.script
    assert "CREATE TABLE IF NOT EXISTS external_job_event_order_migration" in connection.script
    assert "WITH missing AS" in connection.script
    assert "ROW_NUMBER() OVER (ORDER BY created_at ASC, id ASC)" in connection.script
    assert "SELECT setval('external_job_events_event_order_seq'::regclass" in connection.script
    assert "GREATEST(sequence_state.last_value, COALESCE(event_state.max_order, 1))" in connection.script
    assert "ON CONFLICT (id) DO NOTHING" in connection.script
    assert "ALTER COLUMN event_order SET NOT NULL" in connection.script
    assert "event_order ASC NULLS FIRST, id ASC" in connection.script


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
