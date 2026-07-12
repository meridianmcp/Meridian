"""Tests for the on-demand tool-manifest discovery tool (142808f3)."""
import pytest

from meridian.tool_manifest import build_tool_manifest, _first_sentence
from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _TITLE_OVERRIDES
from meridian import server as srv


def test_first_sentence_truncates_and_cleans():
    assert _first_sentence("Do a thing. Then another.") == "Do a thing."
    assert _first_sentence("  collapse   whitespace  ") == "collapse whitespace"
    assert _first_sentence("") == ""
    long = "x" * 500
    assert len(_first_sentence(long)) <= 160


def test_build_tool_manifest_shape_and_dedup():
    dupe = [
        {"name": "a", "description": "First. Second."},
        {"name": "a", "description": "dup ignored"},
        {"name": "b", "description": "Bee tool."},
        {"description": "no name — skipped"},
    ]
    m = build_tool_manifest(dupe)
    assert m["count"] == 2
    names = [t["name"] for t in m["tools"]]
    assert names == ["a", "b"]  # declared order, deduped
    assert m["tools"][0]["summary"] == "First."
    assert "note" in m and m["note"]


def test_manifest_covers_full_builtin_toolset():
    m = build_tool_manifest(_MCP_TOOLS_LIST)
    unique_names = {t["name"] for t in _MCP_TOOLS_LIST if t.get("name")}
    assert m["count"] == len(unique_names)
    assert "refresh_tool_manifest" in {t["name"] for t in m["tools"]}


def test_registered_read_only_and_titled():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "refresh_tool_manifest" in by_name
    assert by_name["refresh_tool_manifest"]["inputSchema"]["required"] == []
    assert "refresh_tool_manifest" in _READ_ONLY_TOOLS
    assert _TITLE_OVERRIDES["refresh_tool_manifest"] == "Refresh Tool Manifest"


@pytest.mark.asyncio
async def test_dispatch_refresh_tool_manifest(db):
    """The tool dispatches without a tenant (self-host) and returns the manifest."""
    result = await srv._dispatch_mcp_tool(
        "refresh_tool_manifest", {}, db, "/tmp"
    )
    assert result["count"] == len({t["name"] for t in _MCP_TOOLS_LIST if t.get("name")})
    assert any(t["name"] == "get_sprint_items" for t in result["tools"])
    # No tenant => no list_changed re-fire attempted.
    assert "list_changed_refired" not in result
