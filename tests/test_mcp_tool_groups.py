"""Tests for the tunnel bridge tool-group filter (feat/mcp-tool-groups).

Covers meridian/mcp/tool_groups.py (the connector -> group registry and the
per-tenant enable flags), the tenants.tunnel_tool_filter column, and the two
handler paths that enforce it: tools/list hides disabled tunnel tools and
tools/call refuses them. Native Meridian tools are never filtered.

The bridge is stubbed the same way tests/test_tunnel_bridge.py stubs it, so no
WebSocket or live tunnel is needed.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import db as db_module
from meridian.mcp import handler as mh
from meridian.mcp import tool_groups as tg
from meridian.routes import tunnel as tn


EXPECTED_GROUPS = {
    "chat-files", "code", "outputs", "docs", "latex", "debug",
    "office", "desktop", "zotero", "custom",
}


# ---------------------------------------------------------------------------
# Registry: every tunnel tool maps to exactly one group
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool, group", [
    ("filesystem__read_file", "chat-files"),
    ("codebase__search_graph", "code"),
    ("extractor__find_symbol", "code"),
    ("meridian-outputs__search_outputs", "outputs"),
    ("meridian-docs__document_outline", "docs"),
    ("meridian-latex__lint_tex", "latex"),
    ("mcp-debugger__set_breakpoint", "debug"),
    ("powerpoint__probe", "office"),
    ("word__add_paragraph", "office"),
    ("desktop-commander__read_file", "desktop"),
    ("zotero-mcp__probe", "zotero"),
    ("custom-p0__probe", "custom"),
    ("custom-p3__probe", "custom"),
])
def test_connector_prefix_maps_tool_to_group(tool, group):
    assert tg.group_of_tool(tool) == group


@pytest.mark.parametrize("name", [
    "log_task", "search_code", "read_file", "trace_path",  # native / GitHub bare names
    "unknown__thing",                                       # unknown connector prefix
    "",
])
def test_non_tunnel_names_have_no_group(name):
    assert tg.group_of_tool(name) is None


def test_every_tunnel_slot_is_grouped():
    # No tunnel tool may be ungrouped: each slot the bridge routes must have a
    # group, and its connector display name must resolve to that group.
    from meridian.routes.tunnel import SLOT_DISPLAY_NAMES, _TUNNEL_LABELS
    connectors = tg._connector_groups()
    for label in _TUNNEL_LABELS:
        assert label in tg._SLOT_GROUPS, f"slot {label!r} has no group"
        assert SLOT_DISPLAY_NAMES[label] in connectors, f"slot {label!r} not in registry"
    assert set(tg.GROUPS) == EXPECTED_GROUPS


def test_code_group_holds_codebase_memory_and_extractor_slots():
    # The 'code' umbrella: codebase-memory (slot 'code') and the extractor slot
    # (Serena, slot 'extract').
    assert tg._SLOT_GROUPS["code"] == "code"
    assert tg._SLOT_GROUPS["extract"] == "code"
    assert tg._SLOT_GROUPS["fs"] == "chat-files"


# ---------------------------------------------------------------------------
# parse_tool_filter: defaults, malformed input, and the enable-flag shape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   ", "not json", "[1, 2]", '"text"', 42])
def test_malformed_or_missing_setting_leaves_everything_enabled(raw):
    f = tg.parse_tool_filter(raw)
    assert f == tg.ToolFilter()
    assert f.is_enabled("mcp-debugger__set_breakpoint")


def test_non_boolean_flags_and_unknown_groups_are_ignored():
    raw = json.dumps({
        "groups": {"debug": "no", "code": 0, "bogus": False},
        "tools": {"codebase__search_graph": None},
    })
    f = tg.parse_tool_filter(raw)
    assert f.disabled_groups == frozenset()
    assert f.disabled_tools == frozenset()


def test_setting_accepts_json_text_or_decoded_dict():
    value = {"groups": {"debug": False}}
    assert tg.parse_tool_filter(json.dumps(value)) == tg.parse_tool_filter(value)
    assert tg.parse_tool_filter(value).disabled_groups == frozenset({"debug"})


# ---------------------------------------------------------------------------
# ToolFilter behaviour
# ---------------------------------------------------------------------------

_SAMPLE_TUNNEL_TOOLS = [
    {"name": "filesystem__read_file"},
    {"name": "codebase__search_graph"},
    {"name": "extractor__find_symbol"},
    {"name": "mcp-debugger__set_breakpoint"},
    {"name": "mcp-debugger__continue_execution"},
]


def _names(tools):
    return [t["name"] for t in tools]


def test_disabled_group_filters_its_tools_and_nothing_else():
    f = tg.parse_tool_filter({"groups": {"debug": False}})
    assert _names(f.filter_tools(_SAMPLE_TUNNEL_TOOLS)) == [
        "filesystem__read_file", "codebase__search_graph", "extractor__find_symbol",
    ]


def test_disabled_single_tool_filters_only_that_tool():
    f = tg.parse_tool_filter({"tools": {"codebase__search_graph": False}})
    assert _names(f.filter_tools(_SAMPLE_TUNNEL_TOOLS)) == [
        "filesystem__read_file", "extractor__find_symbol",
        "mcp-debugger__set_breakpoint", "mcp-debugger__continue_execution",
    ]


def test_group_off_wins_over_tool_on():
    f = tg.parse_tool_filter({
        "groups": {"code": False},
        "tools": {"codebase__search_graph": True},
    })
    assert not f.is_enabled("codebase__search_graph")
    assert not f.is_enabled("extractor__find_symbol")
    assert f.is_enabled("filesystem__read_file")


def test_native_tools_are_never_filtered_even_with_every_group_off():
    f = tg.parse_tool_filter({"groups": {g: False for g in tg.GROUPS}})
    native = [{"name": "log_task"}, {"name": "search_code"}, {"name": "read_file"}]
    assert _names(f.filter_tools(native)) == ["log_task", "search_code", "read_file"]
    assert f.disabled_reason("log_task") is None
    assert f.filter_tools(_SAMPLE_TUNNEL_TOOLS) == []


def test_disabled_reason_names_the_group_or_the_tool():
    f = tg.parse_tool_filter({
        "groups": {"debug": False},
        "tools": {"codebase__search_graph": False},
    })
    group_reason = f.disabled_reason("mcp-debugger__set_breakpoint")
    assert "debug" in group_reason and "set_breakpoint" in group_reason
    tool_reason = f.disabled_reason("codebase__search_graph")
    assert "codebase__search_graph" in tool_reason and "group" not in tool_reason
    assert f.disabled_reason("filesystem__read_file") is None
    assert f.disabled_reason("log_task") is None


# ---------------------------------------------------------------------------
# Handler integration: tools/list and tools/call (tunnel stubbed)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_bridge_state():
    tn._tunnel_tool_routes.clear()
    yield
    tn._tunnel_tool_routes.clear()


def _tenant_with_filter(value):
    tenant = {"id": "t1", "plan": "pro"}
    if value is not None:
        tenant["tunnel_tool_filter"] = value
    return tenant


def _list_tools(monkeypatch, tenant, tunnel_tools):
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def fake_list(tid, reserved):
        return list(tunnel_tools)

    monkeypatch.setattr(tn, "list_tunnel_tools", fake_list)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))


def test_tools_list_hides_tools_of_a_disabled_group(monkeypatch):
    tenant = _tenant_with_filter(json.dumps({"groups": {"debug": False}}))
    resp = _list_tools(monkeypatch, tenant, _SAMPLE_TUNNEL_TOOLS)
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "codebase__search_graph" in names
    assert "filesystem__read_file" in names
    assert "mcp-debugger__set_breakpoint" not in names
    assert "mcp-debugger__continue_execution" not in names
    assert "log_task" in names  # native tools untouched
    # The fetch succeeded, so a disabled group is not reported as a degraded tunnel.
    assert "meridian/tunnelHealth" not in (resp["result"].get("_meta") or {})


def test_tools_list_hides_a_single_disabled_tool(monkeypatch):
    tenant = _tenant_with_filter({"tools": {"codebase__search_graph": False}})
    resp = _list_tools(monkeypatch, tenant, _SAMPLE_TUNNEL_TOOLS)
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "codebase__search_graph" not in names
    assert "extractor__find_symbol" in names
    assert "mcp-debugger__set_breakpoint" in names


def test_tools_list_unchanged_when_no_filter_is_stored(monkeypatch):
    resp = _list_tools(monkeypatch, _tenant_with_filter(None), _SAMPLE_TUNNEL_TOOLS)
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {t["name"] for t in _SAMPLE_TUNNEL_TOOLS} <= names


def _call_tool(monkeypatch, tenant, name):
    routed = {"hits": []}
    monkeypatch.setattr(tn, "has_active_tunnel", lambda tid: True)

    async def fake_call(tid, tool_name, args, **kwargs):
        routed["hits"].append(tool_name)
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(tn, "call_tunnel_tool", fake_call)
    monkeypatch.setattr(tn, "call_tunnel_tool_with_release_tracking", fake_call)
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": {}}}
    resp = asyncio.run(mh._handle_mcp_request(body, db=None, data_dir="/tmp", tenant=tenant))
    return resp, routed["hits"]


def test_tools_call_refuses_a_tool_in_a_disabled_group(monkeypatch):
    tenant = _tenant_with_filter(json.dumps({"groups": {"debug": False}}))
    resp, routed = _call_tool(monkeypatch, tenant, "mcp-debugger__set_breakpoint")
    assert routed == []  # never forwarded to the tunnel
    assert resp["error"]["code"] == -32603
    assert "mcp-debugger__set_breakpoint" in resp["error"]["message"]
    assert "disabled" in resp["error"]["message"]


def test_tools_call_refuses_a_single_disabled_tool(monkeypatch):
    tenant = _tenant_with_filter(json.dumps({"tools": {"codebase__search_graph": False}}))
    resp, routed = _call_tool(monkeypatch, tenant, "codebase__search_graph")
    assert routed == []
    assert resp["error"]["code"] == -32603
    assert "codebase__search_graph" in resp["error"]["message"]


def test_tools_call_forwards_an_enabled_tunnel_tool(monkeypatch):
    tenant = _tenant_with_filter(json.dumps({"groups": {"debug": False}}))
    resp, routed = _call_tool(monkeypatch, tenant, "codebase__search_graph")
    assert routed == ["codebase__search_graph"]
    assert resp["result"]["content"][0]["text"] == "ok"


def test_tools_call_never_refuses_a_native_tool(monkeypatch):
    tenant = _tenant_with_filter(json.dumps({"groups": {g: False for g in tg.GROUPS}}))
    resp, routed = _call_tool(monkeypatch, tenant, "log_task")
    assert routed == []
    # log_task dispatches natively (db=None), so it cannot be the tunnel-filter refusal.
    assert "tunnel tool settings" not in json.dumps(resp)


# ---------------------------------------------------------------------------
# Storage: tenants.tunnel_tool_filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tunnel_tool_filter_column_round_trips(db):
    tenant = await db_module.upsert_tenant(db, "tool-filter-roundtrip@example.com")
    row = await db_module.get_tenant_by_id(db, tenant["id"])
    assert row["tunnel_tool_filter"] is None  # NULL = every tunnel tool enabled
    value = json.dumps({"groups": {"debug": False}, "tools": {"codebase__search_graph": False}})
    await db_module.update_tenant(db, tenant["id"], tunnel_tool_filter=value)
    row = await db_module.get_tenant_by_id(db, tenant["id"])
    assert row["tunnel_tool_filter"] == value
    assert tg.parse_tool_filter(row["tunnel_tool_filter"]).disabled_groups == frozenset({"debug"})


@pytest.mark.asyncio
async def test_tunnel_tool_filter_migration_is_idempotent(db):
    await db_module._migrate_tunnel_tool_filter(db)
    await db_module._migrate_tunnel_tool_filter(db)
    assert await db_module._column_exists(db, "tenants", "tunnel_tool_filter")
