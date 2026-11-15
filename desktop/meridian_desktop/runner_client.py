"""Read-only client for the core's local runner command. Pure Python: no Qt import.

The core runs its local runner with ``python -m meridian.local_runner``. Each subcommand
prints one JSON object on stdout. This module runs that command for two verbs only, status
and tail-log. Any other verb raises RunnerVerbRefused before a process is started.

status is not a pure read: the core can persist a recovered exit code into the runner's state
file, so the call is a bounded read with one known write. tail-log only reads the log. The
mutating verbs (start, stop, restart, preflight, run-script) are never passed. doctor is excluded
too, because the core writes a probe file during its checks (core-doctor-nonwriting-state-check).

The command runs with shell=False and a list argv, and the scope is one argv element. Every call
has a timeout. A runtime failure never raises: it returns a RunnerResult with a short error code,
fixed message text and, when the command ran to an exit, its exit code. The command's own output
is never copied into an error message. stdout is read whole under the timeout and then checked
against MAX_OUTPUT_BYTES; the core's own bound on tail-log keeps it small.

A successful payload is returned as parsed, with no redaction. The caller must redact log_tail
before showing it.

No interpreter is assumed. A client built without ``python`` reports not_available and starts
nothing, so a frozen build never runs its own executable as the core. The caller supplies the
core's interpreter (its pixi environment) and working directory.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MODULE = "meridian.local_runner"
READ_VERBS = ("status", "tail-log")
# Verbs the core offers that this client never passes. A test pins this list.
MUTATING_VERBS = ("start", "stop", "restart", "preflight", "run-script", "doctor")

DEFAULT_TIMEOUT = 10.0
# The core's own default for tail-log (DEFAULT_LOG_TAIL_BYTES in meridian/local_runner.py).
DEFAULT_TAIL_BYTES = 8000
MAX_TAIL_BYTES = 65536
MAX_OUTPUT_BYTES = 1024 * 1024
SCOPE_MAX_LENGTH = 128

ERR_NOT_AVAILABLE = "not_available"
ERR_TIMEOUT = "timeout"
ERR_TOO_LARGE = "too_large"
ERR_BAD_OUTPUT = "bad_output"
ERR_NONZERO_EXIT = "nonzero_exit"
ERR_BAD_SCOPE = "bad_scope"

NOT_AVAILABLE_MESSAGE = "The Meridian runner command is not available on this computer."
TIMEOUT_MESSAGE = "The runner command did not finish in time."
TOO_LARGE_MESSAGE = f"The runner command printed more than {MAX_OUTPUT_BYTES} bytes."
BAD_SCOPE_MESSAGE = "The runner scope is empty, too long, starts with a dash, or has a control character."
NO_JSON_MESSAGE = "The runner command did not print a JSON object (exit code {code})."
NONZERO_MESSAGE = "The runner command exited with code {code}."
VERB_REFUSED_MESSAGE = "Only the read verbs status and tail-log may be run."

_NO_MODULE = b"No module named"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SpawnFn = Callable[..., "subprocess.CompletedProcess[bytes]"]


class RunnerVerbRefused(ValueError):
    """A verb other than status or tail-log was asked for. Raised before any process starts."""

    def __init__(self) -> None:
        super().__init__(VERB_REFUSED_MESSAGE)


@dataclass(frozen=True)
class RunnerResult:
    """The outcome of one read call. ``ok`` is True only when ``payload`` holds a JSON object.

    On failure ``error_code`` is a short slug and ``error_message`` is fixed text. ``exit_code``
    is set when the command ran to an exit, and is None when it never did (timeout, not_available).
    """

    verb: str
    ok: bool
    payload: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str = ""
    exit_code: int | None = None


def scope_is_usable(scope: object) -> bool:
    """True when ``scope`` is text that can be passed as one argument.

    It must not be blank, must be at most SCOPE_MAX_LENGTH characters, must not start with a
    dash (argparse would read it as an option) and must hold no control character.
    """
    if not isinstance(scope, str) or not scope.strip() or len(scope) > SCOPE_MAX_LENGTH:
        return False
    if scope.startswith("-"):
        return False
    return all(ord(ch) >= 32 and ord(ch) != 127 for ch in scope)


def _checked_timeout(timeout: object) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive, finite number of seconds")
    return float(timeout)


def _checked_tail_bytes(max_bytes: object) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_TAIL_BYTES:
        raise ValueError(f"max_bytes must be an integer from 1 to {MAX_TAIL_BYTES}")
    return max_bytes


def _failure(verb: str, code: str, message: str, exit_code: int | None = None) -> RunnerResult:
    return RunnerResult(verb=verb, ok=False, error_code=code, error_message=message, exit_code=exit_code)


def _json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _read_reply(verb: str, completed: subprocess.CompletedProcess) -> RunnerResult:
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    code = completed.returncode
    if len(stdout) > MAX_OUTPUT_BYTES:
        return _failure(verb, ERR_TOO_LARGE, TOO_LARGE_MESSAGE, code)
    if code != 0 and not stdout.strip() and _NO_MODULE in stderr:
        # The core package or its runner module is not on the interpreter's path. stderr is read
        # for this one check only, and nothing from it is copied anywhere.
        return _failure(verb, ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, code)
    payload = _json_object(stdout)
    if payload is None:
        return _failure(verb, ERR_BAD_OUTPUT, NO_JSON_MESSAGE.format(code=code), code)
    if code != 0:
        return _failure(verb, ERR_NONZERO_EXIT, NONZERO_MESSAGE.format(code=code), code)
    return RunnerResult(verb=verb, ok=True, payload=payload, exit_code=code)


class RunnerClient:
    """Run the core runner's read verbs. Every call returns a RunnerResult or raises
    RunnerVerbRefused (for a verb that is not a read verb). ``spawn`` is injectable so tests
    can see each call without starting a process; it defaults to subprocess.run.
    """

    def __init__(
        self,
        *,
        python: str | os.PathLike[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        spawn: SpawnFn = subprocess.run,
    ) -> None:
        self._python = os.fspath(python) if python is not None else None
        self._cwd = cwd
        self._timeout = _checked_timeout(timeout)
        self._spawn = spawn

    def status(self, scope: str) -> RunnerResult:
        """The runner's status snapshot for ``scope``. See the module docstring for its one write."""
        return self.run("status", scope)

    def tail_log(self, scope: str, *, max_bytes: int = DEFAULT_TAIL_BYTES) -> RunnerResult:
        """The tail of the runner's log for ``scope``, at most ``max_bytes`` from the end of the file."""
        return self.run("tail-log", scope, max_bytes=max_bytes)

    def run(self, verb: str, scope: str, *, max_bytes: int = DEFAULT_TAIL_BYTES) -> RunnerResult:
        """Run one read verb for ``scope``.

        Raise RunnerVerbRefused for any verb other than status or tail-log, before anything is
        spawned. Raise ValueError for a max_bytes outside 1 to MAX_TAIL_BYTES. A bad scope or a
        missing interpreter returns an error result without spawning.
        """
        if verb not in READ_VERBS:
            raise RunnerVerbRefused()
        tail_bytes = _checked_tail_bytes(max_bytes)
        if not scope_is_usable(scope):
            return _failure(verb, ERR_BAD_SCOPE, BAD_SCOPE_MESSAGE)
        if self._python is None:
            return _failure(verb, ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE)
        argv = [self._python, "-m", MODULE, verb, "--scope", scope]
        if verb == "tail-log":
            argv += ["--max-bytes", str(tail_bytes)]
        return self._call(verb, argv)

    def _call(self, verb: str, argv: list[str]) -> RunnerResult:
        try:
            completed = self._spawn(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                cwd=self._cwd,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return _failure(verb, ERR_TIMEOUT, TIMEOUT_MESSAGE)
        except (OSError, ValueError):
            # A missing interpreter or working directory, or a path the OS will not start.
            return _failure(verb, ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE)
        return _read_reply(verb, completed)


__all__ = [
    "DEFAULT_TAIL_BYTES",
    "DEFAULT_TIMEOUT",
    "ERR_BAD_OUTPUT",
    "ERR_BAD_SCOPE",
    "ERR_NONZERO_EXIT",
    "ERR_NOT_AVAILABLE",
    "ERR_TIMEOUT",
    "ERR_TOO_LARGE",
    "MAX_OUTPUT_BYTES",
    "MAX_TAIL_BYTES",
    "MODULE",
    "MUTATING_VERBS",
    "READ_VERBS",
    "RunnerClient",
    "RunnerResult",
    "RunnerVerbRefused",
    "scope_is_usable",
]
