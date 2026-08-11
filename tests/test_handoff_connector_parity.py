"""Tests for 1bd5e810 — accept_handoff connector parity across MCP/stdio/HTTP.

The three transports must never advertise or execute a divergent
implementation of accept_handoff — see meridian/mcp/stdio_handler.py's
f46372e8/d0854621 comments for the exact prior incident class this guards
against (a tool implemented+dispatched on HTTP MCP but silently missing
from stdio's list_tools()/call_tool(), or vice versa). All three call sites
(mcp/handler.py's _handle_task_tools, mcp/stdio_handler.py's call_tool via
_dispatch_mcp_tool, routes/handoff.py's accept_handoff_endpoint) forward to
the exact same meridian.handoff.accept_handoff_envelope with equivalent
argument handling (presented_body -> strip_goal_token_banner before the
call, everything else passed straight through).

MCP dispatch and stdio share the SAME db fixture connection (mirrors
tests/test_stdio_handoff_arg_parity.py's _build_stdio_server pattern) so
their results are compared for exact equality against the same board state.
HTTP uses the separately-fixtured `client` TestClient (its own DB/lifespan,
per tests/conftest.py) — seeded with an equivalent scenario via its own
HTTP calls, and checked for the same result *shape* and *verdict* rather
than byte-identical output, since it is a genuinely different underlying
connection.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian import server as srv


def _build_stdio_server(monkeypatch, db):
    """Same pattern as tests/test_stdio_handoff_arg_parity.py's helper."""
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


async def _call_accept_handoff_stdio(server, arguments):
    import mcp.types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="accept_handoff",
                arguments=arguments,
            )
        )
    )
    return json.loads(called.root.content[0].text)


# ---------------------------------------------------------------------------
# Schema parity — stdio advertises the exact same schema mcp_tools.py declares.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_advertises_accept_handoff_with_shared_schema(db, monkeypatch):
    import mcp.types as mcp_types
    from meridian import mcp_tools as mcp_tools_module

    server = _build_stdio_server(monkeypatch, db)
    list_handler = server.request_handlers[mcp_types.ListToolsRequest]
    listed = await list_handler(mcp_types.ListToolsRequest())
    stdio_tool = next(t for t in listed.root.tools if t.name == "accept_handoff")

    canonical = next(
        t for t in mcp_tools_module._MCP_TOOLS_LIST if t["name"] == "accept_handoff"
    )
    assert stdio_tool.inputSchema["properties"].keys() == canonical["inputSchema"]["properties"].keys()
    assert stdio_tool.description == canonical["description"]


# ---------------------------------------------------------------------------
# MCP dispatch vs direct call vs stdio — same db, same result.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_dispatch_matches_direct_call(db, tmp_path):
    p = await db_module.create_project(db, "parity-mcp-direct")
    args = {
        "project_id": p["id"],
        "required_tools": ["meridian", "Serena"],
        "available_tools": ["meridian"],
    }
    direct = await handoff_module.accept_handoff_envelope(
        db, p["id"],
        required_tools=args["required_tools"], available_tools=args["available_tools"],
    )
    via_dispatch = await srv._dispatch_mcp_tool("accept_handoff", args, db, str(tmp_path))
    assert via_dispatch == direct
    assert via_dispatch["result"] == handoff_module.ACCEPT_RESULT_CAPABILITY_UNAVAILABLE


@pytest.mark.asyncio
async def test_stdio_matches_mcp_dispatch_for_ok_result(db, monkeypatch, tmp_path):
    p = await db_module.create_project(db, "parity-stdio-mcp-ok")
    args = {
        "project_id": p["id"],
        "required_tools": ["meridian"],
        "available_tools": ["meridian", "Serena"],
    }
    via_dispatch = await srv._dispatch_mcp_tool("accept_handoff", args, db, str(tmp_path))

    server = _build_stdio_server(monkeypatch, db)
    via_stdio = await _call_accept_handoff_stdio(server, args)

    assert via_stdio == via_dispatch
    assert via_stdio["accepted"] is True
    assert via_stdio["result"] == handoff_module.ACCEPT_RESULT_OK


@pytest.mark.asyncio
async def test_stdio_matches_mcp_dispatch_for_token_failure(db, monkeypatch, tmp_path):
    p = await db_module.create_project(db, "parity-stdio-mcp-token")
    args = {"project_id": p["id"], "goal_token": "definitely-not-a-real-token"}

    via_dispatch = await srv._dispatch_mcp_tool("accept_handoff", args, db, str(tmp_path))
    server = _build_stdio_server(monkeypatch, db)
    via_stdio = await _call_accept_handoff_stdio(server, args)

    assert via_stdio == via_dispatch
    assert via_stdio["result"] == handoff_module.ACCEPT_RESULT_STALE_HANDOFF


@pytest.mark.asyncio
async def test_stdio_presented_body_strips_banner_same_as_mcp(db, monkeypatch, tmp_path):
    """Both transports must strip the <goal_token>/SECURITY banner from a
    full pasted /goal block the same way before checking body_hash — see
    handoff.strip_goal_token_banner, reused identically by mcp/handler.py's
    accept_handoff branch and routes/handoff.py's accept_handoff_endpoint.
    """
    p = await db_module.create_project(db, "parity-body-banner")
    await db_module.set_goal(db, p["id"], "ship it", sprint="v1")
    await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")

    # Mint TWO independent handoffs (two distinct single-use tokens) over the
    # SAME body text — a token is consumed on first successful verification,
    # so the two transports below cannot share one token and still both see
    # "ok"; each needs its own fresh one. A valid-token result carries no
    # per-token data ({"valid": True, "reason": "ok"}, nothing else), so the
    # two full accept_handoff results are still expected to be byte-identical.
    _path1, content1, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
    )
    _path2, content2, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
    )
    import re
    token1 = re.search(r"<goal_token>([^<]+)</goal_token>", content1).group(1).strip()
    token2 = re.search(r"<goal_token>([^<]+)</goal_token>", content2).group(1).strip()

    args_dispatch = {"project_id": p["id"], "goal_token": token1, "presented_body": content1}
    args_stdio = {"project_id": p["id"], "goal_token": token2, "presented_body": content2}
    via_dispatch = await srv._dispatch_mcp_tool("accept_handoff", args_dispatch, db, str(tmp_path))
    server = _build_stdio_server(monkeypatch, db)
    via_stdio = await _call_accept_handoff_stdio(server, args_stdio)

    assert via_stdio == via_dispatch
    assert via_dispatch["accepted"] is True, via_dispatch


# ---------------------------------------------------------------------------
# HTTP — equivalent scenario, own DB, checked for the same verdict/shape.
# ---------------------------------------------------------------------------


def test_http_accept_handoff_capability_unavailable(client):
    proj = client.post("/projects", json={"name": "parity-http-capability"}).json()
    pid = proj["id"]
    resp = client.post(
        f"/projects/{pid}/handoff/accept",
        json={"required_tools": ["meridian", "Serena"], "available_tools": ["meridian"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] is False
    assert data["result"] == "CAPABILITY_UNAVAILABLE"
    assert data["capability_check"]["missing_tools"] == ["Serena"]
    # Same top-level shape as the MCP/stdio result.
    assert set(data.keys()) == {
        "accepted", "result", "reasons", "token_check",
        "capability_check", "tool_manifest_check", "board_check",
    }


def test_http_accept_handoff_ok_when_nothing_flags(client):
    proj = client.post("/projects", json={"name": "parity-http-ok"}).json()
    pid = proj["id"]
    resp = client.post(f"/projects/{pid}/handoff/accept", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] is True
    assert data["result"] == "ok"


def test_http_accept_handoff_unknown_project_returns_404(client):
    resp = client.post(
        "/projects/00000000-0000-0000-0000-000000000000/handoff/accept", json={}
    )
    assert resp.status_code == 404
