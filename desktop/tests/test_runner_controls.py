"""Tests for meridian_desktop/runner_controls.py: the allowlisted argv, the refusals, the timeouts, the typed results and the log.

Every runner call here is a fake spawn. No process starts, no socket opens, and every log goes to pytest's temp
directory. The secrets in the replies below are obviously fake, so a leak check can search for them.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

import pytest

from meridian_desktop import runner_controls as rc
from meridian_desktop.runner_controls import (
    ALREADY_RUNNING,
    CONFIRMED,
    FAILED,
    NOT_AVAILABLE,
    NOT_CONFIRMED,
    REFUSED,
    RunnerControl,
    RunnerRefused,
    build_argv,
    confirmation_text,
    describe_command,
    format_log_line,
    state_of,
)
from meridian_desktop.tunnel_status import NOT_RUNNING, RUNNING, UNKNOWN

PY = "C:/Users/tester/core-env/python.exe"
CWD = "C:/Users/tester/Documents/Meridian/repository"
STAMP = dt.datetime(2026, 10, 22, 17, 0, 0, tzinfo=dt.timezone.utc)
SECRET = "sk_meridian_FAKE-runner-control-0001"
FOLDER_SECRET = "C:/Users/tester/private-folder-0002"


def _clock():
    return STAMP


class FakeSpawn:
    """A fake for subprocess.run. It records each call, and returns a canned reply or raises a canned error."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        reply = self.replies.pop(0) if self.replies else _done(0, {"scope": "local", "child": {"state": "running"}})
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _done(code, payload=None, *, stdout=None, stderr=b""):
    body = stdout if stdout is not None else (json.dumps(payload).encode("utf-8") if payload is not None else b"")
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=body, stderr=stderr)


def _status_payload(scope, state, pid=4321):
    return {"scope": scope, "generated_at": 1.0, "child": {"state": state, "pid": pid}}


def _control(tmp_path, spawn, *, python=PY, cwd=CWD):
    log = tmp_path / "settings" / rc.LOG_NAME
    return RunnerControl(python, cwd, spawn=spawn, log_path=log, clock=_clock), log


# ------------------------------------------------------------------ the argument lists


def test_local_start_argv_is_the_fixed_command_with_the_core_python_as_the_child(tmp_path):
    assert build_argv(PY, "start", "local") == [
        PY, "-m", "meridian.local_runner", "start", "--scope", "local", "--", PY, "-m", "meridian",
    ]


def test_tunnel_start_argv_runs_the_tunnel_client_as_the_child(tmp_path):
    assert build_argv(PY, "start", "tunnel") == [
        PY, "-m", "meridian.local_runner", "start", "--scope", "tunnel", "--", PY, "-m", "meridian", "--tunnel",
    ]


@pytest.mark.parametrize("scope", ["local", "tunnel"])
@pytest.mark.parametrize("subcommand", ["stop", "status"])
def test_stop_and_status_argv_name_only_the_scope(scope, subcommand):
    argv = build_argv(PY, subcommand, scope)

    assert argv == [PY, "-m", "meridian.local_runner", subcommand, "--scope", scope]
    assert "--json" not in argv
    assert all(isinstance(part, str) for part in argv)


def test_the_default_scope_is_tunnel_as_the_contract_says():
    assert rc.DEFAULT_SCOPE == "tunnel"
    assert build_argv(PY, "status") == build_argv(PY, "status", "tunnel")


def test_the_argv_is_a_list_and_contains_no_shell_text():
    argv = build_argv(PY, "start", "local")

    assert isinstance(argv, list)
    assert not any(part in ("sh", "cmd", "powershell") for part in argv)


@pytest.mark.parametrize("scope", ["", "all", "TUNNEL", "local ", " tunnel", "-x", "tunnel;rm", "tunnel --force", None, 3])
def test_build_argv_refuses_any_scope_but_local_and_tunnel(scope):
    with pytest.raises(RunnerRefused):
        build_argv(PY, "status", scope)


@pytest.mark.parametrize(
    "subcommand",
    ["restart", "doctor", "preflight", "run-script", "tail-log", "list-scripts", "start --scope local", "", None],
)
def test_build_argv_refuses_any_subcommand_but_start_stop_and_status(subcommand):
    with pytest.raises(RunnerRefused):
        build_argv(PY, subcommand, "local")


@pytest.mark.parametrize("python", ["", "   ", "C:/py\x00/python.exe", None])
def test_build_argv_refuses_a_python_that_is_blank_or_has_a_nul(python):
    with pytest.raises(RunnerRefused):
        build_argv(python, "status", "local")


def test_describe_command_is_the_same_list_the_dialog_names():
    assert describe_command(PY, "start", "tunnel") == subprocess.list2cmdline(build_argv(PY, "start", "tunnel"))


def test_confirmation_text_names_the_exact_command_the_scope_and_the_folder():
    text = confirmation_text(PY, "start", "local", CWD)

    assert describe_command(PY, "start", "local") in text
    assert "Scope: local" in text
    assert f"Working folder: {CWD}" in text
    assert "port 7878" in text  # the local start names the port it stops first


def test_the_tunnel_confirmation_names_the_ports_and_the_folder_it_exposes():
    text = confirmation_text(PY, "start", "tunnel", CWD)

    assert "8808 to 8813" in text
    assert "claude.ai" in text


def test_the_tunnel_start_says_only_six_slot_ports_are_cleared_and_stop_names_no_port():
    # The core's tunnel start clears 8808 to 8813 only (the first six slot ports), and its stop clears no port by number.
    start = confirmation_text(PY, "start", "tunnel", CWD)
    assert "the first six of its slot ports" in start
    assert "The other slot ports are not cleared" in start

    stop = confirmation_text(PY, "stop", "tunnel", CWD)
    assert not any(str(port) in stop for port in range(8808, 8823))


# ------------------------------------------------------------------ refusals start nothing


@pytest.mark.parametrize("scope", ["all", "", "tunnel;x"])
def test_a_refused_scope_starts_nothing_and_writes_no_log(tmp_path, scope):
    spawn = FakeSpawn()
    control, log = _control(tmp_path, spawn)

    results = [control.start(scope), control.stop(scope), control.status(scope)]

    assert [r.outcome for r in results] == [REFUSED, REFUSED, REFUSED]
    assert spawn.calls == []
    assert not log.exists()


def test_a_missing_python_or_folder_is_not_available_and_starts_nothing(tmp_path):
    spawn = FakeSpawn()
    no_python, _ = _control(tmp_path, spawn, python="")
    no_folder, _ = _control(tmp_path, spawn, cwd="  ")

    assert no_python.start("local").outcome == NOT_AVAILABLE
    assert no_folder.stop("local").outcome == NOT_AVAILABLE
    assert spawn.calls == []


def test_a_spawn_that_cannot_run_is_not_available_not_a_crash(tmp_path):
    control, log = _control(tmp_path, FakeSpawn(OSError("no such python")))

    result = control.start("local")

    assert result.outcome == NOT_AVAILABLE
    assert "no such python" not in result.message
    assert log.read_text(encoding="utf-8").strip().endswith("outcome=not_available pid=not reported")


# ------------------------------------------------------------------ the spawn call is shell-free and timed


def test_each_call_uses_a_list_argv_no_shell_and_the_contract_timeouts(tmp_path):
    spawn = FakeSpawn(
        _done(0, _status_payload("local", "running", pid=11)),
        _done(0, _status_payload("local", "running", pid=12)),
        _done(0, _status_payload("local", "not_started")),
    )
    control, _ = _control(tmp_path, spawn)

    control.status("local")
    control.start("local")
    control.stop("local")

    status_kwargs = spawn.calls[0][1]
    start_kwargs = spawn.calls[1][1]
    stop_kwargs = spawn.calls[2][1]
    assert status_kwargs["timeout"] == 10.0
    assert start_kwargs["timeout"] == 30.0
    assert stop_kwargs["timeout"] == 15.0
    for argv, kwargs in spawn.calls:
        assert isinstance(argv, list)
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == CWD
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["capture_output"] is True


def test_the_timeouts_are_the_contract_values():
    assert rc.TIMEOUTS == {"start": 30.0, "stop": 15.0, "status": 10.0}


def test_a_start_that_times_out_is_not_confirmed_and_is_not_retried(tmp_path):
    spawn = FakeSpawn(subprocess.TimeoutExpired(cmd="x", timeout=30))
    control, log = _control(tmp_path, spawn)

    result = control.start("local")

    assert result.outcome == NOT_CONFIRMED
    assert "Not confirmed" in result.message
    assert "Nothing was retried" in result.message
    assert "30 seconds" in result.message
    assert len(spawn.calls) == 1  # no automatic retry
    assert "outcome=not_confirmed" in log.read_text(encoding="utf-8")


def test_a_stop_that_times_out_is_not_confirmed(tmp_path):
    spawn = FakeSpawn(subprocess.TimeoutExpired(cmd="x", timeout=15))
    control, _ = _control(tmp_path, spawn)

    result = control.stop("tunnel")

    assert result.outcome == NOT_CONFIRMED
    assert "15 seconds" in result.message
    assert len(spawn.calls) == 1


# ------------------------------------------------------------------ typed results


def test_a_start_the_runner_reports_running_is_confirmed_with_its_pid(tmp_path):
    spawn = FakeSpawn(_done(0, _status_payload("local", "running", pid=9876)))
    control, _ = _control(tmp_path, spawn)

    result = control.start("local")

    assert result.outcome == CONFIRMED
    assert result.state == RUNNING
    assert result.pid == 9876
    assert "pid 9876" in result.message


def test_a_start_that_is_already_running_says_so_and_starts_nothing_new(tmp_path):
    stderr = json.dumps({"error": "a local runner for scope 'local' appears to already be running", "type": "RunnerAlreadyRunningError"})
    spawn = FakeSpawn(_done(1, stdout=b"", stderr=stderr.encode("utf-8")))
    control, log = _control(tmp_path, spawn)

    result = control.start("local")

    assert result.outcome == ALREADY_RUNNING
    assert "already running" in result.message
    assert "outcome=already_running" in log.read_text(encoding="utf-8")


def test_a_start_whose_child_exits_at_once_is_failed_not_confirmed(tmp_path):
    spawn = FakeSpawn(_done(0, _status_payload("tunnel", "crashed", pid=None)))
    control, _ = _control(tmp_path, spawn)

    result = control.start("tunnel")

    assert result.outcome == FAILED
    assert result.state == NOT_RUNNING
    assert "did not stay running" in result.message


def test_a_nonzero_exit_is_failed_and_names_only_the_code(tmp_path):
    spawn = FakeSpawn(_done(2, stdout=b"", stderr=f"usage: {FOLDER_SECRET} {SECRET}".encode("utf-8")))
    control, _ = _control(tmp_path, spawn)

    result = control.stop("local")

    assert result.outcome == FAILED
    assert "exited with code 2" in result.message
    assert SECRET not in result.message and FOLDER_SECRET not in result.message


def test_a_stop_is_confirmed_when_the_runner_no_longer_reports_the_child_running(tmp_path):
    spawn = FakeSpawn(_done(0, _status_payload("local", "stopped")))
    control, _ = _control(tmp_path, spawn)

    result = control.stop("local")

    assert result.outcome == CONFIRMED
    assert result.state == NOT_RUNNING


def test_a_stop_is_not_confirmed_while_the_runner_still_reports_running(tmp_path):
    spawn = FakeSpawn(_done(0, _status_payload("local", "running", pid=55)))
    control, _ = _control(tmp_path, spawn)

    result = control.stop("local")

    assert result.outcome == NOT_CONFIRMED
    assert "still reports" in result.message


UNREADABLE_AFTER_STOP = "Not confirmed: the runner state could not be read."


def test_a_stop_whose_runner_state_is_unknown_says_the_state_could_not_be_read_and_never_running(tmp_path):
    spawn = FakeSpawn(_done(0, _status_payload("tunnel", "unknown", pid=60)))
    control, log = _control(tmp_path, spawn)

    result = control.stop("tunnel")

    assert result.outcome == NOT_CONFIRMED
    assert result.state == UNKNOWN
    assert result.message == UNREADABLE_AFTER_STOP
    assert "running" not in result.message
    assert "still reports" not in result.message
    # No control is named: the Tunnel tab and the Settings runner block have no Refresh button.
    assert "Refresh" not in result.message
    assert "outcome=not_confirmed pid=60" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "reply",
    [
        {"scope": "tunnel", "child": {"state": "weird-new-state"}},  # a state the desktop does not know
        {"scope": "tunnel", "child": "running"},  # child is not an object
        {"scope": "tunnel", "child": {"state": 7}},  # state is not text
        {"scope": "tunnel"},  # no child at all
        {"scope": "local", "child": {"state": "running"}},  # a reply for another scope
    ],
)
def test_a_stop_with_a_malformed_state_reply_is_not_confirmed_and_never_says_running(tmp_path, reply):
    control, _ = _control(tmp_path, FakeSpawn(_done(0, reply)))

    result = control.stop("tunnel")

    assert result.outcome == NOT_CONFIRMED
    assert result.state == UNKNOWN
    assert result.message == UNREADABLE_AFTER_STOP
    assert "running" not in result.message


def test_a_stop_whose_reply_is_not_json_is_failed_and_never_says_running(tmp_path):
    control, _ = _control(tmp_path, FakeSpawn(_done(0, stdout=b"not json at all")))

    result = control.stop("local")

    assert result.outcome == FAILED
    assert "running" not in result.message


def test_a_stop_the_runner_confirms_stopped_says_so(tmp_path):
    control, _ = _control(tmp_path, FakeSpawn(_done(0, _status_payload("tunnel", "not_started"))))

    result = control.stop("tunnel")

    assert result.outcome == CONFIRMED
    assert result.message == "The tunnel is stopped."


def test_a_reply_that_is_not_json_is_failed(tmp_path):
    control, _ = _control(tmp_path, FakeSpawn(_done(0, stdout=b"not json at all")))

    result = control.start("local")

    assert result.outcome == FAILED
    assert "did not print a status" in result.message


# ------------------------------------------------------------------ status and the unknown rule (rule 7)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_status_payload("local", "running"), RUNNING),
        (_status_payload("local", "not_started"), NOT_RUNNING),
        (_status_payload("local", "stopped"), NOT_RUNNING),
        (_status_payload("local", "crashed"), NOT_RUNNING),
        (_status_payload("local", "unknown"), UNKNOWN),
        (_status_payload("local", "weird-new-state"), UNKNOWN),
        (_status_payload("tunnel", "running"), UNKNOWN),  # a reply for another scope is never this scope's status
        ({"scope": "local"}, UNKNOWN),
        ({"scope": "local", "child": "running"}, UNKNOWN),
        ({"scope": "local", "child": {"state": 7}}, UNKNOWN),
        ({"scope": "local", "child": {}}, UNKNOWN),
        (None, UNKNOWN),
        ([], UNKNOWN),
    ],
)
def test_state_of_reads_only_a_known_running_child_as_running(payload, expected):
    assert state_of(payload, "local") == expected


def test_a_status_for_the_local_scope_reports_its_state_and_is_not_logged(tmp_path):
    control, log = _control(tmp_path, FakeSpawn(_done(0, _status_payload("local", "running", pid=7))))

    result = control.status("local")

    assert result.outcome == CONFIRMED
    assert result.state == RUNNING
    assert result.message == "The local server is running."
    assert not log.exists()  # a status read is not a start or stop attempt


@pytest.mark.parametrize(
    "reply",
    [
        _done(0, _status_payload("tunnel", "running")),  # another scope's reply
        _done(0, {"scope": "local", "child": {"state": "mystery"}}),
        _done(3, _status_payload("local", "running")),  # the runner exited non-zero
        _done(0, stdout=b"not json"),
    ],
)
def test_a_status_that_cannot_be_read_is_unknown_never_running(tmp_path, reply):
    control, _ = _control(tmp_path, FakeSpawn(reply))

    result = control.status("local")

    assert result.state == UNKNOWN
    assert result.state != RUNNING


def test_a_status_that_times_out_is_unknown_and_never_running(tmp_path):
    control, _ = _control(tmp_path, FakeSpawn(subprocess.TimeoutExpired(cmd="x", timeout=10)))

    result = control.status("tunnel")

    assert result.outcome == NOT_CONFIRMED
    assert result.state == UNKNOWN


# ------------------------------------------------------------------ the control log (rule 6, amendment E)


def test_each_start_and_stop_attempt_appends_one_line_with_time_scope_outcome_and_pid(tmp_path):
    spawn = FakeSpawn(
        _done(0, _status_payload("local", "running", pid=321)),
        _done(0, _status_payload("local", "stopped", pid=321)),
    )
    control, log = _control(tmp_path, spawn)

    control.start("local")
    control.stop("local")

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "2026-10-22T17:00:00+00:00 start scope=local outcome=confirmed pid=321",
        "2026-10-22T17:00:00+00:00 stop scope=local outcome=confirmed pid=321",
    ]


def test_the_log_line_holds_no_secret_even_when_the_runner_output_has_one(tmp_path):
    noisy = {"scope": "tunnel", "child": {"state": "running", "pid": 77}, "token": SECRET, "path": FOLDER_SECRET}
    spawn = FakeSpawn(_done(0, noisy, stderr=f"{SECRET} {FOLDER_SECRET}".encode("utf-8")))
    control, log = _control(tmp_path, spawn)

    control.start("tunnel")

    text = log.read_text(encoding="utf-8")
    assert SECRET not in text
    assert FOLDER_SECRET not in text
    assert CWD not in text and PY not in text
    assert text.strip() == "2026-10-22T17:00:00+00:00 start scope=tunnel outcome=confirmed pid=77"


def test_a_log_line_with_no_pid_says_the_pid_was_not_reported(tmp_path):
    assert format_log_line("2026-10-22T17:00:00+00:00", "stop", "local", NOT_CONFIRMED, None) == (
        "2026-10-22T17:00:00+00:00 stop scope=local outcome=not_confirmed pid=not reported"
    )


def test_a_log_that_cannot_be_written_never_fails_the_call(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so the log folder cannot be made", encoding="utf-8")
    control = RunnerControl(
        PY, CWD, spawn=FakeSpawn(_done(0, _status_payload("local", "running", pid=3))),
        log_path=blocker / "sub" / rc.LOG_NAME, clock=_clock,
    )

    result = control.start("local")

    assert result.outcome == CONFIRMED


def test_the_default_log_is_beside_the_settings_file(tmp_path):
    path = rc.default_log_path(tmp_path / "meridian-desktop" / "settings.json")

    assert path == tmp_path / "meridian-desktop" / rc.LOG_NAME
    assert rc.LOG_NAME == "runner-control.log"


def test_a_start_writes_its_log_to_the_path_it_was_given_only(tmp_path):
    control, log = _control(tmp_path, FakeSpawn(_done(0, _status_payload("local", "running", pid=8))))

    control.start("local")

    assert log.exists()
    assert [p.name for p in log.parent.iterdir()] == [rc.LOG_NAME]
