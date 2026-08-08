"""Coverage for 325276f8: keep the connector/server ``start_session``
role/version/compact/mode schema in parity across the HTTP, streamable-HTTP,
and stdio MCP transports, plus a fresh-session smoke test proving
role='planner' and role='executor' both validate against the advertised
schema and dispatch successfully end-to-end.

Live field report (2026-08-05): the deployed start_session server has always
accepted planner-oriented session behavior (see
``meridian.mcp_tools._select_active_tool_set``, which explicitly branches on
role="executor" vs role="planner"), but the active connector schema rejected
role='planner' before dispatch with an enum validation error — a
client/connector manifest mismatch, not a server bug. Root cause: the stdio
transport's ``start_session`` Tool schema (meridian/mcp/stdio_handler.py) was
a hand-duplicated copy of the canonical schema in meridian/mcp_tools.py that
had drifted — its "role" enum only ever listed "executor", and it was
missing "compact"/"mode" entirely. The fix:

1. meridian/mcp_tools.py's canonical start_session schema now allows
   role in {"executor", "planner"}.
2. meridian/mcp/stdio_handler.py's start_session Tool is now generated from
   that exact same schema via ``_shared_tool()`` (the pattern already used
   for execute_batch / batch_read / batch_mutate) instead of being
   hand-duplicated, so the two surfaces can no longer drift apart on this
   tool.
3. The stdio dispatch branch now routes through ``_dispatch_mcp_tool`` (the
   same function the HTTP/streamable-HTTP transport uses) instead of calling
   ``_start_session_composite`` directly, so stdio also gets
   capability_contract, execution_policy, active_tool_set, pending_goal
   delivery, the /goal-skill setup_warning check, and mode="continue"
   fast-resume — all of which it silently lacked before.

NOTE: this file was originally authored (on now-orphaned branch
worktree-wf_d4373b23-8e3-13, commit 8fcdf88b) as ``tests/test_mcp_dispatch.py``.
That filename has since been claimed by an unrelated 627187b8 execute_batch
transport-parity suite on dev, so this rescue pass (325276f8) re-lands the
same coverage under a collision-free name instead.
"""
from __future__ import annotations

import json

from meridian import db as db_module
import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.mcp import handler as mh
from meridian.mcp_tools import _MCP_TOOLS_LIST


def _canonical_start_session_schema() -> dict:
    return next(t for t in _MCP_TOOLS_LIST if t["name"] == "start_session")


def _build_stdio_server(monkeypatch, db):
    """Build the stdio MCP server with its lazy DB pinned to ``db``.

    Same pattern as tests/test_stdio_handoff_arg_parity.py and
    tests/test_cov_handler.py's ``_stdio_server``.
    """
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


async def _stdio_list_tools(server):
    import mcp.types as mcp_types

    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    return listed.root.tools


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
# 1. The canonical schema itself allows role='planner' (previously only
#    'executor' — this is the literal enum the 2026-08-05 field report says
#    a strict connector-side validator was rejecting role='planner' against).
# ---------------------------------------------------------------------------


def test_canonical_start_session_schema_allows_planner_and_executor_role():
    schema = _canonical_start_session_schema()
    role_enum = schema["inputSchema"]["properties"]["role"]["enum"]
    assert set(role_enum) == {"executor", "planner"}


# ---------------------------------------------------------------------------
# 2. tools/list schema parity test: the stdio-advertised start_session schema
#    is now IDENTICAL to the canonical HTTP/streamable-HTTP schema (generated
#    via _shared_tool(), not hand-duplicated) — the actual parity assertion
#    the sprint item asked for.
# ---------------------------------------------------------------------------


async def test_stdio_start_session_schema_matches_canonical_schema(db, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    tools = await _stdio_list_tools(server)
    tool = next(t for t in tools if t.name == "start_session")

    canonical = _canonical_start_session_schema()

    assert tool.inputSchema == canonical["inputSchema"]
    assert tool.description == canonical["description"]

    # Re-assert the specific fields the field report + sprint notes flagged.
    props = tool.inputSchema["properties"]
    assert set(props["role"]["enum"]) == {"executor", "planner"}
    # "compact" and "mode" were entirely absent from the old hand-duplicated
    # stdio schema even though the dispatch always silently accepted (and,
    # for "compact", used) them.
    assert "compact" in props
    assert "mode" in props
    assert "version" in props


# ---------------------------------------------------------------------------
# 3. Fresh-session smoke test: role='planner' and role='executor' both
#    validate against the advertised schema (would have been rejected
#    pre-fix by a client validating role='planner' against the old enum)
#    and dispatch successfully end-to-end over the stdio transport, each
#    producing the role-appropriate active_tool_set.
# ---------------------------------------------------------------------------


async def test_stdio_fresh_session_smoke_role_planner_and_executor(db, monkeypatch):
    project = await db_module.create_project(db, "role-parity-smoke")
    server = _build_stdio_server(monkeypatch, db)

    planner_result = await _stdio_call(
        server,
        "start_session",
        {
            "project_id": project["id"],
            "session_name": "planner-smoke",
            "role": "planner",
        },
    )
    assert "error" not in planner_result
    assert planner_result["active_tool_set"]["role"] == "planner"

    executor_result = await _stdio_call(
        server,
        "start_session",
        {
            "project_id": project["id"],
            "session_name": "executor-smoke",
            "role": "executor",
        },
    )
    assert "error" not in executor_result
    assert executor_result["active_tool_set"]["role"] == "executor"


async def test_http_fresh_session_smoke_role_planner_and_executor(db):
    """Same smoke test over the HTTP/streamable-HTTP dispatch path, proving
    both transports agree — role='planner' was never rejected server-side,
    only by the stale connector schema."""
    project = await db_module.create_project(db, "http-role-parity-smoke")

    planner_result = await mh._dispatch_mcp_tool(
        "start_session",
        {"project_id": project["id"], "session_name": "http-planner-smoke", "role": "planner"},
        db, "/tmp",
    )
    assert "error" not in planner_result
    assert planner_result["active_tool_set"]["role"] == "planner"

    executor_result = await mh._dispatch_mcp_tool(
        "start_session",
        {"project_id": project["id"], "session_name": "http-executor-smoke", "role": "executor"},
        db, "/tmp",
    )
    assert "error" not in executor_result
    assert executor_result["active_tool_set"]["role"] == "executor"


# ---------------------------------------------------------------------------
# 4. Behavioral distinction survives on stdio too: role='executor' injects
#    executor_config guidance (full/compact=False payload only); role='planner'
#    does not. Proves the stdio dispatch fix is a real behavior superset, not
#    just a schema patch.
# ---------------------------------------------------------------------------


async def test_stdio_start_session_executor_role_injects_executor_config_planner_does_not(
    db, monkeypatch
):
    project = await db_module.create_project(db, "executor-config-role-gate")
    server = _build_stdio_server(monkeypatch, db)

    executor_full = await _stdio_call(
        server,
        "start_session",
        {
            "project_id": project["id"],
            "session_name": "executor-full",
            "role": "executor",
            "compact": False,
        },
    )
    assert "error" not in executor_full
    assert "executor_config" in executor_full

    planner_full = await _stdio_call(
        server,
        "start_session",
        {
            "project_id": project["id"],
            "session_name": "planner-full",
            "role": "planner",
            "compact": False,
        },
    )
    assert "error" not in planner_full
    assert "executor_config" not in planner_full


# ---------------------------------------------------------------------------
# 5. capability-contract field parity: stdio start_session now surfaces
#    capability_contract, matching the HTTP/streamable-HTTP transport — the
#    "capability-contract fields" half of the 2026-08-05 field report's ask.
# ---------------------------------------------------------------------------


async def test_stdio_start_session_surfaces_capability_contract(db, monkeypatch):
    project = await db_module.create_project(db, "capability-contract-parity")
    server = _build_stdio_server(monkeypatch, db)

    result = await _stdio_call(
        server,
        "start_session",
        {"project_id": project["id"], "session_name": "cap-contract-smoke"},
    )
    assert "error" not in result
    assert "capability_contract" in result
