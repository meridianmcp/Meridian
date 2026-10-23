"""Regression tests for issues #3 and #8.

#8: fan_out_sprint_items silently dropped required_tool and tool_requirements, the exact failure the issue reports.
    Both the legacy insert loop and the strict batch mapper must now carry them, and a malformed tool_requirements
    value must raise before anything is written.
#3: the stdio server hand-declared list_projects and get_project_by_name, so its descriptions drifted from the
    HTTP transport. Both must come from the shared registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian import db as db_module
from meridian.db import sprint_items
from meridian.tool_requirements import ToolRequirementError

REPO_ROOT = Path(__file__).resolve().parents[1]

_TOOL_REQ = {
    "name": "find_symbol",
    "server_or_namespace": "Serena",
    "required_or_preferred": "required",
    "purpose": "locate target",
}


@pytest.mark.asyncio
async def test_legacy_fan_out_keeps_the_tool_pins(db):
    p = await db_module.create_project(db, "alpha")
    ids = await db_module.fan_out_sprint_items(db, p["id"], [
        {"title": "pinned", "required_tool": "Serena: find_symbol", "tool_requirements": [_TOOL_REQ]},
        {"title": "plain"},
    ])

    pinned = await db_module.get_sprint_item(db, ids[0])
    plain = await db_module.get_sprint_item(db, ids[1])

    assert pinned["required_tool"] == "Serena: find_symbol"
    assert pinned["tool_requirements"] is not None
    assert "find_symbol" in pinned["tool_requirements"]
    assert plain["required_tool"] is None
    assert plain["tool_requirements"] is None


@pytest.mark.asyncio
async def test_legacy_fan_out_rejects_a_malformed_tool_requirement_before_writing_anything(db):
    p = await db_module.create_project(db, "beta")
    bad = {"title": "bad", "tool_requirements": [{"name": ""}]}

    with pytest.raises(ToolRequirementError):
        await db_module.fan_out_sprint_items(db, p["id"], [{"title": "ok"}, bad])

    async with db.execute(
        "SELECT COUNT(*) AS c FROM sprint_items WHERE project_id = ?", (p["id"],)
    ) as cur:
        row = await cur.fetchone()
    # Postgres rows come back as dicts and SQLite rows as tuples, so read the value either way.
    assert (row["c"] if isinstance(row, dict) else row[0]) == 0


def test_strict_batch_mapper_passes_the_tool_pins_through():
    entry = sprint_items._fan_out_spec_to_batch_entry(
        {"title": "pinned", "required_tool": "Serena: find_symbol", "tool_requirements": [_TOOL_REQ]}, 0,
    )
    assert entry["required_tool"] == "Serena: find_symbol"
    assert entry["tool_requirements"] == [_TOOL_REQ]


def test_strict_batch_mapper_omits_empty_pins():
    entry = sprint_items._fan_out_spec_to_batch_entry({"title": "plain", "required_tool": ""}, 0)
    assert "required_tool" not in entry
    assert "tool_requirements" not in entry


def test_stdio_server_uses_the_shared_definitions_for_the_two_project_tools():
    source = (REPO_ROOT / "meridian" / "mcp" / "stdio_handler.py").read_text(encoding="utf-8")
    assert '_shared_tool("list_projects")' in source
    assert '_shared_tool("get_project_by_name")' in source
    assert 'name="list_projects"' not in source
    assert 'name="get_project_by_name"' not in source
