"""Is the local tunnel running? The Home tunnel card's status line. Pure Python: no Qt import.

The tunnel runs as the runner's child process for the scope named ``tunnel``. The check is one
read-only runner call, ``status --scope tunnel``, made through runner_client.RunnerClient.status.
That client never passes a mutating verb. The child state in the reply becomes one of three words:

- running: the runner reports the tunnel process as running (verified alive right now);
- not running: the runner reports it as not started, stopped or crashed;
- unknown: everything else. A failed call, a missing runner, a reply that is not a status for the
  tunnel scope, a child state this module does not read, or a status call that raises. Unknown is
  never shown as running (local-trust contract, rule 7).

Each check result carries the time the check finished and one fixed sentence saying why. The
sentence never copies the runner's own output. This module starts nothing, stops nothing and holds
no secret. It also holds the words that name the tools which connect through the tunnel, and the
plain-words instructions for starting the tunnel by hand. The desktop starts and stops the tunnel only
from the Tunnel panel's buttons in Settings (ui/tunnel_panel.py), after the owner confirms each one, and only when
tunnel_control_enabled is true (docs/local-trust-contract.md). This module itself still starts nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult

TUNNEL_SCOPE = "tunnel"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# The three words a check can give.
RUNNING = "running"
NOT_RUNNING = "not running"
UNKNOWN = "unknown"

# Child states the runner's status reply can carry (RunnerStatus.child.state). Any other value is unknown.
_CHILD_STATE = {
    "running": RUNNING,
    "not_started": NOT_RUNNING,
    "stopped": NOT_RUNNING,
    "crashed": NOT_RUNNING,
    "unknown": UNKNOWN,
}
_CHILD_REASON = {
    "running": "The runner reports the tunnel process as running.",
    "not_started": "The runner has no record of a tunnel process.",
    "stopped": "The runner reports the tunnel process as stopped.",
    "crashed": "The runner reports the tunnel process as exited with an error.",
    "unknown": "The runner could not verify whether the tunnel process is running.",
}

UNKNOWN_PREFIX = "The tunnel state is unknown."
NO_RESULT_TEXT = "The runner status check gave no result."
NO_RESULT_REASON = f"{UNKNOWN_PREFIX} {NO_RESULT_TEXT}"
NOT_AVAILABLE_REASON = (
    f"{UNKNOWN_PREFIX} {NOT_AVAILABLE_MESSAGE} The desktop cannot check the tunnel without that command."
)
WRONG_SCOPE_REASON = f"{UNKNOWN_PREFIX} The runner reply is not a status for the tunnel scope."
NO_CHILD_REASON = f"{UNKNOWN_PREFIX} The runner reply did not include a tunnel process state."
UNREAD_REASON = f"{UNKNOWN_PREFIX} The runner reports a tunnel process state the desktop does not read."
# Filled with the exception's class name only. The exception text is never shown.
CHECK_FAILED_REASON = f"{UNKNOWN_PREFIX} The status check failed unexpectedly ({{}})."

# The tools that connect through the tunnel, named as the user sees them.
DEPENDENT_TOOLS = ("codebase-memory", "Serena", "filesystem")
DEPENDENCY_TEXT = (
    "The codebase-memory, Serena and filesystem tools connect through the tunnel. "
    "While the tunnel is not running, they are unavailable."
)

# Where the owner starts or checks the tunnel. The buttons are in the Tunnel panel in Settings, and Start asks first. No
# command is typed by hand, so these hints name the panel and never a terminal command.
START_HINT = (
    "To start it, press Start on the Home tunnel card or in the Tunnel panel. The desktop asks first and starts nothing without a yes."
)
CHECK_HINT = "Press Check now on Home to check it again."

# The top bar's one line and its detail line in Hosted mode. The Home tunnel card shows the same answers
# with its own headlines. The check time and "no time" text are shared with it.
BAR_HEADLINES = {
    RUNNING: "Tunnel running",
    NOT_RUNNING: "Tunnel not running",
    UNKNOWN: "Tunnel status unknown",
}
BAR_CHECKING_TEXT = "Tunnel status: checking..."
CHECKED_TEMPLATE = "Checked at {}."
NO_TIME_TEXT = "The check did not finish, so no time was recorded."
NO_FAILURE_NAME = "unknown error"
@dataclass(frozen=True)
class TunnelStatus:
    """One check's answer. ``state`` is RUNNING, NOT_RUNNING or UNKNOWN. ``reason`` is one fixed sentence."""

    state: str
    reason: str
    checked_at: datetime


def tunnel_state_of(result: object) -> tuple[str, str]:
    """Map one runner status reply to (state, reason). Pure: no call is made here.

    Only a reply that is ok, names the tunnel scope and carries a known child state can give running
    or not running. Everything else, including a value that is not a RunnerResult, is unknown.
    """
    if not isinstance(result, RunnerResult):
        return UNKNOWN, NO_RESULT_REASON
    if not result.ok:
        if result.error_code == ERR_NOT_AVAILABLE:
            return UNKNOWN, NOT_AVAILABLE_REASON
        return UNKNOWN, f"{UNKNOWN_PREFIX} {result.error_message or NO_RESULT_TEXT}"
    payload = result.payload
    if not isinstance(payload, dict) or payload.get("scope") != TUNNEL_SCOPE:
        return UNKNOWN, WRONG_SCOPE_REASON
    child = payload.get("child")
    child_state = child.get("state") if isinstance(child, dict) else None
    if not isinstance(child_state, str) or not child_state:
        return UNKNOWN, NO_CHILD_REASON
    if child_state not in _CHILD_STATE:
        return UNKNOWN, UNREAD_REASON
    return _CHILD_STATE[child_state], _CHILD_REASON[child_state]


def read_tunnel_status(
    status: Callable[[str], RunnerResult],
    *,
    clock: Callable[[], datetime] = datetime.now,
) -> TunnelStatus:
    """Run one read-only status call for the tunnel scope and map its reply.

    ``status`` is RunnerClient.status, or a fake in tests. It is called once, with TUNNEL_SCOPE. A
    call that raises is unknown, never running, and only its class name is kept. The check time is
    read after the call returns.
    """
    try:
        result = status(TUNNEL_SCOPE)
    except Exception as exc:  # noqa: BLE001 - a failed call is unknown, never running
        state, reason = UNKNOWN, CHECK_FAILED_REASON.format(exc.__class__.__name__)
    else:
        state, reason = tunnel_state_of(result)
    return TunnelStatus(state=state, reason=reason, checked_at=clock())


def bar_texts(status: TunnelStatus | None, failure: str | None = None) -> tuple[str, str]:
    """The top bar's (line, detail) for one finished check. Pure: no call and no Qt.

    ``status`` is None when the check raised. Then ``failure`` holds only the exception's class name,
    and the line says no time was recorded. Otherwise the line names the state and the time checked.
    The detail says why, which tools need the tunnel, and for a tunnel that is not running (or not
    known) how to start it or check it by hand. Nothing here starts or stops anything.
    """
    if status is None:
        line = f"{BAR_HEADLINES[UNKNOWN]}. {NO_TIME_TEXT}"
        reason = CHECK_FAILED_REASON.format(failure or NO_FAILURE_NAME)
        return line, " ".join((reason, DEPENDENCY_TEXT, CHECK_HINT))
    line = f"{BAR_HEADLINES[status.state]}. {CHECKED_TEMPLATE.format(status.checked_at.strftime(TIME_FORMAT))}"
    parts = [status.reason, DEPENDENCY_TEXT]
    if status.state == NOT_RUNNING:
        parts.append(START_HINT)
    elif status.state == UNKNOWN:
        parts.append(CHECK_HINT)
    return line, " ".join(parts)


__all__ = [
    "BAR_CHECKING_TEXT",
    "BAR_HEADLINES",
    "CHECK_HINT",
    "CHECKED_TEMPLATE",
    "CHECK_FAILED_REASON",
    "DEPENDENCY_TEXT",
    "DEPENDENT_TOOLS",
    "NOT_AVAILABLE_REASON",
    "NOT_RUNNING",
    "NO_CHILD_REASON",
    "NO_FAILURE_NAME",
    "NO_RESULT_REASON",
    "NO_TIME_TEXT",
    "RUNNING",
    "START_HINT",
    "TIME_FORMAT",
    "TUNNEL_SCOPE",
    "TunnelStatus",
    "UNKNOWN",
    "UNREAD_REASON",
    "WRONG_SCOPE_REASON",
    "bar_texts",
    "read_tunnel_status",
    "tunnel_state_of",
]
