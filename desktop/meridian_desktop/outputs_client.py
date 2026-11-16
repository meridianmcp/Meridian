"""Client for the Outputs and Research read routes on the local Meridian server. Pure Python: no Qt import.

Two read-only GET routes on the local server:

- GET {local_url}/desktop/outputs/search?q=<text>&limit=<n>. Each hit is an indexed output
  file with its provenance: kind, archival flag, canonical path, generating script, columns or
  keys, size and modified time. The same response carries the index state (convergence).
- GET {local_url}/desktop/research/runs?limit=<n>. Each run has an id, a state, a provider
  and its start, update and completion times. The response shape matches the core MCP tool
  list_research_runs: {"runs": [...], "count": n}.

Each call returns a typed result with one state:

- results: the route answered with data.
- empty: the route answered with no rows.
- not_running: nothing listens on the loopback address, so nothing is connected.
- missing_route: the server answered 404, so this server version has no such route yet.
- invalid: the search text is blank or too long. Nothing was sent.
- error: any other failure, such as a timeout, an unreadable body, a non-loopback address,
  or an error the server reported.

Requests go through local_client's loopback GET, so they keep its limits: an http URL on
127.0.0.1, localhost or ::1 only, no proxy, no redirects, a response size cap and a deadline.
The address is checked before any request, so a transport never sees a non-loopback address.
The transport is injectable: transport(base_url, path, timeout) returns an object with a
status and a body. Tests pass a fake.

Server text is copied only as collapsed, length-capped plain text. Messages are fixed text,
so a result never carries a response body, a traceback or a secret.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError

OUTPUTS_SEARCH_PATH = "/desktop/outputs/search"
RESEARCH_RUNS_PATH = "/desktop/research/runs"
DEFAULT_LIMIT = 25
DEFAULT_RUN_LIMIT = 50
MAX_LIMIT = 100
MAX_QUERY_CHARS = 200
MAX_PATH_CHARS = 1024
MAX_FIELD_CHARS = 300
MAX_LIST_ITEMS = 50
SEARCH_TIMEOUT = 20.0
RUNS_TIMEOUT = 10.0

# Typed states.
RESULTS = "results"
EMPTY = "empty"
NOT_RUNNING = "not_running"
MISSING_ROUTE = "missing_route"
INVALID = "invalid"
ERROR = "error"

BLANK_QUERY_MESSAGE = "Type a search term first. Nothing was sent to the local Meridian server."
QUERY_TOO_LONG_MESSAGE = (
    f"The search text is longer than {MAX_QUERY_CHARS} characters. Shorten it and search again. Nothing was sent."
)
NOT_RUNNING_MESSAGE = (
    "Not connected: the local Meridian server is not running on this computer. "
    "Start Meridian, then try again."
)
OUTPUTS_MISSING_ROUTE_MESSAGE = (
    "The local Meridian server is running, but it has no outputs search route yet. "
    "Update Meridian on this computer to a version that has it, then search again."
)
RUNS_MISSING_ROUTE_MESSAGE = (
    "The local Meridian server is running, but it has no research runs route yet. "
    "Update Meridian on this computer to a version that has it, then press Refresh."
)
TIMEOUT_MESSAGE = "The local Meridian server did not answer in time. A large outputs folder can take a while. Try again."
UNREACHABLE_MESSAGE = "The local Meridian server could not be reached."
TOO_LARGE_MESSAGE = "The local Meridian server sent more data than the desktop reads."
NOT_LOOPBACK_MESSAGE = (
    "The local Meridian server address is not on this computer (127.0.0.1, localhost or ::1), "
    "so nothing was sent."
)
BAD_URL_MESSAGE = "The local Meridian server address could not be used."
REDIRECT_MESSAGE = "The local Meridian server sent a redirect, which the desktop does not follow."
UNREADABLE_MESSAGE = "The local Meridian server sent a response the desktop could not read."
UNCHECKED_MESSAGE = "The local Meridian server could not be checked."
HTTP_STATUS_MESSAGE = "The local Meridian server answered with HTTP {}, which the desktop does not expect."
SERVER_ERROR_MESSAGE = "The local Meridian server reported an error: {}"
NO_DETAIL = "no detail was given"
OUTPUTS_RESULTS_MESSAGE = '{} output(s) match "{}".'
OUTPUTS_EMPTY_MESSAGE = 'No outputs match "{}".'
OUTPUTS_EMPTY_DEGRADED_MESSAGE = (
    'No outputs match "{}" yet. The index is still being built or has errors, '
    "so this is not proof that the file is missing. Search again in a moment."
)
RUNS_RESULTS_MESSAGE = "{} research run(s)."
RUNS_EMPTY_MESSAGE = "No research runs to show yet."

# A local request error maps to a typed state and fixed text. Anything unlisted is an error.
_LOCAL_ERRORS: dict[str, tuple[str, str]] = {
    "not_running": (NOT_RUNNING, NOT_RUNNING_MESSAGE),
    "timeout": (ERROR, TIMEOUT_MESSAGE),
    "unreachable": (ERROR, UNREACHABLE_MESSAGE),
    "too_large": (ERROR, TOO_LARGE_MESSAGE),
    "not_loopback": (ERROR, NOT_LOOPBACK_MESSAGE),
    "bad_url": (ERROR, BAD_URL_MESSAGE),
}

# A path is copied exactly, so only control characters are refused. Whitespace is kept.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Reply:
    """A response: the HTTP status and the raw body bytes. A non-2xx status is still a Reply."""

    status: int
    body: bytes


# transport(base_url, path, timeout) -> Reply. A transport raises LocalClientError for a failed request.
Transport = Callable[[str, str, float], Reply]


@dataclass(frozen=True)
class OutputHit:
    """One indexed output and its provenance. Empty text means the server did not report it."""

    path: str
    score: float | None
    kind: str
    is_archival: bool
    canonical_path: str
    generating_script: str
    csv_columns: tuple[str, ...]
    json_keys: tuple[str, ...]
    size: int | None
    modified: str


@dataclass(frozen=True)
class IndexState:
    """The index health from the same response. ``degraded`` is True whenever results may be incomplete."""

    degraded: bool
    partial: bool
    pending_count: int
    db_write_failed: bool
    walk_error_count: int
    last_rebuilt_at: str


@dataclass(frozen=True)
class OutputsResult:
    state: str
    message: str
    query: str = ""
    hits: tuple[OutputHit, ...] = ()
    index: IndexState | None = None


@dataclass(frozen=True)
class ResearchRun:
    """One research run. Empty text means the server did not report that field (for example, provider)."""

    run_id: str
    state: str
    provider: str
    started_at: str
    updated_at: str
    completed_at: str


@dataclass(frozen=True)
class ResearchResult:
    state: str
    message: str
    runs: tuple[ResearchRun, ...] = ()


class _Failed(Exception):
    """A request failed before a usable body arrived. It carries the typed state and fixed text."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state
        self.message = message


# ----------------------------------------------------------------- transport


def fetch(base_url: str, path: str, timeout: float) -> Reply:
    """The default transport: one loopback GET through local_client. A non-2xx status is returned, not raised."""
    raw = local_client._get(base_url, path, timeout)
    return Reply(raw.status, raw.body)


def _get_object(
    base_url: str,
    path: str,
    timeout: float,
    transport: Transport | None,
    missing_route_message: str,
) -> dict[str, Any]:
    """GET one route and return its JSON object. Raises _Failed for any answer that is not a usable 200 object."""
    try:
        local_client.assert_loopback(base_url)
    except LocalClientError:
        raise _Failed(ERROR, NOT_LOOPBACK_MESSAGE) from None
    call = fetch if transport is None else transport
    try:
        reply = call(base_url, path, timeout)
    except LocalClientError as exc:
        state, message = _LOCAL_ERRORS.get(exc.code, (ERROR, UNCHECKED_MESSAGE))
        raise _Failed(state, message) from None
    except Exception:  # noqa: BLE001 - a read must never raise into the UI
        raise _Failed(ERROR, UNCHECKED_MESSAGE) from None
    if reply.status == 404:
        raise _Failed(MISSING_ROUTE, missing_route_message)
    if 300 <= reply.status < 400:
        raise _Failed(ERROR, REDIRECT_MESSAGE)
    if reply.status != 200:
        raise _Failed(ERROR, HTTP_STATUS_MESSAGE.format(reply.status))
    payload = _json_object(reply.body)
    if payload is None:
        raise _Failed(ERROR, UNREADABLE_MESSAGE)
    return payload


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


# ----------------------------------------------------------------- field readers


def _text(value: Any) -> str:
    """Plain text from a server value: whitespace collapsed, length capped. Anything else is empty."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    try:
        raw = str(value)
    except ValueError:  # an int too large to print
        return ""
    text = " ".join(raw.split())
    if len(text) > MAX_FIELD_CHARS:
        return text[: MAX_FIELD_CHARS - 3] + "..."
    return text


def _path_text(value: Any) -> str:
    """A file path, kept exactly as sent. A path with control characters or over the cap is refused."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS or _CONTROL.search(value):
        return ""
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _size(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _flag(value: Any) -> bool:
    return value is True


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names = (_text(item) for item in value[:MAX_LIST_ITEMS])
    return tuple(name for name in names if name)


def _limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    """The row limit, clamped to 1 through MAX_LIMIT. A value that is not a whole number gets the default."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(value, MAX_LIMIT))


# ----------------------------------------------------------------- query


def check_query(query: object) -> tuple[str, str | None]:
    """Normalise a search text and say why it cannot be sent. Returns (text, problem); problem is None when it can."""
    text = " ".join(query.split()) if isinstance(query, str) else ""
    if not text:
        return "", BLANK_QUERY_MESSAGE
    if len(text) > MAX_QUERY_CHARS:
        return text, QUERY_TOO_LONG_MESSAGE
    return text, None


# ----------------------------------------------------------------- outputs


def _hit(item: Any) -> OutputHit | None:
    if not isinstance(item, dict):
        return None
    path = _path_text(item.get("path"))
    if not path:
        return None
    return OutputHit(
        path=path,
        score=_number(item.get("score")),
        kind=_text(item.get("kind")),
        is_archival=_flag(item.get("is_archival")),
        canonical_path=_path_text(item.get("canonical_path")),
        generating_script=_text(item.get("generating_script")),
        csv_columns=_names(item.get("csv_columns")),
        json_keys=_names(item.get("json_keys")),
        size=_size(item.get("size")),
        modified=_text(item.get("mtime")),
    )


def _index_state(payload: dict[str, Any]) -> IndexState | None:
    """The index state from the response. None when the server reports none of its fields.

    The convergence object is read when present. Otherwise the top-level partial, pending and
    write-error fields of the search response are read. ``degraded`` is conservative: any
    partial, queued, unreadable or unsaved signal makes the index degraded, even if the server
    said otherwise.
    """
    convergence = payload.get("convergence")
    if isinstance(convergence, dict):
        source: dict[str, Any] = convergence
    elif any(key in payload for key in ("partial", "pending_count", "db_write_error")):
        source = payload
    else:
        return None
    partial = _flag(source.get("partial_index", source.get("partial")))
    pending = _count(source.get("pending_count"))
    db_failed = bool(source.get("db_write_error"))
    walk_errors = source.get("walk_errors")
    walk_count = len(walk_errors) if isinstance(walk_errors, list) else 0
    inconclusive = _flag(source.get("inconclusive")) or walk_count > 0 or db_failed
    degraded = _flag(source.get("degraded")) or partial or pending > 0 or inconclusive
    return IndexState(
        degraded=degraded,
        partial=partial,
        pending_count=pending,
        db_write_failed=db_failed,
        walk_error_count=walk_count,
        last_rebuilt_at=_text(source.get("last_rebuilt_at")),
    )


def search_outputs(
    base_url: str,
    query: object,
    limit: object = DEFAULT_LIMIT,
    *,
    transport: Transport | None = None,
    timeout: float = SEARCH_TIMEOUT,
) -> OutputsResult:
    """Search the outputs index on the local server. Never raises.

    A blank or over-long query is refused before any request. A 404 is a missing route, not an
    empty result. A 200 with an ``error`` field, or without a ``hits`` list, is an error.
    """
    text, problem = check_query(query)
    if problem is not None:
        return OutputsResult(INVALID, problem, query=text)
    path = OUTPUTS_SEARCH_PATH + "?" + urllib.parse.urlencode({"q": text, "limit": _limit(limit)})
    try:
        payload = _get_object(base_url, path, timeout, transport, OUTPUTS_MISSING_ROUTE_MESSAGE)
    except _Failed as failure:
        return OutputsResult(failure.state, failure.message, query=text)
    server_error = payload.get("error")
    if server_error:
        return OutputsResult(ERROR, SERVER_ERROR_MESSAGE.format(_text(server_error) or NO_DETAIL), query=text)
    raw_hits = payload.get("hits")
    if not isinstance(raw_hits, list):
        return OutputsResult(ERROR, UNREADABLE_MESSAGE, query=text)
    hits = tuple(hit for hit in (_hit(item) for item in raw_hits[:MAX_LIMIT]) if hit is not None)
    index = _index_state(payload)
    if hits:
        return OutputsResult(RESULTS, OUTPUTS_RESULTS_MESSAGE.format(len(hits), text), text, hits, index)
    if index is not None and index.degraded:
        message = OUTPUTS_EMPTY_DEGRADED_MESSAGE.format(text)
    else:
        message = OUTPUTS_EMPTY_MESSAGE.format(text)
    return OutputsResult(EMPTY, message, text, (), index)


# ----------------------------------------------------------------- research


def _run(item: Any) -> ResearchRun | None:
    if not isinstance(item, dict):
        return None
    run_id = _text(item.get("id"))
    if not run_id:
        return None
    return ResearchRun(
        run_id=run_id,
        state=_text(item.get("status") or item.get("state")),
        provider=_text(item.get("provider")),
        started_at=_text(item.get("started_at")),
        updated_at=_text(item.get("updated_at")),
        completed_at=_text(item.get("completed_at")),
    )


def list_research_runs(
    base_url: str,
    limit: object = DEFAULT_RUN_LIMIT,
    *,
    transport: Transport | None = None,
    timeout: float = RUNS_TIMEOUT,
) -> ResearchResult:
    """List research runs on the local server. Read-only. Never raises.

    A 404 is a missing route. A 200 whose ``runs`` is not a list is an error, so a broken
    response is never shown as an empty list.
    """
    path = RESEARCH_RUNS_PATH + "?" + urllib.parse.urlencode({"limit": _limit(limit, DEFAULT_RUN_LIMIT)})
    try:
        payload = _get_object(base_url, path, timeout, transport, RUNS_MISSING_ROUTE_MESSAGE)
    except _Failed as failure:
        return ResearchResult(failure.state, failure.message)
    server_error = payload.get("error")
    if server_error:
        return ResearchResult(ERROR, SERVER_ERROR_MESSAGE.format(_text(server_error) or NO_DETAIL))
    raw_runs = payload.get("runs")
    if not isinstance(raw_runs, list):
        return ResearchResult(ERROR, UNREADABLE_MESSAGE)
    runs = tuple(run for run in (_run(item) for item in raw_runs[:MAX_LIMIT]) if run is not None)
    if runs:
        return ResearchResult(RESULTS, RUNS_RESULTS_MESSAGE.format(len(runs)), runs)
    return ResearchResult(EMPTY, RUNS_EMPTY_MESSAGE)


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_RUN_LIMIT",
    "EMPTY",
    "ERROR",
    "INVALID",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "MISSING_ROUTE",
    "NOT_RUNNING",
    "OUTPUTS_SEARCH_PATH",
    "RESEARCH_RUNS_PATH",
    "RESULTS",
    "IndexState",
    "OutputHit",
    "OutputsResult",
    "Reply",
    "ResearchResult",
    "ResearchRun",
    "Transport",
    "check_query",
    "fetch",
    "list_research_runs",
    "search_outputs",
]
