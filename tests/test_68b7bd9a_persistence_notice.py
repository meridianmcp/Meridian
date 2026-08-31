"""Tests for 68b7bd9a — strengthened persistence-disclosure notice.

Verifies the generated tools/list output (not just the source constant in
isolation) so the check exercises the same post-processing pipeline an MCP
client actually sees. Mirrors the structure of
tests/test_37f8e868_tool_description_tier_tags.py: import _MCP_TOOLS_LIST
directly, spot-check named tools, and separately hit the live /tools HTTP
endpoint via the `client` fixture to confirm the same contract holds
end-to-end.

Acceptance criteria for 68b7bd9a: strengthen the notice without claiming
unsupported behavior; verify it appears on log_task, pin_decision,
checkpoint, generate_handoff, add_sprint_item, add_note, add_insight, and
request_hitl.
"""
from __future__ import annotations

from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _PERSISTENCE_NOTICE,
    _READ_ONLY_TOOLS,
)

# The 8 tools named explicitly in item 68b7bd9a's acceptance criteria.
NAMED_TOOLS = [
    "log_task",
    "pin_decision",
    "checkpoint",
    "generate_handoff",
    "add_sprint_item",
    "add_note",
    "add_insight",
    "request_hitl",
]

# Other mutating sprint-item tools the blanket mechanism (not an allowlist)
# also covers -- not in item 68b7bd9a's named list, but worth a regression
# guard since the discovery brief for this item confirmed they qualify.
OTHER_COVERED_TOOLS = [
    "update_sprint_item",
    "complete_sprint_item",
    "claim_sprint_item",
]

# Genuinely read-only tools that must NOT carry the notice.
READ_ONLY_SPOT_CHECK = [
    "get_sprint_items",
    "list_projects",
    "get_notes",
    "read_note",
]


def _tool_map() -> dict:
    return {t["name"]: t for t in _MCP_TOOLS_LIST}


# ---------------------------------------------------------------------------
# Part 1 -- strengthened notice text contains the required disclosures
# ---------------------------------------------------------------------------


def test_notice_text_names_required_storage_categories() -> None:
    """68b7bd9a -- notice must name each genuinely-stored category."""
    required_fragments = [
        "task log entries",
        "pinned decisions",
        "sprint items",
        "notes",
        "handoff/goal state",
        "HITL queue items",
        "Postgres",
        "Neon",
        "SQLite",
        "dashboard",
        "API",
    ]
    for fragment in required_fragments:
        assert fragment in _PERSISTENCE_NOTICE, (
            f"Expected {fragment!r} in strengthened _PERSISTENCE_NOTICE"
        )


def test_notice_does_not_overclaim_per_record_deletion() -> None:
    """68b7bd9a -- HITL queue items and handoff state have no per-record
    delete (db.dismiss_hitl_request only sets status='dismissed'; there is
    no delete_handoff/delete_goal_state function). The notice must say so
    rather than implying arbitrary per-item deletion exists for everything.
    """
    assert "no per-record delete" in _PERSISTENCE_NOTICE
    assert "HITL queue items" in _PERSISTENCE_NOTICE
    assert "handoff state" in _PERSISTENCE_NOTICE


# ---------------------------------------------------------------------------
# Part 2 -- the notice actually appears on the 8 named tools + siblings
# ---------------------------------------------------------------------------


def test_named_tools_carry_the_notice() -> None:
    """68b7bd9a -- all 8 acceptance-criteria tools carry the strengthened
    notice verbatim in the generated tool description.
    """
    tm = _tool_map()
    missing = []
    for name in NAMED_TOOLS:
        tool = tm.get(name)
        assert tool is not None, f"Expected tool '{name}' to exist"
        if _PERSISTENCE_NOTICE not in tool.get("description", ""):
            missing.append(name)
    assert not missing, f"Tools missing persistence notice: {missing}"


def test_other_mutating_sprint_tools_also_carry_notice() -> None:
    """68b7bd9a -- the blanket (not-read-only) mechanism also covers these;
    no allowlist gap.
    """
    tm = _tool_map()
    missing = []
    for name in OTHER_COVERED_TOOLS:
        tool = tm.get(name)
        assert tool is not None, f"Expected tool '{name}' to exist"
        if _PERSISTENCE_NOTICE not in tool.get("description", ""):
            missing.append(name)
    assert not missing, f"Tools missing persistence notice: {missing}"


def test_read_only_tools_do_not_carry_the_notice() -> None:
    """68b7bd9a -- genuinely read-only tools (other than generate_handoff,
    which is force-included) must not carry the notice.
    """
    tm = _tool_map()
    leaked = []
    for name in READ_ONLY_SPOT_CHECK:
        assert name in _READ_ONLY_TOOLS, f"'{name}' expected in _READ_ONLY_TOOLS"
        tool = tm.get(name)
        assert tool is not None, f"Expected tool '{name}' to exist"
        if _PERSISTENCE_NOTICE in tool.get("description", ""):
            leaked.append(name)
    assert not leaked, f"Read-only tools unexpectedly carry persistence notice: {leaked}"


def test_notice_not_doubled() -> None:
    """68b7bd9a -- idempotency: the notice must appear at most once per tool."""
    doubled = []
    for tool in _MCP_TOOLS_LIST:
        desc = tool.get("description", "")
        if desc.count(_PERSISTENCE_NOTICE) > 1:
            doubled.append(tool["name"])
    assert not doubled, f"Tools with doubled persistence notice: {doubled}"


# ---------------------------------------------------------------------------
# Part 3 -- live /tools endpoint reflects the same contract end-to-end
# ---------------------------------------------------------------------------


def test_tools_list_endpoint_carries_notice_on_named_tools(client: any) -> None:
    """68b7bd9a -- the ACTUAL GENERATED tools/list output (via the live
    /tools endpoint), not just the source constant, carries the
    strengthened notice for the 8 named tools.
    """
    resp = client.get("/tools")
    assert resp.status_code == 200
    tools = resp.json()
    assert isinstance(tools, list) and tools

    by_name = {t.get("name"): t for t in tools}
    missing = []
    for name in NAMED_TOOLS:
        tool = by_name.get(name)
        assert tool is not None, f"Expected tool '{name}' in /tools response"
        if _PERSISTENCE_NOTICE not in tool.get("description", ""):
            missing.append(name)
    assert not missing, f"/tools response missing persistence notice on: {missing}"


def test_tools_list_endpoint_omits_notice_on_read_only_tools(client: any) -> None:
    """68b7bd9a -- read-only tools in the live /tools response stay free of
    the notice (generate_handoff is the sole documented exception)."""
    resp = client.get("/tools")
    assert resp.status_code == 200
    tools = resp.json()

    by_name = {t.get("name"): t for t in tools}
    leaked = []
    for name in READ_ONLY_SPOT_CHECK:
        tool = by_name.get(name)
        assert tool is not None, f"Expected tool '{name}' in /tools response"
        if _PERSISTENCE_NOTICE in tool.get("description", ""):
            leaked.append(name)
    assert not leaked, f"/tools response leaks persistence notice on read-only: {leaked}"
