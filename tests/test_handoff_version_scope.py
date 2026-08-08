"""Coverage for 325276f8: start_session's `version` sprint-scoping stays
correct and IDENTICAL across roles ('executor' and 'planner') and across the
HTTP and stdio MCP transports, now that stdio delegates to the exact same
``handle_start_session`` implementation HTTP uses (see
tests/test_325276f8_start_session_schema_parity.py for the schema-parity
half of this fix).

Root cause context: the 2026-08-05 field report that prompted 325276f8 noted
that role='planner' was rejected by a stale connector schema before the
call ever reached the server, but that *omitting* role succeeded and
returned "an effective v0.2.6 scope" — i.e. version scoping itself was never
broken server-side, only the ability to reach the server with role='planner'
at all. This file locks in that version scoping keeps working identically
now that role='planner' is a reachable, schema-legal value, on both
transports, and that the two transports resolve the same scope for
identical input (a real risk once stdio started sharing HTTP's dispatch
path — this proves it didn't regress).
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.mcp import handler as mh


def _build_stdio_server(monkeypatch, db):
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


async def _stdio_call(server, name, arguments):
    import mcp.types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments)
        )
    )
    return json.loads(called.root.content[0].text)


# ---------------------------------------------------------------------------
# 1. HTTP/streamable-HTTP: explicit version scoping resolves identically for
#    both role='executor' and role='planner'.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["executor", "planner"])
async def test_http_start_session_version_scope_consistent_across_roles(db, role):
    project = await db_module.create_project(db, f"http-version-scope-{role}")
    await db_module.add_sprint_item(db, project["id"], "v1", "v1 item", force=True)
    await db_module.add_sprint_item(db, project["id"], "v2", "v2 item", force=True)

    result = await mh._dispatch_mcp_tool(
        "start_session",
        {
            "project_id": project["id"],
            "session_name": f"http-scope-{role}",
            "role": role,
            "version": "v2",
        },
        db, "/tmp",
    )
    assert "error" not in result
    assert result["sprint_version"] == "v2"


# ---------------------------------------------------------------------------
# 2. stdio: same guarantee, now reachable at all for role='planner' since the
#    schema fix, and routed through the shared dispatch since the handler fix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["executor", "planner"])
async def test_stdio_start_session_version_scope_consistent_across_roles(
    db, monkeypatch, role
):
    project = await db_module.create_project(db, f"stdio-version-scope-{role}")
    await db_module.add_sprint_item(db, project["id"], "v1", "v1 item", force=True)
    await db_module.add_sprint_item(db, project["id"], "v2", "v2 item", force=True)

    server = _build_stdio_server(monkeypatch, db)
    result = await _stdio_call(
        server,
        "start_session",
        {
            "project_id": project["id"],
            "session_name": f"stdio-scope-{role}",
            "role": role,
            "version": "v2",
        },
    )
    assert "error" not in result
    assert result["sprint_version"] == "v2"


# ---------------------------------------------------------------------------
# 3. Cross-transport parity: identical input resolves to the identical scope
#    on both transports — the concrete regression risk introduced by routing
#    stdio's start_session through the same dispatch HTTP uses.
# ---------------------------------------------------------------------------


async def test_stdio_and_http_start_session_return_same_version_scope(db, monkeypatch):
    project = await db_module.create_project(db, "cross-transport-version-scope")
    await db_module.add_sprint_item(db, project["id"], "v1", "unscoped item", force=True)
    await db_module.add_sprint_item(db, project["id"], "v3", "target item", force=True)

    http_result = await mh._dispatch_mcp_tool(
        "start_session",
        {
            "project_id": project["id"],
            "session_name": "http-cross",
            "role": "planner",
            "version": "v3",
        },
        db, "/tmp",
    )

    server = _build_stdio_server(monkeypatch, db)
    stdio_result = await _stdio_call(
        server,
        "start_session",
        {
            "project_id": project["id"],
            "session_name": "stdio-cross",
            "role": "planner",
            "version": "v3",
        },
    )

    assert http_result["sprint_version"] == "v3"
    assert stdio_result["sprint_version"] == "v3"


# ---------------------------------------------------------------------------
# 4. Omitting role (the exact call shape the 2026-08-05 field report noted
#    already worked pre-fix) still auto-infers a scope on both transports —
#    a backward-compatibility guard for the schema/dispatch change.
# ---------------------------------------------------------------------------


async def test_start_session_omitted_role_still_autoscopes_both_transports(db, monkeypatch):
    project = await db_module.create_project(db, "omitted-role-autoscope")
    await db_module.add_sprint_item(db, project["id"], "v4", "only version", force=True)

    http_result = await mh._dispatch_mcp_tool(
        "start_session",
        {"project_id": project["id"], "session_name": "http-noscope"},
        db, "/tmp",
    )
    assert "error" not in http_result
    assert http_result["sprint_version"] == "v4"

    server = _build_stdio_server(monkeypatch, db)
    stdio_result = await _stdio_call(
        server,
        "start_session",
        {"project_id": project["id"], "session_name": "stdio-noscope"},
    )
    assert "error" not in stdio_result
    assert stdio_result["sprint_version"] == "v4"
