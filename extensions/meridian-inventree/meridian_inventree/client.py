"""HTTP client for one InvenTree instance.

The transport is injectable so the test suite never touches the network.
Errors carry a short, stable `code`. Messages never contain the token, request
headers, or response bodies. The client does not log the token.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Final

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0
# Wall-clock limit for one request, from send to the last byte. The per-socket timeout alone
# lets a server trickle bytes just under that timeout for as long as it likes.
REQUEST_DEADLINE_SECONDS: Final[float] = 60.0
MAX_PAGE_SIZE: Final[int] = 100
# Largest response body the client keeps from one HTTP answer.
MAX_RESPONSE_BYTES: Final[int] = 2 * 1024 * 1024
_READ_CHUNK_BYTES: Final[int] = 64 * 1024
# Page requests beyond the full-size read that a paginated call may spend.
_PAGE_SLACK: Final[int] = 3
_LOCAL_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost"})
_DEFAULT_PORTS: Final[dict[str, int]] = {"https": 443, "http": 80}

# (method, url, headers, body_bytes_or_None, timeout_seconds) -> (status, body_bytes)
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]]


class InvenTreeError(Exception):
    """A failed InvenTree call. `code` is stable; `message` is safe to show."""

    def __init__(self, code: str, message: str | None = None, status: int | None = None) -> None:
        self.code = code
        self.status = status
        self.message = message or code
        super().__init__(self.message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Authorization header never follows one to another host."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, bytes]:
    """Default transport. Returns (status, body) for any HTTP answer.

    Raises OSError (or a subclass) when the server cannot be reached, including when a success
    answer runs past REQUEST_DEADLINE_SECONDS. Raises bad_response when a success body is larger
    than MAX_RESPONSE_BYTES. An error answer always returns its status; its body is returned
    empty when it cannot be read in full, because callers never use an error body.
    """
    deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    # An empty ProxyHandler stops urllib from reading *_PROXY variables or the system proxy,
    # so the request always reaches the allowlisted origin directly.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), _read_capped(response, deadline)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _read_error_body(exc, deadline)
        finally:
            exc.close()


def _read_error_body(stream: Any, deadline: float) -> bytes:
    """Body of an HTTP error answer, or b"" when it cannot be kept.

    The status is what callers classify, and the body is never shown or parsed. So a body that is
    too large, runs past the deadline, or is cut off mid-read must not hide the status.
    """
    try:
        return _read_capped(stream, deadline)
    except (InvenTreeError, OSError, http.client.HTTPException):
        return b""


class InvenTreeClient:
    """Authenticated client for one InvenTree instance. Never prints or logs the token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: Transport | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not _valid_token(token):
            # Rejects header-injection characters. The message does not echo the value.
            raise InvenTreeError("invalid_token", "token must be a non-empty ASCII string with no whitespace")
        self._base = _validated_base(base_url)
        self._token = token
        self._transport: Transport = transport if transport is not None else urllib_transport
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"InvenTreeClient(base_url={self._base!r})"

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET a path and return its parsed JSON."""
        raw = self._send("GET", self._url(path, params))
        return _decode_json(raw, "GET", path)

    def post(self, path: str, body: Any) -> Any:
        """POST a JSON body and return the parsed JSON response."""
        raw = self._send("POST", self._url(path), _encode_body(body))
        return _decode_json(raw, "POST", path)

    def patch(self, path: str, body: Any) -> Any:
        """PATCH a JSON body and return the parsed JSON response."""
        raw = self._send("PATCH", self._url(path), _encode_body(body))
        return _decode_json(raw, "PATCH", path)

    def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        limit: int = 50,
    ) -> list[Any]:
        """Collect up to `limit` items across pages. Use paginate_with_flag to learn about truncation."""
        items, _truncated = self.paginate_with_flag(path, params, limit)
        return items

    def paginate_with_flag(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        limit: int = 50,
    ) -> tuple[list[Any], bool]:
        """Collect up to `limit` items across pages. Returns (items, truncated).

        `truncated` is True when more items existed than were returned. Raises bad_response
        when the server needs more page requests than _max_pages(limit) allows, when a page
        is empty but still names a next link, or when a next link loops.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query: dict[str, Any] = dict(params or {})
        # VERIFY-LIVE: confirm the pinned InvenTree list endpoints accept `limit` and
        # `offset` (DRF limit/offset pagination) against the instance's /api-doc/.
        query["limit"] = min(limit, MAX_PAGE_SIZE)
        max_pages = _max_pages(limit)
        next_url: str | None = self._url(path, query)
        items: list[Any] = []
        seen: set[str] = set()
        pages = 0
        while next_url is not None:
            if next_url in seen:
                raise InvenTreeError("bad_response", f"GET {path} pagination link loops")
            if pages >= max_pages:
                raise InvenTreeError("bad_response", f"GET {path} needed more than {max_pages} pages for limit {limit}")
            seen.add(next_url)
            pages += 1
            page = _decode_json(self._send("GET", next_url), "GET", path)
            results, link = _split_page(page, path)
            if not results and link is not None:
                # An empty page that keeps pointing onward would otherwise repeat forever.
                raise InvenTreeError("bad_response", f"GET {path} returned an empty page with a next link")
            items.extend(results)
            if len(items) > limit:
                del items[limit:]
                return items, True
            if len(items) == limit:
                return items, link is not None
            next_url = self._follow(link, next_url) if link is not None else None
        return items, False

    def check_version(self) -> dict[str, Any]:
        """GET /api/ and return the JSON object. Raises version_unknown if it is not one."""
        raw = self._send("GET", self._url("/api/"))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise InvenTreeError("version_unknown", "InvenTree /api/ did not return JSON") from None
        if not isinstance(data, dict):
            raise InvenTreeError("version_unknown", "InvenTree /api/ did not return a JSON object")
        return data

    def roles(self) -> list[Any]:
        """GET /api/user/roles/ and return the role list."""
        # VERIFY-LIVE: the plan assumes this endpoint returns a JSON list. Confirm the shape
        # against the pinned instance's /api-doc/ before the read path depends on it.
        data = self.get("/api/user/roles/")
        if not isinstance(data, list):
            raise InvenTreeError("bad_response", "GET /api/user/roles/ did not return a list (VERIFY-LIVE)")
        return data

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        # Check before adding a leading slash, so "https://..." and "//host/..." are caught.
        parts = urllib.parse.urlsplit(path)
        if parts.scheme or parts.netloc:
            raise ValueError("path must be relative to the InvenTree base URL")
        if not path.startswith("/"):
            path = "/" + path
        url = self._base + path
        query = _encode_query(params)
        if query:
            url += ("&" if "?" in url else "?") + query
        return url

    def _follow(self, link: str, current: str) -> str:
        url = urllib.parse.urljoin(current, link)
        origin = _origin(url)
        if origin is None:
            raise InvenTreeError("bad_response", "pagination link has an invalid port")
        if origin != _origin(self._base):
            raise InvenTreeError(
                "bad_response",
                "pagination link points outside the configured InvenTree instance",
            )
        return url

    def _send(self, method: str, url: str, body: bytes | None = None) -> bytes:
        headers = {"Authorization": f"Token {self._token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        path = urllib.parse.urlsplit(url).path
        started = time.monotonic()
        try:
            status, raw = self._transport(method, url, headers, body, self._timeout)
        except (OSError, http.client.HTTPException) as exc:
            raise InvenTreeError(
                "unreachable",
                f"could not reach InvenTree ({type(exc).__name__})",
            ) from None
        late = time.monotonic() - started > REQUEST_DEADLINE_SECONDS
        logger.debug("inventree %s %s -> %s", method, path, status)
        if 200 <= status <= 299:
            if late:
                # The default transport enforces this while it reads. This check covers injected transports.
                raise InvenTreeError(
                    "unreachable",
                    f"InvenTree did not answer {method} {path} within {REQUEST_DEADLINE_SECONDS:g} seconds",
                )
            if len(raw) > MAX_RESPONSE_BYTES:
                # Guards injected transports too. The default transport already stops reading at the cap.
                raw = b""
                raise _too_large()
            return raw
        # An error answer keeps its status even when it arrived late or its body was too large.
        # Callers classify by the status, and never use the body of an error answer.
        raise _status_error(status, method, path)


def _insecure_url(detail: str) -> InvenTreeError:
    return InvenTreeError("insecure_url", detail)


def _validated_base(base_url: str) -> str:
    cleaned = base_url.strip()
    parts = urllib.parse.urlsplit(cleaned)
    try:
        port = parts.port
    except ValueError:
        raise _insecure_url("base_url has an invalid port") from None
    if port == 0:
        # An explicit port 0 is invalid. It is not the scheme's default port.
        raise _insecure_url("base_url has an invalid port")
    host = (parts.hostname or "").lower()
    if not host:
        raise _insecure_url("base_url has no host")
    if parts.username is not None or parts.password is not None:
        raise _insecure_url("base_url must not embed credentials")
    scheme = parts.scheme.lower()
    if not (scheme == "https" or (scheme == "http" and host in _LOCAL_HOSTS)):
        raise _insecure_url("base_url must use https, or http for 127.0.0.1 or localhost")
    if "?" in cleaned or "#" in cleaned or parts.path not in ("", "/"):
        # Every URL is built from the origin, so a base with a path, query or fragment
        # would silently change where requests go. The message does not echo the value.
        raise InvenTreeError(
            "invalid_url",
            "base_url must be the instance origin only, with no path, query or fragment",
        )
    return cleaned.rstrip("/")


def _valid_token(token: object) -> bool:
    """True for a non-empty string of printable ASCII with no whitespace (0x21 to 0x7E)."""
    return isinstance(token, str) and bool(token) and all(0x21 <= ord(c) <= 0x7E for c in token)


def _max_pages(limit: int) -> int:
    """Page requests one paginate call may make: a full-size read plus a few spare pages."""
    return math.ceil(limit / MAX_PAGE_SIZE) + _PAGE_SLACK


def _too_large() -> InvenTreeError:
    return InvenTreeError("bad_response", f"InvenTree response is larger than {MAX_RESPONSE_BYTES} bytes")


def _read_capped(stream: Any, deadline: float | None = None) -> bytes:
    """Read a whole response body and refuse one over MAX_RESPONSE_BYTES.

    Reads in chunks and stops at the cap, so an oversized body is never held whole in memory.
    `deadline` is a time.monotonic() value. It is checked after every read, and a read past it
    raises TimeoutError. read1 returns as soon as any bytes arrive, so a slow trickle is seen
    at each byte. A read already waiting on the socket can still run until the per-socket timeout.
    """
    read = getattr(stream, "read1", None) or stream.read
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = read(_READ_CHUNK_BYTES)
        if deadline is not None and time.monotonic() > deadline:
            chunks.clear()
            raise TimeoutError("response was not complete within the request deadline")
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            chunks.clear()
            raise _too_large() from None
        chunks.append(chunk)


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """(scheme, host, port) of a URL, or None when its port is invalid (not a number, or 0)."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        return None
    if port == 0:
        # An explicit port 0 must not fall back to the scheme's default port.
        return None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return scheme, (parts.hostname or "").lower(), port


def _status_error(status: int, method: str, path: str) -> InvenTreeError:
    where = f"{method} {path}"
    if status in (401, 403):
        return InvenTreeError(
            "unauthorized",
            f"InvenTree refused {where} (HTTP {status}): check the token and its role",
            status,
        )
    if status == 404:
        return InvenTreeError("not_found", f"{where} was not found (HTTP 404)", status)
    if status == 400:
        return InvenTreeError("invalid", f"InvenTree rejected {where} as invalid (HTTP 400)", status)
    if 500 <= status <= 599:
        return InvenTreeError("server_error", f"InvenTree server error for {where} (HTTP {status})", status)
    return InvenTreeError("http_error", f"unexpected HTTP {status} for {where}", status)


def _decode_json(raw: bytes, method: str, path: str) -> Any:
    """Parse the body of a 2xx answer. An empty or non-JSON body is None, not an error.

    A body the parser cannot read because it is nested too deeply, or holds an integer past the
    interpreter's digit limit, is bad_response: it is JSON the client cannot read. Any other
    ValueError from the parser is refused the same way, so none escapes to the caller.
    Callers that need an object or a list check the shape themselves and refuse with bad_response.
    """
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    except RecursionError:
        raise InvenTreeError("bad_response", f"{method} {path} returned JSON nested too deeply") from None
    except ValueError:
        # Valid JSON the parser refuses, such as an integer past sys.get_int_max_str_digits().
        raise InvenTreeError("bad_response", f"{method} {path} returned JSON the client cannot parse") from None


def _split_page(page: Any, path: str) -> tuple[list[Any], str | None]:
    """Return (results, next_link) for a paginated object or a plain JSON list."""
    if isinstance(page, list):
        return page, None
    if isinstance(page, dict) and isinstance(page.get("results"), list):
        link = page.get("next")
        if link is not None and not isinstance(link, str):
            raise InvenTreeError("bad_response", f"GET {path} returned a non-string next link")
        if isinstance(link, str) and not link.strip():
            # An empty next link means there is no next page. It is not a link back to this one.
            link = None
        return page["results"], link
    raise InvenTreeError("bad_response", f"GET {path} did not return a list")


def _encode_body(body: Any) -> bytes:
    return json.dumps(body).encode("utf-8")


def _encode_query(params: Mapping[str, Any] | None) -> str:
    if not params:
        return ""
    pairs = [(key, _query_value(value)) for key, value in params.items() if value is not None]
    return urllib.parse.urlencode(pairs, doseq=True)


def _query_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
