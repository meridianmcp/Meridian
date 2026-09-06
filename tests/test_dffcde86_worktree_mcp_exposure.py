"""Tests for dffcde86 — MCP exposure of the active_worktrees registry.

Adds two read-only MCP tools, ``list_active_worktrees`` and
``list_worktrees_pending_cleanup``, wrapping the already-stable
``db_module.list_active_worktrees`` / ``db_module.list_worktrees_pending_cleanup``
helpers (see tests/test_a03c0eeb_worktree_disk_cleanup.py and
tests/test_e401221d_orphan_reaper.py for their own DB-level coverage — this
file only covers the new MCP surface: schema registration, dispatch, and
stdio transport parity).

Coordination note: ``meridian/mcp/handler.py`` was live whole-file write-
locked by a concurrent, active sibling executor session (session
"fix-e6f58c25") for the first ~28 minutes of this work (claimed
2026-09-06 07:24:54, confirmed live via get_session_activity, released
2026-09-06 07:52:03 once that sibling item completed). Per this repo's
"coordinate (request_hitl) before editing" protocol, a HITL request was filed
and auto-answered (aggressive HITL mode) to defer touching handler.py until
released; ``meridian/mcp_tools.py`` and ``meridian/mcp/stdio_handler.py`` were
edited first (both were unclaimed) while waiting. The lock cleared during
this session, the file was then claimed properly
(``claim_file(..., item_id="dffcde86-71f7-40a5-a5e4-6ba320267ed2")``) and the
two dispatch branches were added to the existing ``_handle_file_claims``
group (mirroring its ``get_file_claims`` branch) — so this file tests the
complete, working feature end to end on both transports.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
import meridian.server as srv
from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS

NEW_TOOL_NAMES = ("list_active_worktrees", "list_worktrees_pending_cleanup")


def _tool(name: str) -> dict:
    return next(t for t in _MCP_TOOLS_LIST if t["name"] == name)


# ---------------------------------------------------------------------------
# 1. Canonical schema (_MCP_TOOLS_LIST) checks — served verbatim by both the
#    HTTP /mcp tools/list handler (meridian/mcp/handler.py:
#    ``tools = list(_server._MCP_TOOLS_LIST)``) and the stdio transport's
#    list_tools() (via _shared_tool(), see part 3 below).
# ---------------------------------------------------------------------------


def test_tool_names_present_exactly_once():
    names = [t["name"] for t in _MCP_TOOLS_LIST if t["name"] in NEW_TOOL_NAMES]
    assert sorted(names) == sorted(NEW_TOOL_NAMES)


@pytest.mark.parametrize("name", NEW_TOOL_NAMES)
def test_schema_advertises_project_id_and_project_name_alternative(name):
    """Generic project-scoping contract (mirrors
    test_every_project_id_tool_schema_advertises_project_name in
    test_core.py): project_id present, project_name sibling present with the
    exact 'alternative to project_id' phrasing, neither required."""
    tool = _tool(name)
    props = tool["inputSchema"]["properties"]
    assert "project_id" in props
    assert props["project_id"]["type"] == "string"
    assert "project_name" in props
    assert props["project_name"]["type"] == "string"
    assert "alternative to project_id" in props["project_name"]["description"]
    required = tool["inputSchema"].get("required") or []
    assert "project_id" not in required
    assert "project_name" not in required


@pytest.mark.parametrize("name", NEW_TOOL_NAMES)
def test_tool_is_tagged_read_only(name):
    assert name in _READ_ONLY_TOOLS
    tool = _tool(name)
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["annotations"]["idempotentHint"] is True


@pytest.mark.parametrize("name", NEW_TOOL_NAMES)
def test_tool_category_and_role_relevance(name):
    tool = _tool(name)
    # Same category as the sibling file-coordination read tools
    # (get_file_claims, get_symbol_claims) it's designed to sit next to.
    assert tool["category"] == "file-locking"
    # "both" (not "executor" like get_file_claims): a planner session
    # legitimately wants worktree visibility too, per this item's own framing.
    assert tool["role_relevance"] == "both"


# ---------------------------------------------------------------------------
# 2. HTTP/MCP dispatch — _dispatch_mcp_tool routes to the real DB helpers via
#    the _handle_file_claims group (mirrors test_get_file_claims_mcp_dispatch
#    in tests/test_file_claims.py).
# ---------------------------------------------------------------------------


async def test_list_active_worktrees_mcp_dispatch(db):
    p = await db_module.create_project(db, "dffcde86-active-dispatch")
    session = await db_module.register_session(db, p["id"], "wt-dispatch-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/dffcde86", ".claude/worktrees/dffcde86",
    )

    result = await srv._dispatch_mcp_tool(
        "list_active_worktrees", {"project_id": p["id"]}, db, "/tmp",
    )

    assert len(result) == 1
    assert result[0]["id"] == wt["id"]
    assert result[0]["session_name"] == "wt-dispatch-sess"


async def test_list_active_worktrees_mcp_dispatch_resolves_project_name(db):
    """project_name is resolved to project_id by _dispatch_mcp_tool's shared
    b6ab6e83 resolver before this tool ever sees args — same alternative-id
    contract every other project-scoped tool gets for free."""
    p = await db_module.create_project(db, "dffcde86-active-by-name")
    session = await db_module.register_session(db, p["id"], "wt-dispatch-sess-2")
    await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/byname", ".claude/worktrees/byname",
    )

    result = await srv._dispatch_mcp_tool(
        "list_active_worktrees", {"project_name": "dffcde86-active-by-name"}, db, "/tmp",
    )

    assert len(result) == 1


async def test_list_active_worktrees_mcp_dispatch_requires_project():
    """No project_id and no resolvable project_name -> a clean error dict,
    never a raw KeyError/TypeError from the underlying DB call."""
    result = await srv._dispatch_mcp_tool(
        "list_active_worktrees", {}, db=None, data_dir="/tmp",
    )
    assert result == {"error": "project_id is required (or pass project_name)"}


async def test_list_active_worktrees_excludes_removed(db):
    p = await db_module.create_project(db, "dffcde86-active-excludes-removed")
    session = await db_module.register_session(db, p["id"], "wt-removed-sess")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/gone", ".claude/worktrees/gone",
    )
    await db_module.remove_worktree(db, wt["id"])

    result = await srv._dispatch_mcp_tool(
        "list_active_worktrees", {"project_id": p["id"]}, db, "/tmp",
    )
    assert result == []


async def test_list_worktrees_pending_cleanup_mcp_dispatch(db):
    """MCP layer doesn't reshape/filter DB results, and correctly surfaces
    the terminal-item-status branch (db/__init__.py:8581)."""
    p = await db_module.create_project(db, "dffcde86-pending-dispatch")
    session = await db_module.register_session(db, p["id"], "wt-pending-sess")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "do the worktree thing")
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/pending", ".claude/worktrees/pending",
        item_id=item["id"],
    )

    # Not yet eligible — item still pending.
    not_yet = await srv._dispatch_mcp_tool(
        "list_worktrees_pending_cleanup", {"project_id": p["id"]}, db, "/tmp",
    )
    assert wt["id"] not in {w["id"] for w in not_yet}

    await db.execute(
        "UPDATE sprint_items SET status = 'done' WHERE id = ?", (item["id"],),
    )
    await db.commit()

    now_pending = await srv._dispatch_mcp_tool(
        "list_worktrees_pending_cleanup", {"project_id": p["id"]}, db, "/tmp",
    )
    assert wt["id"] in {w["id"] for w in now_pending}


async def test_list_worktrees_pending_cleanup_mcp_dispatch_project_optional(db):
    """Omitting project_id scopes across every project — matches the
    server-wide periodic sweep's own query (db/__init__.py:8581 docstring)."""
    p = await db_module.create_project(db, "dffcde86-pending-no-project")
    session = await db_module.register_session(db, p["id"], "wt-pending-sess-2")
    await db_module.close_session(db, session["id"])
    wt = await db_module.register_worktree(
        db, session["id"], p["id"], "worktree/noproj", ".claude/worktrees/noproj",
    )

    result = await srv._dispatch_mcp_tool(
        "list_worktrees_pending_cleanup", {}, db, "/tmp",
    )
    assert wt["id"] in {w["id"] for w in result}


# ---------------------------------------------------------------------------
# 3. stdio transport parity — schema served identically to the canonical one,
#    and calling it via the stdio call_tool() closure dispatches to the same
#    real data (not just schema-visible).
# ---------------------------------------------------------------------------


def _build_stdio_server(monkeypatch, db):
    """Build the stdio MCP server with its lazy DB pinned to ``db``.

    Same pattern as tests/test_325276f8_start_session_schema_parity.py and
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
    import json

    import mcp.types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments)
        )
    )
    return json.loads(called.root.content[0].text)


@pytest.mark.parametrize("name", NEW_TOOL_NAMES)
async def test_stdio_schema_matches_canonical_schema(db, monkeypatch, name):
    server = _build_stdio_server(monkeypatch, db)
    tools = await _stdio_list_tools(server)
    stdio_tool = next(t for t in tools if t.name == name)

    canonical = _tool(name)
    assert stdio_tool.inputSchema == canonical["inputSchema"]
    assert stdio_tool.description == canonical["description"]


async def test_stdio_call_list_active_worktrees_returns_real_data(db, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    project = await db_module.create_project(db, "dffcde86-stdio-active")
    session = await db_module.register_session(db, project["id"], "stdio-sess")
    wt = await db_module.register_worktree(
        db, session["id"], project["id"], "worktree/stdio", ".claude/worktrees/stdio",
    )

    result = await _stdio_call(
        server, "list_active_worktrees", {"project_id": project["id"]},
    )

    assert isinstance(result, list)
    assert result[0]["id"] == wt["id"]


async def test_stdio_call_list_worktrees_pending_cleanup_returns_real_data(db, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    project = await db_module.create_project(db, "dffcde86-stdio-pending")
    session = await db_module.register_session(db, project["id"], "stdio-sess-2")
    await db_module.close_session(db, session["id"])
    wt = await db_module.register_worktree(
        db, session["id"], project["id"], "worktree/stdio2", ".claude/worktrees/stdio2",
    )

    result = await _stdio_call(
        server, "list_worktrees_pending_cleanup", {"project_id": project["id"]},
    )

    assert isinstance(result, list)
    assert wt["id"] in {w["id"] for w in result}
