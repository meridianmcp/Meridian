"""Tests for 56e9b3c7 — MCP exposure of the bulk stale-claim reconciliation
sweep.

``meridian.db.sprint_items.reconcile_stale_claims`` and the matching
``handle_reconcile_stale_claims`` handler in
``meridian/mcp/handlers/sprint_tools.py`` were both already fully
implemented and covered at the DB layer (see
tests/test_claim_concurrency.py and
tests/test_268d4e9b_coordination_matrix.py for dry-run, live-run,
two-project isolation, bounded-batch truncation, version scope,
active/ambiguous exclusion, full recovery, and concurrent-sweep race
safety) — but the handler was never actually wired into any dispatch
surface: not ``meridian/mcp/handler.py``'s sprint-tools dispatch table,
not ``meridian/mcp_tools.py``'s ``_MCP_TOOLS_LIST`` schema, not
``meridian/mcp/stdio_handler.py``'s stdio advertisement/dispatch. This
file only covers the new MCP surface (schema registration, HTTP/MCP
dispatch, stdio transport parity) — never the underlying reconciliation
logic, which is already exhaustively tested elsewhere.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
import meridian.server as srv
from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS

TOOL_NAME = "reconcile_stale_claims"


def _tool() -> dict:
    return next(t for t in _MCP_TOOLS_LIST if t["name"] == TOOL_NAME)


# ---------------------------------------------------------------------------
# 1. Canonical schema (_MCP_TOOLS_LIST) checks.
# ---------------------------------------------------------------------------


def test_tool_present_exactly_once():
    names = [t["name"] for t in _MCP_TOOLS_LIST if t["name"] == TOOL_NAME]
    assert names == [TOOL_NAME]


def test_schema_advertises_project_id_and_project_name_alternative():
    """Generic project-scoping contract (mirrors
    test_every_project_id_tool_schema_advertises_project_name in
    test_core.py): project_id present, project_name sibling present with the
    exact 'alternative to project_id' phrasing, project_id required."""
    tool = _tool()
    props = tool["inputSchema"]["properties"]
    assert "project_id" in props
    assert props["project_id"]["type"] == "string"
    assert "project_name" in props
    assert props["project_name"]["type"] == "string"
    assert "alternative to project_id" in props["project_name"]["description"]
    # project_id is never listed as strictly required (mirrors every sibling
    # project-scoped tool) — project_name is an accepted runtime alternative;
    # the resolver + handler enforce a real project at call time instead.
    assert "project_id" not in (tool["inputSchema"].get("required") or [])


def test_tool_defaults_to_dry_run_in_its_own_description():
    """The whole point of this sweep is that it's safe to run unattended
    against a live production board — the schema text must say so, not just
    the DB docstring."""
    tool = _tool()
    assert "dry_run" in tool["description"]
    assert "default" in tool["description"].lower()


def test_tool_is_not_tagged_read_only():
    """Unlike reconcile_sprint_drift (pure read), this tool performs real
    writes (release locks, reset status, write an audit row) whenever the
    caller explicitly passes dry_run=False — it must never be tagged
    read-only/idempotent just because dry_run defaults to True."""
    assert TOOL_NAME not in _READ_ONLY_TOOLS
    tool = _tool()
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["annotations"]["idempotentHint"] is False


def test_tool_category_and_role_relevance():
    tool = _tool()
    assert tool["category"] == "sprint-management"
    assert tool["role_relevance"] == "executor"


# ---------------------------------------------------------------------------
# 2. HTTP/MCP dispatch — _dispatch_mcp_tool routes to the real DB helper via
#    handler.py's _handle_sprint_tools / _standard_dispatch table.
# ---------------------------------------------------------------------------


async def test_reconcile_stale_claims_mcp_dispatch_dry_run(db):
    p = await db_module.create_project(db, "56e9b3c7-mcp-dispatch-dry-run")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "dry run via mcp")
    owner = await db_module.register_session(db, p["id"], "mcp-dry-run-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    result = await srv._dispatch_mcp_tool(
        "reconcile_stale_claims", {"project_id": p["id"]}, db, "/tmp",
    )

    assert result["dry_run"] is True
    assert [v["item_id"] for v in result["stale"]] == [item["id"]]
    assert result["reset"] == []

    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "in_progress"


async def test_reconcile_stale_claims_mcp_dispatch_live_run_resets(db):
    p = await db_module.create_project(db, "56e9b3c7-mcp-dispatch-live-run")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "live run via mcp",
        touches_resources=["file:mcp_reconcile_me.py"], prospect_bypass=True,
    )
    owner = await db_module.register_session(db, p["id"], "mcp-live-run-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db_module.claim_file(db, "mcp_reconcile_me.py", owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    result = await srv._dispatch_mcp_tool(
        "reconcile_stale_claims",
        {"project_id": p["id"], "dry_run": False, "actor": "mcp-sweeper"},
        db, "/tmp",
    )

    assert len(result["reset"]) == 1
    assert result["reset"][0]["item_id"] == item["id"]

    reset_item = await db_module.get_sprint_item(db, item["id"])
    assert reset_item["status"] == "pending"
    assert (await db_module.get_file_claims(db, "mcp_reconcile_me.py"))["file_lock"] is None


async def test_reconcile_stale_claims_mcp_dispatch_resolves_project_name(db):
    p = await db_module.create_project(db, "56e9b3c7-mcp-dispatch-by-name")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "resolved by name")
    owner = await db_module.register_session(db, p["id"], "mcp-byname-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    result = await srv._dispatch_mcp_tool(
        "reconcile_stale_claims", {"project_name": "56e9b3c7-mcp-dispatch-by-name"}, db, "/tmp",
    )

    assert [v["item_id"] for v in result["stale"]] == [item["id"]]


async def test_reconcile_stale_claims_mcp_dispatch_item_ids_scope(db):
    """item_ids narrows the sweep to an explicit allow-list — verifies the
    MCP layer forwards this argument through untouched."""
    p = await db_module.create_project(db, "56e9b3c7-mcp-dispatch-item-ids")
    keep = await db_module.add_sprint_item(db, p["id"], "v1", "scope me in")
    skip = await db_module.add_sprint_item(db, p["id"], "v1", "scope me out", force=True)
    owner_a = await db_module.register_session(db, p["id"], "mcp-scope-a")
    owner_b = await db_module.register_session(db, p["id"], "mcp-scope-b")
    await db_module.claim_sprint_item(db, p["id"], keep["id"], actor=owner_a["id"])
    await db_module.claim_sprint_item(db, p["id"], skip["id"], actor=owner_b["id"])
    await db.execute(
        "UPDATE sessions SET status = 'closed' WHERE id IN (?, ?)",
        (owner_a["id"], owner_b["id"]),
    )
    await db.commit()

    result = await srv._dispatch_mcp_tool(
        "reconcile_stale_claims",
        {"project_id": p["id"], "item_ids": [keep["id"]]},
        db, "/tmp",
    )

    assert [v["item_id"] for v in result["stale"]] == [keep["id"]]


# ---------------------------------------------------------------------------
# 3. stdio transport parity — schema served identically to the canonical one,
#    and calling it via the stdio call_tool() closure dispatches to the same
#    real data (not just schema-visible).
# ---------------------------------------------------------------------------


def _build_stdio_server(monkeypatch, db):
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


async def test_stdio_schema_matches_canonical_schema(db, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    tools = await _stdio_list_tools(server)
    stdio_tool = next(t for t in tools if t.name == TOOL_NAME)

    canonical = _tool()
    assert stdio_tool.inputSchema == canonical["inputSchema"]
    assert stdio_tool.description == canonical["description"]


async def test_stdio_call_reconcile_stale_claims_dispatches_real_data(db, monkeypatch):
    server = _build_stdio_server(monkeypatch, db)
    project = await db_module.create_project(db, "56e9b3c7-stdio-dispatch")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "stale via stdio")
    owner = await db_module.register_session(db, project["id"], "stdio-owner")
    await db_module.claim_sprint_item(db, project["id"], item["id"], actor=owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    result = await _stdio_call(
        server, TOOL_NAME, {"project_id": project["id"], "dry_run": True},
    )

    assert result["dry_run"] is True
    assert [v["item_id"] for v in result["stale"]] == [item["id"]]
