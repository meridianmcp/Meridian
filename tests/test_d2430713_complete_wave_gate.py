"""d2430713 — complete_wave_gate MCP tool tests.

Coverage:
  1. Gate rejected without any verification_payload (missing evidence).
  2. Gate rejected with fabricated/self-report payload (status!='ok').
  3. Gate rejected with non-zero exit_code (tests failed).
  4. Gate rejected with status='not_configured'.
  5. Gate rejected with status='not_connected'.
  6. Gate accepted with a genuine passing run_verification result.
  7. After gate completes, next-wave items are reported as unblocked.
  8. Duplicate gate completion is rejected.
  9. MCP tool schema is registered correctly.
 10. MCP tool dispatches via _dispatch_mcp_tool.
 11. project_name resolves to project_id correctly.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import server as srv
from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _READ_ONLY_TOOLS,
    _TITLE_OVERRIDES,
    _TOOL_EXAMPLES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _project(db, name: str = "wave-gate-test"):
    proj = await srv._dispatch_mcp_tool(
        "create_project", {"name": name}, db, "/tmp"
    )
    return proj["id"]


_GOOD_PAYLOAD = {
    "status": "ok",
    "exit_code": 0,
    "passed": 42,
    "failed": 0,
    "stdout_tail": "42 passed in 5.3s",
    "stderr_tail": "",
}


# ---------------------------------------------------------------------------
# 1. Missing verification_payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_no_payload(db):
    pid = await _project(db, "gate-no-payload")
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1"},
        db, "/tmp",
    )
    assert "error" in result
    assert "verification_payload" in result["error"].lower()


# ---------------------------------------------------------------------------
# 2. Self-report / bad status rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_bad_status(db):
    pid = await _project(db, "gate-bad-status")
    # An executor self-reporting success without running anything.
    bad_payload = {"status": "ok", "exit_code": 0, "passed": None, "failed": None}
    # Even with exit_code=0, passed=None is technically allowed — what matters
    # is a non-ok status is rejected.
    bad_payload2 = {"status": "self_reported", "exit_code": 0, "passed": 0, "failed": 0}
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": bad_payload2},
        db, "/tmp",
    )
    assert "error" in result
    assert "self_reported" in result["error"] or "status" in result["error"]


# ---------------------------------------------------------------------------
# 3. Non-zero exit_code rejected (tests failed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_nonzero_exit(db):
    pid = await _project(db, "gate-failed-tests")
    failing_payload = {
        "status": "ok",
        "exit_code": 1,
        "passed": 10,
        "failed": 3,
        "stdout_tail": "10 passed, 3 failed in 2s",
        "stderr_tail": "FAILED test_foo.py",
    }
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": failing_payload},
        db, "/tmp",
    )
    assert "error" in result
    assert "exit_code" in result["error"]


# ---------------------------------------------------------------------------
# 4. status='not_configured' rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_not_configured(db):
    pid = await _project(db, "gate-not-configured")
    not_configured = {
        "status": "not_configured",
        "exit_code": None,
        "passed": None,
        "failed": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": not_configured},
        db, "/tmp",
    )
    assert "error" in result
    assert "not_configured" in result["error"].lower()


# ---------------------------------------------------------------------------
# 5. status='not_connected' rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_not_connected(db):
    pid = await _project(db, "gate-not-connected")
    not_connected = {
        "status": "not_connected",
        "exit_code": None,
        "passed": None,
        "failed": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": not_connected},
        db, "/tmp",
    )
    assert "error" in result
    assert "not_connected" in result["error"].lower()


# ---------------------------------------------------------------------------
# 6. Gate accepted with genuine passing payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_accepted_with_passing_payload(db):
    pid = await _project(db, "gate-accepted")
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert result.get("gate_completed") is True
    assert result.get("wave_label") == "wave-1"
    assert result.get("next_wave_label") == "wave-2"
    assert "gate_id" in result
    assert result["gate_id"]  # non-empty UUID


# ---------------------------------------------------------------------------
# 7. Next-wave items are reported after gate completes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_reports_next_wave_items(db):
    pid = await _project(db, "gate-next-wave-items")
    # Add two items in wave-2 (the next wave after wave-1).
    item_a = await db_module.add_sprint_item(
        db, pid, "v1", "next-wave item A", touches_resources=["file:foo.py"]
    )
    item_b = await db_module.add_sprint_item(
        db, pid, "v1", "next-wave item B", touches_resources=["file:bar.py"],
        force=True,
    )
    # Manually assign them to wave-2 (normally assign_sprint_waves does this).
    await db_module.patch_sprint_item(db, pid, item_a["id"], wave="wave-2")
    await db_module.patch_sprint_item(db, pid, item_b["id"], wave="wave-2")

    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert result.get("gate_completed") is True
    assert result.get("next_wave_label") == "wave-2"
    assert result.get("next_wave_item_count") == 2
    assert set(result.get("next_wave_item_ids", [])) == {item_a["id"], item_b["id"]}


# ---------------------------------------------------------------------------
# 8. Duplicate gate completion rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_duplicate_rejected(db):
    pid = await _project(db, "gate-duplicate")
    # First call should succeed.
    first = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert first.get("gate_completed") is True

    # Second call with same wave_label on same project should fail.
    second = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert "error" in second
    assert "already been completed" in second["error"] or "already" in second["error"]


# ---------------------------------------------------------------------------
# 9. MCP tool schema is registered correctly
# ---------------------------------------------------------------------------

def test_complete_wave_gate_registered_in_tools_list():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "complete_wave_gate" in by_name, "complete_wave_gate not in _MCP_TOOLS_LIST"
    tool = by_name["complete_wave_gate"]
    schema = tool["inputSchema"]
    props = schema["properties"]
    assert "project_id" in props
    assert "wave_label" in props
    assert "verification_payload" in props
    assert "actor" in props
    # wave_label and verification_payload are required
    assert "wave_label" in schema.get("required", [])
    assert "verification_payload" in schema.get("required", [])
    # Not read-only (it writes a gate result).
    assert "complete_wave_gate" not in _READ_ONLY_TOOLS
    # Has a title override.
    assert _TITLE_OVERRIDES.get("complete_wave_gate") == "Complete Wave Gate"
    assert tool.get("title") == "Complete Wave Gate"
    # Has an example.
    assert "complete_wave_gate" in _TOOL_EXAMPLES


# ---------------------------------------------------------------------------
# 10. Dispatches through _dispatch_mcp_tool without error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_wave_gate_dispatch_works(db):
    """Verifies the MCP dispatch table includes complete_wave_gate."""
    pid = await _project(db, "gate-dispatch")
    # A call with missing project_id should return an error dict, not raise.
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"wave_label": "wave-1", "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    # Should return an error about missing project_id, not _MISS sentinel.
    assert isinstance(result, dict)
    assert "error" in result


# ---------------------------------------------------------------------------
# 11. project_name resolves to project_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_wave_gate_project_name_resolution(db):
    pid = await _project(db, "gate-by-name")
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_name": "gate-by-name", "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert result.get("gate_completed") is True
    assert result.get("wave_label") == "wave-1"


# ---------------------------------------------------------------------------
# 12. Direct DB function test: complete_wave_gate validates payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_complete_wave_gate_rejects_non_dict(db):
    pid = await db_module.create_project(db, name="gate-db-direct")
    pid = pid["id"]
    with pytest.raises(ValueError, match="verification_payload dict"):
        await db_module.complete_wave_gate(db, pid, "wave-1", True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_db_complete_wave_gate_rejects_error_status(db):
    pid = (await db_module.create_project(db, name="gate-db-error-status"))["id"]
    with pytest.raises(ValueError, match="status='error'"):
        await db_module.complete_wave_gate(
            db, pid, "wave-1",
            {"status": "error", "exit_code": None, "passed": None, "failed": None},
        )


@pytest.mark.asyncio
async def test_db_complete_wave_gate_succeeds(db):
    pid = (await db_module.create_project(db, name="gate-db-success"))["id"]
    result = await db_module.complete_wave_gate(db, pid, "wave-1", _GOOD_PAYLOAD)
    assert result["gate_completed"] is True
    assert result["wave_label"] == "wave-1"
    assert result["next_wave_label"] == "wave-2"
    assert result["gate_id"]
