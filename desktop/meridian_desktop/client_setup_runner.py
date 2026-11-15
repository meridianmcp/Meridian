"""Run the Meridian core's client setup command and read what it prints. Pure Python: no Qt import.

The desktop never edits a client's settings file itself. It starts the core's own command in a
subprocess, from the core checkout, with an argument list and no shell, and reads the JSON the
command prints. The command is <core python> -m meridian.client_setup. Plan changes nothing. Apply
and Remove change a file only when they run with --yes, and the core makes a backup first.

Every outcome is a typed value, so the Settings page can say what happened in plain words:
  - a core checkout or Python that is not set, or missing, is a refusal and no process starts;
  - a command that fails, times out or cannot start is an error with plain text;
  - only names, paths and actions are read from the output. A file's document or text is never kept.
    When the command fails without the core's JSON error, only the last few stderr lines are shown,
    with secret-shaped values redacted and each line cut to a fixed length (stderr_detail).
  - a checkout without meridian/client_setup.py is refused before any process starts, because the
    command cannot run from it.
The process runner is injected, so tests use a fake and never start the real command.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian_desktop.desktop_log import redact_text
from meridian_desktop.settings_store import SettingsStore

COMMAND_TIMEOUT_SECONDS = 60
CORE_MODULE = "meridian.client_setup"
MODES = ("apply", "remove")

# The desktop's clients in display order: (id, label). The ids are the core's --client values.
CLIENTS: tuple[tuple[str, str], ...] = (
    ("claude-code", "Claude Code"),
    ("codex", "Codex"),
    ("claude-desktop", "Claude Desktop"),
)
LABELS = dict(CLIENTS)
# The clients the core's client setup command writes. A client outside this set has no command to run.
CORE_CLIENTS = frozenset({"codex", "claude-desktop"})

# Row states, as the Settings page shows them.
IN_PLACE = "in_place"
WOULD_CHANGE = "would_change"
MISSING = "missing"
NOT_READABLE = "not_readable"
NOT_COVERED = "not_covered"
NOT_CHECKED = "not_checked"
STATE_TEXT = {
    IN_PLACE: "In place",
    WOULD_CHANGE: "Would change",
    MISSING: "Missing",
    NOT_READABLE: "Not readable",
    NOT_COVERED: "Not covered by the Meridian core",
    NOT_CHECKED: "Not checked yet",
}

# Kinds of command result, so a caller can branch on the kind and not on the text.
KIND_OK = "ok"
KIND_NOT_CONFIGURED = "not_configured"
KIND_NOT_COVERED = "not_covered"
KIND_TIMEOUT = "timeout"
KIND_CANNOT_START = "cannot_start"
KIND_CORE_ERROR = "core_error"
KIND_FAILED = "failed"
KIND_BAD_OUTPUT = "bad_output"

# The actions the core reports, for a dry run and for a real change.
PREVIEW_ACTIONS = frozenset({"unchanged", "would_write", "would_remove"})
CHANGE_ACTIONS = frozenset({"unchanged", "written", "removed"})

NOT_SET_CHECKOUT_TEXT = (
    "Meridian core checkout is not set. Press Find it again, or Choose the Meridian core folder, "
    "and the Python is found after that."
)
NOT_SET_PYTHON_TEXT = (
    "Python for Meridian core is not set. Press Find it again, or pick the python.exe with Browse and Save."
)
CHECKOUT_MISSING_TEXT = (
    "The Meridian core checkout folder does not exist: {path}. In Settings, choose the folder again "
    "and save it."
)
CHECKOUT_NOT_CORE_TEXT = (
    "{path} is not a Meridian core checkout: it has no meridian package. In Settings, choose the "
    "folder that holds meridian/ and save it."
)
PYTHON_MISSING_TEXT = (
    "The Python program for Meridian core does not exist: {path}. In Settings, choose the python.exe "
    "again and save it."
)
TIMEOUT_TEXT = (
    "The Meridian setup command did not finish within {seconds} seconds. Check that the Python for "
    "Meridian core runs, then press Check setup again."
)
CANNOT_START_TEXT = (
    "The Python for Meridian core could not be started ({name}). In Settings, choose a python.exe "
    "that runs."
)
FAILED_TEXT = (
    "The Meridian setup command failed with exit code {code}. Check the Meridian core checkout and "
    "Python, then try again."
)
BAD_OUTPUT_TEXT = (
    "The Meridian setup command printed output this desktop does not understand. Update the Meridian "
    "core checkout, then try again."
)
NOT_COVERED_TEXT = (
    "The Meridian core's client setup does not cover {label} yet, so this desktop cannot set it up. "
    "Update the Meridian core checkout to a version that covers it, then press Check setup."
)
NOT_COVERED_ACTION_TEXT = (
    "{label} is not covered by the Meridian core's client setup, so nothing is changed for it."
)
NO_RESULT_TEXT = "The Meridian core did not report on this client. Press Check setup again."
UNKNOWN_ACTION_TEXT = "The Meridian core reported an action this desktop does not know ({action})."
CHECKED_TEXT = "Checked. Nothing was written."
PARTIAL_TEXT = "The Meridian core could not read every client. The rows say why."
BAD_MODE_TEXT = "Unknown setup action."
NO_SETUP_COMMAND_TEXT = (
    "The Meridian core checkout at {path} has no client setup command (meridian/client_setup.py), so this "
    "desktop cannot run the setup from it. In Settings, choose a Meridian core checkout that has that file, "
    "then press Check setup."
)
STDERR_HEADING_TEXT = "Last lines of the setup command's error output:"
# At most this many stderr lines are shown, and each is cut to this many characters. Redaction runs first.
STDERR_TAIL_LINES = 5
STDERR_LINE_CHARS = 240


@dataclass(frozen=True)
class CommandOutput:
    """What a finished process printed. The runner reads only these three fields."""

    returncode: int
    stdout: str
    stderr: str


# The process runner: (argv, working directory, timeout in seconds) -> CommandOutput. It raises
# subprocess.TimeoutExpired on a timeout and OSError when the program cannot start.
CommandRunner = Callable[[Sequence[str], Path, int], CommandOutput]


def run_process(argv: Sequence[str], cwd: Path, timeout: int) -> CommandOutput:
    """Run argv with no shell, from cwd, and wait at most timeout seconds."""
    extra: dict[str, object] = {}
    if sys.platform == "win32":
        # A GUI app must not open a console window for the child process.
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        **extra,
    )
    return CommandOutput(completed.returncode, completed.stdout or "", completed.stderr or "")


@dataclass(frozen=True)
class ClientChange:
    """One client's result from the core: its file, the action, and the names of the entries."""

    client: str
    path: str
    action: str
    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    backup: str | None = None
    restart_required: bool = False


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one command. ok is True only when the core ran and its output was read.

    detail holds the last stderr lines of a failure that had no JSON error (see stderr_detail). It is empty otherwise.
    """

    ok: bool
    kind: str
    message: str = ""
    changes: tuple[ClientChange, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ClientRow:
    """One client's row on the Settings page."""

    client: str
    label: str
    state: str
    path: str = ""
    detail: str = ""

    @property
    def covered(self) -> bool:
        return self.client in CORE_CLIENTS


@dataclass(frozen=True)
class CheckResult:
    """The whole check: one row per client, and a plain-words message about the check as a whole."""

    rows: tuple[ClientRow, ...]
    message: str = ""
    configured: bool = True
    detail: str = ""


class _NotReady(Exception):
    """The core checkout or Python is not set, or is missing. Carries the plain-words reason."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def names_text(change: ClientChange) -> str:
    """'Adds: a, b. Changes: c. Removes: d.' Names only. Says so plainly when nothing changes."""
    parts = []
    if change.added:
        parts.append("Adds: " + ", ".join(change.added) + ".")
    if change.changed:
        parts.append("Changes: " + ", ".join(change.changed) + ".")
    if change.removed:
        parts.append("Removes: " + ", ".join(change.removed) + ".")
    return " ".join(parts) if parts else "No Meridian entry changes."


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def parse_changes(stdout: str) -> tuple[ClientChange, ...]:
    """Read the core's JSON result. Raises ValueError when the shape is not what the core prints."""
    data = json.loads(stdout)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("no results list")
    changes: list[ClientChange] = []
    for item in data["results"]:
        if not isinstance(item, dict):
            raise ValueError("a result is not an object")
        client, path, action = item.get("client"), item.get("path"), item.get("action")
        if not all(isinstance(value, str) for value in (client, path, action)):
            raise ValueError("a result is missing client, path or action")
        names = item.get("changes") if isinstance(item.get("changes"), dict) else {}
        backup = item.get("backup")
        if backup is not None and not isinstance(backup, str):
            raise ValueError("backup is not text")
        changes.append(
            ClientChange(
                client=client,
                path=path,
                action=action,
                added=_names(names.get("added")),
                changed=_names(names.get("changed")),
                removed=_names(names.get("removed")),
                backup=backup,
                restart_required=item.get("restart_required") is True,
            )
        )
    return tuple(changes)


def _core_error(stderr: str) -> str | None:
    """The core's own {"error": "..."} message, or None when stderr is not that shape."""
    try:
        data = json.loads(stderr)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("error"), str) and data["error"].strip():
        return data["error"].strip()
    return None


def stderr_detail(stderr: str) -> str:
    """The heading and the last few non-empty stderr lines, redacted and cut. Empty when stderr holds no text.

    Redaction runs on each whole line before it is cut, so a secret cut at the limit is never left half shown.
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return ""
    tail = [redact_text(line)[:STDERR_LINE_CHARS] for line in lines[-STDERR_TAIL_LINES:]]
    return "\n".join([STDERR_HEADING_TEXT, *tail])


def message_with_detail(message: str, detail: str) -> str:
    """The message, then the detail on its own line. Either part may be empty."""
    return "\n".join(part for part in (message, detail) if part)


def _argv(python: str, mode: str, client: str, *, write: bool) -> list[str]:
    argv = [python, "-m", CORE_MODULE, mode, "--client", client]
    if write:
        argv.append("--yes")
    return argv


def _plan_argv(python: str, client: str) -> list[str]:
    return [python, "-m", CORE_MODULE, "plan", "--client", client]


def _not_covered_row(client: str, label: str) -> ClientRow:
    return ClientRow(client, label, NOT_COVERED, detail=NOT_COVERED_TEXT.format(label=label))


def unchecked_rows() -> tuple[ClientRow, ...]:
    """One row per client before any check. Claude Code is shown as not covered from the start."""
    return tuple(
        _not_covered_row(client, label) if client not in CORE_CLIENTS else ClientRow(client, label, NOT_CHECKED)
        for client, label in CLIENTS
    )


def _row_from_change(change: ClientChange, label: str) -> ClientRow:
    """Turn one core result into a row. Missing is decided by whether the file exists now."""
    if change.action == "unchanged":
        return ClientRow(change.client, label, IN_PLACE, path=change.path)
    if change.action == "would_write":
        state = WOULD_CHANGE if Path(change.path).exists() else MISSING
        return ClientRow(change.client, label, state, path=change.path, detail=names_text(change))
    return ClientRow(
        change.client,
        label,
        NOT_READABLE,
        path=change.path,
        detail=UNKNOWN_ACTION_TEXT.format(action=change.action),
    )


class ClientSetupRunner:
    """Check, preview and change the clients' setup through the core command. The run function is injected."""

    def __init__(self, settings_store: SettingsStore, run: CommandRunner | None = None) -> None:
        self._store = settings_store
        self._run = run if run is not None else run_process

    # ----------------------------------------------------------- core location

    def _location(self) -> tuple[Path, str]:
        """The core checkout and Python, checked. Raises _NotReady with plain words when either is not usable."""
        settings = self._store.load().settings
        checkout = settings.core_checkout.strip()
        python = settings.core_python.strip()
        if not checkout:
            raise _NotReady(NOT_SET_CHECKOUT_TEXT)
        if not python:
            raise _NotReady(NOT_SET_PYTHON_TEXT)
        checkout_path = Path(checkout)
        if not checkout_path.is_dir():
            raise _NotReady(CHECKOUT_MISSING_TEXT.format(path=checkout))
        if not (checkout_path / "meridian" / "__init__.py").is_file():
            raise _NotReady(CHECKOUT_NOT_CORE_TEXT.format(path=checkout))
        if not (checkout_path / "meridian" / "client_setup.py").is_file():
            # An older checkout has the package but not the command. Starting it would fail with exit code 1.
            raise _NotReady(NO_SETUP_COMMAND_TEXT.format(path=checkout))
        if not Path(python).is_file():
            raise _NotReady(PYTHON_MISSING_TEXT.format(path=python))
        return checkout_path, python

    # ----------------------------------------------------------- running

    def _invoke(self, argv: list[str], cwd: Path) -> CommandResult:
        """Run one command and read its result. Every failure becomes a typed result, never a raise."""
        try:
            output = self._run(list(argv), cwd, COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return CommandResult(False, KIND_TIMEOUT, TIMEOUT_TEXT.format(seconds=COMMAND_TIMEOUT_SECONDS))
        except OSError as exc:
            return CommandResult(False, KIND_CANNOT_START, CANNOT_START_TEXT.format(name=exc.__class__.__name__))
        if output.returncode != 0:
            core_message = _core_error(output.stderr)
            if core_message is not None:
                return CommandResult(False, KIND_CORE_ERROR, core_message)
            return CommandResult(
                False,
                KIND_FAILED,
                FAILED_TEXT.format(code=output.returncode),
                detail=stderr_detail(output.stderr),
            )
        try:
            changes = parse_changes(output.stdout)
        except ValueError:
            return CommandResult(False, KIND_BAD_OUTPUT, BAD_OUTPUT_TEXT)
        return CommandResult(True, KIND_OK, changes=changes)

    # ----------------------------------------------------------- check

    def check(self) -> CheckResult:
        """Plan for every client. Changes nothing on disk."""
        try:
            checkout, python = self._location()
        except _NotReady as exc:
            return CheckResult(rows=unchecked_rows(), message=exc.message, configured=False)
        result = self._invoke([python, "-m", CORE_MODULE, "plan", "--client", "all"], checkout)
        if result.ok:
            return self._rows_from_changes(result.changes)
        if result.kind == KIND_CORE_ERROR:
            return self._check_one_client_at_a_time(checkout, python)
        return CheckResult(rows=unchecked_rows(), message=result.message, detail=result.detail)

    def _rows_from_changes(self, changes: tuple[ClientChange, ...]) -> CheckResult:
        by_client = {change.client: change for change in changes}
        rows: list[ClientRow] = []
        for client, label in CLIENTS:
            if client not in CORE_CLIENTS:
                rows.append(_not_covered_row(client, label))
            elif client in by_client:
                rows.append(_row_from_change(by_client[client], label))
            else:
                rows.append(ClientRow(client, label, NOT_CHECKED, detail=NO_RESULT_TEXT))
        return CheckResult(rows=tuple(rows), message=CHECKED_TEXT)

    def _check_one_client_at_a_time(self, checkout: Path, python: str) -> CheckResult:
        """Used when 'plan --client all' fails with the core's own error. One unreadable file must not hide the others.

        Each core client is planned on its own. A client that still fails gets a 'not readable' row
        that carries the core's message. Claude Code has no command, so its row is never planned.
        """
        rows: list[ClientRow] = []
        for client, label in CLIENTS:
            if client not in CORE_CLIENTS:
                rows.append(_not_covered_row(client, label))
                continue
            result = self._invoke(_plan_argv(python, client), checkout)
            match = next((change for change in result.changes if change.client == client), None)
            if result.ok and match is not None:
                rows.append(_row_from_change(match, label))
            else:
                shown = message_with_detail(result.message, result.detail)
                rows.append(ClientRow(client, label, NOT_READABLE, detail=shown or NO_RESULT_TEXT))
        return CheckResult(rows=tuple(rows), message=PARTIAL_TEXT)

    # ----------------------------------------------------------- preview and change

    def describe_command(self, client: str, mode: str) -> str:
        """The command a Yes would run, for the confirmation dialog. It is text only and runs nothing."""
        try:
            checkout, python = self._location()
        except _NotReady as exc:
            return exc.message
        return f"{subprocess.list2cmdline(_argv(python, mode, client, write=True))}  (run in {checkout})"

    def preview(self, client: str, mode: str) -> CommandResult:
        """A dry run of apply or remove for one client. It writes nothing."""
        return self._act(client, mode, write=False)

    def run_change(self, client: str, mode: str) -> CommandResult:
        """Apply or remove one client, with --yes. The core makes a backup first. Call only after a yes."""
        return self._act(client, mode, write=True)

    def _act(self, client: str, mode: str, *, write: bool) -> CommandResult:
        if mode not in MODES:
            return CommandResult(False, KIND_BAD_OUTPUT, BAD_MODE_TEXT)
        label = LABELS.get(client, client)
        if client not in CORE_CLIENTS:
            return CommandResult(False, KIND_NOT_COVERED, NOT_COVERED_ACTION_TEXT.format(label=label))
        try:
            checkout, python = self._location()
        except _NotReady as exc:
            return CommandResult(False, KIND_NOT_CONFIGURED, exc.message)
        result = self._invoke(_argv(python, mode, client, write=write), checkout)
        if not result.ok:
            return result
        allowed = CHANGE_ACTIONS if write else PREVIEW_ACTIONS
        if len(result.changes) != 1 or result.changes[0].client != client or result.changes[0].action not in allowed:
            return CommandResult(False, KIND_BAD_OUTPUT, BAD_OUTPUT_TEXT)
        return result


__all__ = [
    "CLIENTS",
    "CORE_CLIENTS",
    "CORE_MODULE",
    "COMMAND_TIMEOUT_SECONDS",
    "CheckResult",
    "ClientChange",
    "ClientRow",
    "ClientSetupRunner",
    "CommandOutput",
    "CommandResult",
    "IN_PLACE",
    "MISSING",
    "NOT_CHECKED",
    "NOT_COVERED",
    "NOT_READABLE",
    "NO_SETUP_COMMAND_TEXT",
    "STATE_TEXT",
    "STDERR_HEADING_TEXT",
    "STDERR_LINE_CHARS",
    "STDERR_TAIL_LINES",
    "WOULD_CHANGE",
    "message_with_detail",
    "names_text",
    "parse_changes",
    "run_process",
    "stderr_detail",
    "unchecked_rows",
]
