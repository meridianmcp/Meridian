"""Owner-confirmed start, stop and status for the Meridian runner. Pure Python: no Qt import.

The contract is docs/local-trust-contract.md. The desktop builds the runner command in this module
and nowhere else, and the only commands it builds are these, run with shell=False and a list argv:

    <core python> -m meridian.local_runner start --scope <scope> -- <core python> <child arguments>
    <core python> -m meridian.local_runner stop --scope <scope>
    <core python> -m meridian.local_runner status --scope <scope>

- Subcommands: start, stop and status. Scopes: local and tunnel. Any other subcommand or scope is refused
  before a process starts (rule 3). Restart is not a subcommand here: the Qt layer runs stop, then start,
  and asks the owner before each one (rule 9).
- The local child is ``-m meridian``, the core's HTTP server. Without flags it serves 127.0.0.1:7878
  (meridian/__main__.py, and meridian/tray_main.py uses the same command). The tunnel child is
  ``-m meridian --tunnel``, the core's tunnel client (meridian/__main__.py). The child runs with the
  configured core python, not a bare ``python`` on PATH, as tray_main does with sys.executable.
- The core's ``status`` takes no ``--json`` flag. argparse rejects it with exit code 2. ``status`` always
  prints one JSON object, so this module calls ``status`` without one. Rule 3 names ``--json`` only
  "where it is used", and the core never uses it.
- Timeouts: start 30 seconds and stop 15 seconds (rule 5). Status gets 10 seconds, the same as the read
  client. A timeout is "not confirmed", and nothing is retried.
- Each start and stop attempt appends one line to the control log, in the settings folder (rule 6 and
  amendment E), whatever its outcome: confirmed, failed, not confirmed (a timeout included), already running,
  or not available (no core Python or folder set, or a process that cannot be started). Only a refused call,
  one whose subcommand or scope is not allowed, is not logged, because it starts no process. The line holds
  the time, the action, the scope, the outcome and the pid when the runner reports one. It never holds the
  runner's output, a token or a path. Status is a read, not an attempt, so it is not logged.
- A state is running only when the runner reports the child running. A missing, failed, unreadable or
  unknown state is unknown, and unknown is never shown as running (rule 7).
- The desktop never reads or logs a tunnel token (rule 8). It passes none, and it reads none from the environment.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian_desktop.log_tail import DEFAULT_MAX_BYTES, LogTail, read_log_tail
from meridian_desktop.settings_store import default_settings_path
from meridian_desktop.tunnel_status import NOT_RUNNING, RUNNING, UNKNOWN

MODULE = "meridian.local_runner"

SCOPE_LOCAL = "local"
SCOPE_TUNNEL = "tunnel"
ALLOWED_SCOPES = (SCOPE_LOCAL, SCOPE_TUNNEL)
# Contract rule 3: the scope comes from the allowlist, and its default is tunnel.
DEFAULT_SCOPE = SCOPE_TUNNEL

START = "start"
STOP = "stop"
STATUS = "status"
ALLOWED_SUBCOMMANDS = (START, STOP, STATUS)

START_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 15.0
STATUS_TIMEOUT_SECONDS = 10.0
TIMEOUTS = {START: START_TIMEOUT_SECONDS, STOP: STOP_TIMEOUT_SECONDS, STATUS: STATUS_TIMEOUT_SECONDS}

# The arguments that follow the core python for each scope's child. Fixed here, never typed by the owner.
CHILD_ARGS = {
    SCOPE_LOCAL: ("-m", "meridian"),
    SCOPE_TUNNEL: ("-m", "meridian", "--tunnel"),
}
# The core's own port for the local server, when MERIDIAN_PORT is not set (meridian/__main__.py).
LOCAL_DEFAULT_PORT = 7878
SCOPE_LABELS = {SCOPE_LOCAL: "local server", SCOPE_TUNNEL: "tunnel"}

LOG_NAME = "runner-control.log"
MAX_OUTPUT_BYTES = 1024 * 1024
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The outcomes of one call. Only CONFIRMED means the runner answered as the action needs it to.
CONFIRMED = "confirmed"
NOT_CONFIRMED = "not_confirmed"
ALREADY_RUNNING = "already_running"
FAILED = "failed"
NOT_AVAILABLE = "not_available"
REFUSED = "refused"

REFUSED_MESSAGE = "This command is not allowed. Only start, stop and status for the local or tunnel scope can be run."
NOT_AVAILABLE_MESSAGE = (
    "The Meridian runner cannot be run. The Meridian core checkout and its Python must be set in Settings, "
    "and the Python must be able to run the core."
)
TOO_LARGE_MESSAGE = "The runner printed more output than the desktop reads, so its answer was not read."
NO_STATUS_MESSAGE = "The runner did not print a status the desktop could read."
NOT_STAYING_MESSAGE = "The {label} did not stay running. The runner reports it {state}."
ALREADY_RUNNING_MESSAGE = "The {label} is already running, so nothing was started."
EXIT_MESSAGE = "The runner command exited with code {code}, so the {verb} did not succeed."
STILL_RUNNING_MESSAGE = "Not confirmed: the runner still reports the {label} as running."
# A stop whose runner state cannot be read. It is not confirmed, and it never says running.
STATE_UNREADABLE_MESSAGE = "Not confirmed: the runner state could not be read."
STATUS_FAILED_MESSAGE = "The {label} status could not be read. It is not shown as running."
STATE_MESSAGES = {
    RUNNING: "The {label} is running.",
    NOT_RUNNING: "The {label} is not running.",
    UNKNOWN: "The {label} state is unknown. It is not shown as running.",
}
STARTED_MESSAGE = "The {label} started, and the runner reports it running (pid {pid})."
STOPPED_MESSAGE = "The {label} is stopped."
TIMEOUT_MESSAGE = (
    "Not confirmed: the {verb} of the {label} did not finish within {seconds} seconds. Nothing was retried. "
    "Check the {label} status before you try again."
)
VERBS = {START: "start", STOP: "stop", STATUS: "status check"}

# Child states the runner's status reply can carry (RunnerStatus.child.state), read as one of three words.
_CHILD_STATE = {
    "running": RUNNING,
    "not_started": NOT_RUNNING,
    "stopped": NOT_RUNNING,
    "crashed": NOT_RUNNING,
    "unknown": UNKNOWN,
}


class RunnerRefused(ValueError):
    """A subcommand, scope or Python that this module will not put in a command. Raised before any process starts."""


@dataclass(frozen=True)
class ControlResult:
    """The outcome of one call. ``state`` is running, not running or unknown. ``pid`` is None when the runner did not report one."""

    action: str
    scope: str
    outcome: str
    message: str
    state: str = UNKNOWN
    pid: int | None = None
    exit_code: int | None = None

    @property
    def confirmed(self) -> bool:
        return self.outcome == CONFIRMED


def _label(scope: str) -> str:
    return SCOPE_LABELS.get(scope, scope)


def build_argv(python: str, subcommand: str, scope: str = DEFAULT_SCOPE) -> list[str]:
    """The argument list for one allowlisted call. Raises RunnerRefused for anything else. Nothing is run here.

    The scope and the subcommand must be exactly one allowlisted value each. The Python must be text with no NUL
    character. No text from the owner or the runner is put into the list.
    """
    if not isinstance(python, str) or not python.strip() or "\x00" in python:
        raise RunnerRefused("the core python is not set")
    if not isinstance(subcommand, str) or subcommand not in ALLOWED_SUBCOMMANDS:
        raise RunnerRefused("the subcommand is not allowed")
    if not isinstance(scope, str) or scope not in ALLOWED_SCOPES:
        raise RunnerRefused("the scope is not allowed")
    argv = [python, "-m", MODULE, subcommand, "--scope", scope]
    if subcommand == START:
        argv += ["--", python, *CHILD_ARGS[scope]]
    return argv


def describe_command(python: str, subcommand: str, scope: str) -> str:
    """The command as the owner reads it in the confirmation dialog. It is the same list build_argv gives."""
    return subprocess.list2cmdline(build_argv(python, subcommand, scope))


def scope_notes(subcommand: str, scope: str) -> str:
    """Fixed plain words about what a call does besides its own work. They are shown in the dialog."""
    if subcommand == START and scope == SCOPE_LOCAL:
        return (
            f"The server first stops any program already using port {LOCAL_DEFAULT_PORT}, or the port set by "
            "MERIDIAN_PORT. It runs in the working folder shown here."
        )
    if subcommand == START and scope == SCOPE_TUNNEL:
        return (
            "The tunnel first stops any program using ports 8808 to 8813, the first six of its slot ports. "
            "The other slot ports are not cleared. It gives claude.ai access to the working folder shown here."
        )
    if subcommand == STOP:
        return "This stops only the process the runner started for this scope, if it is still running."
    return ""


def confirmation_text(python: str, subcommand: str, scope: str, folder: str) -> str:
    """The plain text the confirmation dialog shows: the exact command, the scope and the working folder."""
    notes = scope_notes(subcommand, scope)
    return (
        "The desktop will run this command:\n\n"
        f"{describe_command(python, subcommand, scope)}\n\n"
        f"Scope: {scope}\n"
        f"Working folder: {folder}"
        + (f"\n\n{notes}" if notes else "")
    )


def state_of(payload: object, scope: str) -> str:
    """The child state in one status reply, as running, not running or unknown. Only a status for ``scope`` counts."""
    if not isinstance(payload, dict) or payload.get("scope") != scope:
        return UNKNOWN
    child = payload.get("child")
    raw = child.get("state") if isinstance(child, dict) else None
    if not isinstance(raw, str):
        return UNKNOWN
    return _CHILD_STATE.get(raw, UNKNOWN)


def _pid_of(payload: dict[str, Any]) -> int | None:
    child = payload.get("child")
    pid = child.get("pid") if isinstance(child, dict) else None
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return pid


def _json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _error_type(stderr: bytes) -> str | None:
    """The class name the core printed for a refused start, or None. The rest of stderr is never copied."""
    value = _json_object(stderr) if stderr.strip() else None
    name = value.get("type") if value else None
    return name if isinstance(name, str) else None


def _interpret(subcommand: str, scope: str, completed: Any) -> ControlResult:
    """Turn one finished runner call into a ControlResult. Only fixed text is written, never the runner's output."""
    label = _label(scope)
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    code = completed.returncode
    if len(stdout) > MAX_OUTPUT_BYTES:
        return ControlResult(subcommand, scope, FAILED, TOO_LARGE_MESSAGE, exit_code=code)
    if code != 0:
        if subcommand == START and _error_type(stderr) == "RunnerAlreadyRunningError":
            return ControlResult(
                subcommand, scope, ALREADY_RUNNING, ALREADY_RUNNING_MESSAGE.format(label=label), exit_code=code,
            )
        return ControlResult(
            subcommand, scope, FAILED, EXIT_MESSAGE.format(code=code, verb=VERBS[subcommand]), exit_code=code,
        )
    payload = _json_object(stdout)
    if payload is None:
        return ControlResult(subcommand, scope, FAILED, NO_STATUS_MESSAGE, exit_code=code)
    state = state_of(payload, scope)
    pid = _pid_of(payload)
    if subcommand == STATUS:
        if payload.get("scope") != scope:
            # A reply for another scope is not this scope's status, so it cannot be read as one.
            return ControlResult(subcommand, scope, FAILED, STATUS_FAILED_MESSAGE.format(label=label), exit_code=code)
        return ControlResult(subcommand, scope, CONFIRMED, STATE_MESSAGES[state].format(label=label), state, pid, code)
    if subcommand == START:
        if state == RUNNING:
            message = STARTED_MESSAGE.format(label=label, pid=pid if pid is not None else "not reported")
            return ControlResult(subcommand, scope, CONFIRMED, message, RUNNING, pid, code)
        return ControlResult(
            subcommand, scope, FAILED, NOT_STAYING_MESSAGE.format(label=label, state=state), state, pid, code,
        )
    # stop: confirmed only when the runner reports the child not running. A state the runner still reports as running
    # is not confirmed, and a state that cannot be read is not confirmed either. Neither is ever worded as the other.
    if state == RUNNING:
        return ControlResult(subcommand, scope, NOT_CONFIRMED, STILL_RUNNING_MESSAGE.format(label=label), state, pid, code)
    if state == UNKNOWN:
        return ControlResult(subcommand, scope, NOT_CONFIRMED, STATE_UNREADABLE_MESSAGE, state, pid, code)
    return ControlResult(subcommand, scope, CONFIRMED, STOPPED_MESSAGE.format(label=label), NOT_RUNNING, pid, code)


def format_log_line(stamp: str, action: str, scope: str, outcome: str, pid: int | None) -> str:
    """One control log line: the time, the action, the scope, the outcome and the pid when the runner reported one."""
    reported = str(pid) if pid is not None else "not reported"
    return f"{stamp} {action} scope={scope} outcome={outcome} pid={reported}"


def default_log_path(settings_path: Path | None = None) -> Path:
    """The control log, beside the settings file: %LOCALAPPDATA%\\meridian-desktop, or ~/.meridian-desktop."""
    base = settings_path if settings_path is not None else default_settings_path()
    return base.parent / LOG_NAME


def read_control_log_tail(path: Path | None = None, *, max_bytes: int = DEFAULT_MAX_BYTES) -> LogTail:
    """The bounded, redacted tail of the control log, for the Diagnostics view. It only reads the file."""
    return read_log_tail(path if path is not None else default_log_path(), max_bytes=max_bytes)


def append_control_line(path: Path, line: str) -> None:
    """Append one line to the control log. A log that cannot be written never stops or fails the call.

    The Keys block's refusal listing writes through here too (keys_file.log_listing), so there is one control log file.
    """
    _append_line(path, line)


def _append_line(path: Path, line: str) -> None:
    """Append one line. A log that cannot be written never stops or fails the call."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class RunnerControl:
    """Run the allowlisted runner calls for one core checkout and Python. ``spawn`` is injectable; it defaults to subprocess.run.

    Every method returns a ControlResult and never raises. A call with a refused subcommand or scope starts nothing.
    Start and stop attempts are logged to ``log_path``, or to the default control log when none is given.
    """

    def __init__(
        self,
        python: str | None,
        cwd: str | None,
        *,
        spawn: Callable[..., Any] = subprocess.run,
        log_path: Path | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
        on_started: Callable[[int, str], None] | None = None,
    ) -> None:
        self._python = python or ""
        self._cwd = cwd or ""
        self._spawn = spawn
        self._log_path = log_path
        self._clock = clock
        # Called with (pid, core checkout) when a local start is confirmed with a pid. The restart notice watches it.
        self._on_started = on_started

    def set_on_started(self, callback: Callable[[int, str], None]) -> None:
        """Call ``callback(pid, core checkout)`` when a local start is confirmed with a pid. Set by RunnerActions."""
        self._on_started = callback

    def status(self, scope: str = DEFAULT_SCOPE) -> ControlResult:
        return self._run(STATUS, scope)

    def start(self, scope: str = DEFAULT_SCOPE) -> ControlResult:
        return self._attempt(START, scope)

    def stop(self, scope: str = DEFAULT_SCOPE) -> ControlResult:
        return self._attempt(STOP, scope)

    def _attempt(self, subcommand: str, scope: str) -> ControlResult:
        result = self._run(subcommand, scope)
        if result.outcome != REFUSED:
            self._log(subcommand, scope, result)
        if (
            self._on_started is not None
            and subcommand == START
            and scope == SCOPE_LOCAL
            and result.outcome == CONFIRMED
            and result.pid is not None
        ):
            self._on_started(result.pid, self._cwd)
        return result

    def _run(self, subcommand: str, scope: str) -> ControlResult:
        # A refused subcommand or scope is refused first, whatever the saved settings are.
        if subcommand not in ALLOWED_SUBCOMMANDS or scope not in ALLOWED_SCOPES:
            return ControlResult(subcommand, scope, REFUSED, REFUSED_MESSAGE)
        if not self._python.strip() or not self._cwd.strip():
            return ControlResult(subcommand, scope, NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE)
        try:
            argv = build_argv(self._python, subcommand, scope)
        except RunnerRefused:
            return ControlResult(subcommand, scope, REFUSED, REFUSED_MESSAGE)
        try:
            completed = self._spawn(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=TIMEOUTS[subcommand],
                cwd=self._cwd,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            message = TIMEOUT_MESSAGE.format(
                verb=VERBS[subcommand], label=_label(scope), seconds=int(TIMEOUTS[subcommand]),
            )
            return ControlResult(subcommand, scope, NOT_CONFIRMED, message)
        except (OSError, ValueError):
            # A missing interpreter or working folder, or a path the OS will not start.
            return ControlResult(subcommand, scope, NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE)
        return _interpret(subcommand, scope, completed)

    def _log(self, subcommand: str, scope: str, result: ControlResult) -> None:
        path = self._log_path if self._log_path is not None else default_log_path()
        stamp = self._clock().isoformat(timespec="seconds")
        _append_line(path, format_log_line(stamp, subcommand, scope, result.outcome, result.pid))


__all__ = [
    "ALLOWED_SCOPES",
    "ALLOWED_SUBCOMMANDS",
    "DEFAULT_SCOPE",
    "ALREADY_RUNNING",
    "CONFIRMED",
    "CHILD_ARGS",
    "FAILED",
    "LOCAL_DEFAULT_PORT",
    "LOG_NAME",
    "MODULE",
    "NOT_AVAILABLE",
    "NOT_CONFIRMED",
    "REFUSED",
    "SCOPE_LABELS",
    "SCOPE_LOCAL",
    "SCOPE_TUNNEL",
    "START",
    "START_TIMEOUT_SECONDS",
    "STATUS",
    "STATUS_TIMEOUT_SECONDS",
    "STOP",
    "STOP_TIMEOUT_SECONDS",
    "ControlResult",
    "RunnerControl",
    "RunnerRefused",
    "build_argv",
    "confirmation_text",
    "default_log_path",
    "describe_command",
    "format_log_line",
    "scope_notes",
    "state_of",
]
