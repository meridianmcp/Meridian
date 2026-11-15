"""The words and rows of the Tunnel panel (ui/tunnel_panel.py). Pure Python: no Qt import.

The Tunnel panel is the one place in the desktop for the tunnel. It shows six things, and this module makes the
text and the rows for each one:

- the status: running or not, and when the tunnel was last checked (from tunnel_status.TunnelStatus);
- the two lanes: the direct lane (claude.ai reaches Meridian core and Tigris directly) and the tunnel lane, with one
  line on what each lane needs;
- the slots: the tunnel's fixed slot ports (slot_ports.SLOT_PORTS), one row each, with the plugin each serves and the
  result of the last connect-only port check: listening or not listening, never healthy;
- the recent log: the last LOG_TAIL_LINES lines of the tunnel log, already redacted by log_tail.read_tunnel_log_tail;
- the tool groups the tunnel exposes, each on or off, using the existing tool-group settings (tool_catalog.py and the
  disabled_tools setting);
- the custom connectors, with their tunnel_exposed flag, and the custom_tunnel_opt_in setting, which is off by default.

Nothing here starts, stops, saves or reads a file. The panel calls the status check, the log read and the saves; this
module only turns their answers into words and rows. A state that is not running is never shown as running
(local-trust contract, rule 7).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from meridian_desktop import custom_connectors, log_tail, slot_ports, tool_catalog, tunnel_status
from meridian_desktop.slot_ports import SlotPortResult
from meridian_desktop.log_tail import LogTail
from meridian_desktop.settings_store import Settings
from meridian_desktop.tunnel_status import NOT_RUNNING, RUNNING, TunnelStatus

# The status headlines. The running headline says the tunnel process runs. It does not say claude.ai is connected.
RUNNING_HEADLINE = "Tunnel process running"
NOT_CONNECTED_HEADLINE = "Not connected"
UNKNOWN_HEADLINE = "Not connected: the status is unknown"
NOT_CHECKED_HEADLINE = "Not checked yet"
CHECKING_TEXT = "Checking the tunnel. Nothing is started or stopped."
PANEL_START_HINT = "To start it, press Start below. The desktop asks first and starts nothing without a yes."
PANEL_CHECK_HINT = "Press Check now to check it again."

PANEL_TITLE = "Tunnel"
PANEL_INTRO = (
    "The tunnel is how claude.ai reaches this computer's tools. Check it, start or stop it, and choose what it "
    "exposes, all in this one place. Start, Stop and Restart each ask first. Nothing is started on its own."
)
STATUS_HEADING = "Status"
LANES_HEADING = "Lanes"
GROUPS_HEADING = "Tool groups"
CONNECTORS_HEADING = "Custom connectors"
CHECK_BUTTON_TEXT = "Check now"

# The two lanes. The direct lane needs no desktop; the tunnel lane does.
DIRECT_LANE_NAME = "Direct lane"
TUNNEL_LANE_NAME = "Tunnel lane"
DIRECT_LANE_NEEDS = (
    "claude.ai reaches Meridian core and Tigris directly. It needs the hosted Meridian service. "
    "It does not need this desktop or the tunnel."
)
DIRECT_LANE_LIVE = "Not checked by this panel."
TUNNEL_LANE_NEEDS = (
    "claude.ai reaches this computer's codebase-memory, Serena and filesystem tools through the tunnel. "
    "It needs this desktop or the tunnel running."
)
TUNNEL_LANE_LIVE = "Live."
TUNNEL_LANE_NOT_LIVE = "Not live: the tunnel is not running."
TUNNEL_LANE_UNKNOWN = "Not live: the tunnel status is unknown."

# The slot table. Its rows are the tunnel's fixed slot ports. Each state is the result of a connect-only check on
# 127.0.0.1 (slot_ports). Listening means something accepts a connection on the port. It is not a health check.
SLOTS_HEADING = "Slots"
SLOTS_INTRO = (
    "The slot ports on this computer, checked with a connect-only test on 127.0.0.1 when you press Check now. "
    "Listening means a process accepts a connection on the port. It does not show that the tool works. "
    "Slots have no controls of their own: Start, Stop and Restart act on the whole tunnel."
)
SLOT_COLUMNS = ("Slot", "Port", "Serves", "State")
SLOT_STATE_NOT_CHECKED = "Not checked yet. Press Check now."
SLOT_LISTENING = "Listening on this computer."
SLOT_NOT_LISTENING = "Not listening."
SLOT_NOT_ASSIGNED = "Not assigned to a tunnel plugin."
# The plugin each built-in slot serves, by its name in the core's tunnel plugin list (meridian/tunnel_plugins.py).
SLOT_SERVES = {
    "fs": "filesystem",
    "code": "code-intel",
    "extract": "code-extractor",
    "ppt": "powerpoint",
    "word": "word",
    "dc": "desktop-commander",
    "docs": "meridian-docs",
    "zotero": "zotero-mcp",
    "outputs": "meridian-outputs",
    "debug": "mcp-debugger",
}

# The recent log: the end of the tunnel log file, read through log_tail. The panel only reads it, and writes nothing.
LOG_HEADING = "Recent tunnel log"
LOG_TAIL_LINES = 50
LOG_INTRO = (
    f"The last {LOG_TAIL_LINES} lines of the tunnel log, with secret values redacted. "
    "The panel only reads the log. Nothing is written to it."
)
LOG_NOT_READ_TEXT = "Not read yet. Press Check now to read the log."
LOG_EMPTY_TEXT = "The tunnel log is empty."
LOG_FAILED_TEXT = "The tunnel log could not be read. Press Check now to read it again."

# The tool groups. Each switch is the existing MCP tools setting, so it is the same as the group switch in Settings.
GROUPS_INTRO = (
    "The tool groups the tunnel exposes. Each switch is the existing MCP tools setting, the same as the group switch "
    "under MCP tools in Settings."
)
GROUP_SWITCH_TEXT = "{title} tools on"
GROUP_COUNT_TEXT = "{on} of {total} on"

# The custom connectors. The tunnel opt-in is off by default, and a connector is reached only when both flags are on.
CONNECTORS_INTRO = (
    "claude.ai reaches a custom connector through the tunnel only while the tunnel is up, the opt-in below is on, and "
    "the connector is marked for the tunnel. The tunnel bridge does not serve custom connectors yet, so nothing is "
    "reachable through it at present. Add or remove connectors under Connections in Settings."
)
OPT_IN_TEXT = "Let claude.ai use my custom connectors through the tunnel, while the tunnel is up. Off by default."
CONNECTOR_TUNNEL_TEXT = "Through the tunnel"
NO_CONNECTORS_TEXT = "No custom connectors are saved."
NOT_REACHABLE_OPT_IN_OFF = "Not reachable through the tunnel: the opt-in above is off."
NOT_REACHABLE_NOT_MARKED = "Not reachable through the tunnel: this connector is not marked for it."
REACHABLE_TEXT = (
    "Marked for the tunnel. It is reachable only while the tunnel is up, once the bridge serves connectors."
)
NOT_USABLE_TEXT = "This connector is not usable, so its tunnel flag cannot change. Remove it under Connections."

# The opt-in as the saved settings hold it. Off by default, read from Settings so the two cannot drift apart.
OPT_IN_DEFAULT = Settings().custom_tunnel_opt_in


@dataclass(frozen=True)
class PanelStatus:
    """The status block of the panel: the headline, the check time, and one detail line."""

    headline: str
    checked: str
    detail: str


@dataclass(frozen=True)
class Lane:
    """One lane: its name, whether it is live, and the one line on what it needs."""

    name: str
    live: str
    needs: str


@dataclass(frozen=True)
class GroupRow:
    """One tool group. ``on`` is True only when every tool of the group is on, as the Settings group switch shows it."""

    id: str
    title: str
    on: bool
    count: str


@dataclass(frozen=True)
class ConnectorRow:
    """One saved connector. ``usable`` is False when the saved entry is not a connector the registry accepts."""

    index: int
    name: str
    usable: bool
    tunnel_exposed: bool
    note: str


@dataclass(frozen=True)
class SlotRow:
    """One tunnel slot port: its name, its loopback port, the plugin it serves, and what the port check found."""

    slot: str
    port: int
    serves: str
    state: str


def panel_status(status: TunnelStatus | None, failure: str | None = None) -> PanelStatus:
    """The status block for one finished check. ``status`` is None when the check raised, and then ``failure``
    holds only the exception's class name. The running headline is shown only for a running tunnel process. Anything else
    says not connected."""
    if status is None:
        detail = " ".join(
            (tunnel_status.CHECK_FAILED_REASON.format(failure or tunnel_status.NO_FAILURE_NAME), PANEL_CHECK_HINT)
        )
        return PanelStatus(UNKNOWN_HEADLINE, tunnel_status.NO_TIME_TEXT, detail)
    checked = tunnel_status.CHECKED_TEMPLATE.format(status.checked_at.strftime(tunnel_status.TIME_FORMAT))
    if status.state == RUNNING:
        return PanelStatus(RUNNING_HEADLINE, checked, status.reason)
    if status.state == NOT_RUNNING:
        return PanelStatus(NOT_CONNECTED_HEADLINE, checked, f"{status.reason} {PANEL_START_HINT}")
    return PanelStatus(UNKNOWN_HEADLINE, checked, f"{status.reason} {PANEL_CHECK_HINT}")


def lanes(state: str | None) -> tuple[Lane, Lane]:
    """The direct lane and the tunnel lane. ``state`` is the tunnel's last known state, or None before any check.

    The direct lane is not checked here, so it never says live. The tunnel lane is live only when the state is running.
    """
    if state == RUNNING:
        tunnel_live = TUNNEL_LANE_LIVE
    elif state == NOT_RUNNING:
        tunnel_live = TUNNEL_LANE_NOT_LIVE
    else:
        tunnel_live = TUNNEL_LANE_UNKNOWN
    return (
        Lane(DIRECT_LANE_NAME, DIRECT_LANE_LIVE, DIRECT_LANE_NEEDS),
        Lane(TUNNEL_LANE_NAME, tunnel_live, TUNNEL_LANE_NEEDS),
    )


def group_rows(disabled: Iterable[str]) -> tuple[GroupRow, ...]:
    """One row for each tool group, read from the stored "group:tool" keys. A group is on when all its tools are on."""
    off = set(disabled)
    rows: list[GroupRow] = []
    for group in tool_catalog.GROUPS:
        keys = group.tool_keys
        on_count = sum(1 for key in keys if key not in off)
        rows.append(
            GroupRow(
                id=group.id,
                title=group.title,
                on=on_count == len(keys),
                count=GROUP_COUNT_TEXT.format(on=on_count, total=len(keys)),
            )
        )
    return tuple(rows)


def group_toggle_keys(group_id: str) -> tuple[str, ...]:
    """The stored keys that one group's switch changes. Turning the group on removes them, and off adds them."""
    return tool_catalog.group_keys(group_id)


def connector_text(usable: bool, tunnel_exposed: bool, opt_in: bool) -> str:
    """The one line under a connector: whether claude.ai can reach it through the tunnel, and why not."""
    if not usable:
        return NOT_USABLE_TEXT
    if not opt_in:
        return NOT_REACHABLE_OPT_IN_OFF
    if not tunnel_exposed:
        return NOT_REACHABLE_NOT_MARKED
    return REACHABLE_TEXT


def connector_rows(raw: Sequence, opt_in: bool) -> tuple[ConnectorRow, ...]:
    """One row for each saved entry, checked by the connector registry (custom_connectors.read_rows)."""
    rows: list[ConnectorRow] = []
    for row in custom_connectors.read_rows(raw):
        if row.connector is None:
            rows.append(ConnectorRow(row.index, row.name, False, False, row.problem or NOT_USABLE_TEXT))
            continue
        exposed = row.connector.tunnel_exposed
        rows.append(ConnectorRow(row.index, row.name, True, exposed, connector_text(True, exposed, opt_in)))
    return tuple(rows)


def slot_rows(results: Sequence[SlotPortResult] | None = None) -> tuple[SlotRow, ...]:
    """One row for each slot port, in the order of slot_ports.SLOT_PORTS.

    ``results`` is the last port check. None, or a slot the check did not report, is not checked yet. A listening port
    says listening, never healthy."""
    found = {result.slot: result for result in results or ()}
    rows: list[SlotRow] = []
    for slot, port in slot_ports.SLOT_PORTS:
        result = found.get(slot)
        if result is None:
            state = SLOT_STATE_NOT_CHECKED
        else:
            state = SLOT_LISTENING if result.listening else SLOT_NOT_LISTENING
        rows.append(SlotRow(slot, port, SLOT_SERVES.get(slot, SLOT_NOT_ASSIGNED), state))
    return tuple(rows)


def log_lines(tail: LogTail | None) -> tuple[str, ...]:
    """The last LOG_TAIL_LINES lines of one log read. A read that is missing, unreadable or failed has no lines."""
    if tail is None or tail.status != log_tail.STATUS_OK:
        return ()
    return tail.lines[-LOG_TAIL_LINES:]


def log_message(tail: LogTail | None) -> str:
    """The one line under the log: why it has no lines to show, or "" when it has some."""
    if tail is None:
        return LOG_FAILED_TEXT
    if tail.status == log_tail.STATUS_NO_LOG_YET:
        return log_tail.NO_LOG_YET_TEXT
    if tail.status == log_tail.STATUS_UNREADABLE:
        return log_tail.UNREADABLE_TEXT
    if tail.status != log_tail.STATUS_OK:
        return LOG_FAILED_TEXT
    return "" if tail.lines else LOG_EMPTY_TEXT


__all__ = [
    "CHECKING_TEXT",
    "CHECK_BUTTON_TEXT",
    "CONNECTORS_HEADING",
    "LOG_EMPTY_TEXT",
    "LOG_FAILED_TEXT",
    "LOG_HEADING",
    "LOG_INTRO",
    "LOG_NOT_READ_TEXT",
    "LOG_TAIL_LINES",
    "NOT_CHECKED_HEADLINE",
    "PANEL_CHECK_HINT",
    "CONNECTORS_INTRO",
    "CONNECTOR_TUNNEL_TEXT",
    "ConnectorRow",
    "DIRECT_LANE_NAME",
    "GROUPS_HEADING",
    "GROUPS_INTRO",
    "GROUP_SWITCH_TEXT",
    "GroupRow",
    "Lane",
    "LANES_HEADING",
    "NOT_CONNECTED_HEADLINE",
    "NO_CONNECTORS_TEXT",
    "OPT_IN_DEFAULT",
    "OPT_IN_TEXT",
    "PANEL_INTRO",
    "PANEL_TITLE",
    "PanelStatus",
    "RUNNING_HEADLINE",
    "SLOTS_HEADING",
    "SLOTS_INTRO",
    "SLOT_COLUMNS",
    "SLOT_LISTENING",
    "SLOT_NOT_LISTENING",
    "SLOT_NOT_ASSIGNED",
    "SLOT_SERVES",
    "SLOT_STATE_NOT_CHECKED",
    "SlotRow",
    "STATUS_HEADING",
    "TUNNEL_LANE_NAME",
    "UNKNOWN_HEADLINE",
    "connector_rows",
    "connector_text",
    "group_rows",
    "group_toggle_keys",
    "lanes",
    "log_lines",
    "log_message",
    "panel_status",
    "slot_rows",
]
