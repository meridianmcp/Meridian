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


# ---------------------------------------------------------------------------
# d4886bd3 — the doc-structure write/index tools resolve a document via
# get_document(), which reads ONLY the doc_documents table — populated by
# reindex_document / put_document, NEVER by ingest_document (that stores flat
# note text in a SEPARATE system). Their descriptions must name the CORRECT
# prerequisite so a session isn't led into a dead end: reindex_document /
# put_document, and must NOT present ingest_document as the way to make the
# document resolvable.
# ---------------------------------------------------------------------------

def _full_tool_text(tool: dict) -> str:
    """Description + every inputSchema property description, lowercased."""
    parts = [tool.get("description", "")]
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    for p in props.values():
        if isinstance(p, dict) and p.get("description"):
            parts.append(p["description"])
    return " ".join(parts).lower()


def test_doc_store_tools_name_ingest_document_as_prerequisite():
    """832d67af — the doc-store tools resolve their target via
    get_document(doc_documents). Ground truth (handler.py: the post-ingest
    _persist structure hook calls store.put_document AFTER a successful
    ingest_document): ``ingest_document`` IS the registered MCP tool that
    populates doc_documents. ``reindex_document`` / ``put_document`` are NOT
    registered MCP tools (put_document is an internal DocStructureStore method),
    so descriptions must name ingest_document as the store path and must NOT
    point sessions at a non-existent reindex_document tool (finding b2d1c1a2 —
    an earlier fix, d4886bd3, wrongly enforced the phantom references)."""
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    registered = {t["name"] for t in _MCP_TOOLS_LIST}
    # reindex_document is not, and never was, a registered MCP tool.
    assert "reindex_document" not in registered
    doc_store_tools = (
        "index_equation",
        "insert_equation",
        "update_paragraph",
        "index_figure",
    )
    for name in doc_store_tools:
        assert name in by_name, name
        text = _full_tool_text(by_name[name])
        # Names the REAL store path (ingest_document, a registered tool).
        assert "ingest_document" in text, (name, "should name ingest_document as the store path")
        # Must NOT point at the phantom reindex_document tool.
        assert "reindex_document" not in text, (name, "references non-existent reindex_document tool")
