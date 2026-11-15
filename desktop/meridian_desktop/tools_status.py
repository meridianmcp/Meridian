"""Which Meridian tools each AI client has set up, and whether the local server and the tunnel are live.
Pure Python: no Qt import.

The desktop runs no MCP server itself: the AI clients start them. So this module reads only what the
desktop can check, and starts or stops no tool:

- each client's Meridian entries, from the Meridian core's client setup plan (ClientSetupRunner.check).
  The plan is the only process this module runs. A wrapper around the injected runner refuses every
  argument list except the plan commands the core setup builds, so this read cannot apply or remove an entry.
- the local Meridian server: GET /health through local_client.probe_health, on a loopback address only.
- the tunnel: one status call through tunnel_status.read_tunnel_status.

The tunnel read goes through the core status command, which may record a recovered exit code in its own
state file, so this module never claims the read leaves no trace.

An AI client starts its own tools, so the desktop cannot see whether one of its entries is running. Those
rows say "Not checked" for live. Every row carries one plain sentence on what to do. No secret or token is
read or kept, and an exception's text is never shown, only its class name. The core's own error text is
never shown either, and a core checkout or Python path never reaches a row or the status line.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meridian_desktop import client_setup_runner, local_client, tunnel_status
from meridian_desktop.client_setup_runner import (
    CHECKED_TEXT,
    CORE_MODULE,
    IN_PLACE,
    MISSING,
    NOT_READABLE,
    WOULD_CHANGE,
    CheckResult,
    ClientRow,
    ClientSetupRunner,
    CommandOutput,
    CommandRunner,
    unchecked_rows,
)
from meridian_desktop.runner_client import RunnerResult
from meridian_desktop.settings_store import Settings, SettingsStore

TIME_TEMPLATE = "Time checked: {}."
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# The configured and live columns each take one of these. Live never takes NOT_SET or NOT_STARTED.
YES = "yes"
NO = "no"
UNCHECKED = "unchecked"
NOT_SET = "not_set"
NOT_STARTED = "not_started"
COLUMN_TEXT = {
    YES: "Yes",
    NO: "No",
    UNCHECKED: "Not checked",
    # The desktop never read these client settings: no usable core checkout or Python was set.
    NOT_SET: "Not checked",
    # The tunnel runner has no record of a tunnel process, so the desktop cannot say whether one is configured.
    NOT_STARTED: "Not checked",
}

# The status word for a row, from its configured and live columns.
STATUS_LIVE = "Live"
STATUS_NOT_LIVE = "Configured but not live"
STATUS_LIVE_UNCHECKED = "Configured, live not checked"
STATUS_NOT_CONFIGURED = "Not configured"
STATUS_NOT_RUNNING = "Not running"
STATUS_UNCHECKED = "Not checked"
# A client row the desktop never read (no usable core location). It reads as not checked, never "Not set".
STATUS_NOT_SET = "Not checked"

ENTRIES_TOOL = "Meridian entries"
THIS_COMPUTER = "This computer"
SERVER_TOOL = "Local Meridian server"
TUNNEL_TOOL = "Tunnel"

LIVE_UNCHECKED_TEXT = "The AI client starts this tool itself, so the desktop cannot see whether it is running."
ENTRY_IN_PLACE_TEXT = "Its settings match the Meridian core's. " + LIVE_UNCHECKED_TEXT
ENTRY_MISSING_TEXT = (
    "Not in {label}'s settings yet. In Settings, use Client setup to apply Meridian's entries for {label}."
)
ENTRY_CHANGED_TEXT = (
    "Its settings differ from the Meridian core's. In Settings, use Client setup to apply Meridian's "
    "entries for {label}."
)
NOT_COVERED_TEXT = (
    "The Meridian core's client setup does not cover {label}, so this panel cannot read its Meridian "
    "entries. Check {label}'s own settings for Meridian tools."
)
NO_RESULT_TEXT = "The Meridian core did not report on {label}. Press Refresh to check again."
NO_PLAN_TEXT = "The core check did not return a plan. See the Logs tab."
CHECK_FAILED_TEXT = "The Meridian setup check failed ({name}). No tool is started or stopped."
# The plan's own success line describes only the plan. The panel's line covers the whole read, including the
# tunnel status call, so the panel shows this line in its place.
PANEL_CHECKED_TEXT = "Checked. No tool is started or stopped."

# The one sentence shown when the core checkout or Python cannot be used. Each names the part at fault and
# never the path: the runner's own message holds the machine-local path, and that never reaches the panel.
CHECKOUT_NOT_SET_TEXT = "The Meridian core checkout is not set. Choose it in Settings."
CHECKOUT_INVALID_TEXT = "The Meridian core checkout is not valid. Choose it again in Settings."
PYTHON_MISSING_TEXT = "The Python interpreter is missing. Choose it again in Settings."

SERVER_RUNNING_TEXT = "The local Meridian server answers on this computer."
SERVER_NOT_RUNNING_TEXT = (
    "The local Meridian server is not running. The desktop starts it only from the Settings tab, after you confirm. "
    "Otherwise start it yourself, then press Refresh."
)
SERVER_NOT_MERIDIAN_TEXT = (
    "Something else answers at the local server address, not Meridian. Check the local server address in Settings."
)
SERVER_NO_ANSWER_TEXT = (
    "The check got no usable answer from the local server, so it is not shown as live. Press Refresh to check again."
)
SERVER_NOT_LOOPBACK_TEXT = (
    "{reason} The desktop checks the local server only at a loopback address. "
    "Change the local server address in Settings."
)
SERVER_CHECK_FAILED_TEXT = "The check failed unexpectedly ({name}), so the server is not shown as live."
TUNNEL_NOT_CHECKED_TEXT = "The desktop did not check the tunnel on this read."

_HEALTH_TEXT = {
    local_client.HEALTH_NOT_RUNNING: SERVER_NOT_RUNNING_TEXT,
    local_client.HEALTH_NOT_MERIDIAN: SERVER_NOT_MERIDIAN_TEXT,
}

# The tunnel child state the runner reports for a tunnel it has no record of.
NOT_STARTED_CHILD = "not_started"

# The argument lists this module runs, after the program path. Each is the core's plan command, either for
# every client at once (ClientSetupRunner.check) or for one core client (its per-client fallback).
PLAN_TAILS = frozenset(
    {
        ("-m", CORE_MODULE, "plan", "--client", "all"),
        ("-m", CORE_MODULE, "plan", "--client", "codex"),
        ("-m", CORE_MODULE, "plan", "--client", "claude-desktop"),
    }
)


def status_word(configured: str, live: str) -> str:
    """The status word for one row. Configured decides first, then live."""
    if configured == NOT_SET:
        return STATUS_NOT_SET
    if configured == NOT_STARTED:
        return STATUS_NOT_RUNNING
    if configured == UNCHECKED:
        return STATUS_UNCHECKED
    if configured == NO:
        return STATUS_NOT_CONFIGURED
    if live == YES:
        return STATUS_LIVE
    if live == NO:
        return STATUS_NOT_LIVE
    return STATUS_LIVE_UNCHECKED


@dataclass(frozen=True)
class ToolRow:
    """One tool's row: where it is, whether it is configured, whether it is live, and what to do."""

    client: str
    tool: str
    configured: str
    live: str
    detail: str

    @property
    def status(self) -> str:
        return status_word(self.configured, self.live)

    def cells(self) -> tuple[str, str, str, str, str, str]:
        """The table cells, in the panel's column order."""
        return (
            self.client,
            self.tool,
            self.status,
            COLUMN_TEXT[self.configured],
            COLUMN_TEXT[self.live],
            self.detail,
        )


@dataclass(frozen=True)
class ToolsStatus:
    """One read of every tool row.

    ``message`` is the panel's plain line about the check. ``checked_at`` is None if no read finished.
    """

    rows: tuple[ToolRow, ...]
    message: str = ""
    checked_at: datetime | None = None


def status_line(status: ToolsStatus) -> str:
    """The panel's status line: the plan's message, then the time checked when a read finished."""
    if status.checked_at is None:
        return status.message
    stamp = TIME_TEMPLATE.format(status.checked_at.strftime(TIME_FORMAT))
    return f"{status.message} {stamp}".strip()


class PlanOnlyRefused(RuntimeError):
    """The runner was asked for something other than the core's plan. The call never reaches the runner."""


def _is_plan(argv: tuple[str, ...], python: str | None) -> bool:
    """True only for the program, then exactly one of PLAN_TAILS. When ``python`` is known, it must be the program."""
    if not argv or argv[1:] not in PLAN_TAILS:
        return False
    return python is None or argv[0] == python


class PlanOnlyRunner:
    """The injected runner, limited to the core's plan. It keeps each plan's raw output so the entry names can be read.

    An argument list runs only when it is exactly one of PLAN_TAILS after the program path, and, when ``python``
    is given, that program is ``python``. Anything else is refused before the runner is called: a remove or
    apply verb, a trailing word, a flag such as --yes, or a client the plan does not name.

    The runner's typed result carries only each client's state, not the entry names, so the names are read
    from the plan's own JSON here. Nothing else is kept.
    """

    def __init__(self, run: CommandRunner, python: str | None = None) -> None:
        self._run = run
        self._python = python
        self.outputs: list[tuple[tuple[str, ...], CommandOutput]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, timeout: int) -> CommandOutput:
        args = tuple(argv)
        if not _is_plan(args, self._python):
            raise PlanOnlyRefused("only the Meridian core's plan command is run here")
        output = self._run(list(args), cwd, timeout)
        self.outputs.append((args, output))
        return output


@dataclass(frozen=True)
class _Entries:
    """The Meridian entry names the plan reported for one client, by what the plan would do with them."""

    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.added) | set(self.changed) | set(self.unchanged)))


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _plan_entries(outputs: Sequence[tuple[tuple[str, ...], CommandOutput]]) -> dict[str, _Entries]:
    """Read the entry names from each successful plan's JSON. A plan that does not read is skipped."""
    found: dict[str, _Entries] = {}
    for argv, output in outputs:
        if not _is_plan(argv, None) or output.returncode != 0:
            continue
        try:
            data = json.loads(output.stdout)
        except (ValueError, RecursionError):
            # A deeply nested reply raises RecursionError, not ValueError. local_client._json_object catches it too.
            continue
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("client"), str):
                continue
            changes = item.get("changes")
            changes = changes if isinstance(changes, dict) else {}
            found[item["client"]] = _Entries(
                added=_names(changes.get("added")),
                changed=_names(changes.get("changed")),
                unchanged=_names(changes.get("unchanged")),
            )
    return found


def _entry_row(label: str, name: str, entries: _Entries) -> ToolRow:
    if name in entries.unchanged:
        return ToolRow(label, name, YES, UNCHECKED, ENTRY_IN_PLACE_TEXT)
    if name in entries.changed:
        return ToolRow(label, name, NO, UNCHECKED, ENTRY_CHANGED_TEXT.format(label=label))
    return ToolRow(label, name, NO, UNCHECKED, ENTRY_MISSING_TEXT.format(label=label))


def _covered_rows(row: ClientRow, entries: _Entries | None) -> list[ToolRow]:
    """Rows for one client the core covers. A client the plan could not read gets fixed words and is not configured."""
    label = row.label
    if row.state == NOT_READABLE:
        return [ToolRow(label, ENTRIES_TOOL, UNCHECKED, UNCHECKED, NO_PLAN_TEXT)]
    if row.state not in (IN_PLACE, WOULD_CHANGE, MISSING):
        return [ToolRow(label, ENTRIES_TOOL, UNCHECKED, UNCHECKED, NO_RESULT_TEXT.format(label=label))]
    if entries is not None and entries.names:
        return [_entry_row(label, name, entries) for name in entries.names]
    if row.state == IN_PLACE:
        return [ToolRow(label, ENTRIES_TOOL, YES, UNCHECKED, ENTRY_IN_PLACE_TEXT)]
    return [ToolRow(label, ENTRIES_TOOL, NO, UNCHECKED, ENTRY_MISSING_TEXT.format(label=label))]


def _client_rows(check: CheckResult, entries: dict[str, _Entries], not_ready: str) -> list[ToolRow]:
    rows: list[ToolRow] = []
    for client_row in check.rows:
        label = client_row.label
        if not client_row.covered:
            rows.append(ToolRow(label, ENTRIES_TOOL, UNCHECKED, UNCHECKED, NOT_COVERED_TEXT.format(label=label)))
        elif not check.configured:
            rows.append(ToolRow(label, ENTRIES_TOOL, NOT_SET, UNCHECKED, not_ready))
        else:
            rows.extend(_covered_rows(client_row, entries.get(client_row.client)))
    return rows


def _not_ready_text(settings: Settings) -> str:
    """The one fixed sentence for a core location the runner would refuse.

    It mirrors client_setup_runner's check order, so the sentence names the same part the refusal names.
    Only the part is kept, never the runner's message, because that message holds the machine-local path.
    """
    checkout = settings.core_checkout.strip()
    python = settings.core_python.strip()
    if not checkout:
        return CHECKOUT_NOT_SET_TEXT
    if not python:
        return PYTHON_MISSING_TEXT
    folder = Path(checkout)
    if not folder.is_dir() or not (folder / "meridian" / "__init__.py").is_file():
        return CHECKOUT_INVALID_TEXT
    if not (folder / "meridian" / "client_setup.py").is_file():
        # The runner refuses a checkout without the client setup command, so the panel says the same thing.
        return CHECKOUT_INVALID_TEXT
    if not Path(python).is_file():
        return PYTHON_MISSING_TEXT
    # The runner refused for a reason this mirror does not see (for example, a change between the two reads).
    return CHECKOUT_INVALID_TEXT


def _load_settings(settings_store: SettingsStore) -> Settings | None:
    """The settings the store holds, or None when the file is nested too deeply to parse.

    SettingsStore.load catches only OSError and ValueError. json.loads raises RecursionError on a deeply nested
    file, so that error reaches the caller unless it is caught here.
    """
    try:
        return settings_store.load().settings
    except RecursionError:
        return None


def _local_server_row(settings: Settings, probe: Callable[[str], str]) -> ToolRow:
    """The local server's health, checked only at a loopback address. The probe is never called otherwise."""
    url = settings.local_url
    try:
        local_client.assert_loopback(url)
    except local_client.LocalClientError as exc:
        return ToolRow(THIS_COMPUTER, SERVER_TOOL, NO, UNCHECKED, SERVER_NOT_LOOPBACK_TEXT.format(reason=str(exc)))
    try:
        health = probe(url)
    except Exception as exc:  # noqa: BLE001 - a failed probe is never live; only the class name is kept
        failure = SERVER_CHECK_FAILED_TEXT.format(name=exc.__class__.__name__)
        return ToolRow(THIS_COMPUTER, SERVER_TOOL, YES, NO, failure)
    if health == local_client.HEALTH_RUNNING:
        return ToolRow(THIS_COMPUTER, SERVER_TOOL, YES, YES, SERVER_RUNNING_TEXT)
    return ToolRow(THIS_COMPUTER, SERVER_TOOL, YES, NO, _HEALTH_TEXT.get(health, SERVER_NO_ANSWER_TEXT))


def _reported_child_state(replies: list[object]) -> str | None:
    """The tunnel state the last reply named, if it is an ok status reply. Read only to tell 'not started' apart."""
    if not replies:
        return None
    reply = replies[-1]
    if not isinstance(reply, RunnerResult) or not reply.ok or not isinstance(reply.payload, dict):
        return None
    child = reply.payload.get("child")
    state = child.get("state") if isinstance(child, dict) else None
    return state if isinstance(state, str) else None


def _tunnel_row(status_call: Callable[[str], RunnerResult] | None, clock: Callable[[], datetime]) -> ToolRow:
    """The tunnel from one read-only status call. Only RUNNING is live; an unknown answer is never shown as live.

    The mapping is tunnel_status's. The reply is only recorded, so a 'not started' reply, which means the runner
    has no record of a tunnel process, can be told apart from a stopped or crashed one.
    """
    if status_call is None:
        return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, UNCHECKED, UNCHECKED, TUNNEL_NOT_CHECKED_TEXT)
    replies: list[object] = []

    def recorded(scope: str) -> RunnerResult:
        reply = status_call(scope)
        replies.append(reply)
        return reply

    result = tunnel_status.read_tunnel_status(recorded, clock=clock)
    if result.state == tunnel_status.RUNNING:
        return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, YES, YES, result.reason)
    if result.state == tunnel_status.NOT_RUNNING:
        detail = " ".join((result.reason, tunnel_status.DEPENDENCY_TEXT, tunnel_status.START_HINT))
        if _reported_child_state(replies) == NOT_STARTED_CHILD:
            return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, NOT_STARTED, NO, detail)
        return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, YES, NO, detail)
    detail = " ".join((result.reason, tunnel_status.CHECK_HINT))
    return ToolRow(THIS_COMPUTER, TUNNEL_TOOL, UNCHECKED, UNCHECKED, detail)


def _settings_unreadable(clock: Callable[[], datetime]) -> ToolsStatus:
    """The read when the settings file is nested too deeply to parse.

    No core location or server address is known, so the core is not run, the server is not probed and the
    tunnel is not read. Every row says so with the module's plain failure text, and none is shown as set up or live.
    """
    failure = CHECK_FAILED_TEXT.format(name=RecursionError.__name__)
    check = CheckResult(rows=unchecked_rows(), message=failure, configured=False)
    rows = _client_rows(check, {}, failure)
    rows.append(
        ToolRow(
            THIS_COMPUTER,
            SERVER_TOOL,
            UNCHECKED,
            UNCHECKED,
            SERVER_CHECK_FAILED_TEXT.format(name=RecursionError.__name__),
        )
    )
    rows.append(_tunnel_row(None, clock))
    return ToolsStatus(rows=tuple(rows), message=failure, checked_at=clock())


def read_tools_status(
    settings_store: SettingsStore,
    *,
    run: CommandRunner | None = None,
    probe: Callable[[str], str] = local_client.probe_health,
    tunnel_status_call: Callable[[str], RunnerResult] | None = None,
    clock: Callable[[], datetime] = datetime.now,
) -> ToolsStatus:
    """One read of every tool row. Never raises: a part that fails becomes a row that says so.

    ``run`` is the injected process runner (run_process by default). ``probe`` takes a base address and
    returns a health word (local_client.probe_health by default). ``tunnel_status_call`` is
    RunnerClient.status, or a fake. No tool is started or stopped.
    """
    settings = _load_settings(settings_store)
    if settings is None:
        return _settings_unreadable(clock)
    plan = PlanOnlyRunner(
        run if run is not None else client_setup_runner.run_process,
        python=settings.core_python.strip(),
    )
    # A deeply nested plan reply raises RecursionError while the core parses it. RecursionError is an Exception,
    # so this clause catches it, and _plan_entries catches it again for the names.
    try:
        check = ClientSetupRunner(settings_store, run=plan).check()
    except Exception as exc:  # noqa: BLE001 - the other rows must still be shown
        check = CheckResult(rows=unchecked_rows(), message=CHECK_FAILED_TEXT.format(name=exc.__class__.__name__))
    message = PANEL_CHECKED_TEXT if check.message == CHECKED_TEXT else check.message
    not_ready = ""
    if not check.configured:
        try:
            not_ready = _not_ready_text(settings)
        except OSError:
            not_ready = CHECKOUT_INVALID_TEXT
        message = not_ready
    rows = _client_rows(check, _plan_entries(plan.outputs), not_ready)
    rows.append(_local_server_row(settings, probe))
    rows.append(_tunnel_row(tunnel_status_call, clock))
    return ToolsStatus(rows=tuple(rows), message=message, checked_at=clock())


__all__ = [
    "CHECKOUT_INVALID_TEXT",
    "CHECKOUT_NOT_SET_TEXT",
    "COLUMN_TEXT",
    "ENTRIES_TOOL",
    "NO",
    "NOT_SET",
    "NOT_STARTED",
    "NO_PLAN_TEXT",
    "PLAN_TAILS",
    "PYTHON_MISSING_TEXT",
    "PlanOnlyRefused",
    "PlanOnlyRunner",
    "STATUS_LIVE",
    "STATUS_LIVE_UNCHECKED",
    "STATUS_NOT_CONFIGURED",
    "STATUS_NOT_LIVE",
    "STATUS_NOT_RUNNING",
    "STATUS_NOT_SET",
    "STATUS_UNCHECKED",
    "ToolRow",
    "ToolsStatus",
    "UNCHECKED",
    "YES",
    "read_tools_status",
    "status_line",
    "status_word",
]
