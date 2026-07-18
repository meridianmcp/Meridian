"""Tests for 4d1fb28f — sprint-item-level MCP tool/plugin pinning.

Refiled from a stuck original attempt (3a92ad62), tied to GitHub issue #8/#13:
a planner can declare which specific tool an executor MUST use for a sprint
item (``required_tool``) instead of leaving tool choice to executor habit.
Unlike the opt-in, soft model-tier hint (81396666), this is rendered as
UNCONDITIONAL hard guidance in the /goal block.

Covers:
  * DB layer — add_sprint_item / patch_sprint_item persist + set/clear
    required_tool via the _UNSET sentinel (mirrors test_sprint_item_sprint_name).
  * handoff._build_required_tool_clause — pure-function unit tests.
  * handoff._build_quick_start_goal — the clause is rendered unconditionally
    (no enabled flag) whenever any pending item carries a pin.
  * handoff.build_item_briefing — renders a dedicated <required_tool> tag.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_sprint_item_required_tool_persists(db):
    p = await db_module.create_project(db, "required-tool-test")

    item = await db_module.add_sprint_item(
        db, p["id"], "v0.1", "Rename symbol across module",
        required_tool="Serena: replace_symbol_body",
    )
    assert item["required_tool"] == "Serena: replace_symbol_body"

    # Omitting it entirely defaults to NULL.
    item_no_pin = await db_module.add_sprint_item(
        db, p["id"], "v0.1", "Unrelated task", force=True,
    )
    assert item_no_pin.get("required_tool") is None


@pytest.mark.asyncio
async def test_patch_sprint_item_required_tool_set_and_clear(db):
    p = await db_module.create_project(db, "required-tool-patch-test")
    item = await db_module.add_sprint_item(
        db, p["id"], "v0.1", "Refactor DB layer",
    )
    assert item.get("required_tool") is None

    # Set it.
    patched = await db_module.patch_sprint_item(
        db, p["id"], item["id"], required_tool="meridian__patch_file",
    )
    assert patched["required_tool"] == "meridian__patch_file"

    # Omitting the arg (UNSET sentinel) leaves it untouched.
    untouched = await db_module.patch_sprint_item(
        db, p["id"], item["id"], notes="unrelated edit",
    )
    assert untouched["required_tool"] == "meridian__patch_file"

    # Empty string clears it.
    cleared = await db_module.patch_sprint_item(
        db, p["id"], item["id"], required_tool="",
    )
    assert cleared["required_tool"] is None

    # get_sprint_items surfaces required_tool too (SELECT *).
    await db_module.patch_sprint_item(
        db, p["id"], item["id"], required_tool="Serena: find_symbol",
    )
    items = await db_module.get_sprint_items(db, p["id"])
    assert any(it["id"] == item["id"] and it["required_tool"] == "Serena: find_symbol" for it in items)


@pytest.mark.asyncio
async def test_claim_sprint_item_surfaces_required_tool(db):
    """4d1fb28f — required_tool is advisory (not claim-gated) but must flow
    through on the claimed item dict so a direct-claim caller still sees it."""
    p = await db_module.create_project(db, "required-tool-claim-test")
    item = await db_module.add_sprint_item(
        db, p["id"], "v0.1", "Edit shared config",
        required_tool="Filesystem: edit_file",
    )
    claimed = await db_module.claim_sprint_item(db, p["id"], item["id"])
    assert claimed is not None
    assert claimed.get("blocked") is not True
    assert claimed["required_tool"] == "Filesystem: edit_file"


# ---------------------------------------------------------------------------
# handoff._build_required_tool_clause (pure function)
# ---------------------------------------------------------------------------


def test_build_required_tool_clause_empty_when_no_pins():
    items = [{"id": "a1", "title": "no pin here"}]
    assert handoff_module._build_required_tool_clause(items) == ""


def test_build_required_tool_clause_skips_items_without_id_or_pin():
    items = [
        {"id": "item1", "required_tool": "Serena: find_symbol"},
        {"id": "item2"},  # no required_tool
        {"required_tool": "Serena: find_symbol"},  # no id
    ]
    out = handoff_module._build_required_tool_clause(items)
    assert "item1" in out
    assert "item2" not in out


def test_build_required_tool_clause_renders_hard_guidance():
    items = [
        {"id": "aaa111", "required_tool": "Serena: replace_symbol_body"},
        {"id": "bbb222", "required_tool": "meridian__patch_file"},
    ]
    out = handoff_module._build_required_tool_clause(items)
    assert out.startswith("\n<required_tool>")
    assert out.endswith("</required_tool>")
    assert "aaa111: Serena: replace_symbol_body" in out
    assert "bbb222: meridian__patch_file" in out
    # Hard requirement, not a soft hint like the model-tier clause.
    assert "hard requirement" in out
    assert "not a suggestion" in out


# ---------------------------------------------------------------------------
# handoff._build_quick_start_goal — unconditional rendering
# ---------------------------------------------------------------------------


def test_build_quick_start_goal_renders_required_tool_unconditionally():
    """Unlike model_tier_hints, there is no enabled flag — the pin always
    renders when present, because it's a hard directive not a soft hint."""
    items = [{"id": "aaa111", "required_tool": "Serena: replace_symbol_body"}]
    goal = handoff_module._build_quick_start_goal(items)
    assert "<required_tool>" in goal
    assert "aaa111: Serena: replace_symbol_body" in goal


def test_build_quick_start_goal_no_required_tool_clause_when_unset():
    items = [{"id": "aaa111", "title": "ordinary item"}]
    goal = handoff_module._build_quick_start_goal(items)
    assert "<required_tool>" not in goal


def test_build_quick_start_goal_empty_board_no_required_tool_clause():
    goal = handoff_module._build_quick_start_goal([])
    assert "<required_tool>" not in goal


# ---------------------------------------------------------------------------
# handoff.build_item_briefing
# ---------------------------------------------------------------------------


def test_build_item_briefing_renders_required_tool_tag():
    item = {
        "id": "item-xyz",
        "title": "Rename a symbol",
        "required_tool": "Serena: rename_symbol",
    }
    briefing = handoff_module.build_item_briefing(item)
    assert "<required_tool>" in briefing
    assert "Serena: rename_symbol" in briefing
    assert "MUST use" in briefing


def test_build_item_briefing_omits_required_tool_tag_when_unset():
    item = {"id": "item-xyz", "title": "Ordinary task"}
    briefing = handoff_module.build_item_briefing(item)
    assert "<required_tool>" not in briefing
