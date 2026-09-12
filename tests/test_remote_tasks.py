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

import pytest

from meridian import db as db_module
from meridian import remote_exec
from meridian.db import remote_tasks
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


def _status_stdout(marker, exit_code=None, log_tail="hello from remote\n"):
    lines = [f"MERIDIAN_STATUS={marker}"]
    if exit_code is not None:
        lines.append(f"MERIDIAN_EXIT_CODE={exit_code}")
    lines.append("MERIDIAN_LOG_BEGIN")
    lines.append(log_tail.rstrip("\n"))
    lines.append("MERIDIAN_LOG_END")
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
    assert "line one" in parsed["log_tail"]
    assert "line two" in parsed["log_tail"]


def test_parse_status_check_output_handles_missing_marker_gracefully():
    parsed = remote_exec.parse_status_check_output("garbage, no markers at all")
    assert parsed["marker"] == "NOPID"
    assert parsed["exit_code"] is None
    assert parsed["log_tail"] == ""


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
    whether anything ran."""
    project, session = await _setup(db, "rt-start-4")
    result = await remote_tasks.start_remote_task(
        db, project["id"], session["id"],
        host="unreachable-host", command="python train.py",
        ssh_runner=_disconnected(error="timeout"),
    )
    job = result["job"]
    assert job["status"] == "unknown"
    assert job["pid"] is None
    assert result["launch"]["connected"] is False
    assert result["launch"]["error"] == "timeout"


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
