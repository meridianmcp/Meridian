"""3bbc13f7 — end-to-end coordination-flow integration test.

Exercises the real user path through the MCP dispatcher: create project -> set
goal + sprint items -> start session -> claim -> log task -> complete -> sprint
progress -> delta handoff. Covers the server/handler dispatch seams that unit
tests skip.
"""
from __future__ import annotations

import asyncio

from meridian import db as db_module
from meridian import server as mh  # server re-exports _dispatch_mcp_tool


def _run(coro):
    return asyncio.run(coro)


def test_end_to_end_project_sprint_session_flow(tmp_path):
    db = _run(db_module.init_db(":memory:"))
    out = str(tmp_path)
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "e2e-flow"}, db, out))
        pid = proj["id"]

        _run(mh._dispatch_mcp_tool(
            "set_goal",
            {"project_id": pid, "content": "ship the flow", "sprint": "v1"}, db, out))
        item_a = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "First task", "version": "v1"}, db, out))
        _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "Second task", "version": "v1"}, db, out))

        sess = _run(mh._dispatch_mcp_tool("start_session", {"project_id": pid}, db, out))
        sid = sess["session_id"]

        _run(mh._dispatch_mcp_tool(
            "claim_sprint_item",
            {"project_id": pid, "item_id": item_a["id"], "session_id": sid}, db, out))
        _run(mh._dispatch_mcp_tool(
            "log_task",
            {"session_id": sid, "project_id": pid, "description": "did first task"}, db, out))
        done = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": item_a["id"], "session_id": sid}, db, out))
        assert done["status"] == "done"

        prog = _run(mh._dispatch_mcp_tool(
            "get_sprint_progress", {"project_id": pid, "session_id": sid}, db, out))
        assert isinstance(prog, dict) and prog

        ho = _run(mh._dispatch_mcp_tool(
            "generate_handoff",
            {"project_id": pid, "session_id": sid, "mode": "delta"}, db, out))
        content = ho["content"] if isinstance(ho, dict) else str(ho)
        assert "Second task" in content   # pending item surfaced in the handoff
    finally:
        _run(db.close())
