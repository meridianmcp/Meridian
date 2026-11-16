"""Client for the Code tab: one loopback GET to the local Meridian server's code search route.
Pure Python: stdlib only, no Qt import.

    GET {local_url}/desktop/code/search?q=<query>&limit=<n>

search() maps each answer to one typed state, and it never raises:

- 200 with a JSON object {"results": [{"path", "line", "score", "snippet"}, ...]}: STATE_OK. The
  list may be empty. A malformed entry is skipped, and at most MAX_LIMIT entries are read.
- 404: STATE_ROUTE_MISSING. The server answers, but it has no code search route, so it is too old.
- A non-2xx answer whose JSON error.code is "not_indexed" (the server should send 409): STATE_NOT_INDEXED.
  The code index has not been built.
- Nothing listens on the loopback port: STATE_NOT_RUNNING.
- A blank query: STATE_BLANK_QUERY. Nothing is sent.
- Anything else, such as a redirect, a timeout, an oversize or unreadable body, or an address that is
  not loopback: STATE_ERROR, with fixed text in ``detail``. Response body text is never copied into it.

The base address is checked for loopback before any transport call, so an injected transport never
sees another host. A transport is called as transport(base_url, path, timeout) and returns a Reply
(``status`` and ``body``), the same shape as local_client._get. The default transport is that function,
so redirects are refused, no proxy is used and the body is capped at MAX_RESPONSE_BYTES.
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

SEARCH_PATH = "/desktop/code/search"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_QUERY_CHARS = 200
MAX_PATH_CHARS = 300
MAX_SNIPPET_CHARS = 1000
NOT_INDEXED_CODE = "not_indexed"
_SAFE_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")

# Search states, as search() reports them.
STATE_OK = "ok"
STATE_NOT_RUNNING = "not_running"
STATE_ROUTE_MISSING = "route_missing"
STATE_NOT_INDEXED = "not_indexed"
STATE_BLANK_QUERY = "blank_query"
STATE_ERROR = "error"

BLANK_QUERY_MESSAGE = "Type a search term first. Nothing was sent to the local server."
BAD_RESPONSE_MESSAGE = "The local Meridian server sent a code search answer the desktop could not read."
REDIRECT_MESSAGE = "The local Meridian server sent a redirect, which the desktop does not follow."
HTTP_STATUS_MESSAGE = "The local Meridian server returned HTTP {}."
UNCHECKED_MESSAGE = "The code search could not be completed."


@dataclass(frozen=True)
class Reply:
    """One HTTP answer: the status code and the raw body bytes."""

    status: int
    body: bytes


# transport(base_url, path, timeout) -> Reply. It raises LocalClientError for a failed request.
Transport = Callable[[str, str, float], Reply]


@dataclass(frozen=True)
class CodeHit:
    """One ranked match. ``line`` is 0 when the server gave no usable line. ``score`` is None when it gave none."""

    path: str
    line: int
    score: float | None
    snippet: str


@dataclass(frozen=True)
class SearchResult:
    """The outcome of one search. ``hits`` is set only for STATE_OK. ``detail`` is fixed text for STATE_ERROR."""

    state: str
    hits: tuple[CodeHit, ...] = ()
    detail: str = ""


def _default_transport(base_url: str, path: str, timeout: float) -> Reply:
    reply = local_client._get(base_url, path, timeout)
    return Reply(reply.status, reply.body)


def _clamp_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, limit))


def _json_value(body: bytes) -> Any:
    """The parsed JSON value, or None when the body is not JSON text."""
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _server_code(body: bytes) -> str:
    """The server's error.code when it is a short lowercase slug, otherwise an empty string."""
    payload = _json_value(body)
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else ""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _path(value: object) -> str:
    """A path with its ends stripped and a length cap. Anything that is not text is empty."""
    return _clip(value.strip(), MAX_PATH_CHARS) if isinstance(value, str) else ""


def _snippet(value: object) -> str:
    """A snippet with trailing whitespace removed, so the first line keeps its indentation."""
    return _clip(value.rstrip(), MAX_SNIPPET_CHARS) if isinstance(value, str) else ""


def _line(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 0
    return value


def _score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _hit(entry: object) -> CodeHit | None:
    if not isinstance(entry, dict):
        return None
    path = _path(entry.get("path"))
    if not path:
        return None
    return CodeHit(
        path=path,
        line=_line(entry.get("line")),
        score=_score(entry.get("score")),
        snippet=_snippet(entry.get("snippet")),
    )


def parse_hits(body: bytes) -> tuple[CodeHit, ...] | None:
    """The hits in a 200 body, or None when the body is not an object with a results list."""
    payload = _json_value(body)
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    hits = (_hit(entry) for entry in results[:MAX_LIMIT])
    return tuple(hit for hit in hits if hit is not None)


def _answer(reply: Reply) -> SearchResult:
    status = reply.status
    if 300 <= status < 400:
        return SearchResult(STATE_ERROR, detail=REDIRECT_MESSAGE)
    if status == 404:
        return SearchResult(STATE_ROUTE_MISSING)
    if status == 200:
        hits = parse_hits(reply.body)
        if hits is None:
            return SearchResult(STATE_ERROR, detail=BAD_RESPONSE_MESSAGE)
        return SearchResult(STATE_OK, hits=hits)
    if _server_code(reply.body) == NOT_INDEXED_CODE:
        return SearchResult(STATE_NOT_INDEXED)
    return SearchResult(STATE_ERROR, detail=HTTP_STATUS_MESSAGE.format(status))


def search(
    base_url: str,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    transport: Transport | None = None,
    timeout: float = local_client.DEFAULT_TIMEOUT,
) -> SearchResult:
    """Search the code index of the local server at ``base_url``. Never raises.

    A blank query returns before any check. The base address is then checked for loopback, so
    a non-loopback address never reaches the transport. ``limit`` is clamped to 1..MAX_LIMIT.
    """
    term = query.strip()[:MAX_QUERY_CHARS] if isinstance(query, str) else ""
    if not term:
        return SearchResult(STATE_BLANK_QUERY, detail=BLANK_QUERY_MESSAGE)
    try:
        local_client.assert_loopback(base_url)
    except LocalClientError as exc:
        return SearchResult(STATE_ERROR, detail=str(exc))
    path = SEARCH_PATH + "?" + urllib.parse.urlencode({"q": term, "limit": _clamp_limit(limit)})
    fetch = _default_transport if transport is None else transport
    try:
        reply = fetch(base_url, path, timeout)
    except LocalClientError as exc:
        if exc.code == "not_running":
            return SearchResult(STATE_NOT_RUNNING)
        return SearchResult(STATE_ERROR, detail=str(exc))
    except Exception:  # noqa: BLE001 - a search must never raise into the UI
        return SearchResult(STATE_ERROR, detail=UNCHECKED_MESSAGE)
    return _answer(reply)


__all__ = [
    "BLANK_QUERY_MESSAGE",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_QUERY_CHARS",
    "SEARCH_PATH",
    "STATE_BLANK_QUERY",
    "STATE_ERROR",
    "STATE_NOT_INDEXED",
    "STATE_NOT_RUNNING",
    "STATE_OK",
    "STATE_ROUTE_MISSING",
    "CodeHit",
    "Reply",
    "SearchResult",
    "Transport",
    "parse_hits",
    "search",
]
