"""Tests for meridian_desktop/tunnel_status.py: the Tunnel tab's running, not running or unknown answer.

Every runner call here is a fake. Nothing starts a process, reads a real file or makes a request.
The RunnerClient cases use a client with no interpreter (so no command is built at all) or a spawn
that records the call or raises. This module imports no Qt.
"""

import ast
from datetime import datetime
from pathlib import Path

import pytest

from meridian_desktop import tunnel_status
from meridian_desktop.runner_client import (
    ERR_BAD_OUTPUT,
    ERR_BAD_SCOPE,
    ERR_NONZERO_EXIT,
    ERR_TIMEOUT,
    ERR_TOO_LARGE,
    NOT_AVAILABLE_MESSAGE,
    RunnerClient,
    RunnerResult,
)
from meridian_desktop.tunnel_status import (
    CHECK_FAILED_REASON,
    DEPENDENCY_TEXT,
    DEPENDENT_TOOLS,
    NO_CHILD_REASON,
    NO_RESULT_REASON,
    NOT_AVAILABLE_REASON,
    NOT_RUNNING,
    RUNNING,
    START_HINT,
    CHECK_HINT,
    TUNNEL_SCOPE,
    UNKNOWN,
    UNKNOWN_PREFIX,
    UNREAD_REASON,
    WRONG_SCOPE_REASON,
    read_tunnel_status,
    tunnel_state_of,
)

CHECKED = datetime(2026, 10, 21, 10, 15, 0)
MODULE_PATH = Path(tunnel_status.__file__)


def _clock():
    return CHECKED


class Recorder:
    """A fake runner status call. It records each scope it is asked for, and returns or raises as set.

    ``events``, when given, collects "status" at each call, so a test can order it against the clock.
    """

    def __init__(self, reply=None, *, raises=None, events=None):
        self.reply = reply
        self.raises = raises
        self.events = events
        self.scopes = []

    def __call__(self, scope):
        self.scopes.append(scope)
        if self.events is not None:
            self.events.append("status")
        if self.raises is not None:
            raise self.raises
        return self.reply


def _status_reply(child_state=None, *, scope=TUNNEL_SCOPE, with_child=True):
    """A successful status reply in the shape the core's RunnerStatus.as_dict gives (only the fields read here)."""
    payload = {"scope": scope, "generated_at": 1_800_000_000.0, "warnings": []}
    if with_child:
        payload["child"] = {"state": child_state, "pid": None, "exit_code": None}
    return RunnerResult(verb="status", ok=True, payload=payload, exit_code=0)


def _failed(code, message="The runner command did not finish in time.", *, payload=None):
    return RunnerResult(verb="status", ok=False, payload=payload, error_code=code, error_message=message)


# ------------------------------------------------------------------ the three answers


def test_running_child_reads_as_running_and_stamps_the_check_time():
    recorder = Recorder(_status_reply("running"))
    status = read_tunnel_status(recorder, clock=_clock)

    assert status.state == RUNNING
    assert status.reason == "The runner reports the tunnel process as running."
    assert status.checked_at == CHECKED
    # One call, for the tunnel scope only.
    assert recorder.scopes == ["tunnel"]


@pytest.mark.parametrize("child_state", ["not_started", "stopped", "crashed"])
def test_a_child_that_is_not_running_reads_as_not_running(child_state):
    status = read_tunnel_status(Recorder(_status_reply(child_state)), clock=_clock)

    assert status.state == NOT_RUNNING
    assert status.checked_at == CHECKED


def test_an_unverifiable_child_is_unknown_not_running():
    status = read_tunnel_status(Recorder(_status_reply("unknown")), clock=_clock)

    assert status.state == UNKNOWN
    assert status.reason == "The runner could not verify whether the tunnel process is running."


@pytest.mark.parametrize(
    "child_state",
    ["RUNNING", " running", "running ", "healthy", "<b>running</b>", "x" * 300],
)
def test_a_child_state_the_desktop_does_not_read_is_unknown_and_not_echoed(child_state):
    status = read_tunnel_status(Recorder(_status_reply(child_state)), clock=_clock)

    assert status.state == UNKNOWN
    assert status.reason == UNREAD_REASON
    assert child_state not in status.reason


@pytest.mark.parametrize(
    "reply",
    [
        _status_reply(with_child=False),  # no child at all
        _status_reply(None),  # a child with no state
        _status_reply(""),  # an empty state
        _status_reply(7),  # a state that is not text
    ],
)
def test_a_reply_with_no_usable_child_state_is_unknown(reply):
    status = read_tunnel_status(Recorder(reply), clock=_clock)

    assert status.state == UNKNOWN
    assert status.reason == NO_CHILD_REASON


def test_a_reply_for_another_scope_is_unknown_even_when_its_child_runs():
    status = read_tunnel_status(Recorder(_status_reply("running", scope="zotero")), clock=_clock)

    assert status.state == UNKNOWN
    assert status.reason == WRONG_SCOPE_REASON


def test_an_ok_reply_with_no_payload_is_unknown():
    reply = RunnerResult(verb="status", ok=True, payload=None, exit_code=0)

    assert tunnel_state_of(reply) == (UNKNOWN, WRONG_SCOPE_REASON)


def test_a_value_that_is_not_a_runner_result_is_unknown():
    assert tunnel_state_of({"scope": "tunnel", "child": {"state": "running"}}) == (UNKNOWN, NO_RESULT_REASON)


def test_no_result_is_unknown():
    status = read_tunnel_status(Recorder(None), clock=_clock)

    assert status.state == UNKNOWN
    assert status.reason == NO_RESULT_REASON


# ------------------------------------------------------------------ a missing runner


def test_a_client_with_no_interpreter_is_a_missing_runner_so_unknown_and_nothing_starts():
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("no process may start for a client with no interpreter")

    client = RunnerClient(spawn=spawn)
    status = read_tunnel_status(client.status, clock=_clock)

    assert status.state == UNKNOWN
    assert status.reason == NOT_AVAILABLE_REASON
    assert NOT_AVAILABLE_MESSAGE in status.reason
    assert spawned == []


def test_an_interpreter_that_cannot_start_is_a_missing_runner_so_unknown(tmp_path):
    calls = []

    def spawn(argv, **kwargs):
        calls.append(argv)
        raise FileNotFoundError(2, "no such file", str(argv[0]))

    client = RunnerClient(python=str(tmp_path / "core-python.exe"), spawn=spawn)
    status = read_tunnel_status(client.status, clock=_clock)

    assert status.state == UNKNOWN
    assert status.reason == NOT_AVAILABLE_REASON
    # The one call that was attempted names the tunnel scope, as its own argument.
    assert len(calls) == 1
    assert calls[0][-2:] == ["--scope", "tunnel"]


# ------------------------------------------------------------------ a status call that errors


@pytest.mark.parametrize("code", [ERR_TIMEOUT, ERR_NONZERO_EXIT, ERR_BAD_OUTPUT, ERR_TOO_LARGE, ERR_BAD_SCOPE])
def test_a_failed_status_call_is_unknown_and_never_running(code):
    failed = _failed(code, f"fixed text for {code}.")
    status = read_tunnel_status(Recorder(failed), clock=_clock)

    assert status.state == UNKNOWN
    assert status.state != RUNNING
    assert status.reason == f"{UNKNOWN_PREFIX} fixed text for {code}."


def test_a_failed_call_that_printed_a_running_child_is_still_unknown():
    failed = _failed(
        ERR_NONZERO_EXIT,
        "The runner command exited with code 1.",
        payload={"scope": "tunnel", "child": {"state": "running"}},
    )
    status = read_tunnel_status(Recorder(failed), clock=_clock)

    assert status.state == UNKNOWN


def test_a_status_call_that_raises_is_unknown_and_shows_only_its_class_name():
    status = read_tunnel_status(
        Recorder(raises=RuntimeError("a message that must not be shown")),
        clock=_clock,
    )

    assert status.state == UNKNOWN
    assert status.reason == CHECK_FAILED_REASON.format("RuntimeError")
    assert "must not be shown" not in status.reason
    assert status.checked_at == CHECKED


# ------------------------------------------------------------------ the check time and the call


def test_the_check_time_is_read_after_the_call_returns():
    events = []
    recorder = Recorder(_status_reply("running"), events=events)

    def clock():
        events.append("clock")
        return CHECKED

    status = read_tunnel_status(recorder, clock=clock)

    assert events == ["status", "clock"]
    assert status.checked_at == CHECKED


def test_the_default_clock_gives_a_datetime():
    status = read_tunnel_status(Recorder(_status_reply("running")))

    assert isinstance(status.checked_at, datetime)


def test_the_status_call_is_made_once_for_the_tunnel_scope():
    recorder = Recorder(_status_reply("not_started"))
    read_tunnel_status(recorder, clock=_clock)

    assert recorder.scopes == [TUNNEL_SCOPE] == ["tunnel"]


# ------------------------------------------------------------------ the wording and the module


def test_the_dependency_names_the_three_tools_and_says_they_are_unavailable_while_it_is_down():
    assert DEPENDENT_TOOLS == ("codebase-memory", "Serena", "filesystem")
    for tool in DEPENDENT_TOOLS:
        assert tool in DEPENDENCY_TEXT
    assert "unavailable" in DEPENDENCY_TEXT
    assert "While the tunnel is not running" in DEPENDENCY_TEXT


def test_the_start_hint_points_to_the_tunnel_panel_and_names_no_terminal_command():
    assert "Tunnel panel" in START_HINT
    assert "in Settings" not in START_HINT
    assert "asks first" in START_HINT
    assert "Home" in CHECK_HINT
    assert "pixi run" not in START_HINT
    assert "pixi run" not in CHECK_HINT


def test_no_text_in_the_module_says_not_built_yet():
    texts = (
        DEPENDENCY_TEXT,
            NOT_AVAILABLE_REASON,
        NO_RESULT_REASON,
        NO_CHILD_REASON,
        UNREAD_REASON,
        WRONG_SCOPE_REASON,
        CHECK_FAILED_REASON,
    )
    for text in texts:
        assert "not built yet" not in text.lower()
    assert "not built yet" not in MODULE_PATH.read_text(encoding="utf-8").lower()


def test_the_module_imports_no_qt_and_no_process_or_network_module():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"PySide6", "PyQt5", "PyQt6", "PySide2", "subprocess", "socket", "urllib", "http", "requests", "httpx"}
    assert not imported & forbidden


def test_tunnel_state_of_maps_a_running_reply_without_any_call():
    # tunnel_state_of is pure: it takes the reply, and no client or call is involved.
    assert tunnel_state_of(_status_reply("running")) == (RUNNING, "The runner reports the tunnel process as running.")
