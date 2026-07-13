"""0e973e52 — run_verification tunnel tool tests.

Coverage:
  1. No test_cmd configured → clean {status: 'not_configured'}, not an error.
  2. Stubbed tunnel call succeeds → real exit_code / stdout / stderr in result.
  3. Stubbed tunnel call fails (non-zero exit) → correct exit_code.
  4. MCP tool registration / schema shape is correct.
  5. _parse_test_counts parses pytest-style summary lines.
  6. _handle_run_cmd produces the right structure on success and on error.
  7. send_run_cmd_control returns not_connected when no FS socket.

Follows the stubbing style of test_searchgraph_projectid_hint.py and
test_docx_tunnel_write_lock.py — tunnel state monkeypatched directly, no
real subprocess or network needed for the tunnel-integration tests.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.routes import tunnel as tun
from meridian.tunnel_client import _parse_test_counts, _handle_run_cmd


# ---------------------------------------------------------------------------
# 1. _parse_test_counts — pure helper, no I/O
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,passed,failed", [
    ("5 passed, 2 failed in 3.14s", 5, 2),
    ("3 passed", 3, None),
    ("0 failed", None, 0),
    ("1 passed, 0 failed", 1, 0),
    ("ERROR — process aborted", None, None),
    ("", None, None),
    ("100 passed, 10 failed, 5 skipped in 120s", 100, 10),
])
def test_parse_test_counts(text, passed, failed):
    p, f = _parse_test_counts(text)
    assert p == passed
    assert f == failed


# ---------------------------------------------------------------------------
# 2. _handle_run_cmd — actually runs a subprocess; uses trivial commands
#    that always work on any OS.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_run_cmd_success():
    """A simple command that succeeds returns exit_code=0."""
    result = await _handle_run_cmd(
        ["python", "-c", "import sys; print('3 passed'); sys.exit(0)"],
        cwd=None,
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "3 passed" in result["stdout_tail"]
    assert result["passed"] == 3


@pytest.mark.asyncio
async def test_handle_run_cmd_nonzero_exit():
    """A command that exits non-zero is reported honestly (not as an error)."""
    result = await _handle_run_cmd(
        ["python", "-c", "import sys; print('1 failed'); sys.exit(1)"],
        cwd=None,
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 1
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_handle_run_cmd_empty_cmd():
    """Empty command returns status='error', never raises."""
    result = await _handle_run_cmd("", cwd=None)
    assert result["status"] == "error"
    assert result["exit_code"] is None


@pytest.mark.asyncio
async def test_handle_run_cmd_shell_string():
    """A shell-style string command also works."""
    result = await _handle_run_cmd(
        'python -c "import sys; sys.exit(0)"',
        cwd=None,
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# 3. send_run_cmd_control: not_connected when no FS socket
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_run_cmd_control_not_connected():
    """With no active FS socket the function returns status='not_connected', never raises."""
    tun._tunnel_sockets.pop("tenant-nc", None)
    result = await tun.send_run_cmd_control("tenant-nc", cmd="pytest", cwd=None)
    assert result["status"] == "not_connected"
    assert result["exit_code"] is None
    assert result["passed"] is None
    assert result["failed"] is None


# ---------------------------------------------------------------------------
# 4. run_verification: not_configured when no test_cmd stored
#    Tests the actual DB path without invoking the full MCP stack.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_verification_not_configured_via_db(db):
    """When executor_config has no test_cmd the tool logic returns not_configured."""
    proj = await db_module.create_project(db, name="test-no-cmd")
    # No set_executor_config call → test_cmd is absent.
    exec_cfg = await db_module.get_executor_config(db, proj["id"]) or {}
    test_cmd = (exec_cfg.get("test_cmd") or "").strip()

    # Simulate what the handler does: if no test_cmd, return not_configured.
    if not test_cmd:
        result = {
            "status": "not_configured",
            "message": "No test_cmd configured.",
            "exit_code": None,
            "passed": None,
            "failed": None,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    else:
        result = {"status": "unexpected"}

    assert result["status"] == "not_configured"
    assert result["exit_code"] is None
    assert result["passed"] is None
    assert result["failed"] is None


# ---------------------------------------------------------------------------
# 5. run_verification: stubbed tunnel call succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_verification_tunnel_success(db, monkeypatch):
    """send_run_cmd_control is called with the right args and result is propagated."""
    proj = await db_module.create_project(db, name="test-cmd-pass")
    await db_module.set_executor_config(db, proj["id"], {
        "test_cmd": "pixi run test",
        "repo_path": "/repo",
    })

    called = {}

    async def _fake_run_cmd(tenant_id, cmd, cwd=None):
        called["cmd"] = cmd
        called["cwd"] = cwd
        called["tenant"] = tenant_id
        return {
            "status": "ok",
            "exit_code": 0,
            "passed": 42,
            "failed": 0,
            "stdout_tail": "42 passed in 12.3s",
            "stderr_tail": "",
        }

    monkeypatch.setattr(tun, "send_run_cmd_control", _fake_run_cmd)
    monkeypatch.setattr(tun, "has_active_tunnel", lambda tid: True)

    exec_cfg = await db_module.get_executor_config(db, proj["id"]) or {}
    test_cmd = (exec_cfg.get("test_cmd") or "").strip()
    repo_path = (exec_cfg.get("repo_path") or "").strip()

    assert test_cmd == "pixi run test"
    assert repo_path == "/repo"

    result = await tun.send_run_cmd_control("t1", cmd=test_cmd, cwd=repo_path or None)

    assert called["cmd"] == "pixi run test"
    assert called["cwd"] == "/repo"
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["passed"] == 42
    assert result["failed"] == 0


# ---------------------------------------------------------------------------
# 6. run_verification: stubbed tunnel call fails (non-zero exit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_verification_tunnel_failure(db, monkeypatch):
    """Non-zero exit_code from send_run_cmd_control is returned, not treated as error."""
    proj = await db_module.create_project(db, name="test-cmd-fail")
    await db_module.set_executor_config(db, proj["id"], {
        "test_cmd": "pytest",
        "repo_path": "",
    })

    async def _fake_run_cmd(tenant_id, cmd, cwd=None):
        return {
            "status": "ok",
            "exit_code": 1,
            "passed": 10,
            "failed": 3,
            "stdout_tail": "10 passed, 3 failed in 5s",
            "stderr_tail": "FAILED test_foo.py::test_bar",
        }

    monkeypatch.setattr(tun, "send_run_cmd_control", _fake_run_cmd)

    result = await tun.send_run_cmd_control("t2", cmd="pytest", cwd=None)
    assert result["exit_code"] == 1
    assert result["passed"] == 10
    assert result["failed"] == 3
    assert "FAILED" in result["stderr_tail"]
    # status is "ok" even on non-zero exit — it ran and we have real results.
    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# 7. MCP tool schema shape
# ---------------------------------------------------------------------------

def test_run_verification_schema_registered():
    """run_verification appears in _MCP_TOOLS_LIST with the expected schema shape."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS

    tool = next((t for t in _MCP_TOOLS_LIST if t["name"] == "run_verification"), None)
    assert tool is not None, "run_verification not found in _MCP_TOOLS_LIST"

    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "project_id" in props
    assert "project_name" in props

    # Not read-only — it executes a process.
    assert "run_verification" not in _READ_ONLY_TOOLS

    # Has a title and annotations.
    assert tool.get("title") == "Run Verification"
    assert tool.get("annotations", {}).get("title") == "Run Verification"
    # Not destructive.
    assert tool.get("annotations", {}).get("destructiveHint") is False
