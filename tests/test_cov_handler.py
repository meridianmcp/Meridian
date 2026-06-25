"""Coverage tests for meridian/mcp/handler.py — JSON-RPC dispatch + tool error paths.

Drives ``meridian.mcp.handler._handle_mcp_request`` and ``_dispatch_mcp_tool``
directly via asyncio.run (the same pattern as tests/test_tunnel_bridge.py). Most
error paths are exercised with ``db=None`` or a real in-memory aiosqlite DB.

Targets: initialize / ping / notifications / tools-list / prompts dispatch,
unknown method + unknown tool, read-only + workspace-role gates, GitHub tool
guards, prompt-message builders, and the small pure helpers
(_parse_touches_files, _suggest_files_for_title, _build_prompt_messages).
"""
from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.mcp import handler as mh


def _run(coro):
    return asyncio.run(coro)


def _req(method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _make_db():
    """In-memory aiosqlite DB with the full Meridian schema (all migrations)."""
    import meridian.db as db_module

    return _run(db_module.init_db(":memory:"))


# ---------------------------------------------------------------------------
# Top-level JSON-RPC dispatch: initialize / ping / notifications
# ---------------------------------------------------------------------------

def test_initialize_returns_capabilities():
    resp = _run(mh._handle_mcp_request(_req("initialize"), db=None, data_dir="/tmp"))
    assert resp["id"] == 1
    result = resp["result"]
    assert "protocolVersion" in result
    assert result["serverInfo"]["name"] == "meridian"
    assert "tools" in result["capabilities"]
    assert "prompts" in result["capabilities"]


def test_notifications_initialized_returns_empty_ok():
    resp = _run(mh._handle_mcp_request(_req("notifications/initialized"), db=None, data_dir="/tmp"))
    assert resp["result"] == {}


def test_ping_returns_empty_ok():
    resp = _run(mh._handle_mcp_request(_req("ping"), db=None, data_dir="/tmp"))
    assert resp["result"] == {}


def test_unknown_method_returns_method_not_found():
    resp = _run(mh._handle_mcp_request(_req("does/not/exist"), db=None, data_dir="/tmp"))
    assert resp["error"]["code"] == -32601
    assert "method not found" in resp["error"]["message"]


def test_missing_method_defaults_to_method_not_found():
    # No "method" key at all → falls through to the -32601 path.
    resp = _run(mh._handle_mcp_request({"jsonrpc": "2.0", "id": 9}, db=None, data_dir="/tmp"))
    assert resp["id"] == 9
    assert resp["error"]["code"] == -32601


def test_request_id_preserved_for_string_id():
    resp = _run(mh._handle_mcp_request(_req("ping", req_id="abc-123"), db=None, data_dir="/tmp"))
    assert resp["id"] == "abc-123"


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

def test_tools_list_no_tenant_returns_native_tools():
    resp = _run(mh._handle_mcp_request(_req("tools/list", {}), db=None, data_dir="/tmp"))
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "log_task" in names
    assert "start_session" in names


def test_tools_list_with_tenant_no_github_no_tunnel():
    # Tenant with no github_pat → _github_tools_for_tenant returns []; no tunnel.
    tenant = {"id": "tcov1", "plan": "pro"}
    resp = _run(mh._handle_mcp_request(_req("tools/list", {}), db=None, data_dir="/tmp", tenant=tenant))
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "log_task" in names


def test_tools_list_tunnel_exception_is_swallowed(monkeypatch):
    """A tunnel hiccup must never break native tools/list."""
    from meridian.routes import tunnel as tn
    tenant = {"id": "tcov2", "plan": "pro"}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def boom(tid, reserved):
        raise RuntimeError("tunnel down")

    monkeypatch.setattr(tn, "list_tunnel_tools", boom)
    resp = _run(mh._handle_mcp_request(_req("tools/list", {}), db=None, data_dir="/tmp", tenant=tenant))
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "log_task" in names  # native list survives the exception


# ---------------------------------------------------------------------------
# prompts/list and prompts/get
# ---------------------------------------------------------------------------

def test_prompts_list():
    resp = _run(mh._handle_mcp_request(_req("prompts/list"), db=None, data_dir="/tmp"))
    names = {p["name"] for p in resp["result"]["prompts"]}
    assert names == {
        "start-executor",
        "daily-standup",
        "planning-session-start",
        "executor-goal",
        "hotfix-loop",
    }
    # Every descriptor matches the MCP prompts/list shape.
    for p in resp["result"]["prompts"]:
        assert p["name"] and "description" in p
        assert isinstance(p["arguments"], list)
        for a in p["arguments"]:
            assert {"name", "description", "required"} <= set(a)


def test_prompts_get_planning_session_start():
    resp = _run(mh._handle_mcp_request(
        _req("prompts/get", {"name": "planning-session-start", "arguments": {"project_id": "pidplan"}}),
        db=None, data_dir="/tmp",
    ))
    assert resp["result"]["description"]
    text = resp["result"]["messages"][0]["content"]["text"]
    assert text  # non-empty
    assert "pidplan" in text
    assert "get_planning_brief" in text


def test_prompts_get_hotfix_loop():
    resp = _run(mh._handle_mcp_request(
        _req("prompts/get", {"name": "hotfix-loop", "arguments": {"project_id": "pidfix"}}),
        db=None, data_dir="/tmp",
    ))
    text = resp["result"]["messages"][0]["content"]["text"]
    assert text
    assert "pidfix" in text
    # read -> edit -> push protocol keywords present.
    assert "claim_file" in text and "dev" in text


def test_prompts_get_executor_goal_no_project_returns_template():
    # No project_id / project_name → friendly template, not an error or crash.
    resp = _run(mh._handle_mcp_request(
        _req("prompts/get", {"name": "executor-goal"}),
        db=_make_db(), data_dir="/tmp",
    ))
    assert "error" not in resp
    text = resp["result"]["messages"][0]["content"]["text"]
    assert text
    assert "/goal" in text and "start_session" in text


def test_prompts_get_executor_goal_live_items():
    db = _make_db()
    import meridian.db as db_module

    async def _seed():
        proj = await db_module.create_project(db, "exec-goal-proj")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Wire the prompts endpoint")
        return pid, item["id"]

    pid, item_id = _run(_seed())
    resp = _run(mh._handle_mcp_request(
        _req("prompts/get", {"name": "executor-goal", "arguments": {"project_id": pid}}),
        db=db, data_dir="/tmp",
    ))
    text = resp["result"]["messages"][0]["content"]["text"]
    assert item_id in text  # the live pending item id is rendered
    assert "Wire the prompts endpoint" in text
    assert "complete_sprint_item" in text


def test_prompts_get_executor_goal_by_project_name():
    db = _make_db()
    import meridian.db as db_module

    async def _seed():
        proj = await db_module.create_project(db, "named-exec-proj")
        await db_module.add_sprint_item(db, proj["id"], "v1", "Item via name lookup")
        return proj["id"]

    pid = _run(_seed())
    resp = _run(mh._handle_mcp_request(
        _req("prompts/get", {"name": "executor-goal", "arguments": {"project_name": "named-exec-proj"}}),
        db=db, data_dir="/tmp",
    ))
    text = resp["result"]["messages"][0]["content"]["text"]
    assert "Item via name lookup" in text


def test_prompts_get_start_executor():
    resp = _run(mh._handle_mcp_request(
        _req("prompts/get", {"name": "start-executor", "arguments": {"project_id": "pid42"}}),
        db=None, data_dir="/tmp",
    ))
    assert resp["result"]["description"]
    text = resp["result"]["messages"][0]["content"]["text"]
    assert "pid42" in text


def test_prompts_get_daily_standup_default_placeholder():
    resp = _run(mh._handle_mcp_request(
        _req("prompts/get", {"name": "daily-standup"}),
        db=None, data_dir="/tmp",
    ))
    text = resp["result"]["messages"][0]["content"]["text"]
    assert "<project_id>" in text


def test_prompts_get_unknown_returns_invalid_params():
    resp = _run(mh._handle_mcp_request(
        _req("prompts/get", {"name": "nope"}),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32602
    assert "unknown prompt" in resp["error"]["message"]


# ---------------------------------------------------------------------------
# _build_prompt_messages directly
# ---------------------------------------------------------------------------

def test_build_prompt_messages_unknown_raises():
    with pytest.raises(ValueError, match="unknown prompt"):
        mh._build_prompt_messages("ghost", {})


# ---------------------------------------------------------------------------
# stdio transport — prompts/list + prompts/get parity with the HTTP surface
# ---------------------------------------------------------------------------

def _stdio_server(monkeypatch, db):
    """Build the stdio MCP server with its lazy DB pinned to an in-memory db.

    The registered prompt handlers call the server's internal ``_ensure_db``,
    which calls ``db_module.init_db``. Monkeypatching ``init_db`` to return our
    pre-seeded in-memory connection lets the handlers run without opening a real
    on-disk SQLite file.
    """
    import meridian.server as srv
    import meridian.db as db_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = srv.build_mcp_server()
    return server


def test_stdio_prompts_list_matches_registry(monkeypatch):
    import mcp.types as t

    db = _make_db()
    server = _stdio_server(monkeypatch, db)
    handler = server.request_handlers[t.ListPromptsRequest]
    res = _run(handler(t.ListPromptsRequest(method="prompts/list")))
    names = {p.name for p in res.root.prompts}
    assert names == {p["name"] for p in mh._MCP_PROMPTS}


def test_stdio_get_prompt_hotfix_loop(monkeypatch):
    import mcp.types as t

    db = _make_db()
    server = _stdio_server(monkeypatch, db)
    handler = server.request_handlers[t.GetPromptRequest]
    req = t.GetPromptRequest(
        method="prompts/get",
        params=t.GetPromptRequestParams(name="hotfix-loop", arguments={"project_id": "abc"}),
    )
    res = _run(handler(req))
    msgs = res.root.messages
    assert msgs and msgs[0].role == "user"
    assert "abc" in msgs[0].content.text


def test_stdio_get_prompt_executor_goal_live(monkeypatch):
    import mcp.types as t
    import meridian.db as db_module

    db = _make_db()

    async def _seed():
        proj = await db_module.create_project(db, "stdio-exec-proj")
        item = await db_module.add_sprint_item(db, proj["id"], "v1", "stdio live item")
        return proj["id"], item["id"]

    pid, item_id = _run(_seed())
    server = _stdio_server(monkeypatch, db)
    handler = server.request_handlers[t.GetPromptRequest]
    req = t.GetPromptRequest(
        method="prompts/get",
        params=t.GetPromptRequestParams(name="executor-goal", arguments={"project_id": pid}),
    )
    res = _run(handler(req))
    text = res.root.messages[0].content.text
    assert item_id in text
    assert "stdio live item" in text


def test_stdio_get_prompt_unknown_raises(monkeypatch):
    import mcp.types as t

    db = _make_db()
    server = _stdio_server(monkeypatch, db)
    handler = server.request_handlers[t.GetPromptRequest]
    req = t.GetPromptRequest(
        method="prompts/get",
        params=t.GetPromptRequestParams(name="ghost", arguments={}),
    )
    with pytest.raises(ValueError, match="unknown prompt"):
        _run(handler(req))


# ---------------------------------------------------------------------------
# tools/call — gates and error paths
# ---------------------------------------------------------------------------

def test_tools_call_unknown_tool_returns_internal_error():
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {"name": "no_such_tool", "arguments": {}}),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603
    assert "unknown tool" in resp["error"]["message"]


def test_tools_call_readonly_token_blocks_write_tool():
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {"name": "log_task", "arguments": {}}),
        db=None, data_dir="/tmp", token_type="readonly",
    ))
    assert resp["error"]["code"] == -32603
    assert "read-only" in resp["error"]["message"]


def test_tools_call_readonly_token_allows_readonly_tool():
    # get_tasks is read-only; it should pass the gate and fail later (db=None),
    # surfacing a -32603 that is NOT the "not allowed" message.
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {"name": "get_tasks", "arguments": {"project_id": "x"}}),
        db=None, data_dir="/tmp", token_type="readonly",
    ))
    assert "error" in resp
    assert "not allowed for read-only" not in resp["error"]["message"]


def test_tools_call_enforce_role_readonly_denies_write():
    # A non-write role on a write tool → workspace-role gate fires.
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {"name": "log_task", "arguments": {}}),
        db=None, data_dir="/tmp", enforce_role="viewer",
    ))
    assert resp["error"]["code"] == -32603
    msg = resp["error"]["message"]
    assert "viewer" in msg or "read-only" in msg


def test_tools_call_missing_required_arg_surfaces_keyerror():
    # create_project reads args["name"] directly — missing key → KeyError → -32603.
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {"name": "create_project", "arguments": {}}),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603


def test_tools_call_empty_name_is_unknown_tool():
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {"name": "", "arguments": {}}),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# GitHub tool dispatch guards (no real HTTP — guards short-circuit first)
# ---------------------------------------------------------------------------

def test_github_tool_not_connected_returns_error():
    # Tenant set + github name → routed to GitHub dispatch; no PAT → error result
    # wrapped in a successful JSON-RPC envelope (content text).
    tenant = {"id": "tgh", "plan": "pro"}  # no github_pat
    db = _make_db()
    try:
        resp = _run(mh._handle_mcp_request(
            _req("tools/call", {"name": "list_branches", "arguments": {}}),
            db=db, data_dir="/tmp", tenant=tenant,
        ))
    finally:
        _run(db.close())
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "error" in payload
    assert "GitHub not connected" in payload["error"]


def test_dispatch_github_tool_no_pat():
    tenant = {"id": "t", "plan": "pro"}
    db = _make_db()
    try:
        out = _run(mh._dispatch_github_tool("list_branches", {}, tenant, db))
    finally:
        _run(db.close())
    assert "GitHub not connected" in out["error"]


def test_dispatch_github_tool_no_project_id():
    # Provide a PAT so we pass the first guard, then fail on missing project_id.
    import meridian.db as db_module

    tenant = {"id": "t", "plan": "pro", "github_pat": db_module.encrypt_field("ghp_fake")}
    db = _make_db()
    try:
        out = _run(mh._dispatch_github_tool("list_branches", {}, tenant, db))
    finally:
        _run(db.close())
    assert "project_id is required" in out["error"]


def test_dispatch_github_tool_project_not_found():
    import meridian.db as db_module

    tenant = {"id": "t", "plan": "pro", "github_pat": db_module.encrypt_field("ghp_fake")}
    db = _make_db()
    try:
        out = _run(mh._dispatch_github_tool(
            "list_branches", {"project_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, tenant, db,
        ))
    finally:
        _run(db.close())
    assert "not found" in out["error"]


def test_dispatch_github_tool_no_repo_connected():
    import meridian.db as db_module

    tenant = {"id": "t", "plan": "pro", "github_pat": db_module.encrypt_field("ghp_fake")}
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "gh-proj"))
        out = _run(mh._dispatch_github_tool(
            "list_branches", {"project_id": proj["id"]}, tenant, db,
        ))
    finally:
        _run(db.close())
    # Early guard returns the structured no_github_repo error before the PAT path.
    assert out["error"] == "no_github_repo"


def test_dispatch_github_tool_unknown_name():
    import meridian.db as db_module

    # Project with a repo set so we reach the per-name branch and fall through.
    tenant = {"id": "t", "plan": "pro", "github_pat": db_module.encrypt_field("ghp_fake")}
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "gh-proj2"))
        _run(db.execute(
            "UPDATE projects SET github_repo = ? WHERE id = ?",
            ("owner/repo", proj["id"]),
        ))
        _run(db.commit())
        out = _run(mh._dispatch_github_tool(
            "totally_unknown_github_tool", {"project_id": proj["id"]}, tenant, db,
        ))
    finally:
        _run(db.close())
    assert "Unknown GitHub tool" in out["error"]


# ---------------------------------------------------------------------------
# _dispatch_mcp_tool — real DB happy + error paths
# ---------------------------------------------------------------------------

def test_dispatch_create_project_then_duplicate():
    db = _make_db()
    try:
        created = _run(mh._dispatch_mcp_tool("create_project", {"name": "covproj"}, db, "/tmp"))
        assert created["name"] == "covproj"
        dup = _run(mh._dispatch_mcp_tool("create_project", {"name": "covproj"}, db, "/tmp"))
        assert "already exists" in dup["error"]
        assert dup["project"]["id"] == created["id"]
    finally:
        _run(db.close())


def test_dispatch_get_project_by_name_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="no project found"):
            _run(mh._dispatch_mcp_tool("get_project_by_name", {"name": "ghost-proj"}, db, "/tmp"))
    finally:
        _run(db.close())


def test_dispatch_project_name_resolution_failure_raises():
    # project_name given but not resolvable → ValueError.
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="no project found matching name"):
            _run(mh._dispatch_mcp_tool(
                "get_tasks", {"project_name": "does-not-exist"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_project_name_resolution_success():
    db = _make_db()
    try:
        created = _run(mh._dispatch_mcp_tool("create_project", {"name": "named-proj"}, db, "/tmp"))
        # Pass project_name → resolver swaps in the real project_id.
        tasks = _run(mh._dispatch_mcp_tool(
            "get_tasks", {"project_name": "named-proj"}, db, "/tmp",
        ))
        assert isinstance(tasks, list)
        assert created["id"]
    finally:
        _run(db.close())


def test_dispatch_unknown_tool_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="unknown tool"):
            _run(mh._dispatch_mcp_tool("bogus_tool", {}, db, "/tmp"))
    finally:
        _run(db.close())


def test_dispatch_update_decision_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="decision not found"):
            _run(mh._dispatch_mcp_tool(
                "update_decision", {"decision_id": "nope", "body": "x"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_archive_decision_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="decision not found"):
            _run(mh._dispatch_mcp_tool(
                "archive_decision", {"decision_id": "nope"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_get_hitl_request_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="hitl request not found"):
            _run(mh._dispatch_mcp_tool(
                "get_hitl_request", {"request_id": "nope"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_answer_hitl_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="hitl request not found"):
            _run(mh._dispatch_mcp_tool(
                "answer_hitl", {"request_id": "nope", "answer": "yes"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_dismiss_hitl_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="hitl request not found"):
            _run(mh._dispatch_mcp_tool(
                "dismiss_hitl", {"request_id": "nope"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_complete_sprint_item_not_found_raises():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "csi"}, db, "/tmp"))
        with pytest.raises(ValueError, match="sprint item not found"):
            _run(mh._dispatch_mcp_tool(
                "complete_sprint_item",
                {"project_id": proj["id"], "item_id": "nope"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_get_context_block_project_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="project not found"):
            _run(mh._dispatch_mcp_tool(
                "get_context_block",
                {"project_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_get_planning_brief_project_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="project not found"):
            _run(mh._dispatch_mcp_tool(
                "get_planning_brief",
                {"project_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_reconcile_sprint_drift_project_not_found_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="project not found"):
            _run(mh._dispatch_mcp_tool(
                "reconcile_sprint_drift",
                {"project_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"}, db, "/tmp",
            ))
    finally:
        _run(db.close())


def test_dispatch_capture_insight_requires_body():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ins"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "capture_insight", {"project_id": proj["id"], "title": "t"}, db, "/tmp",
        ))
        assert "requires body" in out["error"]
    finally:
        _run(db.close())


def test_dispatch_capture_insight_from_bullets():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ins2"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "capture_insight",
            {"project_id": proj["id"], "title": "t", "bullet_points": ["a", "b"]},
            db, "/tmp",
        ))
        assert "error" not in out
    finally:
        _run(db.close())


def test_dispatch_update_sprint_item_not_found_returns_error():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "usi"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "update_sprint_item",
            {"project_id": proj["id"], "item_id": "nope", "title": "new"},
            db, "/tmp",
        ))
        assert "not found" in out["error"]
    finally:
        _run(db.close())


def test_dispatch_get_session_log_no_run_returns_error():
    db = _make_db()
    try:
        out = _run(mh._dispatch_mcp_tool(
            "get_session_log", {"session_id": "no-such-session"}, db, "/tmp",
        ))
        assert "no run found" in out["error"]
    finally:
        _run(db.close())


def test_dispatch_log_task_happy_path():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ltp"}, db, "/tmp"))
        sess = _run(mh._dispatch_mcp_tool(
            "register_session",
            {"project_id": proj["id"], "session_name": "s1"}, db, "/tmp",
        ))
        out = _run(mh._dispatch_mcp_tool(
            "log_task",
            {"session_id": sess["id"], "project_id": proj["id"], "description": "did a thing"},
            db, "/tmp",
        ))
        assert out.get("id")
    finally:
        _run(db.close())


def test_dispatch_get_goal_decisions_truncation():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "goaltrunc"}, db, "/tmp"))
        # Stuff a >3000-char decisions blob, then read via get_goal.
        big = "x" * 4000
        _run(db.execute(
            "UPDATE projects SET decisions = ? WHERE id = ?", (big, proj["id"]),
        ))
        _run(db.commit())
        goal = _run(mh._dispatch_mcp_tool("get_goal", {"project_id": proj["id"]}, db, "/tmp"))
        if goal and goal.get("decisions"):
            assert len(goal["decisions"]) == 3000
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# tools/call full envelope success (real DB) — exercises _jsonrpc_ok wrap
# ---------------------------------------------------------------------------

def test_handle_request_tools_call_success_envelope():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "envp"}, db, "/tmp"))
        resp = _run(mh._handle_mcp_request(
            _req("tools/call", {"name": "list_projects", "arguments": {}}),
            db=db, data_dir="/tmp",
        ))
        content = resp["result"]["content"][0]
        assert content["type"] == "text"
        data = json.loads(content["text"])
        assert any(p["id"] == proj["id"] for p in data)
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_parse_touches_files_none_and_empty():
    assert mh._parse_touches_files(None) == []
    assert mh._parse_touches_files("") == []
    assert mh._parse_touches_files("   ") == []


def test_parse_touches_files_json_list():
    out = mh._parse_touches_files('["./a.py", "b/c.py"]')
    assert out == ["a.py", "b/c.py"]


def test_parse_touches_files_comma_string():
    out = mh._parse_touches_files("a.py, b.py , ./c.py")
    assert out == ["a.py", "b.py", "c.py"]


def test_parse_touches_files_actual_list_with_backslashes():
    out = mh._parse_touches_files(["dir\\file.py", "./x.py", ""])
    assert out == ["dir/file.py", "x.py"]


def test_parse_touches_files_json_scalar():
    # A non-list JSON value is wrapped into a single-element list.
    assert mh._parse_touches_files('"only.py"') == ["only.py"]


def test_suggest_files_for_title_matches_hotspots():
    out = mh._suggest_files_for_title("fix the dashboard button tab")
    assert "meridian/static/dashboard.js" in out


def test_suggest_files_for_title_db_and_mcp():
    out = mh._suggest_files_for_title("migration of the mcp tool dispatch schema")
    assert "meridian/db/__init__.py" in out
    assert "meridian/mcp/handler.py" in out


def test_suggest_files_for_title_no_match():
    assert mh._suggest_files_for_title("zzz qqq nothing relevant") == []


def test_meridian_tool_names_includes_natives():
    names = mh._meridian_tool_names()
    assert "log_task" in names
    assert "start_session" in names
    # Cached on second call (same frozenset instance).
    assert mh._meridian_tool_names() is names


# ---------------------------------------------------------------------------
# Broad happy-path coverage — drives the large tool handler bodies on a real DB.
# A module-scoped project/session is reused so the long branches get exercised.
# ---------------------------------------------------------------------------

def _seed_project(db, name="cov-big"):
    proj = _run(mh._dispatch_mcp_tool("create_project", {"name": name}, db, "/tmp"))
    sess = _run(mh._dispatch_mcp_tool(
        "register_session", {"project_id": proj["id"], "session_name": "exec-1"}, db, "/tmp",
    ))
    return proj, sess


def test_broad_tool_happy_paths():
    db = _make_db()
    try:
        proj, sess = _seed_project(db)
        pid, sid = proj["id"], sess["id"]

        def call(name, args):
            return _run(mh._dispatch_mcp_tool(name, args, db, "/tmp"))

        # Goal / north star
        call("set_goal", {"project_id": pid, "content": "Ship v3"})
        call("set_north_star", {"project_id": pid, "north_star": "Best memory layer"})
        goal = call("get_goal", {"project_id": pid})
        assert goal is not None

        # Logging + tasks + search
        call("log_task", {"session_id": sid, "project_id": pid, "description": "did A"})
        call("log_task", {"session_id": sid, "project_id": pid, "description": "did B"})
        assert isinstance(call("get_tasks", {"project_id": pid}), list)
        assert isinstance(call("search_tasks", {"project_id": pid, "query": "did"}), list)

        # Decisions
        dec = call("pin_decision", {"project_id": pid, "title": "Use psycopg3",
                                    "body": "asyncpg DLL issues", "category": "TECHNICAL"})
        assert isinstance(call("get_pinned_decisions", {"project_id": pid}), list)
        if isinstance(dec, dict) and dec.get("id"):
            call("update_decision", {"decision_id": dec["id"], "body": "updated body"})
            call("update_decision", {"decision_id": dec["id"], "new_title": "T2",
                                     "new_body": "superseding body"})

        # Notes / insight
        call("add_note", {"project_id": pid, "title": "wiki", "body": "note body"})
        call("add_note", {"project_id": pid, "title": "MANUAL: do thing", "body": "todo"})
        assert isinstance(call("get_notes", {"project_id": pid}), list)
        call("capture_insight", {"project_id": pid, "title": "insight", "body": "learned X"})

        # Workspace layer
        call("add_workspace_note", {"title": "ws", "body": "b"})
        assert isinstance(call("get_workspace_notes", {}), list)
        call("pin_workspace_decision", {"title": "wsdec", "body": "b"})
        assert isinstance(call("get_workspace_decisions", {}), list)
        assert isinstance(call("get_workspace_settings", {}), dict)
        call("update_workspace_settings", {"sprint_name_default": "Sprint X"})

        # HITL lifecycle
        hitl = call("request_hitl", {"project_id": pid, "question": "rate limit per IP?",
                                     "session_id": sid, "urgency": "normal",
                                     "options": ["IP", "token"], "recommended": "token"})
        assert isinstance(call("list_hitl_requests", {"project_id": pid}), list)
        if isinstance(hitl, dict) and hitl.get("id"):
            got = call("get_hitl_request", {"request_id": hitl["id"]})
            assert got is not None
            call("answer_hitl", {"request_id": hitl["id"], "answer": "token"})
        hitl2 = call("request_hitl", {"project_id": pid, "question": "blocking?",
                                      "session_id": sid, "urgency": "blocking"})
        if isinstance(hitl2, dict) and hitl2.get("id"):
            call("dismiss_hitl", {"request_id": hitl2["id"]})

        # Sprint board
        call("set_sprint", {"project_id": pid, "sprint": "Sprint 1: cov"})
        item = call("add_sprint_item", {"project_id": pid, "version": "v1",
                                        "title": "fix the dashboard tab button panel"})
        iid = item["id"]
        # Duplicate-ish item to trigger fuzzy duplicate warning path.
        call("add_sprint_item", {"project_id": pid, "version": "v1",
                                 "title": "fix the dashboard tab button thing"})
        assert isinstance(call("get_sprint_items", {"project_id": pid}), list)
        assert isinstance(call("get_sprint_progress", {"project_id": pid, "session_id": sid}), dict)
        call("update_sprint_item", {"project_id": pid, "item_id": iid, "title": "renamed item"})
        claimed = call("claim_sprint_item", {"project_id": pid, "item_id": iid, "session_id": sid})
        assert isinstance(claimed, dict)
        completed = call("complete_sprint_item", {"project_id": pid, "item_id": iid, "session_id": sid})
        assert isinstance(completed, dict)

        # Subtasks / split / merge — use a fresh pending parent.
        parent = call("add_sprint_item", {"project_id": pid, "version": "v1", "title": "parent item"})
        st = call("add_subtask", {"project_id": pid, "parent_id": parent["id"], "title": "subtask"})
        assert st is not None
        a = call("add_sprint_item", {"project_id": pid, "version": "v1", "title": "splitme"})
        call("split_sprint_item", {"project_id": pid, "item_id": a["id"],
                                   "titles": ["part one", "part two"]})
        m1 = call("add_sprint_item", {"project_id": pid, "version": "v1", "title": "m1"})
        m2 = call("add_sprint_item", {"project_id": pid, "version": "v1", "title": "m2"})
        call("merge_sprint_items", {"project_id": pid, "item_ids": [m1["id"], m2["id"]],
                                    "new_title": "merged"})

        # Briefs / context / planning
        assert isinstance(call("get_context_block", {"project_id": pid}), dict)
        assert isinstance(call("get_session_brief", {"project_id": pid, "session_id": sid}), dict)
        assert isinstance(call("get_session_brief", {"project_id": pid, "session_id": sid,
                                                     "role": "planner"}), dict)
        assert isinstance(call("get_planning_brief", {"project_id": pid}), dict)
        assert isinstance(call("reconcile_sprint_drift", {"project_id": pid}), dict)

        # Sessions list
        assert isinstance(call("list_sessions", {"project_id": pid}), list)
        call("add_sprint_note", {"session_id": sid, "title": "note", "body": "b"})
        assert isinstance(call("get_sprint_notes", {"session_id": sid}), list)

        # Agent instructions / executor config
        call("set_agent_instructions", {"project_id": pid, "instructions": "Be terse"})
        assert isinstance(call("get_agent_instructions", {"project_id": pid}), dict)
        call("set_executor_config", {"project_id": pid, "test_cmd": "pixi run test"})

        # File / symbol claims
        call("claim_file", {"file_path": "meridian/server.py", "session_id": sid})
        assert isinstance(call("get_file_claims", {"file_path": "meridian/server.py"}), dict)
        assert isinstance(call("get_symbol_claims", {"file_path": "meridian/server.py"}), dict)
        assert isinstance(call("get_symbol_hotspots", {}), dict)
        call("release_file", {"file_path": "meridian/server.py", "session_id": sid})

        # Search + projects listing + by-name
        assert isinstance(call("search_all", {"project_id": pid, "query": "did"}), (list, dict))
        assert isinstance(call("list_projects", {}), list)
        byname = call("get_project_by_name", {"name": "cov-big"})
        assert byname["id"] == pid

        # checkpoint (large branch) + generate_handoff
        ck = call("checkpoint", {"session_id": sid, "project_id": pid})
        assert "next_goal" in ck
        ho = call("generate_handoff", {"project_id": pid, "session_id": sid})
        assert "content" in ho
    finally:
        _run(db.close())


def test_start_session_composite():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ss-cov"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "start_session",
            {"project_id": proj["id"], "session_name": "boot"}, db, "/tmp",
        ))
        assert isinstance(out, dict)
    finally:
        _run(db.close())


def test_set_sprint_unstarted_warning():
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ss-warn"}, db, "/tmp"))
        pid = proj["id"]
        _run(mh._dispatch_mcp_tool("set_goal", {"project_id": pid, "content": "g"}, db, "/tmp"))
        _run(mh._dispatch_mcp_tool(
            "add_sprint_item", {"project_id": pid, "version": "v1", "title": "never claimed"},
            db, "/tmp",
        ))
        out = _run(mh._dispatch_mcp_tool(
            "set_sprint", {"project_id": pid, "sprint": "Sprint 2"}, db, "/tmp",
        ))
        # Unstarted pending items → warning + sprint_not_updated.
        assert out.get("sprint_not_updated") is True
        # force=true overrides.
        forced = _run(mh._dispatch_mcp_tool(
            "set_sprint", {"project_id": pid, "sprint": "Sprint 2", "force": True}, db, "/tmp",
        ))
        assert "sprint_not_updated" not in forced
    finally:
        _run(db.close())


def test_claim_sprint_item_protected_files():
    import meridian.db as db_module

    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "prot"}, db, "/tmp"))
        pid = proj["id"]
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item", {"project_id": pid, "version": "v1", "title": "touch installer"},
            db, "/tmp",
        ))
        # Set touches_files to a protected installer script.
        _run(db.execute(
            "UPDATE sprint_items SET touches_files = ? WHERE id = ?",
            (json.dumps(["hooks.ps1"]), item["id"]),
        ))
        _run(db.commit())
        out = _run(mh._dispatch_mcp_tool(
            "claim_sprint_item", {"project_id": pid, "item_id": item["id"]}, db, "/tmp",
        ))
        assert out.get("error") == "PROTECTED"
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# GitHub HTTP dispatch — httpx mocked so the per-tool bodies execute.
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeHTTP:
    """Stand-in for httpx.AsyncClient — replays a queue or a responder fn."""

    def __init__(self, responder):
        self._responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None, follow_redirects=False):
        return self._responder("GET", url, params)

    async def post(self, url, headers=None, json=None, params=None):
        return self._responder("POST", url, json)


def _gh_db_with_repo(name):
    import meridian.db as db_module

    db = _make_db()
    proj = _run(db_module.create_project(db, name))
    _run(db.execute(
        "UPDATE projects SET github_repo = ?, github_branch = ? WHERE id = ?",
        ("owner/repo", "main", proj["id"]),
    ))
    _run(db.commit())
    return db, proj


def _gh_tenant():
    import meridian.db as db_module
    return {"id": "tgh", "plan": "pro", "github_pat": db_module.encrypt_field("ghp_x")}


def _patch_httpx(monkeypatch, responder):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeHTTP(responder))


def test_github_read_file_and_listing(monkeypatch):
    import base64 as b64

    db, proj = _gh_db_with_repo("ghrf")
    try:
        def responder(method, url, params):
            return _FakeResp(200, {
                "path": "a.py", "sha": "abc", "size": 3,
                "content": b64.b64encode(b"hi!").decode(),
            })
        _patch_httpx(monkeypatch, responder)
        out = _run(mh._dispatch_github_tool(
            "read_file", {"project_id": proj["id"], "path": "a.py"}, _gh_tenant(), db,
        ))
        assert out["content"] == "hi!"

        def responder_dir(method, url, params):
            return _FakeResp(200, [{"name": "a.py", "type": "file", "path": "a.py"}])
        _patch_httpx(monkeypatch, responder_dir)
        out2 = _run(mh._dispatch_github_tool(
            "read_file", {"project_id": proj["id"], "path": ""}, _gh_tenant(), db,
        ))
        assert out2["entries"][0]["name"] == "a.py"
    finally:
        _run(db.close())


def test_github_read_file_404(monkeypatch):
    db, proj = _gh_db_with_repo("ghrf404")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(404))
        out = _run(mh._dispatch_github_tool(
            "read_file", {"project_id": proj["id"], "path": "missing.py"}, _gh_tenant(), db,
        ))
        assert "File not found" in out["error"]
    finally:
        _run(db.close())


def test_github_list_files_and_search(monkeypatch):
    db, proj = _gh_db_with_repo("ghlf")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {
            "tree": [{"path": "src/a.py", "type": "blob"},
                     {"path": "docs/b.md", "type": "blob"},
                     {"path": "src", "type": "tree"}],
        }))
        out = _run(mh._dispatch_github_tool(
            "list_files", {"project_id": proj["id"], "path": "src"}, _gh_tenant(), db,
        ))
        assert out["files"] == ["src/a.py"]

        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {
            "total_count": 1,
            "items": [{"path": "src/a.py", "sha": "s", "html_url": "http://x"}],
        }))
        out2 = _run(mh._dispatch_github_tool(
            "search_code", {"project_id": proj["id"], "query": "foo"}, _gh_tenant(), db,
        ))
        assert out2["total_count"] == 1
    finally:
        _run(db.close())


def test_github_commits_paths(monkeypatch):
    db, proj = _gh_db_with_repo("ghc")
    commit = {
        "sha": "abcdef0123456789",
        "commit": {"message": "fix bug\n\nbody", "author": {"name": "Dev", "date": "2026-01-01"}},
    }
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, [commit]))
        out = _run(mh._dispatch_github_tool(
            "get_commits", {"project_id": proj["id"]}, _gh_tenant(), db,
        ))
        assert out["commits"][0]["author"] == "Dev"

        out2 = _run(mh._dispatch_github_tool(
            "search_commits", {"project_id": proj["id"], "query": "fix"}, _gh_tenant(), db,
        ))
        assert out2["count"] == 1

        full = {**commit, "files": [{"filename": "a.py", "status": "modified",
                                     "additions": 2, "deletions": 1}]}
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, full))
        out3 = _run(mh._dispatch_github_tool(
            "get_commit", {"project_id": proj["id"], "sha": "abcdef"}, _gh_tenant(), db,
        ))
        assert out3["files_changed"] == 1
    finally:
        _run(db.close())


def test_github_workflow_and_issues(monkeypatch):
    db, proj = _gh_db_with_repo("ghw")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {
            "workflow_runs": [{"id": 1, "name": "ci", "status": "completed",
                               "conclusion": "success", "created_at": "x", "html_url": "y"}],
        }))
        out = _run(mh._dispatch_github_tool(
            "get_workflow_runs", {"project_id": proj["id"]}, _gh_tenant(), db,
        ))
        assert out["count"] == 1

        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, [
            {"number": 5, "title": "Bug", "state": "open", "labels": [{"name": "bug"}],
             "created_at": "x", "html_url": "y", "body": "desc"},
            {"number": 6, "title": "PR", "pull_request": {}, "labels": []},
        ]))
        out2 = _run(mh._dispatch_github_tool(
            "list_issues", {"project_id": proj["id"]}, _gh_tenant(), db,
        ))
        # PR excluded.
        assert len(out2["issues"]) == 1
        assert out2["issues"][0]["number"] == 5

        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(201, {
            "number": 7, "title": "New", "state": "open", "html_url": "y",
        }))
        out3 = _run(mh._dispatch_github_tool(
            "create_issue", {"project_id": proj["id"], "title": "New"}, _gh_tenant(), db,
        ))
        assert out3["number"] == 7
    finally:
        _run(db.close())


def test_github_create_issue_missing_title(monkeypatch):
    db, proj = _gh_db_with_repo("ghci")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {}))
        out = _run(mh._dispatch_github_tool(
            "create_issue", {"project_id": proj["id"]}, _gh_tenant(), db,
        ))
        assert "title is required" in out["error"]
    finally:
        _run(db.close())


def test_github_trigger_workflow_and_branches(monkeypatch):
    db, proj = _gh_db_with_repo("ghtw")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(204))
        out = _run(mh._dispatch_github_tool(
            "trigger_workflow",
            {"project_id": proj["id"], "workflow_name": "deploy.yml", "inputs": {"x": "1"}},
            _gh_tenant(), db,
        ))
        assert out["dispatched"] is True

        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, [
            {"name": "main", "commit": {"sha": "deadbeefcafe"}, "protected": True},
        ]))
        out2 = _run(mh._dispatch_github_tool(
            "list_branches", {"project_id": proj["id"]}, _gh_tenant(), db,
        ))
        assert out2["branches"][0]["name"] == "main"
    finally:
        _run(db.close())


def test_github_git_diff(monkeypatch):
    db, proj = _gh_db_with_repo("ghgd")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {
            "total_commits": 2,
            "files": [{"filename": "a.py", "status": "modified",
                       "additions": 1, "deletions": 0, "patch": "@@"}],
        }))
        out = _run(mh._dispatch_github_tool(
            "git_diff", {"project_id": proj["id"], "base": "main", "head": "dev"},
            _gh_tenant(), db,
        ))
        assert out["total_commits"] == 2
    finally:
        _run(db.close())


def test_update_md_section_unknown_file_raises():
    db = _make_db()
    try:
        with pytest.raises(Exception):
            _run(mh._dispatch_mcp_tool(
                "update_md_section",
                {"project_id": "x", "file": "NOPE.md", "anchor": "a", "content": "c"},
                db, "/tmp",
            ))
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# set_active_repo — tunnel tool dispatch
# ---------------------------------------------------------------------------

def test_dispatch_set_active_repo_no_repo_path_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="repo_path is required"):
            _run(mh._dispatch_mcp_tool(
                "set_active_repo", {"repo_path": ""},
                db, "/tmp", tenant={"id": "t1"},
            ))
    finally:
        _run(db.close())


def test_dispatch_set_active_repo_no_tenant_raises():
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="authenticated tenant"):
            _run(mh._dispatch_mcp_tool(
                "set_active_repo", {"repo_path": "/some/repo"},
                db, "/tmp", tenant=None,
            ))
    finally:
        _run(db.close())


def test_dispatch_set_active_repo_not_connected(monkeypatch):
    """set_active_repo with no extract tunnel returns not_connected status."""
    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {})
    db = _make_db()
    try:
        result = _run(mh._dispatch_mcp_tool(
            "set_active_repo", {"repo_path": "/my/repo"},
            db, "/tmp", tenant={"id": "no-tenant"},
        ))
        assert result["status"] == "not_connected"
    finally:
        _run(db.close())


def test_dispatch_set_active_repo_ok(monkeypatch):
    """set_active_repo with a live extract WS returns ok status."""
    sent = []

    class _FakeWS:
        async def send_json(self, obj):
            sent.append(obj)

    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {"t1": _FakeWS()})
    db = _make_db()
    try:
        result = _run(mh._dispatch_mcp_tool(
            "set_active_repo", {"repo_path": "/my/repo"},
            db, "/tmp", tenant={"id": "t1"},
        ))
        assert result["status"] == "ok"
        assert result["repo_path"] == "/my/repo"
        assert sent == [{"type": "set_active_repo", "repo_path": "/my/repo"}]
    finally:
        _run(db.close())
