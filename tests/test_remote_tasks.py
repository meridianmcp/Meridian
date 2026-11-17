"""Tests for sprint item 32d3d5de — W1-E Durable Remote Task primitive v1.

Covers:
  * meridian.remote_exec — pure validation + script-building/parsing.
  * meridian.db.remote_tasks — start_remote_task / get_remote_task_status /
    list_remote_tasks, exercised against the real ``db`` fixture with a
    MOCKED ssh_runner (no real SSH server involved, per this sprint item's
    own instructions) covering the full status state machine: running,
    completed-success, completed-failure, terminated-unexpectedly,
    connection-lost-but-possibly-still-running, and unknown.

No real network/subprocess call is ever made in this file — every SSH call
goes through a fake ``ssh_runner`` matching
:class:`meridian.remote_ssh.SSHRunner`'s protocol.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

import meridian.server  # noqa: F401 -- initialise server before handler imports (see test_1365e01a)
from meridian import db as db_module
from meridian import remote_exec
from meridian import remote_ssh
from meridian.db import remote_tasks
from meridian.mcp.handlers import remote_task_tools
from meridian.remote_ssh import SSHResult


async def _setup(db, project_prefix):
    project = await db_module.create_project(db, project_prefix)
    session = await db_module.register_session(db, project["id"], f"{project_prefix}-session")
    return project, session


def _connected(stdout="", returncode=0, stderr=""):
    async def runner(host, remote_command, *, timeout):
        return SSHResult(connected=True, returncode=returncode, stdout=stdout, stderr=stderr)
    return runner


def _disconnected(error="timeout"):
    async def runner(host, remote_command, *, timeout):
        return SSHResult(connected=False, returncode=None, stdout="", stderr="", error=error)
    return runner


_TEST_NONCE = "0123456789abcdef"


def _status_stdout(
    marker, exit_code=None, log_tail="hello from remote\n", stderr_tail="", pid=None,
    nonce=_TEST_NONCE,
):
    """Build a status-check output the way build_status_check_script's remote
    shell emits it: header lines, the nonce line, then one nonce-framed block
    per log file."""
    lines = [f"MERIDIAN_STATUS={marker}"]
    if exit_code is not None:
        lines.append(f"MERIDIAN_EXIT_CODE={exit_code}")
    if pid is not None:
        lines.append(f"MERIDIAN_PID={pid}")
    lines.append(f"MERIDIAN_NONCE={nonce}")
    lines.append(f"MERIDIAN_STDOUT_BEGIN_{nonce}")
    lines.append(log_tail.rstrip("\n"))
    lines.append(f"MERIDIAN_STDOUT_END_{nonce}")
    lines.append(f"MERIDIAN_STDERR_BEGIN_{nonce}")
    lines.append(stderr_tail.rstrip("\n"))
    lines.append(f"MERIDIAN_STDERR_END_{nonce}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# meridian.remote_exec — pure validation.
# ---------------------------------------------------------------------------


def test_validate_status_accepts_all_and_rejects_unknown():
    for status in remote_exec.REMOTE_TASK_STATUSES:
        assert remote_exec.validate_status(status) == status
    with pytest.raises(remote_exec.RemoteExecError, match="must be one of"):
        remote_exec.validate_status("bogus")


def test_terminal_statuses_are_a_subset_of_all_statuses():
    assert remote_exec.REMOTE_TASK_TERMINAL_STATUSES <= remote_exec.REMOTE_TASK_STATUSES
    # Deliberately non-terminal: still-actionable states.
    assert "running" not in remote_exec.REMOTE_TASK_TERMINAL_STATUSES
    assert "unknown" not in remote_exec.REMOTE_TASK_TERMINAL_STATUSES
    assert "connection-lost-but-possibly-still-running" not in remote_exec.REMOTE_TASK_TERMINAL_STATUSES


def test_validate_host_rejects_empty_and_leading_dash():
    with pytest.raises(remote_exec.RemoteExecError, match="required"):
        remote_exec.validate_host("")
    with pytest.raises(remote_exec.RemoteExecError, match="ssh option"):
        remote_exec.validate_host("-oProxyCommand=evil")


def test_validate_host_rejects_newlines_and_non_string():
    with pytest.raises(remote_exec.RemoteExecError, match="newlines"):
        remote_exec.validate_host("gpu-pod\nrm -rf /")
    with pytest.raises(remote_exec.RemoteExecError, match="must be a string"):
        remote_exec.validate_host(123)


def test_validate_host_accepts_user_at_host_and_alias():
    assert remote_exec.validate_host("ubuntu@gpu-pod-1") == "ubuntu@gpu-pod-1"
    assert remote_exec.validate_host("my-gpu-alias") == "my-gpu-alias"


def test_validate_command_rejects_empty_and_non_string():
    with pytest.raises(remote_exec.RemoteExecError, match="required"):
        remote_exec.validate_command("   ")
    with pytest.raises(remote_exec.RemoteExecError, match="must be a string"):
        remote_exec.validate_command(None)


def test_validate_command_rejects_secret_shaped_input():
    with pytest.raises(ValueError, match="secret"):
        remote_exec.validate_command(
            "export TOKEN=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA && python train.py"
        )


def test_validate_ttl_seconds_none_stays_none():
    assert remote_exec.validate_ttl_seconds(None) is None


def test_validate_ttl_seconds_bounds():
    assert remote_exec.validate_ttl_seconds(60) == 60
    with pytest.raises(remote_exec.RemoteExecError, match="between"):
        remote_exec.validate_ttl_seconds(1)
    with pytest.raises(remote_exec.RemoteExecError, match="between"):
        remote_exec.validate_ttl_seconds(60 * 60 * 24 * 31)
    with pytest.raises(remote_exec.RemoteExecError, match="not a bool"):
        remote_exec.validate_ttl_seconds(True)


def test_validate_start_fields_bundles_all_three():
    out = remote_exec.validate_start_fields(host="gpu-1", command="python train.py", ttl_seconds=120)
    assert out == {"host": "gpu-1", "command": "python train.py", "ttl_seconds": 120}


def test_truncate_log_tail_keeps_last_bytes_only():
    big = "x" * (remote_exec.MAX_LOG_TAIL_BYTES + 500)
    out = remote_exec.truncate_log_tail(big)
    assert len(out.encode("utf-8")) <= remote_exec.MAX_LOG_TAIL_BYTES
    assert out == "x" * remote_exec.MAX_LOG_TAIL_BYTES
    assert remote_exec.truncate_log_tail("") == ""
    assert remote_exec.truncate_log_tail(None) == ""  # type: ignore[arg-type]


def test_build_job_dir_rejects_non_uuid_job_id():
    with pytest.raises(remote_exec.RemoteExecError, match="UUID4"):
        remote_exec.build_job_dir("not-a-uuid")


def test_build_launch_script_backgrounds_with_nohup_and_setsid_and_echoes_pid():
    job_id = "11111111-2222-4333-8444-555555555555"
    script = remote_exec.build_launch_script(job_id, "python train.py")
    assert "nohup" in script
    assert "setsid" in script
    assert "mkdir -p" in script
    assert job_id in script
    assert "tee pid.txt" in script
    assert "exit_code.txt" in script


def test_build_launch_script_quotes_the_command_argument():
    job_id = "11111111-2222-4333-8444-555555555555"
    dangerous = "echo hi; rm -rf ~"
    script = remote_exec.build_launch_script(job_id, dangerous)
    # The dangerous command must appear only inside a single shlex-quoted
    # argument to bash -c, never spliced unquoted into the outer script.
    assert "bash -c '" in script or "bash -c \"" in script


def test_build_status_check_script_covers_all_branches():
    job_id = "11111111-2222-4333-8444-555555555555"
    script = remote_exec.build_status_check_script(job_id)
    assert "exit_code.txt" in script
    assert "kill -0" in script
    assert "MERIDIAN_STATUS=NODIR" in script
    assert "MERIDIAN_STATUS=NOPID" in script


def test_parse_launch_pid_extracts_trailing_digits():
    assert remote_exec.parse_launch_pid("12345\n") == 12345
    assert remote_exec.parse_launch_pid("\n\n  67890  \n") == 67890
    assert remote_exec.parse_launch_pid("no digits here") is None
    assert remote_exec.parse_launch_pid(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "marker,exit_code,expected",
    [
        ("DONE", 0, "completed-success"),
        ("DONE", 1, "completed-failure"),
        ("DONE", None, "unknown"),
        ("RUNNING", None, "running"),
        ("DIED", None, "terminated-unexpectedly"),
        ("NOPID", None, "unknown"),
        ("NODIR", None, "unknown"),
        ("garbage", None, "unknown"),
    ],
)
def test_map_check_marker_to_status(marker, exit_code, expected):
    assert remote_exec.map_check_marker_to_status(marker, exit_code) == expected


def test_parse_status_check_output_round_trips_marker_exit_code_and_log():
    stdout = _status_stdout("DONE", exit_code=0, log_tail="line one\nline two")
    parsed = remote_exec.parse_status_check_output(stdout)
    assert parsed["marker"] == "DONE"
    assert parsed["exit_code"] == 0
    assert "line one" in parsed["stdout_tail"]
    assert "line two" in parsed["stdout_tail"]


def test_parse_status_check_output_handles_missing_marker_gracefully():
    parsed = remote_exec.parse_status_check_output("garbage, no markers at all")
    assert parsed["marker"] == "NOPID"
    assert parsed["exit_code"] is None
    assert parsed["pid"] is None
    assert parsed["stdout_tail"] == ""
    assert parsed["stderr_tail"] == ""


def test_validate_status_accepts_launch_unconfirmed_as_non_terminal():
    assert remote_exec.validate_status("launch_unconfirmed") == "launch_unconfirmed"
    assert "launch_unconfirmed" not in remote_exec.REMOTE_TASK_TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# meridian.db.remote_tasks.start_remote_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_remote_task_success_persists_running_with_pid(db):
    project, session = await _setup(db, "rt-start-1")
    result = await remote_tasks.start_remote_task(
        db, project["id"], session["id"],
        host="gpu-pod-1", command="python train.py",
        ssh_runner=_connected(stdout="54321\n"),
    )
    job = result["job"]
    assert job["status"] == "running"
    assert job["pid"] == 54321
    assert job["host"] == "gpu-pod-1"
    assert job["command"] == "python train.py"
    assert job["project_id"] == project["id"]
    assert job["session_id"] == session["id"]
    assert result["launch"]["connected"] is True
    assert result["launch"]["returncode"] == 0


@pytest.mark.asyncio
async def test_start_remote_task_returns_quickly_regardless_of_job_duration(db):
    """The launch call's OWN latency must not depend on the underlying job —
    this asserts the launch path never awaits anything beyond one SSH round
    trip (the fake runner returns instantly; a real long-running job on the
    remote side plays no part in this call)."""
    project, session = await _setup(db, "rt-start-2")
    calls = []

    async def runner(host, remote_command, *, timeout):
        calls.append((host, remote_command, timeout))
        return SSHResult(connected=True, returncode=0, stdout="99\n", stderr="")

    result = await remote_tasks.start_remote_task(
        db, project["id"], session["id"],
        host="gpu-pod-1", command="sleep 999999",
        ssh_runner=runner,
    )
    assert len(calls) == 1  # exactly one short-lived launch call, nothing more
    assert result["job"]["status"] == "running"


@pytest.mark.asyncio
async def test_start_remote_task_nonzero_launch_exit_is_terminated_unexpectedly(db):
    """SSH itself connects fine but the launch script exits nonzero (e.g. a
    read-only remote $HOME) — the job never actually started."""
    project, session = await _setup(db, "rt-start-3")
    result = await remote_tasks.start_remote_task(
        db, project["id"], session["id"],
        host="gpu-pod-1", command="python train.py",
        ssh_runner=_connected(stdout="", returncode=1, stderr="mkdir: permission denied"),
    )
    job = result["job"]
    assert job["status"] == "terminated-unexpectedly"
    assert job["pid"] is None
    assert job["log_path"] is None


@pytest.mark.asyncio
async def test_start_remote_task_connection_failure_is_unknown(db):
    """Cannot even establish the launching connection: genuinely unknown
    whether anything ran. (A local timeout is NOT this case -- see
    test_timed_out_launch_is_recorded_launch_unconfirmed_with_job_id.)"""
    project, session = await _setup(db, "rt-start-4")
    result = await remote_tasks.start_remote_task(
        db, project["id"], session["id"],
        host="unreachable-host", command="python train.py",
        ssh_runner=_disconnected(error="ssh_connect_failed"),
    )
    job = result["job"]
    assert job["status"] == "unknown"
    assert job["pid"] is None
    assert job["log_path"] is None
    assert result["launch"]["connected"] is False
    assert result["launch"]["error"] == "ssh_connect_failed"


@pytest.mark.asyncio
async def test_start_remote_task_validates_host_before_any_ssh_call(db):
    project, session = await _setup(db, "rt-start-5")
    calls = []

    async def runner(host, remote_command, *, timeout):
        calls.append(1)
        return SSHResult(connected=True, returncode=0, stdout="1\n", stderr="")

    with pytest.raises(remote_exec.RemoteExecError):
        await remote_tasks.start_remote_task(
            db, project["id"], session["id"],
            host="-oProxyCommand=evil", command="python train.py",
            ssh_runner=runner,
        )
    assert calls == []  # never even attempted the SSH call


@pytest.mark.asyncio
async def test_start_remote_task_requires_valid_session(db):
    project = await db_module.create_project(db, "rt-start-6")
    with pytest.raises(ValueError, match="session"):
        await remote_tasks.start_remote_task(
            db, project["id"], "not-a-real-session",
            host="gpu-1", command="python train.py",
            ssh_runner=_connected(stdout="1\n"),
        )


@pytest.mark.asyncio
async def test_start_remote_task_records_optional_sprint_item_id(db):
    project, session = await _setup(db, "rt-start-7")
    result = await remote_tasks.start_remote_task(
        db, project["id"], session["id"],
        host="gpu-1", command="python train.py",
        sprint_item_id="32d3d5de-5808-4168-9a86-6038d994e7c4",
        ssh_runner=_connected(stdout="1\n"),
    )
    assert result["job"]["sprint_item_id"] == "32d3d5de-5808-4168-9a86-6038d994e7c4"


# ---------------------------------------------------------------------------
# meridian.db.remote_tasks.get_remote_task_status — the full state machine.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_roundtrip_start_then_check_running(db):
    project, session = await _setup(db, "rt-status-roundtrip")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"],
        host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="777\n"),
    )
    job_id = started["job"]["id"]

    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_status_stdout("RUNNING")),
    )
    assert checked["job"]["status"] == "running"
    assert checked["connected"] is True
    assert "hello from remote" in checked["job"]["log_tail"]
    assert checked["elapsed_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_status_outcome_completed_success(db):
    project, session = await _setup(db, "rt-status-1")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_connected(stdout=_status_stdout("DONE", exit_code=0)),
    )
    assert checked["job"]["status"] == "completed-success"
    assert checked["job"]["completed_at"] is not None


@pytest.mark.asyncio
async def test_status_outcome_completed_failure(db):
    project, session = await _setup(db, "rt-status-2")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_connected(stdout=_status_stdout("DONE", exit_code=137)),
    )
    assert checked["job"]["status"] == "completed-failure"
    assert checked["job"]["completed_at"] is not None


@pytest.mark.asyncio
async def test_status_outcome_terminated_unexpectedly(db):
    """PID recorded but gone, no exit_code.txt — most often OOM-kill."""
    project, session = await _setup(db, "rt-status-3")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_connected(stdout=_status_stdout("DIED")),
    )
    assert checked["job"]["status"] == "terminated-unexpectedly"
    assert checked["job"]["completed_at"] is not None


@pytest.mark.asyncio
async def test_status_outcome_connection_lost_but_possibly_still_running(db):
    project, session = await _setup(db, "rt-status-4")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_disconnected(error="ssh_connect_failed"),
    )
    assert checked["job"]["status"] == "connection-lost-but-possibly-still-running"
    assert checked["connected"] is False
    assert checked["ssh_error"] == "ssh_connect_failed"
    assert checked["job"]["completed_at"] is None


@pytest.mark.asyncio
async def test_status_outcome_unknown_when_no_pid_or_job_dir(db):
    project, session = await _setup(db, "rt-status-5")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_connected(stdout=_status_stdout("NODIR")),
    )
    assert checked["job"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_connection_lost_never_downgrades_an_already_terminal_job(db):
    """A completed job doesn't become 'connection lost' just because a LATER
    check can't reach the host."""
    project, session = await _setup(db, "rt-status-6")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    job_id = started["job"]["id"]
    completed = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_status_stdout("DONE", exit_code=0)),
    )
    assert completed["job"]["status"] == "completed-success"

    rechecked = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_disconnected(error="timeout"),
    )
    assert rechecked["job"]["status"] == "completed-success"  # unchanged, not downgraded


@pytest.mark.asyncio
async def test_get_remote_task_status_raises_for_unknown_job(db):
    project = await db_module.create_project(db, "rt-status-7")
    with pytest.raises(ValueError, match="not found"):
        await remote_tasks.get_remote_task_status(
            db, project["id"], "00000000-0000-4000-8000-000000000000",
            ssh_runner=_connected(stdout=_status_stdout("RUNNING")),
        )


@pytest.mark.asyncio
async def test_get_remote_task_status_scoped_to_project(db):
    """A job belonging to a different project must not resolve."""
    p1, s1 = await _setup(db, "rt-status-8a")
    p2, _ = await _setup(db, "rt-status-8b")
    started = await remote_tasks.start_remote_task(
        db, p1["id"], s1["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    with pytest.raises(ValueError, match="not found"):
        await remote_tasks.get_remote_task_status(
            db, p2["id"], started["job"]["id"],
            ssh_runner=_connected(stdout=_status_stdout("RUNNING")),
        )


# ---------------------------------------------------------------------------
# meridian.db.remote_tasks.list_remote_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_remote_tasks_excludes_terminal_by_default(db):
    project, session = await _setup(db, "rt-list-1")
    running = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="a",
        ssh_runner=_connected(stdout="1\n"),
    )
    done = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-2", command="b",
        ssh_runner=_connected(stdout="2\n"),
    )
    await remote_tasks.get_remote_task_status(
        db, project["id"], done["job"]["id"],
        ssh_runner=_connected(stdout=_status_stdout("DONE", exit_code=0)),
    )

    active_only = await remote_tasks.list_remote_tasks(db, project["id"])
    ids = {job["id"] for job in active_only}
    assert running["job"]["id"] in ids
    assert done["job"]["id"] not in ids

    everything = await remote_tasks.list_remote_tasks(db, project["id"], include_terminal=True)
    all_ids = {job["id"] for job in everything}
    assert running["job"]["id"] in all_ids
    assert done["job"]["id"] in all_ids


@pytest.mark.asyncio
async def test_list_remote_tasks_filters_by_session(db):
    project = await db_module.create_project(db, "rt-list-2")
    session_a = await db_module.register_session(db, project["id"], "session-a")
    session_b = await db_module.register_session(db, project["id"], "session-b")
    job_a = await remote_tasks.start_remote_task(
        db, project["id"], session_a["id"], host="gpu-1", command="a",
        ssh_runner=_connected(stdout="1\n"),
    )
    await remote_tasks.start_remote_task(
        db, project["id"], session_b["id"], host="gpu-2", command="b",
        ssh_runner=_connected(stdout="2\n"),
    )

    only_a = await remote_tasks.list_remote_tasks(db, project["id"], session_id=session_a["id"])
    assert {job["id"] for job in only_a} == {job_a["job"]["id"]}


@pytest.mark.asyncio
async def test_list_remote_tasks_limit_is_clamped(db):
    project, session = await _setup(db, "rt-list-3")
    for i in range(3):
        await remote_tasks.start_remote_task(
            db, project["id"], session["id"], host=f"gpu-{i}", command="a",
            ssh_runner=_connected(stdout=f"{i}\n"),
        )
    limited = await remote_tasks.list_remote_tasks(db, project["id"], limit=1)
    assert len(limited) == 1
    # A silly/negative limit is clamped, never raises.
    clamped = await remote_tasks.list_remote_tasks(db, project["id"], limit=0)
    assert len(clamped) >= 1


# ---------------------------------------------------------------------------
# Launch detachment: the outer job must not hold the SSH channel open.
# ---------------------------------------------------------------------------


def test_build_launch_script_detaches_outer_job_from_ssh_channel():
    """The backgrounded nohup/setsid job must have stdin and stdout pointed
    away from the SSH channel and stderr pointed at a file, or sshd keeps the
    launching ssh call open for the whole job. Its stderr goes to launch.err,
    not /dev/null, so a launch failure keeps its diagnostics. The inner command
    keeps its own log redirection."""
    job_id = "11111111-2222-4333-8444-555555555555"
    script = remote_exec.build_launch_script(job_id, "python train.py")
    outer = "< /dev/null > /dev/null 2> launch.err &"
    assert script.count(outer) == 1
    assert "2>&1" not in script
    assert script.index(outer) > script.index("bash -c")
    assert "> stdout.log 2> stderr.log < /dev/null" in script


def test_build_launch_script_runs_command_in_a_subshell_so_exit_still_records():
    """A bare ``exit N`` in the command must end only the subshell, so the exit
    status record still runs. The newline before ``)`` stops a trailing shell
    comment from swallowing the closing paren."""
    job_id = "11111111-2222-4333-8444-555555555555"
    script = remote_exec.build_launch_script(job_id, "python train.py  # note")
    assert "( python train.py  # note\n) > stdout.log" in script
    assert "echo $? > exit_code.txt" in script


@pytest.mark.asyncio
async def test_run_ssh_command_passes_stdin_devnull(monkeypatch):
    """ssh must never inherit this process's stdin (the stdio MCP channel)."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="1\n", stderr="")

    monkeypatch.setattr(remote_ssh.subprocess, "run", fake_run)
    result = await remote_ssh.run_ssh_command("gpu-1", "echo hi", timeout=5)
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert seen["argv"][0] == "ssh"
    assert "gpu-1" in seen["argv"]
    assert result.connected is True
    assert result.stdout == "1\n"


@pytest.mark.asyncio
async def test_run_ssh_command_local_timeout_maps_to_timeout_error(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(remote_ssh.subprocess, "run", fake_run)
    result = await remote_ssh.run_ssh_command("gpu-1", "sleep 99", timeout=1)
    assert result.connected is False
    assert result.error == remote_ssh.SSH_TIMEOUT_ERROR


# ---------------------------------------------------------------------------
# launch_unconfirmed: a timed-out launch keeps its job_id and is confirmed later.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timed_out_launch_is_recorded_launch_unconfirmed_with_job_id(db):
    project, session = await _setup(db, "rt-launch-unconfirmed")
    result = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_disconnected(error=remote_ssh.SSH_TIMEOUT_ERROR),
    )
    job = result["job"]
    assert job["status"] == "launch_unconfirmed"
    assert job["id"]  # the job_id is recorded, not dropped
    assert job["pid"] is None
    assert job["log_path"] == f"{remote_exec.build_job_dir(job['id'])}/stdout.log"
    assert result["launch"]["connected"] is False
    assert result["launch"]["error"] == remote_ssh.SSH_TIMEOUT_ERROR
    listed = await remote_tasks.list_remote_tasks(db, project["id"])
    assert job["id"] in {row["id"] for row in listed}  # non-terminal, shown by default


@pytest.mark.asyncio
async def test_status_confirms_launch_unconfirmed_and_persists_pid_and_log_path(db):
    project, session = await _setup(db, "rt-confirm-1")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_disconnected(error=remote_ssh.SSH_TIMEOUT_ERROR),
    )
    job_id = started["job"]["id"]
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_status_stdout("RUNNING", pid=4242)),
    )
    assert checked["job"]["status"] == "running"
    assert checked["job"]["pid"] == 4242
    assert checked["job"]["log_path"] == f"{remote_exec.build_job_dir(job_id)}/stdout.log"
    # Persisted, not just returned: a fresh read sees the same values.
    listed = await remote_tasks.list_remote_tasks(db, project["id"], include_terminal=True)
    row = next(item for item in listed if item["id"] == job_id)
    assert row["pid"] == 4242
    assert row["log_path"] == checked["job"]["log_path"]


# ---------------------------------------------------------------------------
# Status output: exit code, PID and tails are each read from the right place.
# ---------------------------------------------------------------------------


def test_build_status_check_script_tails_each_file_separately():
    job_id = "11111111-2222-4333-8444-555555555555"
    script = remote_exec.build_status_check_script(job_id, log_tail_bytes=4000)
    assert "tail -c 4000 stdout.log" in script
    assert "tail -c 4000 stderr.log" in script
    assert "stdout.log stderr.log" not in script
    assert "MERIDIAN_PID=" in script


def test_parse_status_check_output_reads_each_tail_from_its_own_file():
    """A large stderr must not push stdout out of the result. The old combined
    tail returned only stderr once stderr was about 4 KB."""
    big_stderr = "e" * (remote_exec.MAX_LOG_TAIL_BYTES + 1000)
    stdout = _status_stdout("DONE", exit_code=1, log_tail="hello-out", stderr_tail=big_stderr)
    parsed = remote_exec.parse_status_check_output(stdout)
    assert parsed["stdout_tail"] == "hello-out"
    assert parsed["stderr_tail"] == "e" * remote_exec.MAX_LOG_TAIL_BYTES
    assert parsed["exit_code"] == 1


def test_parse_status_check_output_reads_status_and_exit_code_from_header_only():
    """Job output is untrusted: a log line that looks like a status marker
    must never be taken as the real exit status or status."""
    stdout = _status_stdout(
        "DONE", exit_code=0, log_tail="MERIDIAN_EXIT_CODE=99\nMERIDIAN_STATUS=RUNNING",
    )
    parsed = remote_exec.parse_status_check_output(stdout)
    assert parsed["marker"] == "DONE"
    assert parsed["exit_code"] == 0


def test_parse_status_check_output_reads_pid_from_header():
    parsed = remote_exec.parse_status_check_output(_status_stdout("RUNNING", pid=4242))
    assert parsed["pid"] == 4242


@pytest.mark.asyncio
async def test_status_returns_stdout_and_stderr_tails_per_file(db):
    project, session = await _setup(db, "rt-tails-1")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_connected(stdout=_status_stdout(
            "DONE", exit_code=3, log_tail="out line", stderr_tail="e" * 6000,
        )),
    )
    assert checked["job"]["status"] == "completed-failure"
    assert checked["exit_code"] == 3
    assert checked["stdout_tail"] == "out line"
    assert checked["stderr_tail"] == "e" * remote_exec.MAX_LOG_TAIL_BYTES
    # The persisted log_tail is the tail of stdout.log, the file log_path names.
    assert checked["job"]["log_tail"] == "out line"


@pytest.mark.asyncio
async def test_disconnected_status_returns_empty_tails_and_no_exit_code(db):
    project, session = await _setup(db, "rt-tails-2")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_disconnected(error="ssh_connect_failed"),
    )
    assert checked["stdout_tail"] == ""
    assert checked["stderr_tail"] == ""
    assert checked["exit_code"] is None


# ---------------------------------------------------------------------------
# log_tail: stdout first, stderr as the fallback, and an empty read clears nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stderr_only_failure_persists_stderr_tail_as_log_tail(db):
    """A job that fails with its error only on stderr has an empty stdout tail.
    The stored log_tail must carry the stderr text, so the error survives a
    session restart. A fresh list_remote_tasks read stands in for the restart."""
    project, session = await _setup(db, "rt-logtail-1")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    job_id = started["job"]["id"]
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_status_stdout(
            "DONE", exit_code=1, log_tail="", stderr_tail="Traceback: model not found",
        )),
    )
    assert checked["job"]["status"] == "completed-failure"
    assert checked["job"]["log_tail"] == "Traceback: model not found"
    listed = await remote_tasks.list_remote_tasks(db, project["id"], include_terminal=True)
    row = next(item for item in listed if item["id"] == job_id)
    assert row["log_tail"] == "Traceback: model not found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "earlier_stdout,earlier_stderr",
    [("step 1 done", ""), ("", "warning: slow disk")],
)
async def test_empty_status_read_keeps_an_earlier_stored_tail(
    db, earlier_stdout, earlier_stderr,
):
    project, session = await _setup(db, "rt-logtail-2")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    job_id = started["job"]["id"]
    await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_status_stdout(
            "RUNNING", log_tail=earlier_stdout, stderr_tail=earlier_stderr,
        )),
    )
    empty = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_status_stdout("RUNNING", log_tail="", stderr_tail="")),
    )
    expected = earlier_stdout or earlier_stderr
    assert empty["job"]["log_tail"] == expected
    listed = await remote_tasks.list_remote_tasks(db, project["id"], include_terminal=True)
    row = next(item for item in listed if item["id"] == job_id)
    assert row["log_tail"] == expected


@pytest.mark.asyncio
async def test_stdout_tail_wins_over_stderr_when_both_are_present(db):
    project, session = await _setup(db, "rt-logtail-3")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    both = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_connected(stdout=_status_stdout(
            "DONE", exit_code=2, log_tail="out line", stderr_tail="err line",
        )),
    )
    assert both["job"]["log_tail"] == "out line"
    assert both["stderr_tail"] == "err line"

    # A stderr-only read first, then a stdout read: stdout replaces the fallback.
    other = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-2", command="python train.py",
        ssh_runner=_connected(stdout="2\n"),
    )
    other_id = other["job"]["id"]
    await remote_tasks.get_remote_task_status(
        db, project["id"], other_id,
        ssh_runner=_connected(stdout=_status_stdout(
            "RUNNING", log_tail="", stderr_tail="err first",
        )),
    )
    later = await remote_tasks.get_remote_task_status(
        db, project["id"], other_id,
        ssh_runner=_connected(stdout=_status_stdout("RUNNING", log_tail="out later")),
    )
    assert later["job"]["log_tail"] == "out later"


# ---------------------------------------------------------------------------
# Hosted guard on the three MCP handlers (same pattern as run_verification).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hosted_mode_refuses_all_three_remote_task_tools(db, monkeypatch):
    project, session = await _setup(db, "rt-hosted-1")
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: True)

    async def ssh_must_not_run(*args, **kwargs):
        raise AssertionError("hosted mode must refuse before any SSH call")

    monkeypatch.setattr(remote_ssh, "run_ssh_command", ssh_must_not_run)
    start_args = {
        "project_id": project["id"], "session_id": session["id"],
        "host": "gpu-1", "command": "python train.py",
    }
    started = await remote_task_tools.handle_start_remote_task(start_args, db, "", None, None)
    assert started["hosted"] is True
    assert "hosted Meridian" in started["error"]
    assert await remote_tasks.list_remote_tasks(db, project["id"], include_terminal=True) == []

    status = await remote_task_tools.handle_get_remote_task_status(
        {"project_id": project["id"], "job_id": "00000000-0000-4000-8000-000000000000"},
        db, "", None, None,
    )
    assert status["hosted"] is True
    assert "error" in status

    listed = await remote_task_tools.handle_list_remote_tasks(
        {"project_id": project["id"]}, db, "", None, None,
    )
    assert listed["hosted"] is True
    assert "error" in listed


@pytest.mark.asyncio
async def test_hosted_env_var_refuses_remote_task_start(db, monkeypatch):
    project, session = await _setup(db, "rt-hosted-2")
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")

    async def ssh_must_not_run(*args, **kwargs):
        raise AssertionError("hosted mode must refuse before any SSH call")

    monkeypatch.setattr(remote_ssh, "run_ssh_command", ssh_must_not_run)
    refused = await remote_task_tools.handle_start_remote_task(
        {
            "project_id": project["id"], "session_id": session["id"],
            "host": "gpu-1", "command": "python train.py",
        },
        db, "", None, None,
    )
    assert refused["hosted"] is True


@pytest.mark.asyncio
async def test_self_hosted_list_and_status_handlers_read_the_register(db, monkeypatch):
    project, session = await _setup(db, "rt-selfhosted-2")
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)

    async def fake_run(host, remote_command, *, timeout):
        return SSHResult(connected=True, returncode=0, stdout="4321\n", stderr="")

    monkeypatch.setattr(remote_ssh, "run_ssh_command", fake_run)
    started = await remote_task_tools.handle_start_remote_task(
        {
            "project_id": project["id"], "session_id": session["id"],
            "host": "gpu-1", "command": "python train.py",
        },
        db, "", None, None,
    )
    listed = await remote_task_tools.handle_list_remote_tasks(
        {"project_id": project["id"]}, db, "", None, None,
    )
    assert listed["count"] == 1
    assert listed["jobs"][0]["id"] == started["job"]["id"]
    missing = await remote_task_tools.handle_get_remote_task_status(
        {"project_id": project["id"], "job_id": "00000000-0000-4000-8000-000000000000"},
        db, "", None, None,
    )
    assert "not found" in missing["error"]


@pytest.mark.asyncio
async def test_self_hosted_handler_still_starts_the_task(db, monkeypatch):
    project, session = await _setup(db, "rt-selfhosted-1")
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.setattr("meridian.mcp.handler._hosted_mode", lambda: False)

    async def fake_run(host, remote_command, *, timeout):
        return SSHResult(connected=True, returncode=0, stdout="4321\n", stderr="")

    monkeypatch.setattr(remote_ssh, "run_ssh_command", fake_run)
    result = await remote_task_tools.handle_start_remote_task(
        {
            "project_id": project["id"], "session_id": session["id"],
            "host": "gpu-1", "command": "python train.py",
        },
        db, "", None, None,
    )
    assert result["job"]["status"] == "running"
    assert result["job"]["pid"] == 4321


# ---------------------------------------------------------------------------
# Nonce-framed log tails: job output cannot move the block boundaries.
# ---------------------------------------------------------------------------

_TEST_JOB_ID = "11111111-2222-4333-8444-555555555555"
# Job output carrying the OLD fixed marker strings, plus a marker framed with a
# different nonce. None of these may move a tail boundary.
_FORGED_STDOUT = (
    "before forged\n"
    "MERIDIAN_STDOUT_END\n"
    "MERIDIAN_STDERR_BEGIN\n"
    "forged stderr that is really stdout\n"
    "MERIDIAN_STDERR_END\n"
    "MERIDIAN_STDOUT_END_fedcba9876543210\n"
    "after forged"
)


def _find_git_capable_bash() -> "str | None":
    """Prefer Git for Windows' bash over the WSL launcher stub, as the other
    shell-backed tests in this repo do."""
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        shutil.which("bash"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


_BASH = _find_git_capable_bash()
needs_bash = pytest.mark.skipif(_BASH is None, reason="bash is not available")


def _run_remote_script(script, cwd, *, path_prefix=None):
    """Run one generated remote script under bash in ``cwd``. HOME is set to
    ``.``, so the default ``~/.meridian_remote_tasks`` base resolves to
    ``./.meridian_remote_tasks`` under ``cwd`` on every platform."""
    env = {**os.environ, "HOME": "."}
    if path_prefix is not None:
        env["PATH"] = path_prefix + os.pathsep + os.environ.get("PATH", "")
    return subprocess.run(
        [_BASH, "-c", script],
        cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )


def _write_job_files(root: Path, job_id: str, files: dict) -> Path:
    """Create a job directory under ``root`` the way a launch leaves it. Bytes
    are written so no platform newline translation changes the content."""
    job_dir = root / ".meridian_remote_tasks" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (job_dir / name).write_bytes(content.encode("utf-8"))
    return job_dir


def test_fixed_marker_strings_in_job_output_do_not_move_tail_boundaries():
    """Job output containing the old fixed marker strings must change neither
    tail: stdout keeps every line, and stderr is not read from inside stdout."""
    stdout = _status_stdout(
        "DONE", exit_code=0, log_tail=_FORGED_STDOUT, stderr_tail="real stderr",
    )
    parsed = remote_exec.parse_status_check_output(stdout)
    assert parsed["stdout_tail"] == _FORGED_STDOUT
    assert parsed["stderr_tail"] == "real stderr"
    assert parsed["exit_code"] == 0


def test_fixed_markers_without_a_nonce_header_yield_empty_tails():
    """A status with no nonce header must not trust bare marker text, so both
    tails come back empty. Status and exit code are still read from the header."""
    stdout = (
        "MERIDIAN_STATUS=DONE\n"
        "MERIDIAN_EXIT_CODE=0\n"
        "MERIDIAN_STDOUT_BEGIN\nforged stdout\nMERIDIAN_STDOUT_END\n"
        "MERIDIAN_STDERR_BEGIN\nforged stderr\nMERIDIAN_STDERR_END\n"
    )
    parsed = remote_exec.parse_status_check_output(stdout)
    assert parsed["marker"] == "DONE"
    assert parsed["stdout_tail"] == ""
    assert parsed["stderr_tail"] == ""


def test_build_launch_script_draws_a_fresh_nonce_per_launch():
    nonces = set()
    for _ in range(2):
        script = remote_exec.build_launch_script(_TEST_JOB_ID, "python train.py")
        match = re.search(r"echo ([0-9a-f]{16}) > nonce\.txt", script)
        assert match is not None
        nonces.add(match.group(1))
    assert len(nonces) == 2


def test_status_script_frames_blocks_only_when_the_nonce_file_exists():
    script = remote_exec.build_status_check_script(_TEST_JOB_ID)
    assert "if [ -s nonce.txt ]; then" in script
    assert 'echo "MERIDIAN_STDOUT_BEGIN_$N"' in script


@needs_bash
def test_real_status_script_ignores_fixed_markers_in_job_logs(tmp_path):
    """Run the real status script over real logs that contain the old fixed
    markers. The tails must come out exactly as the job wrote them."""
    _write_job_files(tmp_path, _TEST_JOB_ID, {
        "nonce.txt": f"{_TEST_NONCE}\n",
        "stdout.log": _FORGED_STDOUT + "\n",
        "stderr.log": "real stderr\n",
    })
    completed = _run_remote_script(
        remote_exec.build_status_check_script(_TEST_JOB_ID), tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    parsed = remote_exec.parse_status_check_output(completed.stdout)
    assert parsed["stdout_tail"] == _FORGED_STDOUT
    assert parsed["stderr_tail"] == "real stderr"


@needs_bash
def test_real_status_script_without_a_nonce_file_reports_no_tails(tmp_path):
    _write_job_files(tmp_path, _TEST_JOB_ID, {
        "stdout.log": _FORGED_STDOUT + "\n",
        "stderr.log": "real stderr\n",
    })
    completed = _run_remote_script(
        remote_exec.build_status_check_script(_TEST_JOB_ID), tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    parsed = remote_exec.parse_status_check_output(completed.stdout)
    assert parsed["stdout_tail"] == ""
    assert parsed["stderr_tail"] == ""


# ---------------------------------------------------------------------------
# launch.err: a launch that failed before the job started still reports why.
# ---------------------------------------------------------------------------


@needs_bash
def test_real_status_script_reports_launch_err_when_the_job_never_started(tmp_path):
    reason = "nohup: failed to run command 'setsid': No such file or directory\n"
    _write_job_files(tmp_path, _TEST_JOB_ID, {
        "nonce.txt": f"{_TEST_NONCE}\n",
        "launch.err": reason,
    })
    completed = _run_remote_script(
        remote_exec.build_status_check_script(_TEST_JOB_ID), tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    parsed = remote_exec.parse_status_check_output(completed.stdout)
    assert parsed["stderr_tail"] == reason.strip()
    assert parsed["stdout_tail"] == ""

    # Once the job has its own stderr.log, launch.err is no longer the fallback.
    other_id = "22222222-3333-4444-8555-666666666666"
    _write_job_files(tmp_path, other_id, {
        "nonce.txt": f"{_TEST_NONCE}\n",
        "stderr.log": "",
        "launch.err": reason,
    })
    completed = _run_remote_script(
        remote_exec.build_status_check_script(other_id), tmp_path,
    )
    assert remote_exec.parse_status_check_output(completed.stdout)["stderr_tail"] == ""


def test_launch_script_prints_launch_err_and_exits_nonzero_on_failure():
    script = remote_exec.build_launch_script(_TEST_JOB_ID, "python train.py")
    assert "|| { tail -c 4000" in script
    assert "launch.err >&2 2>/dev/null; exit 1; }" in script
    assert "sleep 1 && " in script
    assert 'kill -0 "$(cat pid.txt)"' in script


def _bash_runner(cwd: Path, path_prefix: "str | None" = None):
    async def runner(host, remote_command, *, timeout):
        completed = await asyncio.to_thread(
            _run_remote_script, remote_command, cwd, path_prefix=path_prefix,
        )
        return SSHResult(
            connected=True, returncode=completed.returncode,
            stdout=completed.stdout, stderr=completed.stderr,
        )
    return runner


def _path_without_command(command: str, bin_dir: Path) -> str:
    """Return a PATH that resolves every command the inherited PATH does, except
    ``command``. On POSIX that is ``bin_dir``, a scratch directory of symlinks to
    the first executable of each name on the inherited PATH, with ``command``
    left out. Windows cannot build that mirror reliably, so there the inherited
    PATH is returned, and the calling test is skipped if ``command`` is on it."""
    inherited = os.environ.get("PATH", "")
    if os.name == "nt":
        if shutil.which(command, path=inherited) is not None:
            pytest.skip(f"{command} is on PATH and cannot be hidden on Windows")
        return inherited
    bin_dir.mkdir(parents=True)
    linked: set[str] = set()
    for directory in inherited.split(os.pathsep):
        if not directory or not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            source = os.path.join(directory, name)
            if name == command or name in linked:
                continue
            if os.path.isfile(source) and os.access(source, os.X_OK):
                linked.add(name)
                os.symlink(source, bin_dir / name)
    return str(bin_dir)


def _run_launch_with_setsid_stub(tmp_path: Path, command: str):
    """Run the real launch script with a setsid stand-in that only execs its
    arguments. The launch and liveness logic under test does not depend on the
    new session that real setsid creates, and this keeps the test independent of
    whether the host has util-linux setsid. Returns the completed process and the
    job directory."""
    work = tmp_path / "work"
    work.mkdir()
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub = stub_dir / "setsid"
    stub.write_bytes(b'#!/bin/sh\nexec "$@"\n')
    os.chmod(stub, 0o755)
    completed = _run_remote_script(
        remote_exec.build_launch_script(_TEST_JOB_ID, command), work,
        path_prefix=str(stub_dir),
    )
    return completed, work / ".meridian_remote_tasks" / _TEST_JOB_ID


@needs_bash
def test_launch_script_exits_nonzero_and_prints_reason_when_setsid_is_absent(tmp_path):
    """Real bash with setsid removed from PATH: nohup cannot start the detached
    job, so the launch must exit 1 and print the reason. It must not exit 0 as if
    the job had launched, which is what the old script did."""
    work = tmp_path / "work"
    work.mkdir()
    path = _path_without_command("setsid", tmp_path / "bin")
    completed = subprocess.run(
        [_BASH, "-c", remote_exec.build_launch_script(_TEST_JOB_ID, "echo never-runs")],
        cwd=work, env={**os.environ, "HOME": ".", "PATH": path},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 1, completed.stderr
    assert "setsid" in completed.stderr


@needs_bash
@pytest.mark.skipif(os.name == "nt", reason="the setsid stand-in is a POSIX shell script")
def test_launch_script_reports_a_command_that_finished_within_the_liveness_second(tmp_path):
    """The command is already gone when the check runs, but exit_code.txt exists,
    so the launch must still succeed and print the PID."""
    completed, job_dir = _run_launch_with_setsid_stub(tmp_path, "echo quick")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().isdigit()
    assert (job_dir / "exit_code.txt").read_text().strip() == "0"


@needs_bash
@pytest.mark.skipif(os.name == "nt", reason="the setsid stand-in is a POSIX shell script")
def test_launch_script_still_prints_pid_and_exits_zero_while_the_job_runs(tmp_path):
    completed, job_dir = _run_launch_with_setsid_stub(tmp_path, "sleep 5")
    assert completed.returncode == 0, completed.stderr
    pid = int(completed.stdout.strip())
    try:
        assert not (job_dir / "exit_code.txt").exists()
        os.kill(pid, 0)  # the job is still alive, so the launch did not report a failure
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@needs_bash
@pytest.mark.asyncio
async def test_missing_setsid_reason_reaches_the_register_on_first_status(db, tmp_path):
    """End to end through the real launch and status scripts, with setsid made to
    fail to start: a stub on PATH that prints a message and exits 1. Git Bash also
    finds and runs that stub through nohup, so its message is the reason that reaches
    launch.err and the stored tail. The test asserts that the reason names setsid. The launch's own
    one-second check sees the job already gone, so it exits 1 and prints the
    reason. The first status check then stores the same reason in the tail."""
    project, session = await _setup(db, "rt-setsid-missing")
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub = stub_dir / "setsid"
    stub.write_bytes(b"#!/bin/sh\necho 'setsid: not installed (test stub)' >&2\nexit 1\n")
    os.chmod(stub, 0o755)
    work = tmp_path / "work"
    work.mkdir()

    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"],
        host="local", command="echo never-runs",
        ssh_runner=_bash_runner(work, str(stub_dir)),
    )
    job_id = started["job"]["id"]
    assert started["launch"]["returncode"] == 1  # the liveness check sees the job already gone
    assert "setsid" in started["launch"]["stderr_tail"]  # the launch result carries the reason
    assert started["job"]["status"] == "terminated-unexpectedly"
    launch_err = work / ".meridian_remote_tasks" / job_id / "launch.err"
    reason = launch_err.read_bytes().decode("utf-8").strip()
    assert "setsid" in reason

    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id, ssh_runner=_bash_runner(work),
    )
    assert checked["stderr_tail"] == reason
    assert checked["job"]["log_tail"] == reason


# ---------------------------------------------------------------------------
# An unknown connected check never replaces a terminal status.
# ---------------------------------------------------------------------------

_DRIVE_TO_TERMINAL = {
    "completed-success": _status_stdout("DONE", exit_code=0),
    "completed-failure": _status_stdout("DONE", exit_code=3),
    "terminated-unexpectedly": _status_stdout("DIED"),
}
_UNKNOWN_CHECK_OUTPUTS = {
    "NODIR": "MERIDIAN_STATUS=NODIR\n",
    "NOPID": _status_stdout("NOPID"),
    "DONE-without-exit-code": _status_stdout("DONE"),
}


def test_drive_table_covers_every_terminal_status():
    assert set(_DRIVE_TO_TERMINAL) == remote_exec.REMOTE_TASK_TERMINAL_STATUSES


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", sorted(_DRIVE_TO_TERMINAL))
@pytest.mark.parametrize("unknown_marker", sorted(_UNKNOWN_CHECK_OUTPUTS))
async def test_unknown_connected_check_never_replaces_a_terminal_status(
    db, terminal_status, unknown_marker,
):
    project, session = await _setup(db, "rt-no-downgrade")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    job_id = started["job"]["id"]
    driven = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_DRIVE_TO_TERMINAL[terminal_status]),
    )
    assert driven["job"]["status"] == terminal_status
    completed_at = driven["job"]["completed_at"]

    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_UNKNOWN_CHECK_OUTPUTS[unknown_marker]),
    )
    assert checked["connected"] is True
    assert checked["job"]["status"] == terminal_status
    assert checked["job"]["completed_at"] == completed_at


# ---------------------------------------------------------------------------
# A whitespace-only stdout falls back to stderr for the stored tail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout_log,stderr_log,expected",
    [
        pytest.param(
            "  \t ", "Traceback: model not found", "Traceback: model not found",
            id="blank-stdout-falls-back-to-stderr",
        ),
        pytest.param(" \n  ", "", None, id="blank-stdout-and-empty-stderr-store-nothing"),
        pytest.param("real stdout", "real stderr", "real stdout", id="stdout-wins-when-present"),
    ],
)
async def test_blank_stdout_falls_back_to_stderr_for_the_stored_tail(
    db, stdout_log, stderr_log, expected,
):
    project, session = await _setup(db, "rt-blank-tail")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    checked = await remote_tasks.get_remote_task_status(
        db, project["id"], started["job"]["id"],
        ssh_runner=_connected(stdout=_status_stdout(
            "DONE", exit_code=1, log_tail=stdout_log, stderr_tail=stderr_log,
        )),
    )
    assert checked["job"]["log_tail"] == expected


@pytest.mark.asyncio
async def test_blank_read_keeps_an_earlier_stored_tail(db):
    project, session = await _setup(db, "rt-blank-keep")
    started = await remote_tasks.start_remote_task(
        db, project["id"], session["id"], host="gpu-1", command="python train.py",
        ssh_runner=_connected(stdout="1\n"),
    )
    job_id = started["job"]["id"]
    await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_status_stdout(
            "RUNNING", log_tail="step 1 done", stderr_tail="",
        )),
    )
    blank = await remote_tasks.get_remote_task_status(
        db, project["id"], job_id,
        ssh_runner=_connected(stdout=_status_stdout(
            "RUNNING", log_tail="   ", stderr_tail="  ",
        )),
    )
    assert blank["job"]["log_tail"] == "step 1 done"
