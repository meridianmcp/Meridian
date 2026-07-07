"""Discoverability smoke tests for MCP tool metadata (meridian/mcp_tools.py).

The dashboard docs tab (and any MCP client tool search) matches a query against
each tool's name + description. These tests guard that the project-lookup tools
surface for the natural queries a user/agent would type — the fix for
list_projects / get_project_by_name being hard to find.
"""
from __future__ import annotations

import re

from meridian.mcp_tools import _MCP_TOOLS_LIST


def _search_tools(query: str) -> list[str]:
    """Rank tools by query-word overlap against name + description (desc, score).

    Mirrors a simple name/description keyword search — the same signal the
    dashboard docs filter and MCP tool-search use. Returns tool names ordered
    best-match first, dropping zero-overlap tools.
    """
    q_words = set(re.findall(r"[a-z_]+", query.lower()))
    scored: list[tuple[int, str]] = []
    for tool in _MCP_TOOLS_LIST:
        text = f"{tool['name']} {tool.get('description', '')}".lower()
        words = set(re.findall(r"[a-z_]+", text))
        score = len(q_words & words)
        if score:
            scored.append((score, tool["name"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in scored]


def test_find_project_by_name_query_surfaces_get_project_by_name():
    results = _search_tools("find project by name")
    assert "get_project_by_name" in results[:3], results[:5]


def test_lookup_project_id_query_surfaces_lookup_tools():
    results = _search_tools("look up project id from name")
    # Both project-resolution tools should be near the top.
    assert "get_project_by_name" in results[:3], results[:5]
    assert "list_projects" in results[:5], results[:6]


def test_list_all_projects_query_surfaces_list_projects():
    results = _search_tools("list all projects")
    assert "list_projects" in results[:3], results[:5]


def test_project_lookup_descriptions_have_trigger_phrases():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    lp = by_name["list_projects"]["description"].lower()
    gp = by_name["get_project_by_name"]["description"].lower()
    # list_projects: discoverable + explains the name→id use case.
    assert "list all projects" in lp
    assert "project_id" in lp
    # get_project_by_name: find/look up/search/resolve trigger phrases.
    assert "find a project by name" in gp
    for verb in ("look up", "search", "resolve"):
        assert verb in gp, verb


def test_every_tool_has_name_description_and_annotations():
    """Contract guard: each tool exposes name, description, and MCP annotations."""
    for tool in _MCP_TOOLS_LIST:
        assert tool.get("name"), tool
        assert tool.get("description"), tool["name"]
        ann = tool.get("annotations")
        assert isinstance(ann, dict) and "readOnlyHint" in ann, tool["name"]


# ---------------------------------------------------------------------------
# 514d1fc2 — the touches_resources schema must TEACH the symbol-level format,
# so a session with no prior context can discover file:path.py:symbol_name and
# parallelize non-overlapping edits in the same file.
# ---------------------------------------------------------------------------

def _touches_resources_descriptions() -> dict[str, str]:
    out: dict[str, str] = {}
    for tool in _MCP_TOOLS_LIST:
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        tr = props.get("touches_resources")
        if isinstance(tr, dict) and tr.get("description"):
            out[tool["name"]] = tr["description"]
    return out


def test_touches_resources_schema_documents_symbol_level_format():
    descs = _touches_resources_descriptions()
    # add_sprint_item + update_sprint_item both expose a top-level touches_resources
    # (514d1fc2's named targets; fan_out's is nested under its items array).
    assert "add_sprint_item" in descs and "update_sprint_item" in descs, list(descs)
    for name in ("add_sprint_item", "update_sprint_item"):
        d = descs[name]
        # Each must TEACH the symbol-suffix format with a concrete example, so a
        # cold session can discover that same-file/different-symbol edits parallelize.
        assert "file:path.py:" in d, (name, d)
        assert "symbol" in d.lower(), (name, d)
        assert "same file" in d.lower(), (name, d)
