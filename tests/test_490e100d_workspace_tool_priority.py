"""Tests for 490e100d — workspace/project-level default MCP tool priority per
semantic task category, hard-enforced.

Generalizes 4d1fb28f's per-item ``sprint_items.required_tool`` pin up one
level: a workspace admin sets a durable default once (e.g. ``{"code-reading":
"Serena: find_symbol"}``) via ``update_workspace_settings(tool_priority_map=
...)`` instead of a planner pinning ``required_tool`` on every matching item.
Extends 22b7f3f1's finding that advisory-only tool preference loses to
attention decay — this is rendered as a HARD, unconditional directive in the
/goal block, same structural-enforcement class as 4d1fb28f.

Covers:
  * DB layer — update_workspace_settings/get_workspace_settings roundtrip,
    clear semantics, malformed-row tolerance.
  * handoff._classify_sprint_item_task_category — pure-function unit tests.
  * handoff._build_workspace_tool_priority_clause — pure-function unit tests,
    including item-level required_tool always winning over the workspace
    default.
  * handoff._build_quick_start_goal — the clause is rendered unconditionally
    whenever the workspace default applies to a pending item.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_settings_tool_priority_map_defaults(db):
    ws = await db_module.get_workspace_settings(db)
    assert ws["tool_priority_map"] is None


@pytest.mark.asyncio
async def test_update_workspace_settings_tool_priority_map_roundtrip(db):
    ws = await db_module.update_workspace_settings(
        db,
        tool_priority_map={"code-reading": "Serena: find_symbol"},
    )
    assert ws["tool_priority_map"] == {"code-reading": "Serena: find_symbol"}
    # Persists on re-read.
    ws2 = await db_module.get_workspace_settings(db)
    assert ws2["tool_priority_map"] == {"code-reading": "Serena: find_symbol"}
    # Overwrite with a bigger mapping.
    ws3 = await db_module.update_workspace_settings(
        db,
        tool_priority_map={
            "code-reading": "Serena: find_symbol",
            "research": "Context7: get-library-docs",
        },
    )
    assert ws3["tool_priority_map"] == {
        "code-reading": "Serena: find_symbol",
        "research": "Context7: get-library-docs",
    }


@pytest.mark.asyncio
async def test_update_workspace_settings_tool_priority_map_clear(db):
    await db_module.update_workspace_settings(
        db, tool_priority_map={"code-reading": "Serena: find_symbol"},
    )
    # Empty dict clears it.
    ws = await db_module.update_workspace_settings(db, tool_priority_map={})
    assert ws["tool_priority_map"] is None
    # "" also clears it.
    await db_module.update_workspace_settings(
        db, tool_priority_map={"code-reading": "Serena: find_symbol"},
    )
    ws2 = await db_module.update_workspace_settings(db, tool_priority_map="")
    assert ws2["tool_priority_map"] is None


@pytest.mark.asyncio
async def test_workspace_settings_tool_priority_map_malformed_row_tolerant(db):
    """A malformed / non-dict JSON value in the column must degrade to None,
    never raise (mirrors refresh_triggers' malformed-row tolerance)."""
    await db_module.update_workspace_settings(db)  # ensure the row exists
    settings_key = db_module.workspace._ws_settings_key(None)
    await db.execute(
        "UPDATE workspace_settings SET tool_priority_map = ? WHERE id = ?",
        ("not valid json", settings_key),
    )
    await db.commit()
    ws = await db_module.get_workspace_settings(db)
    assert ws["tool_priority_map"] is None


# ---------------------------------------------------------------------------
# handoff._classify_sprint_item_task_category (pure function)
# ---------------------------------------------------------------------------


def test_classify_sprint_item_task_category_matches_builtin_keyword():
    item = {"id": "a1", "title": "Read and understand the auth module"}
    category = handoff_module._classify_sprint_item_task_category(
        item, ["code-reading", "code-writing"]
    )
    assert category == "code-reading"


def test_classify_sprint_item_task_category_no_match_returns_none():
    item = {"id": "a1", "title": "Deploy the release tag"}
    category = handoff_module._classify_sprint_item_task_category(
        item, ["code-reading", "code-writing"]
    )
    assert category is None


def test_classify_sprint_item_task_category_custom_category_fallback():
    """An unknown/custom category falls back to matching its own name-derived
    keywords, so an admin-defined category works without a code change."""
    item = {"id": "a1", "title": "Do some database migration work"}
    category = handoff_module._classify_sprint_item_task_category(
        item, ["database-migration"]
    )
    assert category == "database-migration"


# ---------------------------------------------------------------------------
# handoff._build_workspace_tool_priority_clause (pure function)
# ---------------------------------------------------------------------------


def test_build_workspace_tool_priority_clause_empty_when_no_map():
    items = [{"id": "a1", "title": "Read the module"}]
    assert handoff_module._build_workspace_tool_priority_clause(items, None) == ""
    assert handoff_module._build_workspace_tool_priority_clause(items, {}) == ""


def test_build_workspace_tool_priority_clause_renders_hard_guidance():
    items = [{"id": "a1", "title": "Read and understand the parser code"}]
    tpm = {"code-reading": "meridian-code"}
    out = handoff_module._build_workspace_tool_priority_clause(items, tpm)
    assert out.startswith("\n<workspace_tool_priority>")
    assert out.endswith("</workspace_tool_priority>")
    assert "a1 (code-reading): meridian-code" in out
    assert "hard requirement" in out
    assert "not a suggestion" in out


def test_build_workspace_tool_priority_clause_no_match_is_empty():
    items = [{"id": "a1", "title": "Deploy the release tag"}]
    tpm = {"code-reading": "meridian-code"}
    assert handoff_module._build_workspace_tool_priority_clause(items, tpm) == ""


def test_build_workspace_tool_priority_clause_item_level_pin_wins():
    """4d1fb28f's per-item required_tool always overrides the workspace
    default — the item is skipped here when it already carries a pin."""
    items = [{
        "id": "a1",
        "title": "Read and understand the parser code",
        "required_tool": "GitHub: search_code",
    }]
    tpm = {"code-reading": "meridian-code"}
    out = handoff_module._build_workspace_tool_priority_clause(items, tpm)
    assert out == ""


def test_build_workspace_tool_priority_clause_skips_items_without_id():
    items = [{"title": "Read and understand the parser code"}]
    tpm = {"code-reading": "meridian-code"}
    assert handoff_module._build_workspace_tool_priority_clause(items, tpm) == ""


# ---------------------------------------------------------------------------
# handoff._build_quick_start_goal — unconditional rendering
# ---------------------------------------------------------------------------


def test_build_quick_start_goal_renders_workspace_tool_priority_unconditionally():
    items = [{"id": "a1", "title": "Read and understand the parser code"}]
    tpm = {"code-reading": "meridian-code"}
    goal = handoff_module._build_quick_start_goal(items, tool_priority_map=tpm)
    assert "<workspace_tool_priority>" in goal
    assert "a1 (code-reading): meridian-code" in goal


def test_build_quick_start_goal_no_workspace_tool_priority_clause_when_unset():
    items = [{"id": "a1", "title": "Read and understand the parser code"}]
    goal = handoff_module._build_quick_start_goal(items, tool_priority_map=None)
    assert "<workspace_tool_priority>" not in goal


def test_build_quick_start_goal_item_pin_takes_precedence_over_workspace_default():
    items = [{
        "id": "a1",
        "title": "Read and understand the parser code",
        "required_tool": "Serena: find_symbol",
    }]
    tpm = {"code-reading": "GitHub: search_code"}
    goal = handoff_module._build_quick_start_goal(items, tool_priority_map=tpm)
    # Item-level required_tool clause still fires...
    assert "<required_tool>" in goal
    assert "a1: Serena: find_symbol" in goal
    # ...but the workspace-default clause does NOT double-pin the same item.
    assert "<workspace_tool_priority>" not in goal
