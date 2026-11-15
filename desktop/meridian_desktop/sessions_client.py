"""Read-only client for the sessions of one project. Pure Python: no Qt import.

The route is GET /projects/{project_id}/sessions?active_only=true on the Meridian server (core repo,
meridian/routes/projects.py). It answers with a JSON list of session rows, newest first. With
active_only=true only active and idle sessions come back.

Two sources, one result type:

- local_sessions: the local server at local_url. Loopback only (local_client.assert_loopback). No
  credentials are sent. It keeps the local client's limits: no proxy, no redirects, a size cap, a deadline.
- hosted_sessions: hosted Meridian at hosted_url, with ``Authorization: Bearer <token>``. The URL must be
  https. Plain http is refused even on loopback, so the token never travels in clear text. The token goes
  only to that host. It is never placed in a result, a message or an error. The request uses hosted_client's
  send, so its timeout, its refusal of redirects and its error rules apply.

Each row keeps four fields: name, status, last seen, and the client name (client_type, or agent_framework
when client_type is empty). The session summary, task log bodies and notes are never copied into a result.

Typed states (SessionsResult.state):
- sessions: the route answered with a list. The list may be empty.
- not_running: nothing listens on the local address. Only the local source gives this state.
- missing_route: HTTP 404. Either this server has no sessions route, or the project is not on this server.
- unauthorised: HTTP 401 or 403, or no usable hosted token. The hosted request is not sent without a token.
- no_project: no project id was given. Nothing is sent.
- error: any other failure: a timeout, an unreachable server, a refused address or URL, a redirect, a body
  that is too large or is not a JSON list, or any other HTTP status.

Every message is fixed text, so a result never carries a token, a response body or a raw exception.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from meridian_desktop import hosted_client, local_client
from meridian_desktop.hosted_client import HostedError
from meridian_desktop.local_client import LocalClientError

SESSIONS_PATH = "/projects/{project_id}/sessions?active_only=true"
SESSIONS_TIMEOUT = 5.0
MAX_SESSIONS = 200
MAX_TEXT_CHARS = 120

# A session is active while it was last seen less than this many minutes ago. It is the server's default
# active-session window (active_session_warning_minutes, 10 by default). The desktop does not read the workspace
# setting, so a workspace that changes the window is not reflected here.
ACTIVE_WINDOW_MINUTES = 10
ACTIVE = "active"
IDLE = "idle"

# One timestamp format for the Sessions tab, the same one the other status panels use.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
UNKNOWN_TIME_TEXT = "Unknown"

# Typed states.
SESSIONS = "sessions"
NOT_RUNNING = "not_running"
MISSING_ROUTE = "missing_route"
UNAUTHORISED = "unauthorised"
NO_PROJECT = "no_project"
ERROR = "error"

# Sources. A result names the source it was read from, so the panel can say where the rows came from.
SOURCE_LOCAL = "local"
SOURCE_HOSTED = "hosted"

NOT_RUNNING_MESSAGE = (
    "The local Meridian server is not running on this computer. Start Meridian to see its sessions here, "
    "or save a hosted token in Settings to see hosted sessions."
)
NO_PROJECT_MESSAGE = "Pick a project to see its sessions. Nothing was sent."
UNKNOWN_PROJECT_MESSAGE = "This project's id is not in the form Meridian uses, so nothing was sent."
NOT_LOOPBACK_MESSAGE = (
    "The local Meridian server address is not on this computer (127.0.0.1, localhost or ::1), so nothing was sent."
)
BAD_LOCAL_URL_MESSAGE = "The local Meridian server address could not be used."
LOCAL_TIMEOUT_MESSAGE = "The local Meridian server did not answer in time. Try again in a moment."
LOCAL_UNREACHABLE_MESSAGE = "The local Meridian server could not be reached."
TOO_LARGE_MESSAGE = "The server sent more data than the desktop reads."
REDIRECT_MESSAGE = "The server sent a redirect, which the desktop does not follow."
BAD_RESPONSE_MESSAGE = "The server sent a sessions list the desktop could not read."
UNCHECKED_MESSAGE = "The sessions could not be checked."
HTTP_STATUS_MESSAGE = "The server answered with HTTP {}, which the desktop does not expect."
MISSING_ROUTE_MESSAGE = (
    "The server answered 404 for this project's sessions. Either this Meridian version has no sessions route, "
    "or the project is not on this server. Pick another project, or update Meridian."
)
REJECTED_MESSAGE = (
    "Meridian did not accept the request (HTTP 401). For hosted Meridian, sign in again with your browser in Settings."
)
FORBIDDEN_MESSAGE = "Meridian refused this project for the saved token (HTTP 403). Use a token that can read this project."
NO_TOKEN_MESSAGE = "No hosted token is saved. Sign in with your browser in Settings."
BAD_TOKEN_MESSAGE = "The saved hosted token has a space or non-ASCII text. Sign in again with your browser in Settings."
BAD_HOSTED_URL_MESSAGE = "The hosted URL in Settings is not usable. Use an https address such as https://usemeridian.us."
HOSTED_TIMEOUT_MESSAGE = "Hosted Meridian did not answer in time. Check your network connection, then try again."
HOSTED_UNREACHABLE_MESSAGE = "Hosted Meridian could not be reached. Check the hosted URL and your network connection."
HOSTED_UNCHECKED_MESSAGE = "Hosted Meridian sessions could not be checked."

# A project id is a UUID, the only form the server's project routes accept.
_PROJECT_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

# A local request error maps to a typed state and fixed text. Anything unlisted is an error.
_LOCAL_ERRORS: dict[str, tuple[str, str]] = {
    "not_running": (NOT_RUNNING, NOT_RUNNING_MESSAGE),
    "timeout": (ERROR, LOCAL_TIMEOUT_MESSAGE),
    "unreachable": (ERROR, LOCAL_UNREACHABLE_MESSAGE),
    "too_large": (ERROR, TOO_LARGE_MESSAGE),
    "not_loopback": (ERROR, NOT_LOOPBACK_MESSAGE),
    "bad_url": (ERROR, BAD_LOCAL_URL_MESSAGE),
}

# A hosted failure maps by the HostedError code. Anything unlisted is an error with fixed text.
_HOSTED_ERRORS: dict[str, tuple[str, str]] = {
    "not_signed_in": (UNAUTHORISED, NO_TOKEN_MESSAGE),
    "bad_token": (UNAUTHORISED, BAD_TOKEN_MESSAGE),
    "bad_base_url": (ERROR, BAD_HOSTED_URL_MESSAGE),
    "timeout": (ERROR, HOSTED_TIMEOUT_MESSAGE),
    "unreachable": (ERROR, HOSTED_UNREACHABLE_MESSAGE),
    "too_large": (ERROR, TOO_LARGE_MESSAGE),
    "redirect_refused": (ERROR, REDIRECT_MESSAGE),
}


@dataclass(frozen=True)
class SessionRow:
    """One session, reduced to the four fields the panel shows. Empty text means the server sent none."""

    name: str
    status: str
    last_seen: str
    client: str


@dataclass(frozen=True)
class SessionsResult:
    """The outcome of one read. ``source`` is empty when no request was made. ``rows`` is set for state sessions."""

    state: str
    message: str
    source: str = ""
    rows: tuple[SessionRow, ...] = ()


# A local transport takes (base_url, path, timeout) and returns an object with .status and .body. It may raise
# LocalClientError. Tests pass a fake, so they open no socket.
LocalTransport = Callable[[str, str, float], Any]


def _local_get(base_url: str, path: str, timeout: float) -> Any:
    """The default local transport: one loopback GET through local_client."""
    return local_client._get(base_url, path, timeout)  # noqa: SLF001 - the shared loopback GET


# ----------------------------------------------------------------- validation


def _project_problem(project_id: object, source: str) -> tuple[str, SessionsResult | None]:
    """Return (canonical id, None) for a usable id, or ("", result) for an id that must not be sent."""
    if not isinstance(project_id, str) or not project_id.strip():
        return "", SessionsResult(NO_PROJECT, NO_PROJECT_MESSAGE, "")
    cleaned = project_id.strip()
    if not _PROJECT_ID.fullmatch(cleaned):
        return "", SessionsResult(ERROR, UNKNOWN_PROJECT_MESSAGE, source)
    return cleaned, None


def _hosted_base(url: object) -> str:
    """Return the hosted base URL. Raise HostedError (BadBaseUrl) unless it is https."""
    base = hosted_client.validate_base_url(url)
    if urllib.parse.urlsplit(base).scheme != "https":
        raise hosted_client.BadBaseUrl(BAD_HOSTED_URL_MESSAGE)
    return base


# ----------------------------------------------------------------- parsing


def _text(value: Any) -> str:
    """Plain text from one server field: control characters removed, whitespace collapsed, length capped."""
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(_CONTROL.sub(" ", value).split())
    if len(collapsed) > MAX_TEXT_CHARS:
        return collapsed[: MAX_TEXT_CHARS - 3] + "..."
    return collapsed


def _row(item: dict[str, Any]) -> SessionRow:
    client = _text(item.get("client_type")) or _text(item.get("agent_framework"))
    return SessionRow(
        name=_text(item.get("name")),
        status=_text(item.get("status")),
        last_seen=_text(item.get("last_seen")),
        client=client,
    )


def _rows(body: bytes) -> tuple[SessionRow, ...] | None:
    """Parse a JSON list of objects into rows, newest first as the server sent them. None for anything else."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return tuple(_row(item) for item in value[:MAX_SESSIONS])


def _from_reply(status: int, body: bytes, source: str) -> SessionsResult:
    """Turn one HTTP reply into a result. The body is parsed only for a 2xx status."""
    if 200 <= status < 300:
        rows = _rows(body)
        if rows is None:
            return SessionsResult(ERROR, BAD_RESPONSE_MESSAGE, source)
        return SessionsResult(SESSIONS, "", source, rows)
    if status == 401:
        return SessionsResult(UNAUTHORISED, REJECTED_MESSAGE, source)
    if status == 403:
        return SessionsResult(UNAUTHORISED, FORBIDDEN_MESSAGE, source)
    if status == 404:
        return SessionsResult(MISSING_ROUTE, MISSING_ROUTE_MESSAGE, source)
    if 300 <= status < 400:
        return SessionsResult(ERROR, REDIRECT_MESSAGE, source)
    return SessionsResult(ERROR, HTTP_STATUS_MESSAGE.format(int(status)), source)


# ----------------------------------------------------------------- sources


def local_sessions(
    base_url: str,
    project_id: str,
    *,
    transport: LocalTransport | None = None,
    timeout: float = SESSIONS_TIMEOUT,
) -> SessionsResult:
    """GET the project's sessions from the local server. Loopback only, no credentials.

    The address and the project id are checked before anything is sent, so a refused value makes no request.
    """
    pid, problem = _project_problem(project_id, SOURCE_LOCAL)
    if problem is not None:
        return problem
    try:
        local_client.assert_loopback(base_url)
    except LocalClientError:
        return SessionsResult(ERROR, NOT_LOOPBACK_MESSAGE, SOURCE_LOCAL)
    call = _local_get if transport is None else transport
    try:
        reply = call(base_url, SESSIONS_PATH.format(project_id=pid), timeout)
    except LocalClientError as exc:
        state, message = _LOCAL_ERRORS.get(exc.code, (ERROR, UNCHECKED_MESSAGE))
        return SessionsResult(state, message, SOURCE_LOCAL)
    except Exception:  # noqa: BLE001 - a read must never raise into the UI
        return SessionsResult(ERROR, UNCHECKED_MESSAGE, SOURCE_LOCAL)
    return _from_reply(int(reply.status), bytes(reply.body), SOURCE_LOCAL)


def hosted_sessions(
    base_url: str,
    project_id: str,
    token: str | None,
    *,
    transport: hosted_client.Transport | None = None,
    timeout: float = SESSIONS_TIMEOUT,
) -> SessionsResult:
    """GET the project's sessions from hosted Meridian with the bearer token.

    The URL must be https, and the token must be usable. Both are checked before a request is built, so a
    refused value sends nothing. The token goes only in the Authorization header of this one request.
    """
    pid, problem = _project_problem(project_id, SOURCE_HOSTED)
    if problem is not None:
        return problem
    try:
        base = _hosted_base(base_url)
        bearer = hosted_client.usable_token(token)
    except HostedError as exc:
        state, message = _HOSTED_ERRORS.get(exc.code, (ERROR, HOSTED_UNCHECKED_MESSAGE))
        return SessionsResult(state, message, SOURCE_HOSTED)
    request = urllib.request.Request(
        base + SESSIONS_PATH.format(project_id=pid),
        method="GET",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
            "User-Agent": hosted_client.USER_AGENT,
        },
    )
    send_with = hosted_client._urllib_transport if transport is None else transport  # noqa: SLF001
    try:
        reply = hosted_client._send(send_with, request, timeout)  # noqa: SLF001 - the shared timeout and error rules
    except HostedError as exc:
        state, message = _HOSTED_ERRORS.get(exc.code, (ERROR, HOSTED_UNCHECKED_MESSAGE))
        return SessionsResult(state, message, SOURCE_HOSTED)
    except Exception:  # noqa: BLE001 - a read must never raise, and the exception may hold request headers
        return SessionsResult(ERROR, HOSTED_UNCHECKED_MESSAGE, SOURCE_HOSTED)
    return _from_reply(int(reply.status), bytes(reply.body), SOURCE_HOSTED)


# ----------------------------------------------------------------- last seen


def parse_timestamp(text: str) -> datetime | None:
    """Parse a server timestamp. A value with no offset is read as UTC. None when it cannot be read."""
    cleaned = text.strip() if isinstance(text, str) else ""
    if not cleaned:
        return None
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def last_seen_age(last_seen: str, now: datetime) -> str:
    """Describe how long ago a session was last seen, such as "just now", "5 min ago", "3 h ago" or "2 d ago".

    ``now`` is an aware datetime. A time in the future (clock skew) reads as "just now". Text that cannot be
    read as a time reads as "unknown".
    """
    seen = parse_timestamp(last_seen)
    if seen is None:
        return "unknown"
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    seconds = (current - seen).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


# ----------------------------------------------------------------- active or idle


def session_state(last_seen: str, now: datetime) -> str:
    """ACTIVE while the session was last seen less than ACTIVE_WINDOW_MINUTES ago, otherwise IDLE.

    This is the server's own rule: the active-session check in meridian/mcp/handler.py counts a session as active
    while ``now - last_seen < threshold``, and the threshold defaults to 10 minutes. The boundary is strict, so a
    session seen exactly 10 minutes ago is idle. A last-seen value that cannot be read is IDLE, because nothing
    shows that the session was seen. A time in the future (clock skew) is ACTIVE.
    """
    seen = parse_timestamp(last_seen)
    if seen is None:
        return IDLE
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    seconds = (current - seen).total_seconds()
    return ACTIVE if seconds < ACTIVE_WINDOW_MINUTES * 60 else IDLE


# ----------------------------------------------------------------- one timestamp format


def format_local(when: datetime) -> str:
    """The one timestamp format of the Sessions tab: local wall-clock time, 24-hour, with seconds.

    A naive value is read as UTC, the way the server writes its times. The result is converted to this computer's
    local zone before it is formatted.
    """
    aware = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    return aware.astimezone().strftime(TIME_FORMAT)


def local_time_text(last_seen: str) -> str:
    """A server timestamp in the one local format, or UNKNOWN_TIME_TEXT when it cannot be read."""
    seen = parse_timestamp(last_seen)
    if seen is None:
        return UNKNOWN_TIME_TEXT
    try:
        return format_local(seen)
    except (OverflowError, OSError, ValueError):
        return UNKNOWN_TIME_TEXT


__all__ = [
    "ACTIVE",
    "ACTIVE_WINDOW_MINUTES",
    "ERROR",
    "IDLE",
    "MAX_SESSIONS",
    "MISSING_ROUTE",
    "NO_PROJECT",
    "NOT_RUNNING",
    "SESSIONS",
    "SESSIONS_TIMEOUT",
    "SOURCE_HOSTED",
    "SOURCE_LOCAL",
    "TIME_FORMAT",
    "UNAUTHORISED",
    "UNKNOWN_TIME_TEXT",
    "SessionRow",
    "SessionsResult",
    "format_local",
    "hosted_sessions",
    "last_seen_age",
    "local_sessions",
    "local_time_text",
    "parse_timestamp",
    "session_state",
]
