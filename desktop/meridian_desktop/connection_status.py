"""What the connection bar says, and the check behind it. Pure Python: no Qt import.

The bar states three things: who is signed in and until when, what the desktop is connected to (the hosted URL, with
its state), and the local server when one is live. The check reads the saved session. It calls GET /me on the hosted
URL only when a token is saved. It renews the session only when a refresh token was saved and fewer than WARN_DAYS
remain. It probes the local server on loopback. It starts, stops and changes nothing.

States: connected, warning (connected with fewer than WARN_DAYS left), reconnecting (the hosted server could not be
reached), not_connected (no token, a rejected token, an expired session, or a failed renewal), and checking.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from meridian_desktop import hosted_session, local_client
from meridian_desktop.hosted_client import HostedError, Transport
from meridian_desktop.secret_store import SecretStore

STATE_CONNECTED = "connected"
STATE_WARNING = "warning"
STATE_RECONNECTING = "reconnecting"
STATE_NOT_CONNECTED = "not_connected"
STATE_CHECKING = "checking"

WARN_DAYS = 14
DAY_SECONDS = 86400
UNCONFIRMED_ACCOUNT = "account not confirmed yet"
NOT_CONNECTED_HEADLINE = "Not connected: sign in again."
CHECKING_HEADLINE = "Checking the connection..."
RETRY_NOTE = "It is tried again in five minutes."

_HOSTED_WORDS = {
    STATE_CONNECTED: "Connected",
    STATE_WARNING: "Connected",
    STATE_RECONNECTING: "Reconnecting",
    STATE_NOT_CONNECTED: "Not connected",
}


@dataclass(frozen=True)
class HostedStatus:
    """The hosted side of one check. ``state`` is connected, reconnecting or not_connected."""

    state: str
    account: str = ""
    expires_at: float | None = None


@dataclass(frozen=True)
class ConnectionSnapshot:
    """One check's result. ``local_netloc`` is host:port of a live local server, or None. Times are epoch seconds."""

    hosted: HostedStatus
    signed_in: bool
    local_netloc: str | None
    checked_at: float


@dataclass(frozen=True)
class ConnectionView:
    """The three lines the bar shows, the state that picks its colours, and whether the bar offers a sign-in button."""

    state: str
    headline: str
    hosted: str
    local: str
    sign_in_offered: bool = False


def _days_left(expires_at: float, now: float) -> float:
    return (expires_at - now) / DAY_SECONDS


def _read_hosted(
    hosted_url: str, secrets: SecretStore, transport: Transport | None, now: float
) -> tuple[HostedStatus, bool]:
    """The hosted state, and whether a token is saved. Renews first only when a refresh token and a short expiry allow."""
    saved = hosted_session.load_saved(secrets)
    if saved is None:
        return HostedStatus(STATE_NOT_CONNECTED), False
    session = saved.session
    expires = session.expires_at
    if expires is not None and expires <= now:
        return HostedStatus(STATE_NOT_CONNECTED, session.account, expires), True
    access = saved.access_token
    if saved.refresh_token and expires is not None and _days_left(expires, now) < WARN_DAYS:
        try:
            grant = hosted_session.refresh_grant(hosted_url, saved.refresh_token, transport)
            session = hosted_session.save_refreshed(secrets, grant, session, now=now)
        except hosted_session.RefreshFailed:
            return HostedStatus(STATE_NOT_CONNECTED, session.account, expires), True
        except HostedError:
            return HostedStatus(STATE_RECONNECTING, session.account, expires), True
        access = grant.access_token
        expires = session.expires_at
    check = hosted_session.check_account(hosted_url, access, transport)
    if check.state == hosted_session.ACCOUNT_OK:
        return HostedStatus(STATE_CONNECTED, check.email or session.account, expires), True
    if check.state == hosted_session.ACCOUNT_REJECTED:
        return HostedStatus(STATE_NOT_CONNECTED, session.account, expires), True
    return HostedStatus(STATE_RECONNECTING, session.account, expires), True


def _read_local(local_url: str, probe: Callable[[str], str]) -> str | None:
    """host:port when the local server answers as running, else None. A probe that raises reads as not live."""
    try:
        if probe(local_url) != local_client.HEALTH_RUNNING:
            return None
    except Exception:  # noqa: BLE001 - a failed probe is shown as no local server
        return None
    return urllib.parse.urlsplit(local_url).netloc or None


def read_connection(
    *,
    hosted_url: str,
    local_url: str,
    secrets: SecretStore,
    transport: Transport | None,
    probe: Callable[[str], str],
    clock: Callable[[], float] = time.time,
) -> ConnectionSnapshot:
    """One check of the hosted session and the local server. Raises only for a keychain or URL fault the caller reports."""
    now = clock()
    hosted, signed_in = _read_hosted(hosted_url, secrets, transport, now)
    return ConnectionSnapshot(
        hosted=hosted,
        signed_in=signed_in,
        local_netloc=_read_local(local_url, probe),
        checked_at=now,
    )


def _host_label(hosted_url: str) -> str:
    return f"Hosted ({urllib.parse.urlsplit(hosted_url).netloc or hosted_url})"


def _until_text(expires_at: float | None) -> str:
    if expires_at is None:
        return ""
    date = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"Signed in until {date}."


def _local_line(local_netloc: str | None) -> str:
    if local_netloc is None:
        return "Local: none."
    return f"Local: Meridian server at {local_netloc} (live)."


def connection_view(snapshot: ConnectionSnapshot, *, hosted_url: str) -> ConnectionView:
    """The bar's three lines for one check. The state word in each line says the state in plain words, too."""
    host = _host_label(hosted_url)
    local = _local_line(snapshot.local_netloc)
    if not snapshot.signed_in:
        return ConnectionView(
            STATE_NOT_CONNECTED, "", f"Not connected: {host}.", local,
        )
    hosted = snapshot.hosted
    if hosted.state == STATE_NOT_CONNECTED:
        return ConnectionView(
            STATE_NOT_CONNECTED, NOT_CONNECTED_HEADLINE, f"Not connected: {host}.", local, sign_in_offered=True,
        )
    warns = (
        hosted.state == STATE_CONNECTED
        and hosted.expires_at is not None
        and _days_left(hosted.expires_at, snapshot.checked_at) < WARN_DAYS
    )
    state = STATE_RECONNECTING if hosted.state == STATE_RECONNECTING else (STATE_WARNING if warns else STATE_CONNECTED)
    headline = f"Signed in: {hosted.account or UNCONFIRMED_ACCOUNT}."
    until = _until_text(hosted.expires_at)
    if until:
        headline += f" {until}"
    if state == STATE_WARNING:
        headline += f" Under {WARN_DAYS} days left: sign in again before then."
    # The button is offered only when the session is close to expiry: the one case where signing in again helps.
    offered = state == STATE_WARNING
    return ConnectionView(state, headline, f"{_HOSTED_WORDS[state]}: {host}.", local, sign_in_offered=offered)


def checking_view(*, hosted_url: str) -> ConnectionView:
    """What the bar shows while a check runs, or before the first one."""
    return ConnectionView(STATE_CHECKING, CHECKING_HEADLINE, f"Checking: {_host_label(hosted_url)}.", "Local: checking...")


def failed_view(*, hosted_url: str, failure: str) -> ConnectionView:
    """What the bar shows when the check itself raised. ``failure`` is an exception class name, never its text."""
    return ConnectionView(
        STATE_RECONNECTING,
        f"Connection check failed ({failure}). {RETRY_NOTE}",
        f"Reconnecting: {_host_label(hosted_url)}.",
        "Local: not checked.",
    )


__all__ = [
    "STATE_CHECKING",
    "STATE_CONNECTED",
    "STATE_NOT_CONNECTED",
    "STATE_RECONNECTING",
    "STATE_WARNING",
    "WARN_DAYS",
    "ConnectionSnapshot",
    "ConnectionView",
    "HostedStatus",
    "checking_view",
    "connection_view",
    "failed_view",
    "read_connection",
]
