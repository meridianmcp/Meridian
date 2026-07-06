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


def test_dispatch_github_tool_search_code_no_repo_connected():
    """dc462628 — search_code hits the shared no-repo guard and returns a clear
    error rather than a misleading empty result (removes the 'false confidence'
    the tool description now documents)."""
    import meridian.db as db_module

    tenant = {"id": "t", "plan": "pro", "github_pat": db_module.encrypt_field("ghp_fake")}
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "gh-search-proj"))
        out = _run(mh._dispatch_github_tool(
            "search_code", {"project_id": proj["id"], "query": "foo"}, tenant, db,
        ))
    finally:
        _run(db.close())
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


def test_decision_assumption_field_and_planning_brief():
    # 2b39549d — assumption + assumption_status on decisions, surfaced in
    # get_planning_brief until confirmed. Writes go through db_module directly to
    # avoid the committable-category DECISIONS.md side effect of the MCP tool.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "assump-proj"))
        dec = _run(db_module.pin_decision(
            db, proj["id"], "Use psycopg3", "asyncpg DLL issues on Windows",
            assumption="psycopg3 pool handles our concurrency",
        ))
        assert dec["assumption"] == "psycopg3 pool handles our concurrency"
        assert dec["assumption_status"] == "unvalidated"
        # Surfaced in the planning brief (read-only dispatch).
        brief = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        ua = brief["unvalidated_assumptions"]
        assert len(ua) == 1 and ua[0]["decision_id"] == dec["id"]
        assert ua[0]["assumption_status"] == "unvalidated"
        # Confirming it drops it out of the brief.
        _run(db_module.update_pinned_decision(
            db, dec["id"], assumption_status="confirmed"))
        brief2 = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        assert brief2["unvalidated_assumptions"] == []
        # Invalid status is rejected.
        with pytest.raises(ValueError, match="assumption_status must be"):
            _run(db_module.update_pinned_decision(
                db, dec["id"], assumption_status="bogus"))
    finally:
        _run(db.close())


def test_decision_without_assumption_has_null_status():
    # 2b39549d — a decision with no assumption has null fields and never appears
    # in the planning brief's unvalidated_assumptions.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "noassump"))
        dec = _run(db_module.pin_decision(db, proj["id"], "X", "Y"))
        assert dec["assumption"] is None and dec["assumption_status"] is None
        brief = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        assert brief["unvalidated_assumptions"] == []
    finally:
        _run(db.close())


def test_validate_assumption_confirmed():
    # 8ec5493b — confirm: stamps status, saves code-anchored note, no HITL.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "va-confirm"))
        dec = _run(db_module.pin_decision(
            db, proj["id"], "Use psycopg3", "body", assumption="pool handles load"))
        res = _run(mh._dispatch_mcp_tool("validate_assumption", {
            "decision_id": dec["id"],
            "finding": "Benchmarked: pool sustains 200 conns",
            "confirmed": True,
            "file_path": "meridian/pg_adapter.py", "symbol": "PostgresPool",
        }, db, "/tmp"))
        assert res["assumption_status"] == "confirmed"
        assert res["hitl"] is None
        assert res["note"]["file_path"] == "meridian/pg_adapter.py"
        assert res["note"]["symbol"] == "PostgresPool"
        assert "confirmed" in (res["note"].get("tags") or "")
        # Decision row stamped.
        dec2 = _run(db_module.get_pinned_decision(db, dec["id"]))
        assert dec2["assumption_status"] == "confirmed"
        # No blocking HITL created.
        hitls = _run(db_module.list_hitl_requests(db, proj["id"], status="pending"))
        assert all(h.get("urgency") != "blocking" for h in hitls)
    finally:
        _run(db.close())


def test_validate_assumption_invalidated_fires_blocking_hitl():
    # 8ec5493b — invalidate: stamps status + fires a blocking HITL.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "va-invalid"))
        sess = _run(db_module.register_session(db, proj["id"], "planner-1"))
        dec = _run(db_module.pin_decision(
            db, proj["id"], "Use psycopg3", "body", assumption="pool handles load"))
        res = _run(mh._dispatch_mcp_tool("validate_assumption", {
            "decision_id": dec["id"],
            "finding": "pool deadlocks at 50 conns on Windows",
            "confirmed": False,
            "session_id": sess["id"],
        }, db, "/tmp"))
        assert res["assumption_status"] == "invalidated"
        assert res["hitl"] is not None
        assert res["hitl"]["urgency"] == "blocking"
        assert res["hitl"]["session_id"] == sess["id"]
        assert "invalidated" in (res["note"].get("tags") or "")
        dec2 = _run(db_module.get_pinned_decision(db, dec["id"]))
        assert dec2["assumption_status"] == "invalidated"
        hitls = _run(db_module.list_hitl_requests(db, proj["id"], status="pending"))
        assert any(h.get("urgency") == "blocking" for h in hitls)
    finally:
        _run(db.close())


def test_validate_assumption_stale_session_degrades():
    # 8ec5493b — a non-existent session_id must not crash (FK); the blocking HITL
    # is created project-level (session_id None) instead.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "va-stale"))
        dec = _run(db_module.pin_decision(db, proj["id"], "T", "B", assumption="A"))
        res = _run(mh._dispatch_mcp_tool("validate_assumption", {
            "decision_id": dec["id"], "finding": "nope", "confirmed": False,
            "session_id": "ghost-session",
        }, db, "/tmp"))
        assert res["hitl"]["urgency"] == "blocking"
        assert res["hitl"]["session_id"] is None
    finally:
        _run(db.close())


def test_validate_assumption_unknown_decision_errors():
    db = _make_db()
    try:
        out = _run(mh._dispatch_mcp_tool("validate_assumption", {
            "decision_id": "no-such-decision", "finding": "x", "confirmed": True,
        }, db, "/tmp"))
        assert "error" in out and "not found" in out["error"]
    finally:
        _run(db.close())


def test_validate_assumption_requires_confirmed_field():
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "va-req"))
        dec = _run(db_module.pin_decision(db, proj["id"], "T", "B", assumption="A"))
        out = _run(mh._dispatch_mcp_tool("validate_assumption", {
            "decision_id": dec["id"], "finding": "x",
        }, db, "/tmp"))
        assert "error" in out and "confirmed" in out["error"]
    finally:
        _run(db.close())


def test_save_finding_basic_and_decision_link():
    # e1f43ee7 — capture primitive: addressable note + provenance + decision link.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "sf-proj"))
        res = _run(mh._dispatch_mcp_tool("save_finding", {
            "project_id": proj["id"],
            "summary": "psycopg3 supports pipeline mode\nextra detail",
            "source_url": "https://example.com/x", "source_type": "web",
        }, db, "/tmp"))
        assert res["source_type"] == "web" and res["decision_id"] is None
        note = res["note"]
        assert note["source"] == "https://example.com/x"
        assert "finding" in (note.get("tags") or "")
        assert note["title"].startswith("Finding: psycopg3 supports pipeline mode")
        # Unknown source_type falls back to web.
        res2 = _run(mh._dispatch_mcp_tool("save_finding", {
            "project_id": proj["id"], "summary": "x", "source_type": "bogus",
        }, db, "/tmp"))
        assert res2["source_type"] == "web"
        # Decision linkage tags the note decision:<id>.
        dec = _run(db_module.pin_decision(db, proj["id"], "T", "B"))
        res3 = _run(mh._dispatch_mcp_tool("save_finding", {
            "project_id": proj["id"], "summary": "supports X",
            "decision_id": dec["id"], "source_type": "arxiv",
        }, db, "/tmp"))
        assert res3["decision_id"] == dec["id"]
        assert f"decision:{dec['id']}" in (res3["note"].get("tags") or "")
        assert "arxiv" in (res3["note"].get("tags") or "")
        # Unknown decision errors; empty summary errors.
        out = _run(mh._dispatch_mcp_tool("save_finding", {
            "project_id": proj["id"], "summary": "y", "decision_id": "ghost",
        }, db, "/tmp"))
        assert "error" in out and "not found" in out["error"]
        out2 = _run(mh._dispatch_mcp_tool("save_finding", {
            "project_id": proj["id"], "summary": "   ",
        }, db, "/tmp"))
        assert "error" in out2
    finally:
        _run(db.close())


def test_capture_research_finding_infers_arxiv():
    # b1d36e93 — wrapper over save_finding; arXiv URL → source_type=arxiv.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "crf-proj"))
        res = _run(mh._dispatch_mcp_tool("capture_research_finding", {
            "project_id": proj["id"], "url": "https://arxiv.org/abs/1234.5678",
            "summary": "Paper shows Y",
        }, db, "/tmp"))
        assert res["source_type"] == "arxiv"
        assert res["note"]["source"] == "https://arxiv.org/abs/1234.5678"
        assert "arxiv" in (res["note"].get("tags") or "")
        # Non-arxiv URL → web; related_decision_id links it.
        dec = _run(db_module.pin_decision(db, proj["id"], "T", "B"))
        res2 = _run(mh._dispatch_mcp_tool("capture_research_finding", {
            "project_id": proj["id"], "url": "https://blog.example.com/post",
            "summary": "Blog says Z", "related_decision_id": dec["id"],
        }, db, "/tmp"))
        assert res2["source_type"] == "web" and res2["decision_id"] == dec["id"]
        # Missing url errors.
        out = _run(mh._dispatch_mcp_tool("capture_research_finding", {
            "project_id": proj["id"], "summary": "no url",
        }, db, "/tmp"))
        assert "error" in out
    finally:
        _run(db.close())


def test_planning_brief_last_session_view():
    # 81170c84 — get_planning_brief surfaces the most recent session's completed
    # items, task log, and recent decisions.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "ls-proj"))
        sess = _run(db_module.register_session(db, proj["id"], "executor-1"))
        task = _run(db_module.log_task(
            db, sess["id"], proj["id"], "Implemented OAuth refresh"))
        item = _run(db_module.add_sprint_item(
            db, proj["id"], "v1", "OAuth refresh endpoint"))
        _run(db_module.complete_sprint_item(
            db, proj["id"], item["id"], task_id=task["id"]))
        _run(db_module.pin_decision(db, proj["id"], "Use psycopg3", "body"))
        brief = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        ls = brief["last_session"]
        assert ls is not None
        assert ls["session_id"] == sess["id"]
        assert ls["name"] == "executor-1"
        assert any(ci["id"] == item["id"] for ci in ls["completed_items"])
        assert any(
            "OAuth refresh" in (t["description"] or "") for t in ls["recent_tasks"])
        assert any(
            d["title"] == "Use psycopg3" for d in ls["recent_pinned_decisions"])
    finally:
        _run(db.close())


def test_planning_brief_last_session_none_when_no_sessions():
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "ls-empty"))
        brief = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        assert brief["last_session"] is None
    finally:
        _run(db.close())


def test_planning_brief_latest_retrospective_present_and_absent():
    """aef94e4a — get_planning_brief surfaces the latest sprint retrospective
    note (tag=retrospective), and None when none exists."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "retro-brief"))
        b0 = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        assert "latest_retrospective" in b0
        assert b0["latest_retrospective"] is None
        _run(db_module.add_project_note(
            db, proj["id"], "Sprint Retrospective — v1",
            "What shipped: lots. Patterns: good. Direction: forward.",
            tags="retrospective,strategy", kind="insight", priority="high"))
        b1 = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        lr = b1["latest_retrospective"]
        assert lr is not None
        assert lr["title"] == "Sprint Retrospective — v1"
        assert "What shipped" in lr["body"]
        assert lr["slug"]
    finally:
        _run(db.close())


def test_planning_brief_includes_current_timestamp():
    """de193a81 — get_planning_brief returns current_timestamp so a planner
    spanning multiple calendar days never guesses 'today'/'yesterday'."""
    import re
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "ts-proj"))
        brief = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        assert "current_timestamp" in brief
        assert re.match(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", brief["current_timestamp"]
        )
    finally:
        _run(db.close())


def test_detect_insight_candidate_matches_and_ignores():
    """2d932f60 — the keyword detector flags decision/insight phrasing and
    ignores neutral text."""
    from meridian.handoff import detect_insight_candidate
    assert detect_insight_candidate("We decided to use psycopg3") == "we decided"
    assert detect_insight_candidate("Root cause: event-loop deadlock") == "root cause"
    assert detect_insight_candidate("Add a logout button") is None
    assert detect_insight_candidate("") is None
    assert detect_insight_candidate(None) is None


def test_add_sprint_item_proposes_insight_capture():
    """2d932f60 — add_sprint_item surfaces an insight_hint when the title reads
    like a decision, and stays quiet for a plain title."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "insight-proj"))
        hinted = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"], "version": "v1",
            "title": "Decision: we chose Cytoscape over ECharts for the graph",
            "force": True,
        }, db, "/tmp"))
        assert "insight_hint" in hinted
        assert "add_insight" in hinted["insight_hint"]
        plain = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"], "version": "v1",
            "title": "Add a logout button to the header",
            "force": True,
        }, db, "/tmp"))
        assert "insight_hint" not in plain
    finally:
        _run(db.close())


def test_generate_handoff_surfaces_insight_hints(tmp_path):
    """2d932f60 — generate_handoff scans the session task log and surfaces
    insight candidates so they aren't lost at session end."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "ih-handoff-proj"))
        sess = _run(db_module.register_session(db, proj["id"], "exec-ih"))
        _run(db_module.log_task(
            db, sess["id"], proj["id"],
            "Turns out the ProactorEventLoop deadlocks watchfiles on Windows"))
        out = _run(mh._dispatch_mcp_tool(
            "generate_handoff",
            {"project_id": proj["id"], "session_id": sess["id"]},
            db, str(tmp_path)))
        assert "insight_hints" in out
        assert any(h["signal"] == "turns out" for h in out["insight_hints"])
    finally:
        _run(db.close())


def test_generate_handoff_warns_on_long_goal(tmp_path):
    """0fe01e93 — generate_handoff flags a /goal text over the 4000-char limit."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "longgoal-proj"))
        _run(db_module.set_goal(db, proj["id"], "", sprint="x" * 4100))
        out = _run(mh._dispatch_mcp_tool(
            "generate_handoff", {"project_id": proj["id"]}, db, str(tmp_path)))
        assert out["goal_length_warning"] is not None
        assert "4000" in out["goal_length_warning"]

        proj2 = _run(db_module.create_project(db, "shortgoal-proj"))
        _run(db_module.set_goal(db, proj2["id"], "", sprint="short and tidy"))
        out2 = _run(mh._dispatch_mcp_tool(
            "generate_handoff", {"project_id": proj2["id"]}, db, str(tmp_path)))
        assert out2["goal_length_warning"] is None
    finally:
        _run(db.close())


def test_generate_handoff_content_is_fence_free(tmp_path):
    """642b1818 — generate_handoff returns a single plain-text copyable block:
    markdown code-fence lines are stripped so it pastes cleanly into a fenced
    chat / dashboard textarea."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "fence-proj"))
        _run(db_module.set_goal(db, proj["id"], "", sprint="do the thing"))
        # a strategic note whose body has a fenced block that the handoff renders
        _run(db_module.add_project_note(
            db, proj["id"], "Config", "```json\n{\"a\": 1}\n```", "planning",
            priority="high"))
        out = _run(mh._dispatch_mcp_tool(
            "generate_handoff", {"project_id": proj["id"]}, db, str(tmp_path)))
        assert "```" not in out["content"]
    finally:
        _run(db.close())


def test_add_note_warns_on_similar_existing_note():
    """6e4e2371 — adding a note whose title closely matches an existing one
    returns a similar_notes warning (never blocks the write)."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "dedup-proj"))
        _run(mh._dispatch_mcp_tool("add_note", {
            "project_id": proj["id"],
            "title": "Windows event loop deadlock gotcha",
            "body": "Use SelectorEventLoop on Windows.",
        }, db, "/tmp"))
        dup = _run(mh._dispatch_mcp_tool("add_note", {
            "project_id": proj["id"],
            "title": "Windows event loop deadlock gotchas",  # near-identical
            "body": "Different body entirely.",
        }, db, "/tmp"))
        assert "similar_notes" in dup
        assert dup["similar_notes"][0]["similarity"] >= 0.82
        # A clearly different note does not trigger the warning.
        fresh = _run(mh._dispatch_mcp_tool("add_note", {
            "project_id": proj["id"],
            "title": "Stripe billing overage metering",
            "body": "unrelated",
        }, db, "/tmp"))
        assert "similar_notes" not in fresh
    finally:
        _run(db.close())


def test_add_note_extracts_hashtags_as_tags():
    """41b8a927 — #hashtags in a note's title/body are recognised as tags so
    the note is searchable by tag; explicit tags are preserved alongside them."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "hashtag-proj"))
        created = _run(mh._dispatch_mcp_tool("add_note", {
            "project_id": proj["id"],
            "title": "Windows fix",
            "body": "Use SelectorEventLoop. #windows #eventloop",
        }, db, "/tmp"))
        slug = created.get("slug")
        by_win = _run(db_module.get_project_notes(db, proj["id"], tag="windows"))
        assert any(n.get("slug") == slug for n in by_win)
        by_evl = _run(db_module.get_project_notes(db, proj["id"], tag="eventloop"))
        assert any(n.get("slug") == slug for n in by_evl)
        created2 = _run(mh._dispatch_mcp_tool("add_note", {
            "project_id": proj["id"],
            "title": "Billing",
            "body": "Stripe overage #billing",
            "tags": "finance",
        }, db, "/tmp"))
        slug2 = created2.get("slug")
        assert any(n.get("slug") == slug2
                   for n in _run(db_module.get_project_notes(db, proj["id"], tag="finance")))
        assert any(n.get("slug") == slug2
                   for n in _run(db_module.get_project_notes(db, proj["id"], tag="billing")))
    finally:
        _run(db.close())


def test_note_relevance_score_weights():
    """98890df1 — more references / recency / a decision link each raise the score."""
    from meridian.db import _note_relevance_score
    base = _note_relevance_score(0, 100.0, False)
    assert _note_relevance_score(5, 100.0, False) > base   # references help
    assert _note_relevance_score(0, 0.0, False) > base     # recency helps
    assert _note_relevance_score(0, 100.0, True) > base    # decision link helps


def test_get_notes_relevance_sort_surfaces_referenced_note():
    """98890df1 — get_notes(sort=relevance) ranks a heavily cross-referenced note
    above unreferenced ones (relevance > pure recency)."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "rank-proj"))
        a = _run(mh._dispatch_mcp_tool("add_note", {
            "project_id": proj["id"], "title": "Core architecture decision",
            "body": "The keystone note.",
        }, db, "/tmp"))
        a_slug = a["slug"]
        for i in range(2):
            _run(mh._dispatch_mcp_tool("add_note", {
                "project_id": proj["id"], "title": f"Follow-up note number {i}",
                "body": f"See [[{a_slug}]] for the rationale.",
            }, db, "/tmp"))
        ranked = _run(mh._dispatch_mcp_tool("get_notes", {
            "project_id": proj["id"], "sort": "relevance",
        }, db, "/tmp"))
        assert isinstance(ranked, list)
        assert ranked[0]["slug"] == a_slug
        assert "relevance" in ranked[0]
        assert ranked[0]["relevance"] > ranked[-1]["relevance"]
    finally:
        _run(db.close())


def test_planning_brief_new_handoff_signal():
    # ab514e43 — new-handoff signal: latest_handoff + since-based new flag.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "nh-proj"))
        sess = _run(db_module.register_session(db, proj["id"], "executor-7"))
        # No handoff yet → no signal, but generated_at always present.
        b0 = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        assert b0["latest_handoff"] is None
        assert b0["new_handoff_available"] is False
        assert b0["handoff_signal"] is None
        assert b0["generated_at"]
        # Record a handoff → surfaced as new (no since baseline).
        _run(db_module.record_handoff(
            db, proj["id"], "delta", "handoff body here", session_id=sess["id"]))
        b1 = _run(mh._dispatch_mcp_tool(
            "get_planning_brief", {"project_id": proj["id"]}, db, "/tmp"))
        lh = b1["latest_handoff"]
        assert lh is not None and lh["session_id"] == sess["id"]
        assert lh["session_name"] == "executor-7" and lh["mode"] == "delta"
        assert "handoff body" in lh["body_preview"]
        assert b1["new_handoff_available"] is True
        assert b1["handoff_signal"] and "executor-7" in b1["handoff_signal"]
        # since in the future → not new.
        b2 = _run(mh._dispatch_mcp_tool("get_planning_brief", {
            "project_id": proj["id"], "since": "2999-01-01 00:00:00"}, db, "/tmp"))
        assert b2["new_handoff_available"] is False
        assert b2["handoff_signal"] is None
        # since in the past → new.
        b3 = _run(mh._dispatch_mcp_tool("get_planning_brief", {
            "project_id": proj["id"], "since": "2000-01-01 00:00:00"}, db, "/tmp"))
        assert b3["new_handoff_available"] is True
    finally:
        _run(db.close())


def test_refresh_context_compact_recovery():
    # d8bd59c4 — compact post-compaction recovery snapshot.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "rc-proj"))
        sess = _run(db_module.register_session(db, proj["id"], "exec-rc"))
        _run(db_module.add_sprint_item(db, proj["id"], "v1", "pending item A"))
        _run(db_module.pin_decision(
            db, proj["id"], "Urgent thing", "body",
            priority="urgent", assumption="risky guess"))
        _run(db_module.record_handoff(
            db, proj["id"], "full", "handoff body", session_id=sess["id"]))
        _run(db_module.add_project_note(
            db, proj["id"], "Key note", "note body", "tag", priority="high"))
        rc = _run(mh._dispatch_mcp_tool(
            "refresh_context", {"project_id": proj["id"]}, db, "/tmp"))
        assert rc["project_name"] == "rc-proj"
        assert rc["sprint_progress"]["total"] >= 1
        assert any(it["title"] == "pending item A" for it in rc["next_pending_items"])
        assert rc["active_session_id"] == sess["id"]
        assert len(rc["recent_handoffs"]) == 1
        assert any(d["title"] == "Urgent thing" for d in rc["high_priority_decisions"])
        assert any(
            a["assumption_status"] == "unvalidated"
            for a in rc["unvalidated_assumptions"])
        assert any(n.get("slug") for n in rc["key_note_slugs"])
    finally:
        _run(db.close())


def test_get_session_brief_executor_and_planner_roles_differ():
    # 1750dccf — role tailors the brief: executor gets version scope + file
    # claims; planner gets decisions-needing-revisit. They must differ.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "role-proj"))
        sess = _run(db_module.register_session(
            db, proj["id"], "exec-r", sprint_version="v0.1.x"))
        _run(db_module.register_session(db, proj["id"], "older-exec"))
        _run(db_module.add_sprint_item(db, proj["id"], "v0.1.x", "scoped item"))
        _run(db_module.claim_file(db, "meridian/server.py", sess["id"]))
        _run(db_module.pin_decision(
            db, proj["id"], "Risky choice", "body", assumption="might not hold"))
        ex = _run(mh._dispatch_mcp_tool("get_session_brief", {
            "project_id": proj["id"], "role": "executor", "session_id": sess["id"],
        }, db, "/tmp"))
        pl = _run(mh._dispatch_mcp_tool("get_session_brief", {
            "project_id": proj["id"], "role": "planner", "session_id": sess["id"],
        }, db, "/tmp"))
        # Executor-only sections.
        assert ex["role"] == "executor"
        assert "version_scope" in ex["text"]
        assert "my_file_claims" in ex["text"]
        assert "meridian/server.py" in ex["text"]
        assert "decisions_needing_revisit" not in ex["text"]
        # Planner-only sections.
        assert pl["role"] == "planner"
        assert "decisions_needing_revisit" in pl["text"]
        assert "my_file_claims" not in pl["text"]
        # The two briefs are genuinely different.
        assert ex["text"] != pl["text"]
    finally:
        _run(db.close())


def test_add_sprint_item_drift_check_blocks_and_force_overrides():
    # 7e212375 — a title that overlaps an existing migration is blocked with a
    # drift_warning; force=true bypasses and adds it.
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "drift-proj"))
        out = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"], "version": "v1",
            "title": "handoffs table body persistence",
        }, db, "/tmp"))
        assert out.get("drift_warning") is True
        assert out["matches"]
        items = _run(db_module.get_sprint_items(db, proj["id"]))
        assert not any(
            "handoffs table body" in (it.get("title") or "") for it in items)
        # force=true bypasses the drift check.
        out2 = _run(mh._dispatch_mcp_tool("add_sprint_item", {
            "project_id": proj["id"], "version": "v1",
            "title": "handoffs table body persistence", "force": True,
        }, db, "/tmp"))
        assert not out2.get("drift_warning")
        items2 = _run(db_module.get_sprint_items(db, proj["id"]))
        assert any(
            "handoffs table body" in (it.get("title") or "") for it in items2)
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


def test_dispatch_sprint_item_quality_gates_and_actor():
    """5823db0b — required_notes blocks completion until evidence exists; claim
    and complete record the actor."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "qg"}, db, "/tmp"))
        pid = proj["id"]
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "gated work", "version": "v1"}, db, "/tmp"))
        iid = item["id"]
        # Flag the item as requiring evidence.
        _run(mh._dispatch_mcp_tool(
            "update_sprint_item",
            {"project_id": pid, "item_id": iid, "required_notes": True}, db, "/tmp"))
        # Claim with an actor — recorded on the item.
        claimed = _run(mh._dispatch_mcp_tool(
            "claim_sprint_item",
            {"project_id": pid, "item_id": iid, "actor": "agent-A"}, db, "/tmp"))
        assert claimed["actor"] == "agent-A"
        # Completing without evidence is refused by the gate.
        blocked = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": iid, "actor": "agent-A"}, db, "/tmp"))
        assert blocked["error"] == "EVIDENCE_REQUIRED"
        # Completing with evidence notes succeeds and records actor + notes.
        done = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": iid, "actor": "agent-A",
             "notes": "shipped X; tests green"}, db, "/tmp"))
        assert done["status"] == "done"
        assert done["actor"] == "agent-A"
        assert "shipped X" in (done["notes"] or "")
    finally:
        _run(db.close())


def test_dispatch_analyze_sprint_synthesizes_brief():
    """e77f09d1 — analyze_sprint reports parallelism, dependency chains, and
    resource conflicts in one structured brief."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "as"}, db, "/tmp"))
        pid = proj["id"]
        a = _run(mh._dispatch_mcp_tool("add_sprint_item",
            {"project_id": pid, "title": "provision cache", "version": "v1",
             "touches_resources": ["file:server.py"]}, db, "/tmp"))
        _run(mh._dispatch_mcp_tool("add_sprint_item",
            {"project_id": pid, "title": "tune indexer", "version": "v1",
             "touches_resources": ["file:server.py"]}, db, "/tmp"))
        _run(mh._dispatch_mcp_tool("add_sprint_item",
            {"project_id": pid, "title": "wire billing", "version": "v1",
             "touches_resources": ["file:db.py"], "depends_on": a["id"]}, db, "/tmp"))
        brief = _run(mh._dispatch_mcp_tool("analyze_sprint",
            {"project_id": pid, "version": "v1"}, db, "/tmp"))
        assert "summary" in brief
        assert brief["recommended_strategy"] in ("parallel", "sequential")
        # The two file:server.py items conflict — reported explicitly.
        conflict_resources = [c["resource"] for c in brief["file_conflicts"]]
        assert "file:server.py" in conflict_resources
        # "wire billing" depends_on "provision cache" — a chain of length >= 2.
        assert brief["longest_chain"] >= 2
        assert brief["parallelism"]["eligible_count"] >= 2
    finally:
        _run(db.close())


def test_generate_default_session_name_from_pending_item():
    """599d0097 — an unnamed session is named from the first pending item title
    + timestamp, and start_session works with no session_name."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "dn"}, db, "/tmp"))
        pid = proj["id"]
        _run(mh._dispatch_mcp_tool("add_sprint_item",
            {"project_id": pid, "title": "Wire Billing OAuth", "version": "v1"}, db, "/tmp"))
        name = _run(db_module.generate_default_session_name(db, pid))
        assert name.startswith("wire-billing-oauth-")
        # start_session with NO session_name succeeds and registers a named session.
        res = _run(mh._dispatch_mcp_tool("start_session", {"project_id": pid}, db, "/tmp"))
        sid = res["session_id"]
        sessions = _run(db_module.get_sessions(db, pid, active_only=False))
        sess = next(s for s in sessions if s["id"] == sid)
        assert sess["name"] and "wire-billing-oauth" in sess["name"]
    finally:
        _run(db.close())


def test_generate_default_session_name_empty_board():
    """2bce89ed — empty board falls back to a memorable adjective+noun+timestamp
    (e.g. 'brisk-otter-0701-213045'), not the anonymous 'session-<ts>'."""
    import re
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "eb"}, db, "/tmp"))
        name = _run(db_module.generate_default_session_name(db, proj["id"]))
        assert not name.startswith("session-")
        # adjective-noun-mmdd-hhmmss
        assert re.match(r"^[a-z]+-[a-z]+-\d{4}-\d{6}$", name), name
    finally:
        _run(db.close())


def test_dispatch_complete_sprint_item_no_gate_when_not_flagged():
    """5823db0b — items without required_notes complete freely (no regression)."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ng"}, db, "/tmp"))
        pid = proj["id"]
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "free work", "version": "v1"}, db, "/tmp"))
        done = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": item["id"]}, db, "/tmp"))
        assert done["status"] == "done"
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# bb29a06f — ADVISORY completion sanity-check (plausibility of evidence).
# Extends the required_notes gate: a weakly-supported completion whose title
# shares no keywords with a recent commit gets a soft advisory. Never blocks.
# ---------------------------------------------------------------------------

def _no_commits(monkeypatch):
    """Force _fetch_recent_commits to return no commits (advisory should fire
    for a weakly-supported completion)."""
    async def _fake(_project, _tenant):
        return []
    monkeypatch.setattr(mh, "_fetch_recent_commits", _fake)


def _commits(monkeypatch, commits):
    """Force _fetch_recent_commits to return a fixed commit list."""
    async def _fake(_project, _tenant):
        return list(commits)
    monkeypatch.setattr(mh, "_fetch_recent_commits", _fake)


def test_sprint_item_advisory_weak_completion_no_commit(monkeypatch):
    """(a) Weak completion (no task/notes) + no matching commit → advisory present,
    and the completion still succeeds."""
    _no_commits(monkeypatch)
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "adv-a"}, db, "/tmp"))
        pid = proj["id"]
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "Add rate limiter middleware bucket",
             "version": "v1"}, db, "/tmp"))
        done = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": item["id"]}, db, "/tmp"))
        assert done["status"] == "done"  # completion never blocked
        assert "completion_advisory" in done
        assert "double-check it actually shipped" in done["completion_advisory"]
    finally:
        _run(db.close())


def test_sprint_item_advisory_absent_when_commit_matches(monkeypatch):
    """(b) Weak completion but a recent commit shares >=3 keywords with the
    title → plausibly supported → NO advisory."""
    _commits(monkeypatch, [
        {"sha": "abc123def456",
         "message": "Add rate limiter middleware bucket for API"},
    ])
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "adv-b"}, db, "/tmp"))
        pid = proj["id"]
        # force=True so add_sprint_item's own drift guard (which fires when the
        # mocked commit matches the title) doesn't refuse to create the item.
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "Add rate limiter middleware bucket",
             "version": "v1", "force": True}, db, "/tmp"))
        done = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": item["id"]}, db, "/tmp"))
        assert done["status"] == "done"
        assert "completion_advisory" not in done
    finally:
        _run(db.close())


def test_sprint_item_advisory_absent_with_notes_evidence(monkeypatch):
    """(c) Completion carrying evidence (notes= arg) → evidence exists → NO
    advisory even when no commit matches."""
    _no_commits(monkeypatch)
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "adv-c"}, db, "/tmp"))
        pid = proj["id"]
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "Add rate limiter middleware bucket",
             "version": "v1"}, db, "/tmp"))
        done = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": item["id"],
             "notes": "shipped the limiter; tests green"}, db, "/tmp"))
        assert done["status"] == "done"
        assert "completion_advisory" not in done
    finally:
        _run(db.close())


def test_sprint_item_advisory_absent_with_task_id_evidence(monkeypatch):
    """(c') Completion linking a task_id → evidence exists → NO advisory."""
    _no_commits(monkeypatch)
    db = _make_db()
    try:
        import meridian.db as db_module
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "adv-c2"}, db, "/tmp"))
        pid = proj["id"]
        sess = _run(db_module.register_session(db, pid, "adv-sess"))
        task = _run(db_module.log_task(db, sess["id"], pid, "did the work"))
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "Add rate limiter middleware bucket",
             "version": "v1"}, db, "/tmp"))
        done = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": item["id"], "task_id": task["id"]},
            db, "/tmp"))
        assert done["status"] == "done"
        assert "completion_advisory" not in done
    finally:
        _run(db.close())


def test_sprint_item_advisory_never_blocks_on_commit_fetch_error(monkeypatch):
    """(d) A failing commit-fetch must never break completion — the advisory is
    silently skipped and the item still completes."""
    async def _boom(_project, _tenant):
        raise RuntimeError("git unavailable")
    monkeypatch.setattr(mh, "_fetch_recent_commits", _boom)
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "adv-d"}, db, "/tmp"))
        pid = proj["id"]
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "title": "Add rate limiter middleware bucket",
             "version": "v1"}, db, "/tmp"))
        done = _run(mh._dispatch_mcp_tool(
            "complete_sprint_item",
            {"project_id": pid, "item_id": item["id"]}, db, "/tmp"))
        assert done["status"] == "done"  # completion succeeded despite git error
    finally:
        _run(db.close())


def test_read_write_claim_distinction():
    """ffa03655 — shared read claims coexist; write is exclusive and waits for readers."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "rw"))
        pid = proj["id"]
        s1 = _run(db_module.register_session(db, pid, "reader-1"))
        s2 = _run(db_module.register_session(db, pid, "reader-2"))
        s3 = _run(db_module.register_session(db, pid, "writer"))
        r1 = _run(db_module.claim_file(db, "x.py", s1["id"], mode="read"))
        r2 = _run(db_module.claim_file(db, "x.py", s2["id"], mode="read"))
        assert r1["claimed"] and r1["claim_mode"] == "read"
        assert r2["claimed"] and set(r2["readers"]) >= {s1["id"], s2["id"]}
        # Writer blocked while readers hold the file.
        w = _run(db_module.claim_file(db, "x.py", s3["id"], mode="write"))
        assert not w["claimed"] and w["reason"] == "read_locked"
        # Readers release → writer can claim exclusively.
        _run(db_module.release_file(db, "x.py", s1["id"]))
        _run(db_module.release_file(db, "x.py", s2["id"]))
        w2 = _run(db_module.claim_file(db, "x.py", s3["id"], mode="write"))
        assert w2["claimed"] and w2["claim_mode"] == "write"
        # New reader blocked by the exclusive write lock.
        r3 = _run(db_module.claim_file(db, "x.py", s1["id"], mode="read"))
        assert not r3["claimed"] and r3["reason"] == "write_locked"
    finally:
        _run(db.close())


def test_store_and_get_findings_roundtrip():
    """c35370cc — findings persist and read back by key, incl. via dispatch."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "f"}, db, "/tmp"))
        pid = proj["id"]
        _run(mh._dispatch_mcp_tool("store_finding",
            {"project_id": pid, "content": "auth uses JWT", "key": "auth"}, db, "/tmp"))
        _run(db_module.store_finding(db, pid, "db is postgres", key="db"))
        allf = _run(mh._dispatch_mcp_tool("get_findings", {"project_id": pid}, db, "/tmp"))
        assert len(allf) == 2
        authf = _run(mh._dispatch_mcp_tool("get_findings", {"project_id": pid, "key": "auth"}, db, "/tmp"))
        assert len(authf) == 1 and "JWT" in authf[0]["content"]
    finally:
        _run(db.close())


def test_session_messaging_and_barrier():
    """d3a3a01d — send/receive + non-blocking idle_until_all_done barrier."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "m"))
        pid = proj["id"]
        s1 = _run(db_module.register_session(db, pid, "a"))
        s2 = _run(db_module.register_session(db, pid, "b"))
        _run(db_module.send_message(db, pid, s2["id"], "do Y", from_session_id=s1["id"]))
        msgs = _run(db_module.receive_messages(db, s2["id"]))
        assert len(msgs) == 1 and msgs[0]["payload"] == "do Y"
        # Marked read → next receive is empty.
        assert _run(db_module.receive_messages(db, s2["id"])) == []
        # Barrier: both active → not done.
        st = _run(db_module.idle_until_all_done(db, [s1["id"], s2["id"]]))
        assert not st["all_done"] and set(st["pending"]) == {s1["id"], s2["id"]}
        # Close both → done.
        _run(db_module.close_session(db, s1["id"]))
        _run(db_module.close_session(db, s2["id"]))
        st2 = _run(db_module.idle_until_all_done(db, [s1["id"], s2["id"]]))
        assert st2["all_done"] and st2["pending"] == []
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


def test_hosted_tools_call_bumps_session_last_seen():
    """4b698ea5 — on the HOSTED path, any native Meridian tool carrying a
    session_id refreshes that session's last_seen (mirrors the stdio handler).
    Previously the hosted /mcp path never did this, so hosted/tunnel sessions
    went stale between the sparse tools that happen to write last_seen."""
    import meridian.db as db_module
    import meridian.server as srv
    from datetime import datetime, timezone

    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "hb"))
        sess = _run(db_module.register_session(db, proj["id"], "s1"))
        # Age last_seen to 9 minutes ago.
        _run(db.execute(
            "UPDATE sessions SET last_seen = datetime('now','-9 minutes') WHERE id = ?",
            (sess["id"],),
        ))
        _run(db.commit())
        srv._CONNECTED_SESSIONS.pop(sess["id"], None)

        # A read-only native tool that carries session_id (no last_seen write of
        # its own) must still refresh last_seen via the handler's implicit bump.
        resp = _run(mh._handle_mcp_request(
            _req("tools/call", {
                "name": "get_session_brief",
                "arguments": {"session_id": sess["id"], "project_id": proj["id"]},
            }),
            db=db, data_dir="/tmp",
        ))
        assert "result" in resp

        rows = {r["id"]: r for r in _run(db_module.get_sessions(db, proj["id"], active_only=False))}
        ls = datetime.fromisoformat(rows[sess["id"]]["last_seen"].replace(" ", "T")).replace(tzinfo=timezone.utc)
        assert (datetime.now(timezone.utc) - ls).total_seconds() < 60  # refreshed
        # And it was marked "connected" so the keepalive loop holds it fresh.
        assert sess["id"] in srv._CONNECTED_SESSIONS
    finally:
        srv._CONNECTED_SESSIONS.pop(sess["id"], None)
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


def test_add_sprint_item_auto_infers_touches_resources():
    """07bdfdbb — a title with no explicit resources gets inferred ones."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "infer1"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": proj["id"], "version": "v1", "title": "fix server oauth route"},
            db, "/tmp",
        ))
        tr = out.get("touches_resources")
        if isinstance(tr, str):
            tr = json.loads(tr)
        assert "inferred:file:meridian/server.py" in tr
    finally:
        _run(db.close())


def test_add_sprint_item_explicit_resources_not_inferred():
    """Explicit touches_resources are kept verbatim — no inference applied."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "infer2"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": proj["id"], "version": "v1", "title": "fix server route",
             "touches_resources": ["file:custom.py"]},
            db, "/tmp",
        ))
        tr = out.get("touches_resources")
        if isinstance(tr, str):
            tr = json.loads(tr)
        assert tr == ["file:custom.py"]
    finally:
        _run(db.close())


def test_add_sprint_item_no_keyword_match_stays_undeclared():
    """A title that matches no hotspot keyword gets no inferred resources."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "infer4"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": proj["id"], "version": "v1", "title": "zzz qqq wibble"},
            db, "/tmp",
        ))
        tr = out.get("touches_resources")
        if isinstance(tr, str) and tr:
            tr = json.loads(tr)
        assert not tr
    finally:
        _run(db.close())


def test_fan_out_sprint_items_auto_infers_per_item():
    """07bdfdbb — fan-out infers resources per item from each title."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "infer3"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "fan_out_sprint_items",
            {"project_id": proj["id"], "items": [
                {"title": "update dashboard tab", "version": "v1"},
                {"title": "fix db migration query", "version": "v1"},
            ]},
            db, "/tmp",
        ))
        assert out["count"] == 2
        items = _run(mh._dispatch_mcp_tool(
            "get_sprint_items", {"project_id": proj["id"]}, db, "/tmp",
        ))
        all_tr: list[str] = []
        for it in items:
            tr = it.get("touches_resources")
            if isinstance(tr, str) and tr:
                tr = json.loads(tr)
            all_tr.extend(tr or [])
        assert "inferred:file:meridian/static/dashboard.js" in all_tr
        assert "inferred:file:meridian/db/__init__.py" in all_tr
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


def test_claim_sprint_item_race_returns_already_claimed():
    """df573218 — claiming an item another session already grabbed returns a
    structured already_claimed response pointing at the next item, not a crash."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "race"}, db, "/tmp"))
        pid = proj["id"]
        a = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "version": "v1", "title": "a", "touches_resources": ["file:a.py"]},
            db, "/tmp",
        ))
        b = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "version": "v1", "title": "b", "touches_resources": ["file:b.py"]},
            db, "/tmp",
        ))
        # First claim succeeds.
        first = _run(mh._dispatch_mcp_tool(
            "claim_sprint_item", {"project_id": pid, "item_id": a["id"]}, db, "/tmp",
        ))
        assert first.get("status") == "in_progress"
        # Second claim of the SAME item → already_claimed + points at b.
        out = _run(mh._dispatch_mcp_tool(
            "claim_sprint_item", {"project_id": pid, "item_id": a["id"]}, db, "/tmp",
        ))
        assert out["status"] == "already_claimed"
        assert out["current_status"] == "in_progress"
        assert out["next_available_id"] == b["id"]
    finally:
        _run(db.close())


def test_claim_sprint_item_race_no_next_item():
    """When the raced item is the only one, next_available_id is None."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "race2"}, db, "/tmp"))
        pid = proj["id"]
        a = _run(mh._dispatch_mcp_tool(
            "add_sprint_item",
            {"project_id": pid, "version": "v1", "title": "only", "touches_resources": ["file:a.py"]},
            db, "/tmp",
        ))
        _run(mh._dispatch_mcp_tool(
            "claim_sprint_item", {"project_id": pid, "item_id": a["id"]}, db, "/tmp",
        ))
        out = _run(mh._dispatch_mcp_tool(
            "claim_sprint_item", {"project_id": pid, "item_id": a["id"]}, db, "/tmp",
        ))
        assert out["status"] == "already_claimed"
        assert out["next_available_id"] is None
    finally:
        _run(db.close())


def test_start_session_reports_hitl_auto_answer_mode():
    """72e12ed8 — orientation carries the project's HITL auto-answer mode + directive."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "hitlmode"}, db, "/tmp"))
        pid = proj["id"]
        # Default mode 0 → OFF directive.
        out = _run(mh._dispatch_mcp_tool(
            "start_session", {"project_id": pid, "session_name": "s1"}, db, "/tmp",
        ))
        assert out["hitl_auto_answer_mode"] == 0
        assert "auto-answer OFF" in out["hitl_auto_answer_directive"]
        # Set mode 2 → AGGRESSIVE directive on a fresh session.
        _run(db_module.update_project_settings(db, pid, hitl_auto_answer=2))
        out2 = _run(mh._dispatch_mcp_tool(
            "start_session", {"project_id": pid, "session_name": "s2"}, db, "/tmp",
        ))
        assert out2["hitl_auto_answer_mode"] == 2
        assert "AGGRESSIVE" in out2["hitl_auto_answer_directive"]
    finally:
        _run(db.close())


def test_update_sprint_item_blocks_in_progress():
    """586eeda9 — mutating an in_progress item is blocked unless force=true."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "mutguard"}, db, "/tmp"))
        pid = proj["id"]
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item", {"project_id": pid, "version": "v1", "title": "a",
                                "touches_resources": ["file:a.py"]}, db, "/tmp",
        ))
        _run(mh._dispatch_mcp_tool(
            "claim_sprint_item", {"project_id": pid, "item_id": item["id"]}, db, "/tmp",
        ))
        out = _run(mh._dispatch_mcp_tool(
            "update_sprint_item", {"project_id": pid, "item_id": item["id"], "title": "b"},
            db, "/tmp",
        ))
        assert out["error"] == "IN_PROGRESS"
        assert "force=true" in out["message"]
        forced = _run(mh._dispatch_mcp_tool(
            "update_sprint_item",
            {"project_id": pid, "item_id": item["id"], "title": "b", "force": True},
            db, "/tmp",
        ))
        assert forced.get("error") != "IN_PROGRESS"
        assert forced.get("title") == "b"
    finally:
        _run(db.close())


def test_update_sprint_item_pending_not_blocked():
    """A pending (unclaimed) item mutates freely."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "mutok"}, db, "/tmp"))
        pid = proj["id"]
        item = _run(mh._dispatch_mcp_tool(
            "add_sprint_item", {"project_id": pid, "version": "v1", "title": "a"}, db, "/tmp",
        ))
        out = _run(mh._dispatch_mcp_tool(
            "update_sprint_item", {"project_id": pid, "item_id": item["id"], "title": "renamed"},
            db, "/tmp",
        ))
        assert out.get("title") == "renamed"
    finally:
        _run(db.close())


def test_fan_out_warns_on_active_session():
    """586eeda9 — fan_out_sprint_items surfaces an active-session warning."""
    import meridian.db as db_module
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "fanwarn"}, db, "/tmp"))
        pid = proj["id"]
        _run(db_module.register_session(db, pid, "live-worker"))
        out = _run(mh._dispatch_mcp_tool(
            "fan_out_sprint_items",
            {"project_id": pid, "items": [{"title": "x", "version": "v1"}]},
            db, "/tmp",
        ))
        assert out["count"] == 1
        assert "active_session_warning" in out
        assert "live-worker" in out["active_session_warning"]
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
    """set_active_repo with no extract tunnel raises actionable ValueError."""
    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {})
    db = _make_db()
    try:
        with pytest.raises(ValueError, match="tunnel not connected"):
            _run(mh._dispatch_mcp_tool(
                "set_active_repo", {"repo_path": "/my/repo"},
                db, "/tmp", tenant={"id": "no-tenant"},
            ))
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


# ---------------------------------------------------------------------------
# 3adbc954 — set_executor_config must persist filesystem_roots (was dropped)
# ---------------------------------------------------------------------------

def test_set_executor_config_persists_filesystem_roots():
    """filesystem_roots passed to set_executor_config round-trips (regression)."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "fsr"}, db, "/tmp"))
        pid = proj["id"]
        out = _run(mh._dispatch_mcp_tool(
            "set_executor_config",
            {"project_id": pid, "filesystem_roots": ["C:/a", "D:/b"]},
            db, "/tmp",
        ))
        assert out["filesystem_roots"] == ["C:/a", "D:/b"]
        # Read back through a fresh get to prove it persisted to the DB.
        import meridian.db as db_module
        cfg = _run(db_module.get_executor_config(db, pid))
        assert cfg["filesystem_roots"] == ["C:/a", "D:/b"]
    finally:
        _run(db.close())


def test_set_executor_config_persists_max_turns():
    """d2c47f43 — max_turns round-trips through set_executor_config."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "mt"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "set_executor_config", {"project_id": proj["id"], "max_turns": 75}, db, "/tmp",
        ))
        assert out["max_turns"] == 75
    finally:
        _run(db.close())


def test_set_executor_config_filesystem_roots_coexist_with_repo_path():
    """Setting repo_path later does not wipe a previously-set filesystem_roots."""
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "fsr2"}, db, "/tmp"))
        pid = proj["id"]
        _run(mh._dispatch_mcp_tool(
            "set_executor_config",
            {"project_id": pid, "filesystem_roots": ["/x"]},
            db, "/tmp",
        ))
        out = _run(mh._dispatch_mcp_tool(
            "set_executor_config",
            {"project_id": pid, "repo_path": "/x/repo"},
            db, "/tmp",
        ))
        assert out["filesystem_roots"] == ["/x"]
        assert out["repo_path"] == "/x/repo"
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# b2a417ad — start_session points the Serena pool at the project's repo so
# claude.ai chat sessions (no X-Meridian-Repo-Path header) route correctly.
# ---------------------------------------------------------------------------

def test_start_session_sends_active_repo_and_fs_roots(monkeypatch):
    """start_session with a tenant + known repo_path sends BOTH the extract
    set_active_repo control and the fs add_fs_roots control."""
    extract_sent = []
    fs_sent = []

    class _FakeExtractWS:
        async def send_json(self, obj):
            extract_sent.append(obj)

    class _FakeFsWS:
        async def send_json(self, obj):
            fs_sent.append(obj)

    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {"t1": _FakeExtractWS()})
    monkeypatch.setattr(tn, "_tunnel_sockets", {"t1": _FakeFsWS()})
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ss-route"}, db, "/tmp"))
        pid = proj["id"]
        _run(mh._dispatch_mcp_tool(
            "set_executor_config",
            {"project_id": pid, "repo_path": "C:/repo/here"},
            db, "/tmp",
        ))
        _run(mh._dispatch_mcp_tool(
            "start_session",
            {"project_id": pid, "session_name": "chat"}, db, "/tmp",
            tenant={"id": "t1"},
        ))
        assert {"type": "set_active_repo", "repo_path": "C:/repo/here"} in extract_sent
        assert {"type": "add_fs_roots", "roots": ["C:/repo/here"]} in fs_sent
    finally:
        _run(db.close())


def test_start_session_unions_repo_path_and_filesystem_roots(monkeypatch):
    """bc2e5ff0 — start_session serves repo_path AND filesystem_roots (deduped),
    and the Serena target stays the repo_path."""
    extract_sent = []
    fs_sent = []

    class _FakeExtractWS:
        async def send_json(self, obj):
            extract_sent.append(obj)

    class _FakeFsWS:
        async def send_json(self, obj):
            fs_sent.append(obj)

    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {"t1": _FakeExtractWS()})
    monkeypatch.setattr(tn, "_tunnel_sockets", {"t1": _FakeFsWS()})
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ss-union"}, db, "/tmp"))
        pid = proj["id"]
        _run(mh._dispatch_mcp_tool(
            "set_executor_config",
            {"project_id": pid, "repo_path": "C:/repo",
             "filesystem_roots": ["C:/repo", "D:/Outputs", "E:/data"]},
            db, "/tmp",
        ))
        _run(mh._dispatch_mcp_tool(
            "start_session",
            {"project_id": pid, "session_name": "chat"}, db, "/tmp",
            tenant={"id": "t1"},
        ))
        # repo_path first, then the extra roots; the duplicate C:/repo is deduped.
        assert {"type": "add_fs_roots",
                "roots": ["C:/repo", "D:/Outputs", "E:/data"]} in fs_sent
        # Serena indexes one project — it stays pinned to repo_path.
        assert {"type": "set_active_repo", "repo_path": "C:/repo"} in extract_sent
    finally:
        _run(db.close())


def test_start_session_filesystem_roots_only_no_repo_path(monkeypatch):
    """With only filesystem_roots set (no repo_path), the Serena target falls
    back to the first root and all roots are served."""
    extract_sent = []
    fs_sent = []

    class _FakeExtractWS:
        async def send_json(self, obj):
            extract_sent.append(obj)

    class _FakeFsWS:
        async def send_json(self, obj):
            fs_sent.append(obj)

    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {"t1": _FakeExtractWS()})
    monkeypatch.setattr(tn, "_tunnel_sockets", {"t1": _FakeFsWS()})
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ss-rootsonly"}, db, "/tmp"))
        pid = proj["id"]
        _run(mh._dispatch_mcp_tool(
            "set_executor_config",
            {"project_id": pid, "filesystem_roots": ["D:/Outputs", "E:/data"]},
            db, "/tmp",
        ))
        _run(mh._dispatch_mcp_tool(
            "start_session",
            {"project_id": pid, "session_name": "chat"}, db, "/tmp",
            tenant={"id": "t1"},
        ))
        assert {"type": "add_fs_roots", "roots": ["D:/Outputs", "E:/data"]} in fs_sent
        assert {"type": "set_active_repo", "repo_path": "D:/Outputs"} in extract_sent
    finally:
        _run(db.close())


def test_start_session_injects_codebase_directive_when_index_healthy(monkeypatch):
    """9f6aec5f + 2c645647 — a healthy code index injects codebase_context AND
    prepends the CODEBASE INDEX directive to agent_instructions."""
    import meridian.server as srv
    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {})
    monkeypatch.setattr(tn, "_tunnel_sockets", {})

    async def fake_cc(tenant_id, project_id, *, compact):
        return {"packages": ["meridian"], "note": "idx"}
    monkeypatch.setattr(srv, "_build_codebase_context", fake_cc)

    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ci-dir"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "start_session",
            {"project_id": proj["id"], "session_name": "chat"}, db, "/tmp",
            tenant={"id": "t1"},
        ))
        assert out["codebase_context"]["packages"] == ["meridian"]
        assert out["agent_instructions"].startswith(srv.CODEBASE_INDEX_DIRECTIVE)
    finally:
        _run(db.close())


def test_start_session_no_codebase_directive_when_no_index(monkeypatch):
    """No healthy index → no codebase_context and no directive prepended."""
    import meridian.server as srv
    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {})
    monkeypatch.setattr(tn, "_tunnel_sockets", {})

    async def fake_cc(tenant_id, project_id, *, compact):
        return None
    monkeypatch.setattr(srv, "_build_codebase_context", fake_cc)

    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ci-none"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "start_session",
            {"project_id": proj["id"], "session_name": "chat"}, db, "/tmp",
            tenant={"id": "t1"},
        ))
        assert "codebase_context" not in out
        assert srv.CODEBASE_INDEX_DIRECTIVE not in (out.get("agent_instructions") or "")
    finally:
        _run(db.close())


def test_start_session_no_repo_path_sends_nothing(monkeypatch):
    """No repo_path configured → no control messages, start_session still works."""
    extract_sent = []

    class _FakeExtractWS:
        async def send_json(self, obj):
            extract_sent.append(obj)

    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "_tunnel_extract_sockets", {"t1": _FakeExtractWS()})
    monkeypatch.setattr(tn, "_tunnel_sockets", {})
    db = _make_db()
    try:
        proj = _run(mh._dispatch_mcp_tool("create_project", {"name": "ss-norepo"}, db, "/tmp"))
        out = _run(mh._dispatch_mcp_tool(
            "start_session",
            {"project_id": proj["id"], "session_name": "chat"}, db, "/tmp",
            tenant={"id": "t1"},
        ))
        assert isinstance(out, dict)
        assert extract_sent == []
    finally:
        _run(db.close())
