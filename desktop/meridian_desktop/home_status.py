"""The Home tab's card texts, in plain words. Pure Python: no Qt import.

Each card's check runs off the UI thread and hands back a plain result: the local server's facts, the services
facts, a tunnel status, a Zotero status, a tools status or an update check. This module turns one result into a
CardView: a headline, one detail sentence, the time checked, the rows of a compact list, and at most one action.

Nothing here starts, stops or installs anything. Only fixed words and the module's own sentences reach a view, so no
exception text, traceback, path or secret is shown. A card that cannot check says why and what to do, in one sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from meridian_desktop import local_client, tunnel_status, updater
from meridian_desktop.latex_client import (
    ENGINE_CONNECTED,
    ENGINE_NOT_ANSWERING,
    ENGINE_NOT_RUNNING,
    EngineStatus,
    port_text,
)
from meridian_desktop.slot_ports import PROBE_HOST, SlotPortResult
from meridian_desktop.tools_status import (
    NO,
    NOT_SET,
    NOT_STARTED,
    UNCHECKED,
    ToolRow,
    ToolsStatus,
)
from meridian_desktop.tunnel_status import NOT_AVAILABLE_REASON, NOT_RUNNING, RUNNING, UNKNOWN, TunnelStatus
from meridian_desktop.zotero_client import API_DISABLED, API_NOT_RUNNING, API_ON, ZoteroStatus

# Tones: the colour family a card uses. Idle is "not checked" or "not running, which is not an error".
TONE_IDLE = "idle"
TONE_CHECKING = "checking"
TONE_OK = "ok"
TONE_ATTENTION = "attention"
TONE_OFF = "off"

# What a card's action does. The window maps each key to one control.
ACTION_SETTINGS = "settings"
ACTION_LAUNCH_ZOTERO = "launch_zotero"
ACTION_RELEASE_PAGE = "release_page"
ACTION_CHECK_TOOLS = "check_tools"

TIME_FORMAT = "%H:%M:%S"
CHECKED_TEMPLATE = "Checked at {}."
NOT_CHECKED_HEADLINE = "Not checked yet"
CHECKING_HEADLINE = "Checking..."
COULD_NOT_CHECK_HEADLINE = "Could not check"
STILL_CHECKING_HEADLINE = "Still checking"
FAILED_TEMPLATE = "The check failed unexpectedly ({}). Press Check now to try again."
STALLED_TEMPLATE = (
    "This check has taken more than {} seconds. It may still finish, and this card updates when it does. "
    "Press Check now to start it again."
)
SET_UP_IN_SETTINGS = "Set up in Settings"
OPEN_SETTINGS_LABEL = "Open Settings"
CHECK_AGAIN_LABEL = "Check again"
LAUNCH_ZOTERO_LABEL = "Launch Zotero"
OPEN_RELEASE_LABEL = "Open release page"
# The tools rows also show in the full Tools table in Settings, which says "press Refresh". Home says "press Check now".
REFRESH_WORDS = "press Refresh"
CHECK_NOW_WORDS = "press Check now"


@dataclass(frozen=True)
class RowView:
    """One line of a compact list. ``action`` is one of the ACTION_* keys, or empty when the row needs nothing."""

    text: str
    detail: str = ""
    action: str = ""
    action_label: str = ""


@dataclass(frozen=True)
class CardView:
    """Everything a card shows. Every field is plain text, shown as it is."""

    headline: str
    detail: str = ""
    checked: str = ""
    tone: str = TONE_IDLE
    rows: tuple[RowView, ...] = ()
    action: str = ""
    action_label: str = ""


# ----------------------------------------------------------------- shared

def checked_text(when: datetime | None) -> str:
    """The time a check finished, or empty when no check has finished."""
    if when is None:
        return ""
    return CHECKED_TEMPLATE.format(when.strftime(TIME_FORMAT))


def checking_view() -> CardView:
    return CardView(headline=CHECKING_HEADLINE, tone=TONE_CHECKING)


def stalled_view(seconds: int) -> CardView:
    return CardView(headline=STILL_CHECKING_HEADLINE, detail=STALLED_TEMPLATE.format(seconds), tone=TONE_ATTENTION)


def failed_view(name: str, checked: str = "") -> CardView:
    """The card for a check that raised. Only the exception's class name is shown."""
    return CardView(
        headline=COULD_NOT_CHECK_HEADLINE,
        detail=FAILED_TEMPLATE.format(name or "unknown error"),
        checked=checked,
        tone=TONE_ATTENTION,
    )


def _check_now(text: str) -> str:
    """A tools-module sentence says 'press Refresh'. The Home card says 'press Check now' instead."""
    return text.replace("Press Refresh", "Press Check now").replace(REFRESH_WORDS, CHECK_NOW_WORDS)


# ----------------------------------------------------------------- local server

@dataclass(frozen=True)
class LocalFacts:
    """One read of the local server. ``refusal`` is set when the address is not on this computer, and then no probe ran."""

    url: str
    refusal: str = ""
    health: str = ""


def local_view(facts: LocalFacts | None, failure: str = "", checked: str = "") -> CardView:
    if facts is None:
        return failed_view(failure, checked)
    port = port_text(facts.url)
    if facts.refusal:
        return CardView(
            headline="Not checked",
            detail=f"{facts.refusal} Change the Local URL in Settings to an address on this computer.",
            checked=checked,
            tone=TONE_ATTENTION,
        )
    if facts.health == local_client.HEALTH_RUNNING:
        return CardView(
            headline="Running",
            detail=f"The local Meridian server answers on port {port}.",
            checked=checked,
            tone=TONE_OK,
        )
    if facts.health == local_client.HEALTH_NOT_RUNNING:
        return CardView(
            headline="Not running",
            detail=(
                f"Nothing listens on port {port}. Start Meridian on this computer, then press Check now. "
                "The desktop starts the server only from Settings, after you confirm."
            ),
            checked=checked,
            tone=TONE_ATTENTION,
        )
    if facts.health == local_client.HEALTH_NOT_MERIDIAN:
        return CardView(
            headline="Another program answers",
            detail=(
                f"Port {port} answers, but not with Meridian's health check. Stop that program, or change the "
                "Local URL in Settings, then press Check now."
            ),
            checked=checked,
            tone=TONE_ATTENTION,
        )
    return CardView(
        headline=COULD_NOT_CHECK_HEADLINE,
        detail=f"The check got no usable answer from port {port}. Press Check now to try again.",
        checked=checked,
        tone=TONE_ATTENTION,
    )


# ----------------------------------------------------------------- services

@dataclass(frozen=True)
class ServiceFacts:
    """The LaTeX engine and the slot ports, each read on its own. A check that raised has its class name in its failure."""

    engine_url: str
    engine: EngineStatus | None = None
    engine_failure: str = ""
    ports: tuple[SlotPortResult, ...] | None = None
    ports_failure: str = ""


_ENGINE_HEADLINE = {
    ENGINE_CONNECTED: "LaTeX engine running",
    ENGINE_NOT_RUNNING: "LaTeX engine not running",
    ENGINE_NOT_ANSWERING: "Another program answers on the engine port",
}
_ENGINE_DETAIL = {
    ENGINE_CONNECTED: "The engine answered its health and extension-version checks.",
    ENGINE_NOT_RUNNING: (
        "Nothing listens on that port. Start the LaTeX engine you installed on this computer if you need it."
    ),
    ENGINE_NOT_ANSWERING: "Something else answers on that port, not the LaTeX engine.",
}
PORTS_NOTE = "A listening port only shows that something accepts a connection. It does not show that Meridian is healthy."


def services_view(facts: ServiceFacts | None, failure: str = "", checked: str = "") -> CardView:
    if facts is None:
        return failed_view(failure, checked)
    rows: list[RowView] = []
    port = port_text(facts.engine_url)
    if facts.engine is None:
        headline, tone = COULD_NOT_CHECK_HEADLINE, TONE_ATTENTION
        rows.append(RowView(text=f"LaTeX engine on port {port}: not checked.", detail=_failed_engine_text(facts)))
    else:
        state = facts.engine.state
        headline = _ENGINE_HEADLINE.get(state, COULD_NOT_CHECK_HEADLINE)
        tone = {ENGINE_CONNECTED: TONE_OK, ENGINE_NOT_RUNNING: TONE_IDLE}.get(state, TONE_ATTENTION)
        detail = _ENGINE_DETAIL.get(state, "The engine check got no usable answer.")
        rows.append(RowView(text=f"LaTeX engine on port {port}.", detail=detail))
    if facts.ports is None:
        rows.append(RowView(text="Slot ports: not checked.", detail=FAILED_TEMPLATE.format(facts.ports_failure or "unknown error")))
    else:
        listening = sum(1 for result in facts.ports if result.listening)
        rows.append(
            RowView(text=f"Slot ports: {listening} of {len(facts.ports)} listening on {PROBE_HOST}.", detail=PORTS_NOTE)
        )
    return CardView(headline=headline, detail="", checked=checked, tone=tone, rows=tuple(rows))


def _failed_engine_text(facts: ServiceFacts) -> str:
    return FAILED_TEMPLATE.format(facts.engine_failure or "unknown error")


# ----------------------------------------------------------------- tunnel

_TUNNEL_HEADLINE = {RUNNING: "Running", NOT_RUNNING: "Not running"}
TUNNEL_NOT_AVAILABLE_DETAIL = (
    "The tunnel cannot be checked until the Meridian core checkout and its Python are set. "
    "Set them in Settings, then press Check now."
)
TUNNEL_DEPENDS = (
    "Hosted tools reach this computer through the tunnel, so they need it running: "
    "codebase-memory, Serena and the filesystem."
)


def tunnel_view(status: TunnelStatus | None, failure: str = "", checked: str = "") -> CardView:
    if status is None:
        return CardView(
            headline=COULD_NOT_CHECK_HEADLINE,
            detail=tunnel_status.CHECK_FAILED_REASON.format(failure or tunnel_status.NO_FAILURE_NAME),
            checked=checked or tunnel_status.NO_TIME_TEXT,
            tone=TONE_ATTENTION,
        )
    if status.state == UNKNOWN and status.reason == NOT_AVAILABLE_REASON:
        # No core setup, so nothing was read. That is not a fault, and the card says what to set.
        return CardView(headline="Not checked", detail=TUNNEL_NOT_AVAILABLE_DETAIL, checked=checked, tone=TONE_IDLE)
    if status.state == RUNNING:
        return CardView(headline="Running", detail=f"{status.reason} {TUNNEL_DEPENDS}", checked=checked, tone=TONE_OK)
    if status.state == NOT_RUNNING:
        detail = f"{status.reason} {tunnel_status.START_HINT}"
        return CardView(headline="Not running", detail=detail, checked=checked, tone=TONE_IDLE)
    detail = f"{status.reason} {tunnel_status.CHECK_HINT}"
    return CardView(headline="Unknown", detail=detail, checked=checked, tone=TONE_ATTENTION)


# ----------------------------------------------------------------- zotero

_ZOTERO_API_DETAIL = {
    API_ON: "The local API answers on 127.0.0.1:23119.",
    API_DISABLED: "Zotero is open, but its local API is off. Turn it on in Zotero under Settings, then Advanced.",
    API_NOT_RUNNING: "Zotero is not answering on its local API. Zotero can take a little while to open.",
}
ZOTERO_NOT_INSTALLED = (
    "Zotero was not found in the usual install folders. Install it from zotero.org, then press Check now."
)


def zotero_view(status: ZoteroStatus | None, failure: str = "", checked: str = "") -> CardView:
    if status is None:
        return failed_view(failure, checked)
    if status.exe is None and status.process_running is not True:
        return CardView(
            headline="Not installed",
            detail=ZOTERO_NOT_INSTALLED,
            checked=checked,
            tone=TONE_ATTENTION,
        )
    if status.process_running is True:
        headline, tone = "Running", TONE_OK
    elif status.process_running is False:
        headline, tone = "Not running", TONE_IDLE
    else:
        headline, tone = "Process not checked", TONE_IDLE
    detail = _ZOTERO_API_DETAIL.get(status.api.state, "The local API could not be checked. Press Check now to try again.")
    can_launch = status.exe is not None and status.process_running is not True
    return CardView(
        headline=headline,
        detail=detail,
        checked=checked,
        tone=tone,
        action=ACTION_LAUNCH_ZOTERO if can_launch else "",
        action_label=LAUNCH_ZOTERO_LABEL if can_launch else "",
    )


# ----------------------------------------------------------------- tools

def _tool_action(row: ToolRow) -> tuple[str, str]:
    """The one action a row offers: set it up in Settings when it is missing, open Settings when the core is not set, else check again."""
    if row.configured == NO:
        return ACTION_SETTINGS, SET_UP_IN_SETTINGS
    if row.configured == NOT_SET:
        return ACTION_SETTINGS, OPEN_SETTINGS_LABEL
    return ACTION_CHECK_TOOLS, CHECK_AGAIN_LABEL


def _tool_row(row: ToolRow) -> RowView:
    action, label = _tool_action(row)
    return RowView(
        text=f"{row.client}: {row.tool}. {row.status}.",
        detail=_check_now(row.detail),
        action=action,
        action_label=label,
    )


def tools_view(status: ToolsStatus | None, failure: str = "", checked: str = "") -> CardView:
    if status is None:
        return failed_view(failure, checked)
    if not status.rows:
        return CardView(headline=NOT_CHECKED_HEADLINE, detail=_check_now(status.message), checked=checked)
    needs = [row for row in status.rows if row.configured == NO or row.live == NO]
    unknown = [row for row in status.rows if row.configured in (UNCHECKED, NOT_SET, NOT_STARTED)]
    if needs:
        headline, tone = f"{len(needs)} need attention", TONE_ATTENTION
    elif len(unknown) == len(status.rows):
        headline, tone = NOT_CHECKED_HEADLINE, TONE_IDLE
    else:
        headline, tone = "All set up", TONE_OK
    return CardView(
        headline=headline,
        detail=_check_now(status.message),
        checked=checked,
        tone=tone,
        rows=tuple(_tool_row(row) for row in status.rows),
    )


# ----------------------------------------------------------------- updates

UPDATES_OFF_DETAIL = (
    "Update checks are off. Turn on the automatic update checkbox in Settings (auto_update and check_updates) "
    "to check for a newer release when Meridian Desktop starts."
)
UPDATES_CHECKING_DETAIL = "Checking GitHub for a newer release."
UPDATES_READY_DETAIL = (
    "A newer release is downloaded and its signature is checked. Restart to update is in the status bar, "
    "and it installs only when you press it."
)
UPDATES_CURRENT_DETAIL = "This is the latest published release."


def updates_view(current: str, result: object | None, *, enabled: bool, checking: bool) -> CardView:
    """The Updates card. ``result`` is the one startup check's result (an updater.UpdateResult), or None before it finishes."""
    headline = f"Version {current}"
    if not enabled:
        return CardView(headline=headline, detail=UPDATES_OFF_DETAIL, tone=TONE_OFF)
    if isinstance(result, updater.UpdateResult):
        if result.status == "update_staged" and result.version:
            return CardView(
                headline=f"Update ready: v{result.version}",
                detail=UPDATES_READY_DETAIL,
                tone=TONE_ATTENTION,
                action=ACTION_RELEASE_PAGE,
                action_label=OPEN_RELEASE_LABEL,
            )
        if result.status == "up_to_date":
            return CardView(headline=headline, detail=UPDATES_CURRENT_DETAIL, tone=TONE_OK)
        if result.status == "offline":
            return CardView(
                headline=COULD_NOT_CHECK_HEADLINE,
                detail=f"{result.reason} Try again later.",
                tone=TONE_IDLE,
            )
        # Refused: an install that cannot run here (a source run, or a copy outside the install folder), or a held release.
        return CardView(headline=COULD_NOT_CHECK_HEADLINE, detail=result.reason, tone=TONE_IDLE)
    if checking:
        return CardView(headline=headline, detail=UPDATES_CHECKING_DETAIL, tone=TONE_CHECKING)
    return CardView(headline=headline, detail=NOT_CHECKED_HEADLINE, tone=TONE_IDLE)


__all__ = [
    "ACTION_CHECK_TOOLS",
    "ACTION_LAUNCH_ZOTERO",
    "ACTION_RELEASE_PAGE",
    "ACTION_SETTINGS",
    "CardView",
    "LocalFacts",
    "RowView",
    "ServiceFacts",
    "TONE_ATTENTION",
    "TONE_CHECKING",
    "TONE_IDLE",
    "TONE_OFF",
    "TONE_OK",
    "checked_text",
    "checking_view",
    "failed_view",
    "local_view",
    "services_view",
    "stalled_view",
    "tools_view",
    "tunnel_view",
    "updates_view",
    "zotero_view",
]
