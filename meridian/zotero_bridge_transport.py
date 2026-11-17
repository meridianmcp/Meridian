"""The real transport and reader for the Zotero bridge client (meridian/zotero_bridge_client.py).

Two loopback endpoints, and nothing else:

- The bridge write API on 127.0.0.1:23125. Each request carries ``Host: 127.0.0.1:23125`` and an
  ``Authorization: Bearer`` header. The token is read from the token file on every call. It is not kept in a module
  variable, not logged, and not placed in an exception message. The client's own headers (``Meridian-Write-Grant``
  and ``Content-Type``) pass through unchanged.
- Zotero's local read API on 127.0.0.1:23119. GET only, with no token.

Every URL is checked before it is sent. A URL whose host is not 127.0.0.1, or whose port is not the expected one, is
refused, and the opener is never called. Proxies are not used, and redirects are not followed, so a reply cannot move
a request or the token to another host.

``get_client()`` builds one ZoteroBridgeClient per process from these two callables, so the grant key the client holds
serves the whole process. ``reset_client_for_tests()`` drops it.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

from meridian.exception_chain import raise_unchained
from meridian.zotero_bridge_api import API_PORT, token_file_path
from meridian.zotero_bridge_client import Reader, Transport, ZoteroBridgeClient

LOOPBACK_HOST = "127.0.0.1"
READER_PORT = 23119
WRITE_TIMEOUT_SECONDS = 120.0
READ_TIMEOUT_SECONDS = 30.0
MAX_REPLY_BYTES = 64 * 1024 * 1024

_TOKEN_TRIM = " \t\r\n\f\v﻿"
# The plugin's token shape (see zotero_bridge_api): a URL-safe value of useful length, with the Meridian prefix.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{32,}")
_NO_TOKEN = "The Zotero bridge token file is missing or unusable. Is the Zotero Bridge plugin installed?"
_NOT_ANSWERED = "The local Zotero endpoint did not answer."
_REFUSED = "The request was refused: Meridian reaches the Zotero bridge only on 127.0.0.1."

T = TypeVar("T")


class BridgeTransportError(Exception):
    """A token, send, or read step failed or was refused. The message is fixed text, and it never carries the token."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Does not follow redirects, so a reply cannot send a request or the token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


def default_opener() -> urllib.request.OpenerDirector:
    """An opener with no proxies and no redirects. Proxy settings from the environment are ignored on purpose."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def read_token() -> str:
    """The bearer token, read from the token file now. Raises BridgeTransportError if the file is missing or unusable."""
    failed = False
    token = ""
    try:
        token = token_file_path().read_text(encoding="utf-8-sig").strip(_TOKEN_TRIM)
    except (OSError, ValueError):
        failed = True
    if failed or not _TOKEN_SHAPE.fullmatch(token):
        raise_unchained(BridgeTransportError(_NO_TOKEN))
    return token


def _require_endpoint(url: str, port: int) -> None:
    """Refuses any URL that does not name 127.0.0.1 on ``port``. Runs before every send."""
    try:
        parts = urlsplit(url)
        allowed = parts.scheme == "http" and parts.hostname == LOOPBACK_HOST and parts.port == port
    except ValueError:
        allowed = False
    if not allowed:
        raise BridgeTransportError(_REFUSED)


def _capped(stream: Any) -> bytes:
    data = stream.read(MAX_REPLY_BYTES + 1)
    return b"" if len(data) > MAX_REPLY_BYTES else data


def _exchange(
    opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout: float
) -> tuple[int, bytes]:
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), _capped(response)
    except urllib.error.HTTPError as error:
        # A refusal from the bridge is a reply, not a failure: its status and body go back to the client.
        with error:
            return error.code, _capped(error)


def _parse(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _send(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    port: int,
    headers: dict[str, str],
    body: Any,
    timeout: float,
) -> tuple[int, Any]:
    _require_endpoint(url, port)
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    # Host and the credential headers are added as unredirected headers, so they never travel with a redirect.
    request.add_unredirected_header("Host", f"{LOOPBACK_HOST}:{port}")
    for name, value in headers.items():
        request.add_unredirected_header(name, value)
    failed = False
    status = 0
    raw = b""
    try:
        status, raw = _exchange(opener, request, timeout)
    except Exception:  # noqa: BLE001 - the text can hold the token (a header value, say); none of it is kept
        failed = True
    if failed:
        raise_unchained(BridgeTransportError(_NOT_ANSWERED))
    return status, _parse(raw)


def make_write_transport(
    *,
    opener: urllib.request.OpenerDirector | None = None,
    token_reader: Callable[[], str] = read_token,
) -> Transport:
    """A ``transport(method, path, headers, body) -> (status, json)`` for the bridge write API on 127.0.0.1:23125."""
    open_with = opener if opener is not None else default_opener()

    def transport(method: str, path: str, headers: dict[str, str], body: Any) -> tuple[int, Any]:
        token = token_reader()
        send_headers = dict(headers)
        send_headers["Authorization"] = f"Bearer {token}"
        url = f"http://{LOOPBACK_HOST}:{API_PORT}{path}"
        return _send(open_with, method, url, API_PORT, send_headers, body, WRITE_TIMEOUT_SECONDS)

    return transport


def make_reader(*, opener: urllib.request.OpenerDirector | None = None) -> Reader:
    """A ``reader(path) -> (status, json)`` for Zotero's local read API on 127.0.0.1:23119. GET only, no token."""
    open_with = opener if opener is not None else default_opener()

    def reader(path: str) -> tuple[int, Any]:
        url = f"http://{LOOPBACK_HOST}:{READER_PORT}{path}"
        return _send(open_with, "GET", url, READER_PORT, {}, None, READ_TIMEOUT_SECONDS)

    return reader


_LOCK = threading.RLock()
_client: ZoteroBridgeClient | None = None


def get_client() -> ZoteroBridgeClient:
    """The process-wide client, built on first use from the real write transport and reader. No network until a call."""
    global _client
    with _LOCK:
        if _client is None:
            _client = ZoteroBridgeClient(transport=make_write_transport(), reader=make_reader())
        return _client


def call(action: Callable[[ZoteroBridgeClient], T]) -> T:
    """Runs ``action`` with the process-wide client. Calls are serialized, because the client is not thread-safe."""
    with _LOCK:
        return action(get_client())


def reset_client_for_tests() -> None:
    """Drops the process-wide client, so the next call builds a new one."""
    global _client
    with _LOCK:
        _client = None
