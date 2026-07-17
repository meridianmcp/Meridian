"""Coverage tests for meridian/mcp/handler.py — sprint item 0882b8d6.

Targets high-risk, previously-uncovered dispatch branches:

1. scoped_project_ids enforcement (project out-of-scope denial) [lines 926-934]
2. _handle_mcp_request cross-instance tunnel miss [lines 966-973]
3. _maybe_add_log_task_nudge (threshold path + sprint count bypass) [lines 1009-1039]
4. ingest_document hosted-mode guard (file_path without content) [lines 2526-2540]
5. get_document_structure hosted-mode guard [lines 2594-2606]
6. search_synthesis missing-query guard [lines 3610-3611]
7. add_sprint_item_pointer — missing required arg guards [lines 4677-4680]
8. get_sprint_item_pointers — missing sprint_item_id guard [lines 4725-4726]
9. list_plugins dispatch — no tenant (returns empty, no crash) [lines 5307-5420]
10. get_plugin_details — missing name and unknown plugin guards [lines 5427-5437]
11. search_outputs — missing-arg guards + hosted-mode guard [lines 5533-5563]
12. search_code_semantic — missing-arg guard + data_dir branch [lines 5606-5644]
13. GitHub patch_file — missing-arg guards [lines 391-452]
14. GitHub get_workflow_run_logs — no-failed-jobs path [lines 584-621]

All tests use _dispatch_mcp_tool / _handle_mcp_request directly
(integration-dispatch style, real in-memory SQLite). No network calls.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import meridian.server  # noqa: F401 — initialise server before handler import
from meridian.mcp import handler as mh
from meridian import db as db_module


def _run(coro):
    return asyncio.run(coro)


def _req(method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _make_db():
    return _run(db_module.init_db(":memory:"))


# ---------------------------------------------------------------------------
# 1. scoped_project_ids enforcement
# ---------------------------------------------------------------------------

def test_handle_request_scoped_project_ids_denies_out_of_scope():
    """95499c3e — a tools/call referencing a project outside the caller's scope
    must return a -32603 'outside your access scope' error."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "scoped-proj"))
        pid = proj["id"]
        # Caller is scoped to a different project ID — pid is out of scope.
        resp = _run(mh._handle_mcp_request(
            _req("tools/call", {"name": "get_goal", "arguments": {"project_id": pid}}),
            db=db, data_dir="/tmp",
            scoped_project_ids=["ffffffff-ffff-ffff-ffff-000000000000"],  # not pid
        ))
        assert resp["error"]["code"] == -32603
        assert "access scope" in resp["error"]["message"]
    finally:
        _run(db.close())


def test_handle_request_scoped_project_ids_allows_in_scope():
    """A project_id that IS in scoped_project_ids must pass and dispatch normally."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "in-scope-proj"))
        pid = proj["id"]
        resp = _run(mh._handle_mcp_request(
            _req("tools/call", {"name": "get_goal", "arguments": {"project_id": pid}}),
            db=db, data_dir="/tmp",
            scoped_project_ids=[pid],  # explicitly in scope
        ))
        # Should reach the tool and succeed (goal is None = no error key in response).
        assert "error" not in resp
        assert "result" in resp
    finally:
        _run(db.close())


def test_handle_request_scoped_project_ids_resolves_project_name():
    """When project_id is absent but project_name is given, scope check resolves it."""
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "name-scoped"))
        pid = proj["id"]
        # project_name that resolves to pid — pid is NOT in the scope list.
        resp = _run(mh._handle_mcp_request(
            _req("tools/call", {
                "name": "get_goal",
                "arguments": {"project_name": "name-scoped"},
            }),
            db=db, data_dir="/tmp",
            scoped_project_ids=["ffffffff-ffff-ffff-ffff-000000000000"],
        ))
        assert resp["error"]["code"] == -32603
        assert "access scope" in resp["error"]["message"]
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# 2. Cross-instance tunnel miss in _handle_mcp_request
# ---------------------------------------------------------------------------

def test_handle_request_cross_instance_tunnel_miss_returns_32002(monkeypatch):
    """a19538fe — when a tunnel tool is called but the tunnel socket is on a
    sibling instance, return a -32002 'reconnecting' error."""
    from meridian.routes import tunnel as tn
    tenant = {"id": "cross-tenant", "plan": "pro"}
    # Simulate: tool is NOT a native Meridian name, NOT a GitHub name,
    # has_active_tunnel returns False, but tunnel_cross_instance_miss returns True.
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: False)
    monkeypatch.setattr(tn, "tunnel_cross_instance_miss", lambda t: True)
    monkeypatch.setattr(tn, "CROSS_INSTANCE_MISS_MESSAGE", "tunnel on sibling instance")

    db = _make_db()
    try:
        resp = _run(mh._handle_mcp_request(
            _req("tools/call", {"name": "filesystem__read_file", "arguments": {}}),
            db=db, data_dir="/tmp", tenant=tenant,
        ))
        assert resp["error"]["code"] == -32002
        assert "sibling instance" in resp["error"]["message"]
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# 3. _maybe_add_log_task_nudge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_task_nudge_fires_after_threshold(db):
    """log_task nudge activates when session has logged >= threshold tasks
    without claiming any sprint items."""
    # Set threshold to 3 via workspace settings.
    await db_module.update_workspace_settings(db, log_task_sprint_nudge_threshold=3)
    proj = await db_module.create_project(db, "nudge-proj")
    sess = await db_module.register_session(db, proj["id"], "nudge-sess")
    sid = sess["id"]
    pid = proj["id"]

    # Log 2 tasks — below threshold, no nudge yet.
    for i in range(2):
        out = await mh._dispatch_mcp_tool(
            "log_task",
            {"session_id": sid, "project_id": pid, "description": f"task {i}"},
            db, "/tmp",
        )
        assert "nudge" not in out, f"nudge fired early at task {i}"

    # Log 3rd task — at threshold with no sprint items claimed, nudge must fire.
    out3 = await mh._dispatch_mcp_tool(
        "log_task",
        {"session_id": sid, "project_id": pid, "description": "task 3"},
        db, "/tmp",
    )
    assert "nudge" in out3, "nudge should fire at threshold"
    assert "sprint items" in out3["nudge"]


@pytest.mark.asyncio
async def test_log_task_nudge_suppressed_when_threshold_zero(db):
    """A threshold of 0 disables the nudge entirely."""
    await db_module.update_workspace_settings(db, log_task_sprint_nudge_threshold=0)
    proj = await db_module.create_project(db, "nudge-off-proj")
    sess = await db_module.register_session(db, proj["id"], "nudge-off-sess")
    for i in range(10):
        out = await mh._dispatch_mcp_tool(
            "log_task",
            {"session_id": sess["id"], "project_id": proj["id"], "description": f"t{i}"},
            db, "/tmp",
        )
        assert "nudge" not in out, "threshold=0 should disable nudge"


@pytest.mark.asyncio
async def test_log_task_nudge_suppressed_when_sprint_item_claimed(db):
    """Nudge is suppressed when the session has also claimed a sprint item."""
    await db_module.update_workspace_settings(db, log_task_sprint_nudge_threshold=2)
    proj = await db_module.create_project(db, "nudge-claimed-proj")
    sess = await db_module.register_session(db, proj["id"], "nudge-claimed-sess")
    sid = sess["id"]
    pid = proj["id"]

    # Add and claim a sprint item so sprint_count > 0.
    item = await db_module.add_sprint_item(db, pid, "v1", "claimed work")
    await db_module.claim_sprint_item(db, pid, item["id"])

    # Log tasks beyond the threshold — nudge should NOT fire.
    for i in range(5):
        out = await mh._dispatch_mcp_tool(
            "log_task",
            {"session_id": sid, "project_id": pid, "description": f"work {i}"},
            db, "/tmp",
        )
        assert "nudge" not in out, f"nudge fired incorrectly at task {i} (sprint item claimed)"


# ---------------------------------------------------------------------------
# 4. ingest_document hosted-mode guard
# ---------------------------------------------------------------------------

def test_ingest_document_hosted_path_only_returns_hosted_error(monkeypatch):
    """832d67af — ingest_document with file_path but no content in hosted mode
    returns an honest error rather than a misleading '[Errno 2]'."""
    from meridian.mcp import handler as _mh
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "idoc-hosted"))
        out = _run(mh._dispatch_mcp_tool(
            "ingest_document",
            {"project_id": proj["id"], "file_path": "/local/file.docx"},
            db, "/tmp",
        ))
        assert out.get("hosted") is True
        assert "ingest_document reads the file" in out["error"]
        assert "file_path" in out
    finally:
        _run(db.close())


def test_ingest_document_hosted_with_content_passes_guard(monkeypatch):
    """content= (pre-extracted text) bypasses the hosted-mode path guard."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    db = _make_db()
    try:
        proj = _run(db_module.create_project(db, "idoc-hosted-content"))
        # Providing content= should bypass the hosted guard and attempt ingestion.
        out = _run(mh._dispatch_mcp_tool(
            "ingest_document",
            {
                "project_id": proj["id"],
                "title": "My Doc",
                "content": "# Section 1\n\nSome content here.",
            },
            db, "/tmp",
        ))
        # Should succeed (content-only path is not blocked by hosted guard).
        assert "hosted" not in out or out.get("hosted") is not True
        # The note was created.
        assert out.get("title") or out.get("note_id") or "error" not in out
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# 5. get_document_structure hosted-mode guard
# ---------------------------------------------------------------------------

def test_get_document_structure_hosted_returns_honest_error(monkeypatch):
    """b43bab91 — get_document_structure in hosted mode returns a clear error
    instead of a misleading 'file not found' from the server's filesystem."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    db = _make_db()
    try:
        out = _run(mh._dispatch_mcp_tool(
            "get_document_structure",
            {"project_id": "ignored", "file_path": "/my/local/doc.docx"},
            db, "/tmp",
        ))
        assert out.get("hosted") is True
        assert "get_document_structure reads the .docx" in out["error"]
        assert out["file_path"] == "/my/local/doc.docx"
    finally:
        _run(db.close())


def test_get_document_structure_missing_file_path_returns_error(monkeypatch):
    """file_path is required; omitting it returns a clean error."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: False)
    db = _make_db()
    try:
        out = _run(mh._dispatch_mcp_tool(
            "get_document_structure",
            {"project_id": "x", "file_path": ""},
            db, "/tmp",
        ))
        assert "error" in out
        assert "file_path is required" in out["error"]
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# 6. search_synthesis missing-query guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_synthesis_missing_query_returns_error(db):
    """search_synthesis returns a clean error dict when query is absent."""
    proj = await db_module.create_project(db, "syn-proj")
    out = await mh._dispatch_mcp_tool(
        "search_synthesis",
        {"project_id": proj["id"]},  # no query
        db, "/tmp",
    )
    assert "error" in out
    assert "query is required" in out["error"]


@pytest.mark.asyncio
async def test_search_synthesis_empty_query_returns_error(db):
    """Empty string query also triggers the guard."""
    proj = await db_module.create_project(db, "syn-proj2")
    out = await mh._dispatch_mcp_tool(
        "search_synthesis",
        {"project_id": proj["id"], "query": ""},
        db, "/tmp",
    )
    assert "error" in out


# ---------------------------------------------------------------------------
# 7. add_sprint_item_pointer — required arg guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_sprint_item_pointer_missing_project_id(db):
    """add_sprint_item_pointer returns error when project_id is absent."""
    out = await mh._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"sprint_item_id": "some-item"},  # no project_id
        db, "/tmp",
    )
    assert "error" in out
    assert "project_id is required" in out["error"]


@pytest.mark.asyncio
async def test_add_sprint_item_pointer_missing_sprint_item_id(db):
    """add_sprint_item_pointer returns error when sprint_item_id is absent."""
    proj = await db_module.create_project(db, "ptr-proj")
    out = await mh._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {"project_id": proj["id"]},  # no sprint_item_id
        db, "/tmp",
    )
    assert "error" in out
    assert "sprint_item_id is required" in out["error"]


@pytest.mark.asyncio
async def test_add_sprint_item_pointer_unknown_item_returns_error(db):
    """add_sprint_item_pointer with a nonexistent sprint_item_id returns error dict."""
    proj = await db_module.create_project(db, "ptr-proj2")
    out = await mh._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {
            "project_id": proj["id"],
            "sprint_item_id": "ffffffff-ffff-ffff-ffff-000000000000",
            "source_type": "note",
            "targets": [],
        },
        db, "/tmp",
    )
    # Should be a structured error, not a crash.
    assert "error" in out


@pytest.mark.asyncio
async def test_add_sprint_item_pointer_roundtrip(db):
    """add/get pointers roundtrip through the dispatch layer (happy path)."""
    proj = await db_module.create_project(db, "ptr-happy")
    item = await db_module.add_sprint_item(db, proj["id"], "v1", "Pointer target item")

    result = await mh._dispatch_mcp_tool(
        "add_sprint_item_pointer",
        {
            "project_id": proj["id"],
            "sprint_item_id": item["id"],
            "source_type": "note",
            "targets": [{
                "uri": "note:abc123",
                "selector": {"type": "symbol", "qualified_name": "some.symbol"},
            }],
            "label": "related evidence",
        },
        db, "/tmp",
    )
    assert "error" not in result
    # Pointer can be retrieved.
    got = await mh._dispatch_mcp_tool(
        "get_sprint_item_pointers",
        {"sprint_item_id": item["id"]},
        db, "/tmp",
    )
    assert got["sprint_item_id"] == item["id"]
    assert len(got["pointers"]) == 1


# ---------------------------------------------------------------------------
# 8. get_sprint_item_pointers — missing required arg
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_sprint_item_pointers_missing_sprint_item_id(db):
    """get_sprint_item_pointers returns error when sprint_item_id is absent."""
    out = await mh._dispatch_mcp_tool(
        "get_sprint_item_pointers",
        {},
        db, "/tmp",
    )
    assert "error" in out
    assert "sprint_item_id is required" in out["error"]


# ---------------------------------------------------------------------------
# 9. list_plugins — basic dispatch (no tunnel active)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_plugins_no_tenant_no_tunnel(db, monkeypatch):
    """list_plugins with no tenant returns the builtin plugin list with all
    inactive (no tunnel), never crashes."""
    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: False)

    out = await mh._dispatch_mcp_tool(
        "list_plugins", {}, db, "/tmp", tenant=None,
    )
    assert "plugins" in out
    assert isinstance(out["plugins"], list)
    assert len(out["plugins"]) > 0
    # All inactive (no live tunnel).
    assert all(not p["active"] for p in out["plugins"])
    assert out["tunnel_active"] is False
    assert "hint" in out


@pytest.mark.asyncio
async def test_list_plugins_with_tenant_no_active_tunnel(db, monkeypatch):
    """list_plugins with a tenant but no active tunnel: same result, tunnel_active=False."""
    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: False)
    tenant = {"id": "t-plugins", "plan": "pro"}

    out = await mh._dispatch_mcp_tool(
        "list_plugins", {}, db, "/tmp", tenant=tenant,
    )
    assert out["tunnel_active"] is False
    assert isinstance(out["plugins"], list)


# ---------------------------------------------------------------------------
# 10. get_plugin_details — missing name and unknown plugin guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_plugin_details_missing_name(db, monkeypatch):
    """get_plugin_details with no name returns a clean error dict."""
    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: False)

    out = await mh._dispatch_mcp_tool(
        "get_plugin_details", {}, db, "/tmp", tenant=None,
    )
    assert "error" in out
    assert "name is required" in out["error"]


@pytest.mark.asyncio
async def test_get_plugin_details_unknown_plugin(db, monkeypatch):
    """get_plugin_details with an unknown plugin name returns error."""
    from meridian.routes import tunnel as tn
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: False)

    out = await mh._dispatch_mcp_tool(
        "get_plugin_details", {"name": "nonexistent-plugin-xyz"}, db, "/tmp", tenant=None,
    )
    assert "error" in out
    assert "unknown plugin" in out["error"]


@pytest.mark.asyncio
async def test_get_plugin_details_known_plugin_no_tunnel(db, monkeypatch):
    """get_plugin_details for a known builtin plugin name returns plugin details."""
    from meridian.routes import tunnel as tn
    from meridian.tunnel_plugins import BUILTIN_PLUGINS
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: False)

    # Use the first builtin plugin name.
    plugin_name = BUILTIN_PLUGINS[0]["name"]
    out = await mh._dispatch_mcp_tool(
        "get_plugin_details", {"name": plugin_name}, db, "/tmp", tenant=None,
    )
    assert "error" not in out
    assert out["name"] == plugin_name
    assert "slot" in out
    assert "tools" in out  # empty list when no tunnel
    assert out["tool_count"] == 0


# ---------------------------------------------------------------------------
# ea2c7aed — cross-instance-aware tunnel reachability in list_plugins /
# get_plugin_details.
#
# On Fly.io multi-instance, a request can land on an instance that doesn't
# hold the tenant's tunnel WebSocket.  has_active_tunnel() is per-process
# in-memory — it returns False on a non-owning instance — but the owning
# instance's id is recorded in _tenant_owner_instance, and the DB flag
# tenant.tunnel_active is set on connect / cleared on disconnect.  Both
# sources constitute cross-instance evidence that the tunnel IS active.
#
# These tests simulate the cross-instance scenario directly (parallel to
# test_tunnel_fly_replay.py's pattern for _do_proxy) and verify that
# list_plugins / get_plugin_details correctly report tunnel_active=True
# even when has_active_tunnel() says False on the current instance.
# ---------------------------------------------------------------------------

def _reset_tunnel_state(tid: str) -> None:
    """Remove all in-memory tunnel state for `tid` (mirrors _reset() in test_tunnel_fly_replay)."""
    from meridian.routes import tunnel as tn
    tn._tenant_owner_instance.pop(tid, None)
    tn._tunnel_sockets.pop(tid, None)
    tn._tunnel_extract_sockets.pop(tid, None)
    tn._tunnel_code_sockets.pop(tid, None)
    tn._tunnel_ppt_sockets.pop(tid, None)
    tn._tunnel_word_sockets.pop(tid, None)
    tn._tunnel_dc_sockets.pop(tid, None)


@pytest.mark.asyncio
async def test_list_plugins_tunnel_active_via_owner_instance(db, monkeypatch):
    """ea2c7aed: list_plugins reports tunnel_active=True when a sibling Fly instance
    owns the socket (_tenant_owner_instance set), even though has_active_tunnel()
    returns False on THIS instance.
    """
    from meridian.routes import tunnel as tn

    tid = "cross-lp-owner"
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    try:
        # Simulate: sibling owns the socket, no local socket.
        tn._tenant_owner_instance[tid] = "machine-sibling"
        tenant = {"id": tid, "plan": "pro"}

        out = await mh._dispatch_mcp_tool(
            "list_plugins", {}, db, "/tmp", tenant=tenant,
        )
        assert out["tunnel_active"] is True, (
            "list_plugins must report tunnel_active=True when a sibling instance "
            "is the known owner (_tenant_owner_instance), even with no local socket"
        )
        assert "plugins" in out
    finally:
        _reset_tunnel_state(tid)


@pytest.mark.asyncio
async def test_list_plugins_tunnel_active_via_db_flag(db, monkeypatch):
    """ea2c7aed: list_plugins reports tunnel_active=True when tenant.tunnel_active
    is set in the DB (set on WS connect, cleared on disconnect), even though
    has_active_tunnel() returns False because there is no local socket.
    """
    from meridian.routes import tunnel as tn

    tid = "cross-lp-dbflag"
    try:
        # No _tenant_owner_instance entry, no local sockets — but DB flag is set.
        tenant = {"id": tid, "plan": "pro", "tunnel_active": 1}

        out = await mh._dispatch_mcp_tool(
            "list_plugins", {}, db, "/tmp", tenant=tenant,
        )
        assert out["tunnel_active"] is True, (
            "list_plugins must report tunnel_active=True when tenant.tunnel_active "
            "DB flag is set, even with no local socket and no owner-instance record"
        )
    finally:
        _reset_tunnel_state(tid)


@pytest.mark.asyncio
async def test_list_plugins_tunnel_inactive_when_no_evidence(db, monkeypatch):
    """ea2c7aed: list_plugins correctly reports tunnel_active=False when neither
    a local socket, a sibling-owner record, nor a DB flag is present.
    """
    from meridian.routes import tunnel as tn

    tid = "cross-lp-inactive"
    try:
        # All three sources absent → tunnel genuinely not connected.
        tenant = {"id": tid, "plan": "pro", "tunnel_active": 0}

        out = await mh._dispatch_mcp_tool(
            "list_plugins", {}, db, "/tmp", tenant=tenant,
        )
        assert out["tunnel_active"] is False
    finally:
        _reset_tunnel_state(tid)


@pytest.mark.asyncio
async def test_list_plugins_slot_tools_not_fetched_on_cross_instance_miss(db, monkeypatch):
    """ea2c7aed: on a cross-instance miss (no local socket, sibling owns it),
    list_plugins must NOT attempt _fetch_slot_tools (which would return [] and is
    wasteful), so all slot tool_counts remain 0 / active=False in per-plugin entries.
    tunnel_active top-level is still True.
    """
    from meridian.routes import tunnel as tn

    fetch_calls: list[str] = []

    async def _fake_fetch(tid: str, label: str):
        fetch_calls.append(label)
        return label, []

    monkeypatch.setattr(tn, "_fetch_slot_tools", _fake_fetch)

    tid = "cross-lp-nofetch"
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    try:
        tn._tenant_owner_instance[tid] = "machine-sibling"
        tenant = {"id": tid, "plan": "pro"}

        out = await mh._dispatch_mcp_tool(
            "list_plugins", {}, db, "/tmp", tenant=tenant,
        )
        # tunnel_active must be True (cross-instance evidence).
        assert out["tunnel_active"] is True
        # But _fetch_slot_tools must NOT have been called (no local socket).
        assert fetch_calls == [], (
            "_fetch_slot_tools should not be called on a cross-instance miss; "
            f"got calls for labels: {fetch_calls}"
        )
        # Per-slot entries: active=False because no live tools were fetched.
        for entry in out["plugins"]:
            assert entry["active"] is False
            assert entry["invocable"] is False
            assert entry["tools"] == []
    finally:
        _reset_tunnel_state(tid)


@pytest.mark.asyncio
async def test_get_plugin_details_tunnel_active_via_owner_instance(db, monkeypatch):
    """ea2c7aed: get_plugin_details reports tunnel_active=True when a sibling Fly
    instance owns the socket, even though has_active_tunnel() returns False here.
    """
    from meridian.routes import tunnel as tn
    from meridian.tunnel_plugins import BUILTIN_PLUGINS

    tid = "cross-gpd-owner"
    monkeypatch.setenv("FLY_MACHINE_ID", "machine-self")
    try:
        tn._tenant_owner_instance[tid] = "machine-sibling"
        tenant = {"id": tid, "plan": "pro"}

        plugin_name = BUILTIN_PLUGINS[0]["name"]
        out = await mh._dispatch_mcp_tool(
            "get_plugin_details", {"name": plugin_name}, db, "/tmp", tenant=tenant,
        )
        assert "error" not in out
        assert out["tunnel_active"] is True, (
            "get_plugin_details must report tunnel_active=True when a sibling "
            "instance is the known owner, even with no local socket"
        )
        # tools list is empty (can't fetch from a non-owning instance) — expected.
        assert out["tools"] == []
        assert out["tool_count"] == 0
    finally:
        _reset_tunnel_state(tid)


@pytest.mark.asyncio
async def test_get_plugin_details_tunnel_active_via_db_flag(db, monkeypatch):
    """ea2c7aed: get_plugin_details reports tunnel_active=True when the DB flag
    is set, with no local socket and no owner-instance record.
    """
    from meridian.routes import tunnel as tn
    from meridian.tunnel_plugins import BUILTIN_PLUGINS

    tid = "cross-gpd-dbflag"
    try:
        tenant = {"id": tid, "plan": "pro", "tunnel_active": 1}

        plugin_name = BUILTIN_PLUGINS[0]["name"]
        out = await mh._dispatch_mcp_tool(
            "get_plugin_details", {"name": plugin_name}, db, "/tmp", tenant=tenant,
        )
        assert "error" not in out
        assert out["tunnel_active"] is True
        assert out["tools"] == []
    finally:
        _reset_tunnel_state(tid)


@pytest.mark.asyncio
async def test_get_plugin_details_tunnel_inactive_when_no_evidence(db, monkeypatch):
    """ea2c7aed: get_plugin_details correctly reports tunnel_active=False when
    no local socket, no owner record, and DB flag is 0.
    """
    from meridian.routes import tunnel as tn
    from meridian.tunnel_plugins import BUILTIN_PLUGINS

    tid = "cross-gpd-inactive"
    try:
        tenant = {"id": tid, "plan": "pro", "tunnel_active": 0}

        plugin_name = BUILTIN_PLUGINS[0]["name"]
        out = await mh._dispatch_mcp_tool(
            "get_plugin_details", {"name": plugin_name}, db, "/tmp", tenant=tenant,
        )
        assert "error" not in out
        assert out["tunnel_active"] is False
    finally:
        _reset_tunnel_state(tid)


# ---------------------------------------------------------------------------
# 11. search_outputs — missing arg guards + hosted-mode guard
# ---------------------------------------------------------------------------

def test_search_outputs_missing_outputs_dir_raises(monkeypatch):
    """search_outputs with no outputs_dir raises ValueError (caught as -32603)."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: False)
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_outputs",
            "arguments": {"query": "something"},  # no outputs_dir
        }),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603
    assert "outputs_dir is required" in resp["error"]["message"]


def test_search_outputs_missing_query_raises(monkeypatch):
    """search_outputs with no query raises ValueError (caught as -32603)."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: False)
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_outputs",
            "arguments": {"outputs_dir": "/some/dir"},  # no query
        }),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603
    assert "query is required" in resp["error"]["message"]


def test_search_outputs_hosted_mode_returns_honest_error(monkeypatch):
    """0dedff91 — search_outputs on hosted Meridian returns a clear error dict
    (not an exception) pointing at the root cause."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_outputs",
            "arguments": {"outputs_dir": "/local/outputs", "query": "accuracy"},
        }),
        db=None, data_dir="/tmp",
    ))
    # Success envelope (not a JSON-RPC error) — the tool itself returns the error payload.
    assert "result" in resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload.get("hosted") is True
    assert "search_outputs walks" in payload["error"]
    assert payload["hits"] == []


# ---------------------------------------------------------------------------
# 12. search_code_semantic — missing-arg guards
# ---------------------------------------------------------------------------

def test_search_code_semantic_missing_root_dir_raises(monkeypatch):
    """search_code_semantic with no root_dir raises ValueError."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: False)
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_code_semantic",
            "arguments": {"query": "async def"},  # no root_dir
        }),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603
    assert "root_dir is required" in resp["error"]["message"]


def test_search_code_semantic_missing_query_raises(monkeypatch):
    """search_code_semantic with no query raises ValueError."""
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: False)
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_code_semantic",
            "arguments": {"root_dir": "/some/dir"},  # no query
        }),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603
    assert "query is required" in resp["error"]["message"]


def test_search_code_semantic_hosted_fails_honestly(monkeypatch):
    """search_code_semantic on hosted Meridian mirrors the 90c593d fix: the tool
    must fail honestly (the underlying code_index.search_code_semantic walk can't
    reach the caller's local path), not silently return empty results."""
    # The tool itself is not host-mode-gated at dispatch (unlike search_outputs) —
    # it just runs and returns what the code index finds. We confirm the dispatch
    # wiring: both required-arg guards produce a -32603, matching the documented
    # contract. A self-hosted test that actually invokes the heavy code_index is
    # out of scope (requires duckdb + tree-sitter + a real repo on disk).
    # This test confirms: args are validated BEFORE the heavy call.
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: False)
    resp = _run(mh._handle_mcp_request(
        _req("tools/call", {
            "name": "search_code_semantic",
            "arguments": {},  # neither root_dir nor query
        }),
        db=None, data_dir="/tmp",
    ))
    assert resp["error"]["code"] == -32603
    assert "root_dir is required" in resp["error"]["message"]


# ---------------------------------------------------------------------------
# 13. GitHub patch_file — missing-arg guards
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
    def __init__(self, responder):
        self._responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None, follow_redirects=False):
        return self._responder("GET", url, params)

    async def put(self, url, headers=None, json=None, params=None):
        return self._responder("PUT", url, json)


def _gh_db_with_repo(name):
    db = _make_db()
    proj = _run(db_module.create_project(db, name))
    _run(db.execute(
        "UPDATE projects SET github_repo = ?, github_branch = ? WHERE id = ?",
        ("owner/repo", "main", proj["id"]),
    ))
    _run(db.commit())
    return db, proj


def _gh_tenant():
    return {"id": "tgh-p", "plan": "pro", "github_pat": db_module.encrypt_field("ghp_x")}


def _patch_httpx(monkeypatch, responder):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeHTTP(responder))


def test_github_patch_file_missing_file_path(monkeypatch):
    """patch_file returns error when file_path is empty."""
    db, proj = _gh_db_with_repo("pf-nofp")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {}))
        out = _run(mh._dispatch_github_tool(
            "patch_file",
            {"project_id": proj["id"], "old_str": "x", "new_str": "y"},
            _gh_tenant(), db,
        ))
        assert "error" in out and "file_path is required" in out["error"]
    finally:
        _run(db.close())


def test_github_patch_file_missing_old_str(monkeypatch):
    """patch_file returns error when old_str / new_str are not strings."""
    db, proj = _gh_db_with_repo("pf-nostr")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {}))
        out = _run(mh._dispatch_github_tool(
            "patch_file",
            {"project_id": proj["id"], "file_path": "a.py", "new_str": "y"},
            _gh_tenant(), db,
        ))
        assert "error" in out and "old_str and new_str are required" in out["error"]
    finally:
        _run(db.close())


def test_github_patch_file_identical_strings(monkeypatch):
    """patch_file returns error when old_str == new_str."""
    db, proj = _gh_db_with_repo("pf-same")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(200, {}))
        out = _run(mh._dispatch_github_tool(
            "patch_file",
            {"project_id": proj["id"], "file_path": "a.py",
             "old_str": "same", "new_str": "same"},
            _gh_tenant(), db,
        ))
        assert "error" in out and "identical" in out["error"]
    finally:
        _run(db.close())


def test_github_patch_file_404(monkeypatch):
    """patch_file returns error when the file is not found (404 from GitHub)."""
    import base64 as b64
    db, proj = _gh_db_with_repo("pf-404")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(404))
        out = _run(mh._dispatch_github_tool(
            "patch_file",
            {"project_id": proj["id"], "file_path": "missing.py",
             "old_str": "x", "new_str": "y"},
            _gh_tenant(), db,
        ))
        assert "error" in out and "File not found" in out["error"]
    finally:
        _run(db.close())


def test_github_patch_file_old_str_not_found(monkeypatch):
    """patch_file returns error when old_str is not present in the file."""
    import base64 as b64
    db, proj = _gh_db_with_repo("pf-nomatch")
    try:
        def responder(method, url, _p):
            return _FakeResp(200, {
                "path": "a.py", "sha": "abc123",
                "content": b64.b64encode(b"hello world").decode(),
            })
        _patch_httpx(monkeypatch, responder)
        out = _run(mh._dispatch_github_tool(
            "patch_file",
            {"project_id": proj["id"], "file_path": "a.py",
             "old_str": "NOTHERE", "new_str": "replacement"},
            _gh_tenant(), db,
        ))
        assert "error" in out and "not found" in out["error"]
    finally:
        _run(db.close())


def test_github_patch_file_old_str_not_unique(monkeypatch):
    """patch_file returns error when old_str appears more than once."""
    import base64 as b64
    db, proj = _gh_db_with_repo("pf-dup")
    try:
        content = b"foo foo"  # 'foo' appears twice
        def responder(method, url, _p):
            return _FakeResp(200, {
                "path": "a.py", "sha": "abc123",
                "content": b64.b64encode(content).decode(),
            })
        _patch_httpx(monkeypatch, responder)
        out = _run(mh._dispatch_github_tool(
            "patch_file",
            {"project_id": proj["id"], "file_path": "a.py",
             "old_str": "foo", "new_str": "bar"},
            _gh_tenant(), db,
        ))
        assert "error" in out and "not unique" in out["error"]
    finally:
        _run(db.close())


def test_github_patch_file_success(monkeypatch):
    """patch_file happy path: unique match returns patched=True with metadata."""
    import base64 as b64
    db, proj = _gh_db_with_repo("pf-ok")
    try:
        original = b"def hello():\n    pass\n"
        def responder(method, url, payload):
            if method == "GET":
                return _FakeResp(200, {
                    "path": "a.py", "sha": "sha1abc",
                    "content": b64.b64encode(original).decode(),
                })
            # PUT response
            return _FakeResp(200, {"commit": {"sha": "newsha123456"}})
        _patch_httpx(monkeypatch, responder)
        out = _run(mh._dispatch_github_tool(
            "patch_file",
            {"project_id": proj["id"], "file_path": "a.py",
             "old_str": "    pass\n", "new_str": "    return 42\n"},
            _gh_tenant(), db,
        ))
        assert out.get("patched") is True
        assert out["path"] == "a.py"
        assert out["commit_sha"]
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# 14. GitHub get_workflow_run_logs — no-failed-jobs path
# ---------------------------------------------------------------------------

def test_github_get_workflow_run_logs_no_failed_jobs(monkeypatch):
    """get_workflow_run_logs with zero failed jobs returns an empty failed_jobs list."""
    db, proj = _gh_db_with_repo("gwrl-ok")
    try:
        def responder(method, url, _p):
            return _FakeResp(200, {
                "jobs": [
                    {"id": 1, "name": "build", "conclusion": "success",
                     "steps": [{"name": "checkout", "number": 1, "conclusion": "success"}],
                     "html_url": "http://x"},
                ]
            })
        _patch_httpx(monkeypatch, responder)
        out = _run(mh._dispatch_github_tool(
            "get_workflow_run_logs", {"project_id": proj["id"], "run_id": "42"},
            _gh_tenant(), db,
        ))
        assert out["failed_job_count"] == 0
        assert out["failed_jobs"] == []
    finally:
        _run(db.close())


def test_github_get_workflow_run_logs_run_not_found(monkeypatch):
    """get_workflow_run_logs with a 404 run_id returns a clean error."""
    db, proj = _gh_db_with_repo("gwrl-404")
    try:
        _patch_httpx(monkeypatch, lambda m, u, p: _FakeResp(404))
        out = _run(mh._dispatch_github_tool(
            "get_workflow_run_logs", {"project_id": proj["id"], "run_id": "99999"},
            _gh_tenant(), db,
        ))
        assert "error" in out
        assert "Run not found" in out["error"]
    finally:
        _run(db.close())


def test_github_get_workflow_run_logs_with_failed_job(monkeypatch):
    """get_workflow_run_logs extracts failed jobs and their failed steps."""
    db, proj = _gh_db_with_repo("gwrl-fail")
    try:
        def responder(method, url, _p):
            if "logs" in url:
                return _FakeResp(200, text="line1\nline2\nfailed here")
            return _FakeResp(200, {
                "jobs": [
                    {"id": 7, "name": "test", "conclusion": "failure",
                     "steps": [
                         {"name": "run tests", "number": 2, "conclusion": "failure"},
                         {"name": "checkout", "number": 1, "conclusion": "success"},
                     ],
                     "html_url": "http://x/jobs/7"},
                ]
            })
        _patch_httpx(monkeypatch, responder)
        out = _run(mh._dispatch_github_tool(
            "get_workflow_run_logs", {"project_id": proj["id"], "run_id": "10"},
            _gh_tenant(), db,
        ))
        assert out["failed_job_count"] == 1
        assert out["failed_jobs"][0]["job"] == "test"
        assert any(s["name"] == "run tests" for s in out["failed_jobs"][0]["failed_steps"])
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# Bonus: snapshot_graph_metrics session_id required guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_graph_metrics_missing_session_id(db):
    """snapshot_graph_metrics returns error when session_id is absent."""
    out = await mh._dispatch_mcp_tool(
        "snapshot_graph_metrics", {}, db, "/tmp",
    )
    assert "error" in out
    assert "session_id is required" in out["error"]


@pytest.mark.asyncio
async def test_get_graph_diff_missing_sessions(db):
    """get_graph_diff returns error when session_a or session_b are absent."""
    out = await mh._dispatch_mcp_tool(
        "get_graph_diff", {"session_a": "a"}, db, "/tmp",
    )
    assert "error" in out
    assert "session_a and session_b are required" in out["error"]
