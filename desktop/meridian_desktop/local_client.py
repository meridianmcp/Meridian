"""Loopback client for the local Meridian server. Pure Python: stdlib urllib only, no Qt import.

Every request goes to an http URL whose host is 127.0.0.1, localhost or ::1
(assert_loopback). Each request has a timeout, which bounds each read and sets a deadline
for the body (see _get), a fixed User-Agent and no credentials. Redirects are refused and not followed.
A response is capped at MAX_RESPONSE_BYTES, enforced before anything is parsed. A proxy
is never used, so loopback traffic stays on this machine.

The RunPod key never crosses this client. Meridian reads it from the keychain
itself, through its ``keyring:`` credential reference.

Errors carry a short code and fixed text. They never carry response body text.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from meridian_desktop import __version__
from meridian_desktop.projects_reader import PROJECT_COLUMNS

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_SCHEME = "http"
MAX_RESPONSE_BYTES = 1024 * 1024
_READ_CHUNK = 64 * 1024
DEFAULT_TIMEOUT = 10.0
USER_AGENT = f"MeridianDesktop/{__version__} (local client)"

HEALTH_RUNNING = "running"
HEALTH_NOT_RUNNING = "not_running"
HEALTH_NOT_MERIDIAN = "not_meridian"
HEALTH_ERROR = "error"

HEALTH_PATH = "/health"
PROJECTS_PATH = "/projects"
RUNPOD_CHECK_PATH = "/compute/runpod/check"
RUNPOD_PODS_PATH = "/compute/runpod/pods"

NOT_RUNNING_MESSAGE = "The local Meridian server is not running."
TIMEOUT_MESSAGE = "The local Meridian server did not answer in time."
UNREACHABLE_MESSAGE = "The local Meridian server could not be reached."
TOO_LARGE_MESSAGE = f"The local Meridian server sent more than {MAX_RESPONSE_BYTES} bytes."
REDIRECT_MESSAGE = "The local Meridian server sent a redirect, which the desktop does not follow."
BAD_JSON_MESSAGE = "The local Meridian server sent a response the desktop could not read."
BAD_URL_MESSAGE = "The local Meridian server address could not be used."

# A server error code is copied only when it is a short lowercase slug.
_SAFE_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")


class LocalClientError(Exception):
    """A local request failed. ``code`` is a short slug. The message is fixed text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _loopback_problem(url: object) -> str | None:
    """Return why ``url`` is refused, or None when it is an allowed loopback URL."""
    if not isinstance(url, str) or not url:
        return "The local server address is empty or is not text."
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return "The local server address is not a valid URL."
    if parts.scheme != ALLOWED_SCHEME:
        return "The local server address must use http."
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return "The local server address must not contain a user name or password."
    if host not in LOOPBACK_HOSTS:
        return "The local server address must be 127.0.0.1, localhost or ::1."
    if port is not None and not 1 <= port <= 65535:
        return "The local server address has an invalid port."
    if parts.query or parts.fragment:
        return "The local server address must not contain a query or fragment."
    return None


def assert_loopback(url: str) -> None:
    """Raise LocalClientError unless ``url`` is an http URL on a loopback host.

    Refused: any scheme other than http, userinfo (a user name or password), any host
    other than 127.0.0.1, localhost or ::1 (lookalikes such as 127.0.0.1.example.com
    included), an invalid or zero port, and a query or fragment.
    """
    problem = _loopback_problem(url)
    if problem is not None:
        raise LocalClientError("not_loopback", problem)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return no follow-up request. urllib then raises HTTPError for the 3xx response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _opener() -> urllib.request.OpenerDirector:
    # An empty ProxyHandler stops urllib from routing loopback requests through a proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())


@dataclass(frozen=True)
class _Reply:
    status: int
    body: bytes


# A transport takes the request and a timeout in seconds, and returns the reply. Tests pass a fake,
# so they need no socket. The default is urllib, with the bounded read and the refused redirect.
Transport = Callable[[urllib.request.Request, float], _Reply]


def _bounded_read(response: Any, deadline: float) -> bytes:
    """Read at most MAX_RESPONSE_BYTES, and stop once ``deadline`` (a time.monotonic value) passes.

    A larger body is refused before any parsing. The deadline is checked before each read,
    and read1 returns as soon as some bytes arrive, so a server that trickles bytes is cut
    off at the first read after the deadline. A read already waiting for data is still
    limited by the socket timeout, so the body read ends at most one timeout after the deadline.
    """
    declared = response.headers.get("Content-Length") if response.headers is not None else None
    if declared is not None and declared.strip().isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise LocalClientError("too_large", TOO_LARGE_MESSAGE)
    body = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise LocalClientError("timeout", TIMEOUT_MESSAGE)
        chunk = response.read1(min(_READ_CHUNK, MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise LocalClientError("too_large", TOO_LARGE_MESSAGE)


def _transport_error(reason: object) -> LocalClientError:
    """Map a failure to open the connection. The reason comes from URLError.

    A refused connection, or one that never opens, means no server is listening on the
    loopback port. Some hosts drop the connection attempt instead of refusing it, so
    both read as not_running. A read timeout is raised outside URLError, so it stays
    "timeout" (see _get).
    """
    if isinstance(reason, (ConnectionRefusedError, TimeoutError)):
        return LocalClientError("not_running", NOT_RUNNING_MESSAGE)
    return LocalClientError("unreachable", UNREACHABLE_MESSAGE)


def _get(base_url: str, path: str, timeout: float, transport: Transport | None = None) -> _Reply:
    """GET one path under base_url. A non-2xx status is returned as a reply, not raised.

    ``timeout`` is the socket timeout for each blocking read: the connect, each header line and
    each body read. The body deadline is ``timeout`` seconds from the start of this call, and it is
    checked before each body read. The header lines have no deadline of their own, and a body read
    already waiting for data can run one more timeout past the deadline. So a call is not bounded
    to ``timeout`` seconds in total. ``transport``, when given, sends the request in place of urllib,
    and the timeout is passed to it unchanged. The loopback check still runs first, so a refused URL
    reaches no transport.
    """
    assert_loopback(base_url)
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    if transport is not None:
        return transport(request, timeout)
    try:
        with _opener().open(request, timeout=timeout) as response:
            return _Reply(response.status, _bounded_read(response, deadline))
    except urllib.error.HTTPError as exc:
        try:
            return _Reply(exc.code, _bounded_read(exc, deadline))
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise _transport_error(exc.reason) from None
    except TimeoutError:
        raise LocalClientError("timeout", TIMEOUT_MESSAGE) from None
    except (OSError, http.client.HTTPException):
        raise LocalClientError("unreachable", UNREACHABLE_MESSAGE) from None
    except ValueError:
        raise LocalClientError("bad_url", BAD_URL_MESSAGE) from None


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _json_list(body: bytes) -> list[dict[str, Any]] | None:
    """Parse a JSON array whose items are all objects. Return None for anything else."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return value


def _server_code(status: int, body: bytes) -> str:
    """Copy the server's error.code when it is a safe slug. Otherwise name the status."""
    payload = _json_object(body)
    error = payload.get("error") if payload is not None else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    return f"http_{status}"


def probe_health(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Return running, not_running, not_meridian or error, read from GET /health.

    running: HTTP 200 with a JSON object whose service is "meridian" and status is "ok".
    not_running: nothing is listening (connection refused, or the connection never opens).
    not_meridian: a response came back, but it is not Meridian's health body. That covers
    any other status, a redirect (not followed), non-JSON, or another service.
    error: the URL was refused, the request timed out, the body was too large, or the
    server could not be reached for another reason.
    """
    try:
        reply = _get(base_url, HEALTH_PATH, timeout)
    except LocalClientError as exc:
        return HEALTH_NOT_RUNNING if exc.code == "not_running" else HEALTH_ERROR
    body = _json_object(reply.body)
    if reply.status == 200 and body is not None and body.get("service") == "meridian" and body.get("status") == "ok":
        return HEALTH_RUNNING
    return HEALTH_NOT_MERIDIAN


def list_projects(
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    transport: Transport | None = None,
) -> list[dict[str, Any]]:
    """GET {base_url}/projects and return the rows, each keyed by projects_reader.PROJECT_COLUMNS.

    The URL is checked first, so a refused URL sends no request. A redirect is refused and not
    followed. Any other non-200 status raises LocalClientError, with the server's error code when
    it is a safe slug. The body must be a JSON array of objects. Anything else raises "bad_json",
    and the body is never echoed. A row keeps only the listed columns, and a column it lacks reads
    as None, as it does for the file reader. ``timeout`` is 10 seconds by default. It is the socket
    timeout for each read and the body deadline, not a limit on the whole call (see _get).
    """
    reply = _get(base_url, PROJECTS_PATH, timeout, transport=transport)
    if 300 <= reply.status < 400:
        raise LocalClientError("redirect_refused", REDIRECT_MESSAGE)
    if reply.status != 200:
        code = _server_code(reply.status, reply.body)
        raise LocalClientError(code, f"The local Meridian server returned HTTP {reply.status}.")
    rows = _json_list(reply.body)
    if rows is None:
        raise LocalClientError("bad_json", BAD_JSON_MESSAGE)
    return [{key: row.get(key) for key in PROJECT_COLUMNS} for row in rows]


def _runpod_json(path: str, base_url: str, timeout: float) -> dict[str, Any]:
    reply = _get(base_url, path, timeout)
    if 300 <= reply.status < 400:
        raise LocalClientError("redirect_refused", REDIRECT_MESSAGE)
    if reply.status != 200:
        raise LocalClientError(_server_code(reply.status, reply.body), f"The local Meridian server returned HTTP {reply.status}.")
    payload = _json_object(reply.body)
    if payload is None:
        raise LocalClientError("bad_json", BAD_JSON_MESSAGE)
    return payload


def runpod_check(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """GET /compute/runpod/check. Return the parsed JSON object on HTTP 200.

    The body holds ok, credential_source, pod_count, billing_available and error. A
    completed check returns 200 even when ok is false, so ``error`` says why.
    Raise LocalClientError for any other status, a refused redirect, an oversize body
    or a body that is not a JSON object.
    """
    return _runpod_json(RUNPOD_CHECK_PATH, base_url, timeout)


def runpod_list_pods(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """GET /compute/runpod/pods. Return the parsed JSON object on HTTP 200.

    The body holds ok, credential_source, pods, pod_count, has_more_pods and error.
    Raise LocalClientError on the same terms as runpod_check.
    """
    return _runpod_json(RUNPOD_PODS_PATH, base_url, timeout)

RUNPOD_OFFERS_PATH = "/compute/offers"


def offers_path(query: Mapping[str, str] | None = None) -> str:
    """The path for GET /compute/offers, with only the given query keys encoded."""
    if not query:
        return RUNPOD_OFFERS_PATH
    return RUNPOD_OFFERS_PATH + "?" + urllib.parse.urlencode(dict(query))


def runpod_offers(base_url: str, query: Mapping[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """GET /compute/offers with the given filters. Return the parsed JSON object on HTTP 200.

    The body holds ok, status, offers, count, gpu_models, providers and error. A
    completed read returns 200 even when ok is false, so ``error`` says why. Raise
    LocalClientError on the same terms as runpod_check.
    """
    return _runpod_json(offers_path(query), base_url, timeout)


__all__ = [
    "ALLOWED_SCHEME",
    "DEFAULT_TIMEOUT",
    "HEALTH_ERROR",
    "HEALTH_NOT_MERIDIAN",
    "HEALTH_NOT_RUNNING",
    "HEALTH_RUNNING",
    "LOOPBACK_HOSTS",
    "MAX_RESPONSE_BYTES",
    "USER_AGENT",
    "LocalClientError",
    "Transport",
    "assert_loopback",
    "list_projects",
    "offers_path",
    "probe_health",
    "runpod_check",
    "runpod_list_pods",
    "runpod_offers",
]
