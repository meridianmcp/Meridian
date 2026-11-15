"""Hosted Meridian client: GET /projects with a bearer token. Pure Python: urllib, plus certifi for the default host's CA set. No Qt import.

``list_projects`` sends one GET to {hosted URL}/projects with ``Authorization: Bearer <token>`` and
returns the parsed JSON list. The caller reads the token from the keychain and passes it in. This
module never stores the token and never writes it to a file.

Refused before any request is made:
- a base URL that is not https, unless its host is 127.0.0.1, localhost or ::1 (http is allowed there);
- a base URL that holds a user name, a password, a query or a fragment;
- an empty token, or a token with a space, a control character or non-ASCII text.

Each request has a 10 second timeout and no retries. Redirects are not followed, so a 3xx reply is an
error and the bearer token never goes to a second host. A response is capped at MAX_RESPONSE_BYTES,
checked before anything is parsed.

Errors are typed. Each message is fixed text, with no token, no response body and no raw exception.
A transport failure is reported by its kind alone, and the exception is not chained, so no traceback
holds the request headers.

The transport is injectable. The default one is urllib. Tests pass a fake, so they need no network.
"""

from __future__ import annotations

import http.client
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import certifi

from meridian_desktop import __version__
from meridian_desktop.local_client import LOOPBACK_HOSTS

HOSTED_TIMEOUT = 10.0
PROJECTS_PATH = "/projects"

# The host that trusts only the certifi CA bundle. A custom hosted URL uses the system store, so a private CA works.
DEFAULT_HOSTED_HOST = "usemeridian.us"


_LOG = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_READ_CHUNK = 64 * 1024
USER_AGENT = f"MeridianDesktop/{__version__} (hosted client)"

NOT_SIGNED_IN_TEXT = "No hosted token is saved. Sign in with your browser in Settings."
REJECTED_TEXT = (
    "Hosted Meridian did not accept the saved token (HTTP 401). "
    "Sign in again with your browser in Settings."
)
FORBIDDEN_TEXT = "Hosted Meridian refused the saved token for projects (HTTP 403). Check that the token can read projects."
NOT_FOUND_TEXT = "Hosted Meridian has no projects page at this URL (HTTP 404). Check the hosted URL in Settings."
TIMEOUT_TEXT = "Hosted Meridian did not answer in time. Check your network connection, then try again."
UNREACHABLE_TEXT = "Hosted Meridian could not be reached. Check the hosted URL and your network connection."
BAD_RESPONSE_TEXT = "Hosted Meridian sent a response the desktop could not read."
TOO_LARGE_TEXT = f"Hosted Meridian sent more than {MAX_RESPONSE_BYTES} bytes, which the desktop does not read."
REDIRECT_TEXT = "Hosted Meridian sent a redirect, which the desktop does not follow. Check the hosted URL in Settings."
TOKEN_FORMAT_TEXT = "A hosted token has no spaces and only plain ASCII characters. Sign in again in Settings."
BAD_URL_TEXT = "The hosted URL is not a valid URL."
BAD_SCHEME_TEXT = "The hosted URL must use https, or http on 127.0.0.1, localhost or ::1."


class HostedError(Exception):
    """A hosted request failed. ``code`` is a short slug, ``status`` the HTTP status when there was one.

    ``str(error)`` is the fixed message. It never holds a token, a response body or a raw exception.
    """

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class NotSignedIn(HostedError):
    """No token is saved (status None), or the server refused the token with HTTP 401."""

    def __init__(self, message: str = NOT_SIGNED_IN_TEXT, status: int | None = None) -> None:
        super().__init__("not_signed_in", message, status)


class Forbidden(HostedError):
    """The server answered HTTP 403 for the projects list."""

    def __init__(self, message: str = FORBIDDEN_TEXT, status: int | None = 403) -> None:
        super().__init__("forbidden", message, status)


class NotFound(HostedError):
    """The server answered HTTP 404 for the projects path."""

    def __init__(self, message: str = NOT_FOUND_TEXT, status: int | None = 404) -> None:
        super().__init__("not_found", message, status)


class HostedTimeout(HostedError):
    """The request did not finish within the timeout."""

    def __init__(self, message: str = TIMEOUT_TEXT) -> None:
        super().__init__("timeout", message)


class HostedUnreachable(HostedError):
    """The connection could not be made or was lost. The cause is not given, only its kind."""

    def __init__(self, message: str = UNREACHABLE_TEXT) -> None:
        super().__init__("unreachable", message)


class BadToken(HostedError):
    """The token holds a space, a control character or non-ASCII text, which a header cannot carry."""

    def __init__(self, message: str = TOKEN_FORMAT_TEXT) -> None:
        super().__init__("bad_token", message)


class BadBaseUrl(HostedError, ValueError):
    """The hosted URL may not be used. It is a ValueError too, so callers that expect one still catch it."""

    def __init__(self, message: str) -> None:
        super().__init__("bad_base_url", message)


@dataclass(frozen=True)
class Reply:
    """One HTTP reply: the status and the body bytes. A non-2xx body may be left empty."""

    status: int
    body: bytes


# A transport takes the request and a timeout in seconds, and returns the reply. It may raise
# TimeoutError for a timeout, or OSError / http.client.HTTPException / ValueError for any other failure.
Transport = Callable[[urllib.request.Request, float], Reply]


def validate_base_url(url: object) -> str:
    """Return the hosted base URL with surrounding spaces and any trailing slash removed.

    Raise BadBaseUrl when the URL may not be used: an empty value, a bad URL, a user name or password,
    a query or fragment, or a scheme and host that are neither https nor http on a loopback host.
    """
    if not isinstance(url, str) or not url.strip():
        raise BadBaseUrl("The hosted URL is empty.")
    text = url.strip()
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise BadBaseUrl(BAD_URL_TEXT) from None
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise BadBaseUrl("The hosted URL must not contain a user name or password.")
    if parts.query or parts.fragment:
        raise BadBaseUrl("The hosted URL must not contain a query or fragment.")
    if parts.scheme == "https" and host:
        return text.rstrip("/")
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return text.rstrip("/")
    raise BadBaseUrl(BAD_SCHEME_TEXT)


def usable_token(token: object) -> str:
    """Return the token with surrounding spaces removed.

    Raise NotSignedIn when the token is empty, and BadToken when it holds a space, a control character
    or non-ASCII text. Such a value cannot be sent in a header, and the error never repeats it.
    """
    if not isinstance(token, str) or not token.strip():
        raise NotSignedIn()
    cleaned = token.strip()
    if not cleaned.isascii() or not cleaned.isprintable() or any(ch.isspace() for ch in cleaned):
        raise BadToken()
    return cleaned


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return no follow-up request, so urllib raises HTTPError for the 3xx reply and nothing is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _bounded_read(response: Any, deadline: float) -> bytes:
    """Read at most MAX_RESPONSE_BYTES, and stop once ``deadline`` (a time.monotonic value) has passed."""
    declared = response.headers.get("Content-Length") if response.headers is not None else None
    if declared is not None and declared.strip().isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise HostedError("too_large", TOO_LARGE_TEXT)
    body = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("hosted read ran past its deadline")
        chunk = response.read1(min(_READ_CHUNK, MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise HostedError("too_large", TOO_LARGE_TEXT)


class _CertificateRefused(OSError):
    """The trust store refused the server's certificate. Raised inside the transport only; _send maps it."""


MAX_ERROR_BYTES = 16 * 1024


def _error_body(exc: urllib.error.HTTPError) -> bytes:
    """At most MAX_ERROR_BYTES of an error reply's body. A body that cannot be read leaves the status alone."""
    try:
        return exc.read(MAX_ERROR_BYTES)
    except (OSError, http.client.HTTPException):
        return b""


def _open_exchange(
    request: urllib.request.Request, timeout: float, deadline: float, context: ssl.SSLContext | None
) -> Reply:
    """One request with the given TLS context (None means the system default). No redirects, a bounded body."""
    opener = urllib.request.build_opener(_NoRedirects(), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=timeout) as response:
            return Reply(response.status, _bounded_read(response, deadline))
    except urllib.error.HTTPError as exc:
        # urllib raises for a non-2xx status and for the refused redirect. The status and a short body are kept: the
        # device flow reads the error name from the body. Headers are never kept.
        body = _error_body(exc)
        exc.close()
        return Reply(exc.code, body)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError("hosted connection timed out") from None
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise _CertificateRefused() from None
        raise


def _tls_context_for(request: urllib.request.Request) -> ssl.SSLContext | None:
    """The TLS context for one request, or None for the system default.

    The default host trusts only the certifi CA bundle, so a machine's own certificate store cannot break it.
    Other hosts keep the system default, so a private CA still works there. Chain and hostname are verified either way.
    """
    if _host_of(request) == DEFAULT_HOSTED_HOST:
        return ssl.create_default_context(cafile=certifi.where())
    return None


def _urllib_transport(request: urllib.request.Request, timeout: float) -> Reply:
    """The real transport: urllib, no redirects, a bounded body. A non-2xx status is returned, not raised.

    The TLS context comes from _tls_context_for, so the default host always uses the certifi bundle.
    """
    deadline = time.monotonic() + timeout
    return _open_exchange(request, timeout, deadline, context=_tls_context_for(request))


def _failure_detail(exc: BaseException) -> str:
    """The exception's class, and for a URLError the reason's class and errno. Never a message text: a message can
    hold a token or a header, and a URLError reason is only the text of an OS error."""
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        reason_name = type(reason).__name__ if reason is not None else "none"
        return f"URLError(reason={reason_name}, errno={getattr(reason, 'errno', None)})"
    return type(exc).__name__


def _host_of(request: urllib.request.Request) -> str:
    """The hostname the request goes to. The URL holds no token, and only the host is logged."""
    return urllib.parse.urlsplit(request.full_url).hostname or "unknown"


def _send(transport: Transport, request: urllib.request.Request, timeout: float) -> Reply:
    """Run the transport and map its failures to typed errors. The original exception is not attached."""
    kind = "unreachable"
    detail = "none"
    try:
        return transport(request, timeout)
    except HostedError:
        raise
    except TimeoutError as exc:
        kind = "timeout"
        detail = type(exc).__name__
    except (OSError, http.client.HTTPException, ValueError) as exc:
        kind = "unreachable"
        detail = _failure_detail(exc)
    # Logged to desktop.log so a failed attempt leaves a line behind: the kind, the host, and the exception's
    # class and errno. No message text, no headers, no token.
    _LOG.warning("hosted request failed: %s, host=%s, %s", kind, _host_of(request), detail)
    # Raised outside the except blocks, so the transport's exception (which may hold request headers)
    # is not attached as context.
    if kind == "timeout":
        raise HostedTimeout() from None
    raise HostedUnreachable() from None


def _json_list(body: bytes) -> list[dict[str, Any]] | None:
    """Parse a JSON array of objects. Return None for anything else."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return value


def _rows(reply: Reply) -> list[dict[str, Any]]:
    """Turn one reply into the projects list, or raise the typed error for its status."""
    status = reply.status
    if 200 <= status < 300:
        rows = _json_list(reply.body)
        if rows is None:
            raise HostedError("bad_response", BAD_RESPONSE_TEXT, status)
        return rows
    if status == 401:
        raise NotSignedIn(REJECTED_TEXT, status)
    if status == 403:
        raise Forbidden(status=status)
    if status == 404:
        raise NotFound(status=status)
    if 300 <= status < 400:
        raise HostedError("redirect_refused", REDIRECT_TEXT, status)
    raise HostedError(f"http_{status}", f"Hosted Meridian returned HTTP {status}.", status)


def list_projects(
    base_url: str,
    token: str | None,
    *,
    transport: Transport | None = None,
    timeout: float = HOSTED_TIMEOUT,
) -> list[dict[str, Any]]:
    """GET {base_url}/projects with the bearer token, and return the parsed list of project rows.

    The URL and the token are checked first, so a refused value sends no request. ``transport`` is
    injectable, and the default is urllib. ``timeout`` is the whole-request limit in seconds.
    """
    base = validate_base_url(base_url)
    bearer = usable_token(token)
    request = urllib.request.Request(
        base + PROJECTS_PATH,
        method="GET",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    reply = _send(transport if transport is not None else _urllib_transport, request, timeout)
    return _rows(reply)


__all__ = [
    "HOSTED_TIMEOUT",
    "MAX_RESPONSE_BYTES",
    "PROJECTS_PATH",
    "BadBaseUrl",
    "BadToken",
    "Forbidden",
    "HostedError",
    "HostedTimeout",
    "HostedUnreachable",
    "NotFound",
    "NotSignedIn",
    "Reply",
    "Transport",
    "list_projects",
    "usable_token",
    "validate_base_url",
]
