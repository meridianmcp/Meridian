"""74a8f420 — wave gate STRUCTURAL enforcement tests.

Wave gates are deterministic, on-the-fly-configurable action pipelines
(push_dev/push_main/deploy/wait/run_verification) attached to a wave or
wave-range. This suite covers the piece that turns them from advisory /goal
prose into a real, structural block:

  1. configure_wave_gate (DB layer): validation, upsert, immutability once passed.
  2. claim_sprint_item: refuses (WAVE_GATE_PENDING) an item in a later wave
     until the configured gate for the boundary wave actually completes.
  3. configure_wave_gate MCP tool: schema registration + dispatch + project_name
     resolution.
  4. _build_quick_start_goal: excludes gated items from the claimable batch
     with a structured <excluded_wave_gate_pending> note.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import server as srv
from meridian.handoff import _build_quick_start_goal
from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS, _TOOL_EXAMPLES


async def _project(db, name: str = "wave-gate-enforce"):
    proj = await srv._dispatch_mcp_tool("create_project", {"name": name}, db, "/tmp")
    return proj["id"]


_GOOD_PAYLOAD = {
    "status": "ok",
    "exit_code": 0,
    "passed": 10,
    "failed": 0,
    "stdout_tail": "10 passed",
    "stderr_tail": "",
}

_PIPELINE = [
    {"type": "push_dev"},
    {"type": "run_verification"},
    {"type": "push_main"},
    {"type": "deploy"},
]


# ---------------------------------------------------------------------------
# 1. configure_wave_gate DB-layer validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_configure_wave_gate_rejects_empty_actions(db):
    pid = (await db_module.create_project(db, name="cfg-empty-actions"))["id"]
    with pytest.raises(ValueError, match="non-empty actions list"):
        await db_module.configure_wave_gate(db, pid, "wave-1", [])


@pytest.mark.asyncio
async def test_configure_wave_gate_rejects_unknown_action_type(db):
    pid = (await db_module.create_project(db, name="cfg-bad-action"))["id"]
    with pytest.raises(ValueError, match="not one of the supported actions"):
        await db_module.configure_wave_gate(
            db, pid, "wave-1", [{"type": "sacrifice_a_goat"}]
        )


@pytest.mark.asyncio
async def test_configure_wave_gate_rejects_empty_wave_end(db):
    pid = (await db_module.create_project(db, name="cfg-empty-wave-end"))["id"]
    with pytest.raises(ValueError, match="non-empty wave_end"):
        await db_module.configure_wave_gate(db, pid, "", _PIPELINE)


@pytest.mark.asyncio
async def test_configure_wave_gate_creates_and_upserts(db):
    pid = (await db_module.create_project(db, name="cfg-upsert"))["id"]
    first = await db_module.configure_wave_gate(
        db, pid, "wave-2", [{"type": "wait", "seconds": 5}], wave_start="wave-1",
    )
    assert first["configured"] is True
    assert first["wave_start"] == "wave-1"
    assert first["wave_end"] == "wave-2"
    assert first["actions"] == [{"type": "wait", "seconds": 5}]

    # On-the-fly reconfiguration (upsert) — new pipeline replaces the old one,
    # same gate_config_id / wave_end.
    second = await db_module.configure_wave_gate(db, pid, "wave-2", _PIPELINE)
    assert second["gate_config_id"] == first["gate_config_id"]
    assert second["actions"] == _PIPELINE

    configs = await db_module.get_wave_gate_configs(db, pid)
    assert len(configs) == 1
    assert configs[0]["actions"] == _PIPELINE
    assert configs[0]["gate_passed"] is False


@pytest.mark.asyncio
async def test_configure_wave_gate_immutable_once_passed(db):
    pid = (await db_module.create_project(db, name="cfg-immutable"))["id"]
    await db_module.configure_wave_gate(db, pid, "wave-1", _PIPELINE)
    await db_module.complete_wave_gate(db, pid, "wave-1", _GOOD_PAYLOAD)

    with pytest.raises(ValueError, match="already completed"):
        await db_module.configure_wave_gate(db, pid, "wave-1", _PIPELINE)

    configs = await db_module.get_wave_gate_configs(db, pid)
    assert configs[0]["gate_passed"] is True


# ---------------------------------------------------------------------------
# 2. claim_sprint_item structural enforcement
# ---------------------------------------------------------------------------

async def _add_item_in_wave(db, pid, title, wave):
    item = await db_module.add_sprint_item(
        db, pid, "v1", title, touches_resources=None, force=True,
    )
    await db_module.patch_sprint_item(db, pid, item["id"], wave=wave)
    return item


@pytest.mark.asyncio
async def test_claim_blocked_by_pending_wave_gate(db):
    pid = await _project(db, "claim-blocked")
    await db_module.configure_wave_gate(db, pid, "wave-1", _PIPELINE)
    item = await _add_item_in_wave(db, pid, "wave-2 item", "wave-2")

    result = await db_module.claim_sprint_item(db, pid, item["id"])
    assert isinstance(result, dict)
    assert result.get("blocked") is True
    assert result.get("error") == "WAVE_GATE_PENDING"
    assert result.get("gate_wave_end") == "wave-1"

    # The item must genuinely still be unclaimed.
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert fresh["status"] in ("pending", "todo")


@pytest.mark.asyncio
async def test_claim_unblocked_after_gate_completes(db):
    pid = await _project(db, "claim-unblocked")
    await db_module.configure_wave_gate(db, pid, "wave-1", _PIPELINE)
    item = await _add_item_in_wave(db, pid, "wave-2 item", "wave-2")

    blocked = await db_module.claim_sprint_item(db, pid, item["id"])
    assert blocked.get("blocked") is True

    await db_module.complete_wave_gate(db, pid, "wave-1", _GOOD_PAYLOAD)

    claimed = await db_module.claim_sprint_item(db, pid, item["id"])
    assert claimed.get("status") == "in_progress"


@pytest.mark.asyncio
async def test_claim_not_blocked_for_item_at_or_before_gate_wave(db):
    """An item IN the gated wave itself (not beyond it) is unaffected — the
    gate only blocks items strictly AFTER its wave_end boundary."""
    pid = await _project(db, "claim-not-blocked-same-wave")
    await db_module.configure_wave_gate(db, pid, "wave-2", _PIPELINE)
    item = await _add_item_in_wave(db, pid, "wave-1 item", "wave-1")

    claimed = await db_module.claim_sprint_item(db, pid, item["id"])
    assert claimed.get("status") == "in_progress"


@pytest.mark.asyncio
async def test_claim_not_blocked_when_no_wave_configured(db):
    pid = await _project(db, "claim-no-gate-configured")
    item = await _add_item_in_wave(db, pid, "wave-5 item", "wave-5")

    claimed = await db_module.claim_sprint_item(db, pid, item["id"])
    assert claimed.get("status") == "in_progress"


@pytest.mark.asyncio
async def test_claim_not_blocked_for_item_with_no_wave(db):
    pid = await _project(db, "claim-item-no-wave")
    await db_module.configure_wave_gate(db, pid, "wave-1", _PIPELINE)
    item = await db_module.add_sprint_item(
        db, pid, "v1", "unwaved item", touches_resources=None, force=True,
    )
    claimed = await db_module.claim_sprint_item(db, pid, item["id"])
    assert claimed.get("status") == "in_progress"


# ---------------------------------------------------------------------------
# 3. configure_wave_gate MCP tool wiring
# ---------------------------------------------------------------------------

def test_configure_wave_gate_registered_in_tools_list():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "configure_wave_gate" in by_name
    tool = by_name["configure_wave_gate"]
    props = tool["inputSchema"]["properties"]
    assert "wave_end" in props
    assert "actions" in props
    assert "wave_start" in props
    assert "wave_end" in tool["inputSchema"].get("required", [])
    assert "actions" in tool["inputSchema"].get("required", [])
    assert "configure_wave_gate" not in _READ_ONLY_TOOLS
    assert "configure_wave_gate" in _TOOL_EXAMPLES


@pytest.mark.asyncio
async def test_configure_wave_gate_dispatch_via_mcp(db):
    pid = await _project(db, "cfg-mcp-dispatch")
    result = await srv._dispatch_mcp_tool(
        "configure_wave_gate",
        {"project_id": pid, "wave_end": "wave-1", "actions": _PIPELINE},
        db, "/tmp",
    )
    assert result.get("configured") is True
    assert result.get("wave_end") == "wave-1"


@pytest.mark.asyncio
async def test_configure_wave_gate_dispatch_missing_actions(db):
    pid = await _project(db, "cfg-mcp-missing-actions")
    result = await srv._dispatch_mcp_tool(
        "configure_wave_gate",
        {"project_id": pid, "wave_end": "wave-1"},
        db, "/tmp",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_configure_wave_gate_project_name_resolution(db):
    pid = await _project(db, "cfg-by-name")
    result = await srv._dispatch_mcp_tool(
        "configure_wave_gate",
        {"project_name": "cfg-by-name", "wave_end": "wave-1", "actions": _PIPELINE},
        db, "/tmp",
    )
    assert result.get("configured") is True
    assert result.get("project_id") == pid


# ---------------------------------------------------------------------------
# 4. _build_quick_start_goal wave-gate exclusion
# ---------------------------------------------------------------------------

def test_build_quick_start_goal_excludes_gated_wave_items():
    items = [
        {"id": "item-w1", "title": "wave 1 item", "wave": "wave-1"},
        {"id": "item-w2", "title": "wave 2 item", "wave": "wave-2"},
    ]
    goal = _build_quick_start_goal(
        items,
        wave_gate_pending=[
            {"wave_end": "wave-1", "actions": _PIPELINE, "gate_passed": False},
        ],
    )
    assert "item-w1" in goal
    assert "excluded_wave_gate_pending" in goal
    assert "item-w2" in goal  # named in the exclusion note itself
    # item-w2 must not appear in the executable directive's item list — it is
    # ONLY present inside the exclusion note.
    _, _, after_note = goal.partition("</excluded_wave_gate_pending>")
    before_note = goal.split("<excluded_wave_gate_pending")[0]
    assert "item-w2" not in before_note


def test_build_quick_start_goal_ignores_already_passed_gate():
    items = [
        {"id": "item-w2", "title": "wave 2 item", "wave": "wave-2"},
    ]
    goal = _build_quick_start_goal(
        items,
        wave_gate_pending=[
            {"wave_end": "wave-1", "actions": _PIPELINE, "gate_passed": True},
        ],
    )
    assert "item-w2" in goal
    assert "excluded_wave_gate_pending" not in goal


def test_build_quick_start_goal_no_wave_gate_pending_is_noop():
    items = [{"id": "item-1", "title": "solo item", "wave": "wave-1"}]
    goal = _build_quick_start_goal(items, wave_gate_pending=None)
    assert "item-1" in goal
    assert "excluded_wave_gate_pending" not in goal
