"""Tests for b905da5a — 3-tier MCP tool workflow classification.

Verifies:
  1. _TOOL_WORKFLOW_TIER maps only to valid tier values.
  2. Every tool in _MCP_TOOLS_LIST has 'workflow_tier' stamped onto it.
  3. Every tool's stamped workflow_tier is a valid value.
  4. The three tiers mandated by Adam's classification are present on
     representative tools from each tier (spot-checks).
  5. The tools/list endpoint response carries the workflow_tier field,
     so any MCP client can see it.
"""
from __future__ import annotations

import pytest

from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _TOOL_WORKFLOW_TIER,
)

VALID_TIERS = {"main-workflow", "common-support", "maintenance-only"}

# ---------------------------------------------------------------------------
# Part 1 — _TOOL_WORKFLOW_TIER dict integrity
# ---------------------------------------------------------------------------

def test_all_tier_values_are_valid() -> None:
    """b905da5a(1a) — every value in _TOOL_WORKFLOW_TIER is a valid tier string."""
    invalid = {
        name: tier
        for name, tier in _TOOL_WORKFLOW_TIER.items()
        if tier not in VALID_TIERS
    }
    assert not invalid, f"Invalid tier values: {invalid}"


def test_tier_dict_covers_main_workflow_tools() -> None:
    """b905da5a(1b) — Adam's explicit main-workflow tools are in _TOOL_WORKFLOW_TIER."""
    expected_main = {
        "start_session", "get_planning_brief", "get_sprint_items",
        "add_workspace_proposal", "promote_proposal", "add_sprint_item",
        "claim_sprint_item", "complete_sprint_item", "generate_handoff",
        "request_hitl",
    }
    missing = expected_main - set(_TOOL_WORKFLOW_TIER.keys())
    assert not missing, f"Main-workflow tools missing from _TOOL_WORKFLOW_TIER: {missing}"
    for name in expected_main:
        assert _TOOL_WORKFLOW_TIER[name] == "main-workflow", (
            f"{name} should be 'main-workflow', got '{_TOOL_WORKFLOW_TIER[name]}'"
        )


def test_tier_dict_covers_common_support_tools() -> None:
    """b905da5a(1c) — Adam's explicit common-support tools are classified correctly."""
    expected_support = {
        "checkpoint", "add_insight", "get_insights", "validate_assumption",
        "merge_sprint_items", "split_sprint_item", "add_sprint_item_pointer",
        "update_sprint_item",
    }
    for name in expected_support:
        assert _TOOL_WORKFLOW_TIER.get(name) == "common-support", (
            f"{name} should be 'common-support', got '{_TOOL_WORKFLOW_TIER.get(name)}'"
        )


def test_tier_dict_covers_maintenance_tools() -> None:
    """b905da5a(1d) — Adam's explicit maintenance-only tools are classified correctly."""
    expected_maintenance = {
        "analyze_sprint", "get_parallelizable_groups", "assign_sprint_waves",
        "reconcile_sprint_drift", "get_symbol_hotspots", "get_symbol_claims",
        "send_message", "receive_messages", "idle_until_all_done",
        "store_finding",
    }
    for name in expected_maintenance:
        assert _TOOL_WORKFLOW_TIER.get(name) == "maintenance-only", (
            f"{name} should be 'maintenance-only', got '{_TOOL_WORKFLOW_TIER.get(name)}'"
        )


# ---------------------------------------------------------------------------
# Part 2 — workflow_tier stamped onto _MCP_TOOLS_LIST entries
# ---------------------------------------------------------------------------

def test_all_tools_have_workflow_tier_stamped() -> None:
    """b905da5a(2a) — every tool in _MCP_TOOLS_LIST has 'workflow_tier' stamped."""
    missing = [t["name"] for t in _MCP_TOOLS_LIST if "workflow_tier" not in t]
    assert not missing, f"Tools missing 'workflow_tier': {missing}"


def test_all_tools_workflow_tier_is_valid() -> None:
    """b905da5a(2b) — every stamped workflow_tier value is a recognized tier."""
    invalid = [
        (t["name"], t["workflow_tier"])
        for t in _MCP_TOOLS_LIST
        if t.get("workflow_tier") not in VALID_TIERS
    ]
    assert not invalid, f"Tools with invalid workflow_tier: {invalid}"


def test_all_three_tiers_represented() -> None:
    """b905da5a(2c) — all three tiers appear in the stamped tool list."""
    tiers_present = {t.get("workflow_tier") for t in _MCP_TOOLS_LIST}
    for tier in VALID_TIERS:
        assert tier in tiers_present, f"No tools stamped with tier '{tier}'"


def test_workflow_tier_spot_checks_on_stamped_list() -> None:
    """b905da5a(2d) — representative spot-checks on the stamped _MCP_TOOLS_LIST entries."""
    tool_map = {t["name"]: t for t in _MCP_TOOLS_LIST}

    # main-workflow
    assert tool_map["start_session"]["workflow_tier"] == "main-workflow"
    assert tool_map["generate_handoff"]["workflow_tier"] == "main-workflow"
    assert tool_map["claim_sprint_item"]["workflow_tier"] == "main-workflow"
    assert tool_map["complete_sprint_item"]["workflow_tier"] == "main-workflow"
    assert tool_map["request_hitl"]["workflow_tier"] == "main-workflow"

    # common-support
    assert tool_map["checkpoint"]["workflow_tier"] == "common-support"
    assert tool_map["add_insight"]["workflow_tier"] == "common-support"
    assert tool_map["update_sprint_item"]["workflow_tier"] == "common-support"
    assert tool_map["log_task"]["workflow_tier"] == "common-support"
    assert tool_map["claim_file"]["workflow_tier"] == "common-support"
    assert tool_map["prospect_symbol"]["workflow_tier"] == "common-support"

    # maintenance-only
    assert tool_map["analyze_sprint"]["workflow_tier"] == "maintenance-only"
    assert tool_map["assign_sprint_waves"]["workflow_tier"] == "maintenance-only"
    assert tool_map["send_message"]["workflow_tier"] == "maintenance-only"
    assert tool_map["store_finding"]["workflow_tier"] == "maintenance-only"
    assert tool_map["get_symbol_hotspots"]["workflow_tier"] == "maintenance-only"
    assert tool_map["idle_until_all_done"]["workflow_tier"] == "maintenance-only"


# ---------------------------------------------------------------------------
# Part 3 — tools/list endpoint carries workflow_tier (integration-style)
# ---------------------------------------------------------------------------

def test_tools_list_endpoint_carries_workflow_tier(client: any) -> None:
    """b905da5a(3) — the /tools endpoint returns workflow_tier on every tool."""
    resp = client.get("/tools")
    assert resp.status_code == 200
    tools = resp.json()
    assert isinstance(tools, list) and tools, "Expected non-empty tool list"
    missing = [t.get("name") for t in tools if "workflow_tier" not in t]
    assert not missing, f"Tools missing workflow_tier in /tools response: {missing}"
    invalid = [
        (t.get("name"), t.get("workflow_tier"))
        for t in tools
        if t.get("workflow_tier") not in VALID_TIERS
    ]
    assert not invalid, f"Tools with invalid workflow_tier in /tools response: {invalid}"
