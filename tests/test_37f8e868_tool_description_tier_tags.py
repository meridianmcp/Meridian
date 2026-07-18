"""Tests for 37f8e868 — tier tags baked into tool description strings.

Verifies:
  1. maintenance-only tools have "[MAINTENANCE] " prefix in their description.
  2. common-support tools have "[SUPPORT] " prefix in their description.
  3. main-workflow tools have NO tier prefix (they are the core loop).
  4. The prefixes are idempotent (not doubled).
  5. tools/list endpoint returns descriptions with the correct prefix.
"""
from __future__ import annotations

import pytest

from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _TOOL_WORKFLOW_TIER,
)

VALID_TIERS = {"main-workflow", "common-support", "maintenance-only"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_map() -> dict:
    return {t["name"]: t for t in _MCP_TOOLS_LIST}


# ---------------------------------------------------------------------------
# Part 1 — description prefix contracts
# ---------------------------------------------------------------------------


def test_maintenance_tools_have_maintenance_prefix() -> None:
    """37f8e868(1a) — every maintenance-only tool starts with '[MAINTENANCE] '."""
    tm = _tool_map()
    bad = []
    for name, tier in _TOOL_WORKFLOW_TIER.items():
        if tier != "maintenance-only":
            continue
        tool = tm.get(name)
        if tool is None:
            continue
        desc = tool.get("description", "")
        if not desc.startswith("[MAINTENANCE] "):
            bad.append((name, desc[:80]))
    assert not bad, (
        f"maintenance-only tools missing '[MAINTENANCE] ' prefix: {bad}"
    )


def test_support_tools_have_support_prefix() -> None:
    """37f8e868(1b) — every common-support tool starts with '[SUPPORT] '."""
    tm = _tool_map()
    bad = []
    for name, tier in _TOOL_WORKFLOW_TIER.items():
        if tier != "common-support":
            continue
        tool = tm.get(name)
        if tool is None:
            continue
        desc = tool.get("description", "")
        if not desc.startswith("[SUPPORT] "):
            bad.append((name, desc[:80]))
    assert not bad, (
        f"common-support tools missing '[SUPPORT] ' prefix: {bad}"
    )


def test_main_workflow_tools_have_no_tier_prefix() -> None:
    """37f8e868(1c) — main-workflow tools must NOT carry a tier prefix."""
    tm = _tool_map()
    bad = []
    for name, tier in _TOOL_WORKFLOW_TIER.items():
        if tier != "main-workflow":
            continue
        tool = tm.get(name)
        if tool is None:
            continue
        desc = tool.get("description", "")
        if desc.startswith("[MAINTENANCE] ") or desc.startswith("[SUPPORT] "):
            bad.append((name, desc[:80]))
    assert not bad, (
        f"main-workflow tools should NOT have a tier prefix: {bad}"
    )


# ---------------------------------------------------------------------------
# Part 2 — spot checks on representative tools
# ---------------------------------------------------------------------------


def test_spot_checks_maintenance_prefix() -> None:
    """37f8e868(2a) — spot-check specific maintenance tools for prefix."""
    tm = _tool_map()
    maintenance_tools = [
        "analyze_sprint",
        "assign_sprint_waves",
        "send_message",
        "refresh_tool_manifest",
        "list_plugins",
        "create_project",
    ]
    for name in maintenance_tools:
        tool = tm.get(name)
        assert tool is not None, f"Expected tool '{name}' to exist"
        desc = tool.get("description", "")
        assert desc.startswith("[MAINTENANCE] "), (
            f"'{name}' should start with '[MAINTENANCE] ', got: {desc[:80]!r}"
        )


def test_spot_checks_support_prefix() -> None:
    """37f8e868(2b) — spot-check specific support tools for prefix."""
    tm = _tool_map()
    support_tools = [
        "log_task",
        "checkpoint",
        "pin_decision",
        "claim_file",
        "search_tasks",
    ]
    for name in support_tools:
        tool = tm.get(name)
        assert tool is not None, f"Expected tool '{name}' to exist"
        desc = tool.get("description", "")
        assert desc.startswith("[SUPPORT] "), (
            f"'{name}' should start with '[SUPPORT] ', got: {desc[:80]!r}"
        )


def test_spot_checks_main_workflow_no_prefix() -> None:
    """37f8e868(2c) — spot-check main-workflow tools have no prefix."""
    tm = _tool_map()
    main_tools = [
        "start_session",
        "generate_handoff",
        "claim_sprint_item",
        "complete_sprint_item",
        "request_hitl",
        "add_sprint_item",
    ]
    for name in main_tools:
        tool = tm.get(name)
        assert tool is not None, f"Expected tool '{name}' to exist"
        desc = tool.get("description", "")
        assert not desc.startswith("[MAINTENANCE] ") and not desc.startswith("[SUPPORT] "), (
            f"'{name}' should NOT have a tier prefix, got: {desc[:80]!r}"
        )


# ---------------------------------------------------------------------------
# Part 3 — idempotency (no double-prefix)
# ---------------------------------------------------------------------------


def test_no_double_prefix() -> None:
    """37f8e868(3) — descriptions must not contain doubled prefixes."""
    for tool in _MCP_TOOLS_LIST:
        desc = tool.get("description", "")
        assert "[MAINTENANCE] [MAINTENANCE] " not in desc, (
            f"{tool['name']}: doubled [MAINTENANCE] prefix"
        )
        assert "[SUPPORT] [SUPPORT] " not in desc, (
            f"{tool['name']}: doubled [SUPPORT] prefix"
        )
        assert "[MAINTENANCE] [SUPPORT] " not in desc, (
            f"{tool['name']}: mixed double prefix on {tool['name']}"
        )
        assert "[SUPPORT] [MAINTENANCE] " not in desc, (
            f"{tool['name']}: mixed double prefix on {tool['name']}"
        )


# ---------------------------------------------------------------------------
# Part 4 — tools/list endpoint returns prefixed descriptions
# ---------------------------------------------------------------------------


def test_tools_list_endpoint_descriptions_have_tier_prefix(client: any) -> None:
    """37f8e868(4) — /tools endpoint returns descriptions with correct tier prefix."""
    resp = client.get("/tools")
    assert resp.status_code == 200
    tools = resp.json()
    assert isinstance(tools, list) and tools

    bad_maintenance = []
    bad_support = []
    bad_main_prefixed = []

    for t in tools:
        name = t.get("name", "")
        desc = t.get("description", "")
        tier = t.get("workflow_tier")
        if tier == "maintenance-only" and not desc.startswith("[MAINTENANCE] "):
            bad_maintenance.append((name, desc[:60]))
        elif tier == "common-support" and not desc.startswith("[SUPPORT] "):
            bad_support.append((name, desc[:60]))
        elif tier == "main-workflow" and (
            desc.startswith("[MAINTENANCE] ") or desc.startswith("[SUPPORT] ")
        ):
            bad_main_prefixed.append((name, desc[:60]))

    assert not bad_maintenance, f"Maintenance tools missing prefix in /tools: {bad_maintenance}"
    assert not bad_support, f"Support tools missing prefix in /tools: {bad_support}"
    assert not bad_main_prefixed, f"Main-workflow tools have unexpected prefix in /tools: {bad_main_prefixed}"
