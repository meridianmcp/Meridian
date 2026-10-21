"""Contract tests for the lean MCP tool surface (meridian/tool_catalog.py).

The three meta-tools must fail closed: a tool outside the active profile is
reported as unavailable, never dispatched, and never enumerable.
"""
from __future__ import annotations

import json

import pytest

from meridian.mcp_profiles import get_tool_allowlist
from meridian.mcp_tools import _MCP_TOOLS_LIST
from meridian.tool_catalog import (
    CATEGORIES,
    DESCRIBE_TOOLS,
    EXECUTE_TOOL,
    LEAN_CORE_TOOL_NAMES,
    LEAN_PROFILE,
    LEAN_PROFILE_TOOL_NAMES,
    META_TOOL_NAMES,
    ROLE_PROFILE_TOOL_NAMES,
    SEARCH_TOOLS,
    ToolCatalogError,
    ToolUnavailableError,
    build_catalog,
    category_index,
    describe_tools,
    execute_tool,
    search_tools,
)

NATIVE_NAMES = frozenset(tool["name"] for tool in _MCP_TOOLS_LIST)
CATALOG = build_catalog(_MCP_TOOLS_LIST)
ALL_ALLOWED = frozenset(CATALOG)

SYNTHETIC_CATALOG = build_catalog(
    [
        {
            "name": "alpha_write",
            "description": "Write a record. Needs x.",
            "inputSchema": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        },
        {"name": "beta_admin", "description": "Admin only.", "inputSchema": {"type": "object"}},
    ]
)


def _recorder():
    calls: list[tuple[str, dict]] = []

    def dispatch(name, arguments):
        calls.append((name, arguments))
        return {"ok": name}

    return calls, dispatch


def test_lean_core_tools_are_native_and_profile_is_core_plus_meta() -> None:
    assert LEAN_CORE_TOOL_NAMES <= NATIVE_NAMES
    assert LEAN_PROFILE_TOOL_NAMES == LEAN_CORE_TOOL_NAMES | META_TOOL_NAMES
    assert len(LEAN_PROFILE_TOOL_NAMES) == 8


def test_role_profiles_keep_lean_core_and_only_native_tools() -> None:
    assert set(ROLE_PROFILE_TOOL_NAMES) == {"executor", "planner", "verifier"}
    for role, names in ROLE_PROFILE_TOOL_NAMES.items():
        assert LEAN_CORE_TOOL_NAMES <= names, role
        assert META_TOOL_NAMES <= names, role
        assert (names - META_TOOL_NAMES) <= NATIVE_NAMES, role
        assert len(names) < 40, role


def test_get_tool_allowlist_resolves_lean_and_role_profiles_and_still_fails_closed() -> None:
    assert get_tool_allowlist(LEAN_PROFILE) == LEAN_PROFILE_TOOL_NAMES
    for role, names in ROLE_PROFILE_TOOL_NAMES.items():
        assert get_tool_allowlist(role) == names
    assert get_tool_allowlist(None) is None
    with pytest.raises(ValueError, match="unknown MCP tool profile"):
        get_tool_allowlist("")
    with pytest.raises(ValueError, match="unknown MCP tool profile"):
        get_tool_allowlist("full")


def test_catalog_covers_every_native_tool_with_known_categories_and_tiers() -> None:
    assert NATIVE_NAMES <= set(CATALOG)
    assert META_TOOL_NAMES <= set(CATALOG)
    for entry in CATALOG.values():
        assert entry.category in CATEGORIES
        assert entry.tier in {"core", "deferred"}
        assert entry.input_schema
    core = {name for name, entry in CATALOG.items() if entry.tier == "core"}
    assert core == LEAN_PROFILE_TOOL_NAMES


def test_catalog_is_deterministic_regardless_of_input_order() -> None:
    again = build_catalog(list(reversed(_MCP_TOOLS_LIST)))
    assert {n: (e.category, e.summary, e.tier) for n, e in CATALOG.items()} == {
        n: (e.category, e.summary, e.tier) for n, e in again.items()
    }


def test_lean_core_tools_land_in_expected_categories() -> None:
    expected = {
        "start_session": "session",
        "claim_sprint_item": "sprint",
        "complete_sprint_item": "sprint",
        "log_task": "tasks",
        "generate_handoff": "session",
    }
    for name, category in expected.items():
        assert CATALOG[name].category == category, name


def test_build_catalog_skips_malformed_entries_and_cannot_shadow_meta_tools() -> None:
    catalog = build_catalog(
        [
            {"name": "search_tools", "description": "Shadow.", "inputSchema": {}},
            {"name": "bad name!", "description": "x"},
            {"name": 7},
            "not-a-dict",
            {"name": "ok_tool", "description": "Fine."},
        ]
    )
    assert catalog["search_tools"].summary != "Shadow."
    assert "bad name!" not in catalog
    assert "ok_tool" in catalog
    assert len(catalog) == len(META_TOOL_NAMES) + 1


def test_bm25_ranks_the_obvious_tool_first() -> None:
    top = search_tools(CATALOG, ALL_ALLOWED, query="claim sprint item", limit=3)
    assert top["results"][0]["name"] == "claim_sprint_item"
    handoff = search_tools(CATALOG, ALL_ALLOWED, query="handoff", limit=10)
    assert "generate_handoff" in {r["name"] for r in handoff["results"]}


def test_search_is_fail_closed_under_lean_profile() -> None:
    result = search_tools(CATALOG, LEAN_PROFILE_TOOL_NAMES, query="create project", limit=50)
    names = {r["name"] for r in result["results"]}
    assert names <= LEAN_PROFILE_TOOL_NAMES
    assert "create_project" not in names
    listing = search_tools(CATALOG, LEAN_PROFILE_TOOL_NAMES, limit=50)
    assert listing["total_matches"] == len(LEAN_PROFILE_TOOL_NAMES)
    assert {r["name"] for r in listing["results"]} == LEAN_PROFILE_TOOL_NAMES


def test_search_detail_levels_and_category_filter() -> None:
    names_only = search_tools(CATALOG, ALL_ALLOWED, category="sprint", limit=50)
    assert names_only["results"]
    assert all(set(r) == {"name"} for r in names_only["results"])
    summary = search_tools(CATALOG, ALL_ALLOWED, category="session", detail="summary", limit=50)
    assert summary["results"]
    assert all("summary" in r and "inputSchema" not in r for r in summary["results"])
    full = search_tools(CATALOG, LEAN_PROFILE_TOOL_NAMES, query="log task", detail="full", limit=1)
    assert isinstance(full["results"][0]["inputSchema"], dict)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"detail": "all"},
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"limit": "5"},
        {"query": "x" * 201},
        {"query": 5},
        {"category": "nope"},
    ],
)
def test_search_rejects_invalid_arguments(kwargs) -> None:
    with pytest.raises(ToolCatalogError):
        search_tools(CATALOG, LEAN_PROFILE_TOOL_NAMES, **kwargs)


@pytest.mark.parametrize("allowed", [None, ["start_session"], "start_session", {1}])
def test_none_or_loose_allowlists_are_rejected_not_read_as_all_tools(allowed) -> None:
    with pytest.raises(ToolCatalogError):
        search_tools(CATALOG, allowed)
    with pytest.raises(ToolCatalogError):
        describe_tools(CATALOG, allowed, ["start_session"])
    with pytest.raises(ToolCatalogError):
        execute_tool(CATALOG, allowed, "start_session", {}, lambda n, a: None)


def test_empty_allowlist_exposes_nothing() -> None:
    empty: frozenset[str] = frozenset()
    assert search_tools(CATALOG, empty)["total_matches"] == 0
    assert describe_tools(CATALOG, empty, ["start_session"])["unavailable"] == ["start_session"]
    with pytest.raises(ToolUnavailableError):
        execute_tool(CATALOG, empty, "start_session", {}, lambda n, a: None)


def test_describe_reports_hidden_and_missing_tools_the_same_way() -> None:
    result = describe_tools(
        CATALOG, LEAN_PROFILE_TOOL_NAMES, ["start_session", "create_project", "no_such_tool"]
    )
    assert [t["name"] for t in result["tools"]] == ["start_session"]
    assert "inputSchema" in result["tools"][0]
    assert result["unavailable"] == ["create_project", "no_such_tool"]


@pytest.mark.parametrize("names", [[], ["a"] * 26, [1], "start_session"])
def test_describe_rejects_bad_name_lists(names) -> None:
    with pytest.raises(ToolCatalogError):
        describe_tools(CATALOG, LEAN_PROFILE_TOOL_NAMES, names)


def test_execute_dispatches_only_exposed_tools_with_required_arguments() -> None:
    calls, dispatch = _recorder()
    allowed = frozenset({"alpha_write"})
    assert execute_tool(SYNTHETIC_CATALOG, allowed, "alpha_write", {"x": "1"}, dispatch) == {
        "ok": "alpha_write"
    }
    assert calls == [("alpha_write", {"x": "1"})]


def test_execute_refuses_hidden_unknown_and_meta_names_without_dispatch() -> None:
    calls, dispatch = _recorder()
    # Meta-tool names are allowed here on purpose: execute_tool must still refuse them.
    allowed = frozenset({"alpha_write", EXECUTE_TOOL, SEARCH_TOOLS})
    messages: set[str] = set()
    probes = ["beta_admin", "no_such_tool", EXECUTE_TOOL, SEARCH_TOOLS, DESCRIBE_TOOLS, 42, None, ["alpha_write"]]
    for name in probes:
        with pytest.raises(ToolUnavailableError) as exc:
            execute_tool(SYNTHETIC_CATALOG, allowed, name, {"x": "1"}, dispatch)
        messages.add(str(exc.value))
    assert len(messages) == 1
    assert calls == []


def test_execute_checks_required_arguments_before_dispatch() -> None:
    calls, dispatch = _recorder()
    allowed = frozenset({"alpha_write"})
    with pytest.raises(ToolCatalogError, match="missing required argument"):
        execute_tool(SYNTHETIC_CATALOG, allowed, "alpha_write", {}, dispatch)
    with pytest.raises(ToolCatalogError, match="arguments must be an object"):
        execute_tool(SYNTHETIC_CATALOG, allowed, "alpha_write", ["x"], dispatch)
    with pytest.raises(ToolCatalogError, match="missing required argument"):
        execute_tool(SYNTHETIC_CATALOG, allowed, "alpha_write", None, dispatch)
    assert calls == []


def test_execute_propagates_dispatch_errors_unchanged() -> None:
    def boom(name, arguments):
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError, match="handler failed"):
        execute_tool(SYNTHETIC_CATALOG, frozenset({"alpha_write"}), "alpha_write", {"x": "1"}, boom)


def test_execute_rejects_non_callable_dispatch() -> None:
    with pytest.raises(ToolCatalogError):
        execute_tool(SYNTHETIC_CATALOG, frozenset({"alpha_write"}), "alpha_write", {"x": "1"}, None)


def test_category_index_counts_only_exposed_tools() -> None:
    index = category_index(CATALOG, LEAN_PROFILE_TOOL_NAMES)
    assert sum(index.values()) == len(LEAN_PROFILE_TOOL_NAMES)
    assert set(index) <= set(CATEGORIES)
    assert index["meta"] == len(META_TOOL_NAMES)
    assert category_index(CATALOG, frozenset()) == {}


def test_lean_tool_list_is_a_small_fraction_of_the_native_list() -> None:
    native_bytes = len(json.dumps(_MCP_TOOLS_LIST, separators=(",", ":")))
    lean_native = [tool for tool in _MCP_TOOLS_LIST if tool["name"] in LEAN_CORE_TOOL_NAMES]
    lean_bytes = len(json.dumps(lean_native, separators=(",", ":")))
    assert lean_bytes * 5 < native_bytes
