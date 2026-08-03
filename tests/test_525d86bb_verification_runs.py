"""525d86bb — record synchronous verification runs and preserve real exit status.

Coverage:
  1. db.verification_runs — durable lifecycle record: create (status='running'),
     complete (real exit_code/status/log artifact), get, list.
  2. complete_verification_run rejects missing evidence (unknown run id),
     rejects double-completion, rejects ambiguous evidence (status='ok' with
     no real int exit_code, or a bool masquerading as one), and accepts an
     honest nonzero exit / a genuinely exit_code-less non-'ok' status.
  3. run_verification (meridian/mcp/handler.py) wiring: persists a run BEFORE
     dispatch and completes it with the REAL send_run_cmd_control result
     right after the one synchronous wait — never creates a row for a
     dispatch that never happened (not_configured).
  4. The Windows shell-string exit-code-masking root cause fix in
     meridian.tunnel_client._shell_subprocess_env — the actual mechanism
     behind test_run_verification.py::test_handle_run_cmd_shell_string now
     passing for real.
"""
from __future__ import annotations

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import db as db_module
from meridian import tunnel_client as tc
from meridian.mcp import handler as mh
from meridian.routes import tunnel as tun


# ---------------------------------------------------------------------------
# db.verification_runs — durable lifecycle record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_verification_run_persists_running_row(db):
    proj = await db_module.create_project(db, name="vr-create")
    run = await db_module.create_verification_run(
        db, proj["id"], "pixi run test",
        cwd="/repo", worktree="/repo/.worktrees/x", actor="sess-1",
    )
    assert run["status"] == "running"
    assert run["command"] == "pixi run test"
    assert run["cwd"] == "/repo"
    assert run["worktree"] == "/repo/.worktrees/x"
    assert run["actor"] == "sess-1"
    assert run["started_at"]
    assert run["ended_at"] is None
    assert run["exit_code"] is None

    fetched = await db_module.get_verification_run(db, run["id"])
    assert fetched == run


@pytest.mark.asyncio
async def test_get_verification_run_unknown_id_returns_none(db):
    assert await db_module.get_verification_run(db, "does-not-exist") is None


@pytest.mark.asyncio
async def test_complete_verification_run_ok_stamps_real_result(db):
    proj = await db_module.create_project(db, name="vr-complete-ok")
    run = await db_module.create_verification_run(db, proj["id"], "pytest")
    completed = await db_module.complete_verification_run(
        db, run["id"], status="ok", exit_code=0, passed=42, failed=0,
        stdout_tail="42 passed", stderr_tail="",
    )
    assert completed["status"] == "ok"
    assert completed["exit_code"] == 0
    assert completed["passed"] == 42
    assert completed["failed"] == 0
    assert completed["stdout_tail"] == "42 passed"
    assert completed["ended_at"] is not None


@pytest.mark.asyncio
async def test_complete_verification_run_nonzero_exit_is_honest_not_rejected(db):
    """A real nonzero exit is legitimate evidence, not ambiguous — must be accepted."""
    proj = await db_module.create_project(db, name="vr-complete-nonzero")
    run = await db_module.create_verification_run(db, proj["id"], "pytest")
    completed = await db_module.complete_verification_run(
        db, run["id"], status="ok", exit_code=1, failed=3,
    )
    assert completed["exit_code"] == 1
    assert completed["status"] == "ok"


@pytest.mark.asyncio
async def test_complete_verification_run_non_ok_status_allows_none_exit_code(db):
    """timeout/error/not_connected are honest 'nothing to report' statuses —
    exit_code=None is correct for them, not ambiguous."""
    proj = await db_module.create_project(db, name="vr-timeout")
    run = await db_module.create_verification_run(db, proj["id"], "pytest")
    completed = await db_module.complete_verification_run(
        db, run["id"], status="timeout", exit_code=None,
        message="timed out after 300s",
    )
    assert completed["status"] == "timeout"
    assert completed["exit_code"] is None
    assert completed["message"] == "timed out after 300s"


@pytest.mark.asyncio
async def test_complete_verification_run_rejects_missing_run(db):
    with pytest.raises(ValueError, match="not found"):
        await db_module.complete_verification_run(
            db, "does-not-exist", status="ok", exit_code=0,
        )


@pytest.mark.asyncio
async def test_complete_verification_run_rejects_double_completion(db):
    proj = await db_module.create_project(db, name="vr-double")
    run = await db_module.create_verification_run(db, proj["id"], "pytest")
    await db_module.complete_verification_run(db, run["id"], status="ok", exit_code=0)
    with pytest.raises(ValueError, match="already completed"):
        await db_module.complete_verification_run(db, run["id"], status="ok", exit_code=1)


@pytest.mark.asyncio
async def test_complete_verification_run_rejects_ambiguous_ok_with_no_exit_code(db):
    proj = await db_module.create_project(db, name="vr-ambiguous")
    run = await db_module.create_verification_run(db, proj["id"], "pytest")
    with pytest.raises(ValueError, match="[Aa]mbiguous"):
        await db_module.complete_verification_run(db, run["id"], status="ok", exit_code=None)


@pytest.mark.asyncio
async def test_complete_verification_run_rejects_bool_exit_code(db):
    proj = await db_module.create_project(db, name="vr-bool")
    run = await db_module.create_verification_run(db, proj["id"], "pytest")
    with pytest.raises(ValueError, match="bool"):
        await db_module.complete_verification_run(db, run["id"], status="ok", exit_code=True)


@pytest.mark.asyncio
async def test_complete_verification_run_rejects_invalid_status(db):
    proj = await db_module.create_project(db, name="vr-invalid-status")
    run = await db_module.create_verification_run(db, proj["id"], "pytest")
    with pytest.raises(ValueError, match="Invalid verification-run status"):
        await db_module.complete_verification_run(db, run["id"], status="bogus")


@pytest.mark.asyncio
async def test_list_verification_runs_returns_all_and_filters_by_status(db):
    # NOTE: rows created back-to-back can share the same second-resolution
    # started_at timestamp (SQLite's datetime('now') has no sub-second
    # precision), so — mirroring the same set-based assertion style
    # test_2a654cb0_wave_runs.py uses for this exact reason — this checks
    # membership/filtering rather than asserting a specific tie-break order.
    proj = await db_module.create_project(db, name="vr-list")
    r1 = await db_module.create_verification_run(db, proj["id"], "cmd-1")
    await db_module.complete_verification_run(db, r1["id"], status="ok", exit_code=0)
    r2 = await db_module.create_verification_run(db, proj["id"], "cmd-2")
    await db_module.complete_verification_run(
        db, r2["id"], status="error", exit_code=None, message="boom",
    )

    all_runs = await db_module.list_verification_runs(db, proj["id"])
    assert {r["id"] for r in all_runs} == {r1["id"], r2["id"]}

    ok_only = await db_module.list_verification_runs(db, proj["id"], status="ok")
    assert [r["id"] for r in ok_only] == [r1["id"]]

    error_only = await db_module.list_verification_runs(db, proj["id"], status="error")
    assert [r["id"] for r in error_only] == [r2["id"]]


# ---------------------------------------------------------------------------
# run_verification handler wiring — persists BEFORE dispatch, completes with
# the ONE real synchronous send_run_cmd_control result (no detached monitor).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_verification_handler_persists_and_completes_run(db, monkeypatch):
    proj = await db_module.create_project(db, name="vr-handler-ok")
    await db_module.set_executor_config(db, proj["id"], {
        "test_cmd": "pixi run test", "repo_path": "/repo",
    })

    async def _fake_run_cmd(tenant_id, cmd, cwd=None):
        return {
            "status": "ok", "exit_code": 0, "passed": 5, "failed": 0,
            "stdout_tail": "5 passed", "stderr_tail": "",
        }

    monkeypatch.setattr(tun, "send_run_cmd_control", _fake_run_cmd)
    monkeypatch.setattr(tun, "has_active_tunnel", lambda tid: True)

    result = await mh._dispatch_mcp_tool(
        "run_verification", {"project_id": proj["id"]}, db, "/tmp",
        tenant={"id": "tenant-vr-ok"},
    )

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    run_id = result["verification_run_id"]
    assert run_id

    persisted = await db_module.get_verification_run(db, run_id)
    assert persisted["status"] == "ok"
    assert persisted["exit_code"] == 0
    assert persisted["command"] == "pixi run test"
    assert persisted["cwd"] == "/repo"
    assert persisted["actor"] == "tenant-vr-ok"
    assert persisted["started_at"] is not None
    assert persisted["ended_at"] is not None


@pytest.mark.asyncio
async def test_run_verification_handler_persists_nonzero_exit_honestly(db, monkeypatch):
    proj = await db_module.create_project(db, name="vr-handler-fail")
    await db_module.set_executor_config(db, proj["id"], {"test_cmd": "pytest"})

    async def _fake_run_cmd(tenant_id, cmd, cwd=None):
        return {
            "status": "ok", "exit_code": 1, "passed": 2, "failed": 1,
            "stdout_tail": "", "stderr_tail": "FAILED test_x",
        }

    monkeypatch.setattr(tun, "send_run_cmd_control", _fake_run_cmd)

    result = await mh._dispatch_mcp_tool(
        "run_verification", {"project_id": proj["id"]}, db, "/tmp",
        tenant={"id": "tenant-vr-fail"},
    )
    assert result["exit_code"] == 1
    persisted = await db_module.get_verification_run(db, result["verification_run_id"])
    assert persisted["exit_code"] == 1
    assert persisted["status"] == "ok"
    assert persisted["stderr_tail"] == "FAILED test_x"


@pytest.mark.asyncio
async def test_run_verification_handler_not_configured_creates_no_run_record(db):
    """No test_cmd configured -> nothing was ever dispatched -> no lifecycle row."""
    proj = await db_module.create_project(db, name="vr-handler-not-configured")
    result = await mh._dispatch_mcp_tool(
        "run_verification", {"project_id": proj["id"]}, db, "/tmp",
        tenant={"id": "tenant-vr-nc"},
    )
    assert result["status"] == "not_configured"
    assert "verification_run_id" not in result
    runs = await db_module.list_verification_runs(db, proj["id"])
    assert runs == []


# ---------------------------------------------------------------------------
# Windows shell-string exit-code masking root cause fix
# (meridian.tunnel_client._shell_subprocess_env)
# ---------------------------------------------------------------------------

def test_shell_subprocess_env_non_windows_returns_none(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "linux")
    assert tc._shell_subprocess_env() is None


def test_shell_subprocess_env_windows_prepends_interpreter_dir(monkeypatch):
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.setattr(tc.os, "environ", {"PATH": r"C:\Windows\System32"})
    env = tc._shell_subprocess_env()
    assert env is not None
    py_dir = tc.os.path.dirname(tc.sys.executable)
    assert env["PATH"].split(tc.os.pathsep)[0] == py_dir
    assert r"C:\Windows\System32" in env["PATH"]


def test_shell_subprocess_env_windows_no_duplicate_when_already_present(monkeypatch):
    py_dir = tc.os.path.dirname(tc.sys.executable)
    monkeypatch.setattr(tc.sys, "platform", "win32")
    monkeypatch.setattr(tc.os, "environ", {"PATH": py_dir + tc.os.pathsep + r"C:\Windows"})
    env = tc._shell_subprocess_env()
    assert env["PATH"].count(py_dir) == 1


@pytest.mark.asyncio
async def test_handle_run_cmd_shell_string_nonzero_exit_also_faithful(monkeypatch):
    """Companion to the required test_handle_run_cmd_shell_string: a shell
    string that genuinely fails must still report ITS real nonzero exit code
    (not conflated with the previously-masked "wrapper couldn't even find the
    program" failure mode this item fixes)."""
    result = await tc._handle_run_cmd(
        'python -c "import sys; sys.exit(3)"',
        cwd=None,
    )
    assert result["status"] == "ok"
    assert result["exit_code"] == 3
