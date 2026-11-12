"""Tests for meridian_inventree.client. No test touches the network."""

from __future__ import annotations

import email.message
import http.client
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from meridian_inventree import client as client_module
from meridian_inventree.client import DEFAULT_TIMEOUT_SECONDS, InvenTreeClient, InvenTreeError

BASE = "https://inventree.example.com"
TOKEN = "tok_9f8e7d6c5b4a3210"


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float


Responder = Callable[[Call], tuple[int, bytes]]


class FakeTransport:
    """Records every request and answers from a responder."""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.calls: list[Call] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        call = Call(method, url, dict(headers), body, timeout)
        self.calls.append(call)
        return self._responder(call)


def reply(status: int, payload: Any) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


def constant(status: int, body: bytes = b"{}") -> FakeTransport:
    return FakeTransport(lambda _call: (status, body))


def paged_responder(total: int, server_cap: int | None = None) -> Responder:
    """Serve `total` rows with limit/offset paging, optionally capping the page size."""

    def respond(call: Call) -> tuple[int, bytes]:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call.url).query)
        requested = int(query.get("limit", ["25"])[0])
        offset = int(query.get("offset", ["0"])[0])
        page = requested if server_cap is None else min(requested, server_cap)
        end = min(total, offset + page)
        rows = [{"pk": index} for index in range(offset + 1, end + 1)]
        link = f"{BASE}/api/part/?limit={page}&offset={end}" if end < total else None
        return reply(200, {"count": total, "next": link, "previous": None, "results": rows})

    return respond


def _raise_refused(_call: Call) -> tuple[int, bytes]:
    raise OSError(f"dial failed for token {TOKEN}")


def _raise_timeout(_call: Call) -> tuple[int, bytes]:
    raise TimeoutError("timed out")


# ---------------------------------------------------------------- auth and headers


def test_auth_header_sent_on_every_request_and_never_in_url() -> None:
    transport = FakeTransport(lambda _call: reply(200, {"ok": True}))
    client = InvenTreeClient(BASE, TOKEN, transport=transport)

    client.get("/api/part/", params={"search": "resistor"})
    client.post("/api/part/", {"example": 1})
    client.patch("/api/part/7/", {"example": 2})

    get_call, post_call, patch_call = transport.calls
    for call in transport.calls:
        assert call.headers["Authorization"] == f"Token {TOKEN}"
        assert call.headers["Accept"] == "application/json"
        assert TOKEN not in call.url
    assert "Content-Type" not in get_call.headers
    assert get_call.body is None
    assert get_call.url == f"{BASE}/api/part/?search=resistor"
    assert post_call.method == "POST"
    assert patch_call.method == "PATCH"
    assert post_call.headers["Content-Type"] == "application/json"
    assert patch_call.headers["Content-Type"] == "application/json"
    assert json.loads(post_call.body or b"") == {"example": 1}
    assert json.loads(patch_call.body or b"") == {"example": 2}


def test_token_never_appears_in_errors_or_repr() -> None:
    responders: dict[str, Responder] = {
        "401": lambda _c: (401, f"denied for {TOKEN}".encode()),
        "403": lambda _c: (403, b"forbidden"),
        "404": lambda _c: (404, b"missing"),
        "400": lambda _c: (400, b"bad input"),
        "500-echoes-token": lambda _c: (500, f"trace with {TOKEN}".encode()),
        "418": lambda _c: (418, b"teapot"),
        "network-with-token": _raise_refused,
        "timeout": _raise_timeout,
    }
    for name, responder in responders.items():
        client = InvenTreeClient(BASE, TOKEN, transport=FakeTransport(responder))
        with pytest.raises(InvenTreeError) as info:
            client.get("/api/part/")
        err = info.value
        for text in (str(err), err.message, repr(err), repr(client)):
            assert TOKEN not in text, f"token leaked in case {name}"
    # A non-JSON 2xx body no longer raises from get() (see the empty-body test below), so the
    # leak check for that body shape runs on check_version(), which still refuses it.
    client = InvenTreeClient(BASE, TOKEN, transport=constant(200, f"<html>login {TOKEN}</html>".encode()))
    with pytest.raises(InvenTreeError) as non_json:
        client.check_version()
    for text in (str(non_json.value), non_json.value.message, repr(non_json.value), repr(client)):
        assert TOKEN not in text, "token leaked in case non-json-200"


@pytest.mark.parametrize(
    "token",
    ["", "   ", "tok en", "tok\nX-Injected: 1", "tok\ten", "tok\x7fen", "tokén", "tok\x00en"],
)
def test_malformed_token_rejected_without_echoing_it(token: str) -> None:
    transport = constant(200)
    with pytest.raises(InvenTreeError) as info:
        InvenTreeClient(BASE, token, transport=transport)
    err = info.value
    assert err.code == "invalid_token"
    for text in (str(err), repr(err), err.message):
        assert "X-Injected" not in text
        if token:
            assert token not in text
    assert transport.calls == []
    # Printable ASCII punctuation with no whitespace is still a valid token.
    InvenTreeClient(BASE, "abc!#$%&'()*+,-./:;<=>?@[]^_`{|}~XYZ", transport=transport)


# ---------------------------------------------------------------- base URL policy


@pytest.mark.parametrize(
    "base_url",
    [
        "http://inventree.example.com",
        "http://10.0.0.5:8000",
        "http://127.0.0.2:8000",
        "http://[::1]:8000",
        "ftp://inventree.example.com",
        "inventree.example.com",
        "https:///no-host",
        "https://user:hunter2@inventree.example.com",
    ],
)
def test_insecure_base_url_refused_before_any_request(base_url: str) -> None:
    transport = constant(200)
    with pytest.raises(InvenTreeError) as info:
        InvenTreeClient(base_url, TOKEN, transport=transport)
    assert info.value.code == "insecure_url"
    assert "hunter2" not in info.value.message
    assert transport.calls == []


@pytest.mark.parametrize(
    ("base_url", "expected_prefix"),
    [
        ("https://inventree.example.com/", "https://inventree.example.com"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://localhost:8000", "http://localhost:8000"),
    ],
)
def test_allowed_base_urls_build_requests(base_url: str, expected_prefix: str) -> None:
    transport = constant(200)
    client = InvenTreeClient(base_url, TOKEN, transport=transport)
    client.get("/api/")
    assert transport.calls[0].url == f"{expected_prefix}/api/"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://inventree.example.com/inventree",
        "https://inventree.example.com/api/",
        "https://inventree.example.com//",
        "https://inventree.example.com/?debug=1",
        "https://inventree.example.com?",
        "https://inventree.example.com/#part",
        "https://inventree.example.com#",
        "http://localhost:8000/inventree",
    ],
)
def test_base_url_with_path_query_or_fragment_is_invalid_url(base_url: str) -> None:
    transport = constant(200)
    with pytest.raises(InvenTreeError) as info:
        InvenTreeClient(base_url, TOKEN, transport=transport)
    assert info.value.code == "invalid_url"
    assert transport.calls == []


@pytest.mark.parametrize("path", ["https://evil.example.net/api/part/", "//evil.example.net/api/part/"])
def test_absolute_or_protocol_relative_paths_rejected(path: str) -> None:
    transport = constant(200)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    with pytest.raises(ValueError):
        client.get(path)
    assert transport.calls == []


# ---------------------------------------------------------------- error mapping


def test_401_maps_to_unauthorized() -> None:
    client = InvenTreeClient(BASE, TOKEN, transport=constant(401))
    with pytest.raises(InvenTreeError) as info:
        client.get("/api/part/")
    assert info.value.code == "unauthorized"
    assert info.value.status == 401


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid"),
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not_found"),
        (500, "server_error"),
        (503, "server_error"),
        (418, "http_error"),
    ],
)
def test_status_codes_map_to_error_codes(status: int, code: str) -> None:
    client = InvenTreeClient(BASE, TOKEN, transport=constant(status))
    with pytest.raises(InvenTreeError) as info:
        client.get("/api/part/")
    assert info.value.code == code
    assert info.value.status == status


def test_every_error_answer_carries_its_numeric_status(
    opener: _RecordingOpener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The write path classifies 502, 503 and 504 by their numeric status, so every HTTP error
    # answer must carry it, however large or slow its body is. The body is never used.
    client = InvenTreeClient(BASE, TOKEN, transport=constant(504, b"gateway timeout"))
    with pytest.raises(InvenTreeError) as plain:
        client.get("/api/part/")
    assert plain.value.code == "server_error"
    assert plain.value.status == 504

    cap = client_module.MAX_RESPONSE_BYTES
    # An injected transport whose error body is over the cap.
    oversized = InvenTreeClient(BASE, TOKEN, transport=constant(503, bytes(cap + 1)))
    with pytest.raises(InvenTreeError) as big:
        oversized.get("/api/part/")
    assert big.value.code == "server_error"
    assert big.value.status == 503

    # The default transport, whose error body is over the cap.
    opener.outcome = urllib.error.HTTPError(
        f"{BASE}/api/part/",
        502,
        "Bad Gateway",
        email.message.Message(),
        io.BytesIO(bytes(cap + 1)),
    )
    with pytest.raises(InvenTreeError) as default:
        InvenTreeClient(BASE, TOKEN).get("/api/part/")
    assert default.value.code == "server_error"
    assert default.value.status == 502

    # An error answer that arrives after the request deadline keeps its status too.
    clock = _FakeClock()
    monkeypatch.setattr(client_module, "time", clock)

    def late(_call: Call) -> tuple[int, bytes]:
        clock.now += client_module.REQUEST_DEADLINE_SECONDS + 1
        return reply(504, {"detail": "timeout"})

    with pytest.raises(InvenTreeError) as slow:
        InvenTreeClient(BASE, TOKEN, transport=FakeTransport(late)).get("/api/part/")
    assert slow.value.code == "server_error"
    assert slow.value.status == 504


@pytest.mark.parametrize(
    "failure",
    [OSError("connection refused"), TimeoutError("timed out"), http.client.BadStatusLine("junk")],
)
def test_network_failures_map_to_unreachable(failure: BaseException) -> None:
    def respond(_call: Call) -> tuple[int, bytes]:
        raise failure

    client = InvenTreeClient(BASE, TOKEN, transport=FakeTransport(respond))
    with pytest.raises(InvenTreeError) as info:
        client.get("/api/")
    assert info.value.code == "unreachable"
    assert info.value.status is None


@pytest.mark.parametrize("status", [200, 204])
@pytest.mark.parametrize("body", [b"", b"   \n", b"not json", b"<html>login</html>", b"\xff\xfe\x00"])
def test_empty_or_non_json_success_body_returns_none(status: int, body: bytes) -> None:
    # A 2xx answer with no JSON body (for example a 204 to a write) is a result, not a fault.
    transport = constant(status, body)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    assert client.get("/api/part/") is None
    assert client.post("/api/part/", {"example": 1}) is None
    assert client.patch("/api/part/7/", {"example": 2}) is None
    # Callers that need an object or a list still refuse None with bad_response.
    with pytest.raises(InvenTreeError) as paged:
        client.paginate("/api/part/")
    assert paged.value.code == "bad_response"
    with pytest.raises(InvenTreeError) as roles:
        client.roles()
    assert roles.value.code == "bad_response"


def test_deeply_nested_json_body_is_bad_response() -> None:
    # Valid JSON that is nested past the parser's recursion limit. It is not "not JSON", so it
    # is not None: the client cannot read it and refuses it as bad_response.
    deep = b"[" * 200_000 + b"]" * 200_000
    client = InvenTreeClient(BASE, TOKEN, transport=constant(200, deep))
    with pytest.raises(InvenTreeError) as info:
        client.get("/api/part/")
    assert info.value.code == "bad_response"
    assert info.value.status is None
    assert "nested too deeply" in info.value.message
    # check_version() keeps its own contract: anything that is not a usable object is version_unknown.
    with pytest.raises(InvenTreeError) as version:
        client.check_version()
    assert version.value.code == "version_unknown"


def test_integer_past_the_digit_limit_is_bad_response() -> None:
    # Python 3.12 caps integer parsing at 4300 digits and raises a plain ValueError past that
    # limit. The body is valid JSON the client cannot read, so it must be bad_response: it
    # is not "not JSON" (None), and no plain ValueError may escape from get, post or paginate.
    huge = b"1" * 5000
    client = InvenTreeClient(BASE, TOKEN, transport=constant(200, huge))
    with pytest.raises(InvenTreeError) as info:
        client.get("/api/part/")
    assert info.value.code == "bad_response"
    assert info.value.status is None
    assert "1111" not in info.value.message
    with pytest.raises(InvenTreeError) as posted:
        client.post("/api/part/", {"example": 1})
    assert posted.value.code == "bad_response"
    wrapped = InvenTreeClient(BASE, TOKEN, transport=constant(200, b"[" + huge + b"]"))
    with pytest.raises(InvenTreeError) as paged:
        wrapped.paginate("/api/part/")
    assert paged.value.code == "bad_response"


class _FakeClock:
    """Stands in for the time module's monotonic clock so a test can move time forward."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


class _TrickleStream:
    """Sends one byte per read and advances the fake clock by `gap` seconds each time."""

    def __init__(self, clock: _FakeClock, gap: float) -> None:
        self._clock = clock
        self._gap = gap
        self.reads = 0

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        assert self.reads < 500, "the client never ended the trickle"
        self._clock.now += self._gap
        return b"x"


def test_total_request_deadline_ends_a_slow_trickle_as_unreachable(
    opener: _RecordingOpener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(client_module, "time", clock)
    # Ten seconds between bytes stays under the 15 second per-socket timeout, so only the
    # 60 second request deadline can end this answer.
    trickle = _TrickleStream(clock, gap=10.0)
    opener.outcome = _FakeResponse(200, trickle)
    with pytest.raises(InvenTreeError) as info:
        InvenTreeClient(BASE, TOKEN).get("/api/part/")
    assert info.value.code == "unreachable"
    assert info.value.status is None
    assert TOKEN not in info.value.message
    # The deadline is 60 seconds after the send at 1000: bytes arrive at 1010 to 1060, and the read at 1070 ends it.
    assert trickle.reads == 7

    # An injected transport whose answer arrives after the deadline is refused too.
    def slow(_call: Call) -> tuple[int, bytes]:
        clock.now += client_module.REQUEST_DEADLINE_SECONDS + 1
        return reply(200, {"ok": True})

    with pytest.raises(InvenTreeError) as late:
        InvenTreeClient(BASE, TOKEN, transport=FakeTransport(slow)).get("/api/")
    assert late.value.code == "unreachable"
    assert client_module.REQUEST_DEADLINE_SECONDS == 60.0


def test_timeout_defaults_to_fifteen_seconds_and_is_passed_through() -> None:
    assert DEFAULT_TIMEOUT_SECONDS == 15.0
    transport = constant(200)
    InvenTreeClient(BASE, TOKEN, transport=transport).get("/api/")
    InvenTreeClient(BASE, TOKEN, transport=transport, timeout=2.5).get("/api/")
    assert transport.calls[0].timeout == 15.0
    assert transport.calls[1].timeout == 2.5


# ---------------------------------------------------------------- pagination


@pytest.mark.parametrize(
    ("total", "limit", "server_cap", "expected_len", "expected_truncated", "expected_calls"),
    [
        (5, 3, None, 3, True, 1),  # cut short: a next link exists past the limit
        (5, 10, None, 5, False, 1),  # everything fits in one page
        (3, 3, None, 3, False, 1),  # limit reached exactly, nothing left
        (5, 10, 2, 5, False, 3),  # server caps pages; client follows next links
        (5, 2, None, 2, True, 1),  # exact page with more behind it
        (5, 4, 2, 4, True, 2),  # limit reached mid-chain
    ],
)
def test_pagination_truncation_flag(
    total: int,
    limit: int,
    server_cap: int | None,
    expected_len: int,
    expected_truncated: bool,
    expected_calls: int,
) -> None:
    transport = FakeTransport(paged_responder(total, server_cap))
    client = InvenTreeClient(BASE, TOKEN, transport=transport)

    items, truncated = client.paginate_with_flag("/api/part/", limit=limit)

    assert [row["pk"] for row in items] == list(range(1, expected_len + 1))
    assert truncated is expected_truncated
    assert len(transport.calls) == expected_calls


def test_paginate_returns_items_without_flag() -> None:
    client = InvenTreeClient(BASE, TOKEN, transport=FakeTransport(paged_responder(5)))
    assert len(client.paginate("/api/part/", limit=3)) == 3


def test_plain_list_response_is_cut_to_limit_and_flagged() -> None:
    rows = [{"pk": index} for index in range(1, 6)]
    client = InvenTreeClient(BASE, TOKEN, transport=constant(200, json.dumps(rows).encode()))
    items, truncated = client.paginate_with_flag("/api/part/", limit=3)
    assert len(items) == 3
    assert truncated is True


def test_pagination_refuses_next_link_to_another_host() -> None:
    def respond(_call: Call) -> tuple[int, bytes]:
        return reply(
            200,
            {"count": 4, "next": "https://attacker.example.net/api/part/?offset=1", "results": [{"pk": 1}]},
        )

    transport = FakeTransport(respond)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    with pytest.raises(InvenTreeError) as info:
        client.paginate("/api/part/", limit=10)
    assert info.value.code == "bad_response"
    assert len(transport.calls) == 1


def test_pagination_detects_a_looping_next_link() -> None:
    def respond(_call: Call) -> tuple[int, bytes]:
        return reply(200, {"count": 2, "next": f"{BASE}/api/part/?limit=50", "results": [{"pk": 1}]})

    transport = FakeTransport(respond)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    with pytest.raises(InvenTreeError) as info:
        client.paginate("/api/part/", limit=50)
    assert info.value.code == "bad_response"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("limit", [0, -1])
def test_pagination_rejects_non_positive_limit(limit: int) -> None:
    client = InvenTreeClient(BASE, TOKEN, transport=constant(200))
    with pytest.raises(ValueError):
        client.paginate("/api/part/", limit=limit)


def _offset_of(call: Call) -> int:
    return int(urllib.parse.parse_qs(urllib.parse.urlsplit(call.url).query).get("offset", ["0"])[0])


@pytest.mark.parametrize(
    ("limit", "expected_calls"),
    [(10, 4), (250, 6)],  # ceil(limit / 100) + 3 page requests
)
def test_pagination_stops_at_the_page_cap(limit: int, expected_calls: int) -> None:
    def respond(call: Call) -> tuple[int, bytes]:
        # One new row per page and a fresh next link, so only the cap can end the walk.
        offset = _offset_of(call)
        link = f"{BASE}/api/part/?limit=1&offset={offset + 1}"
        return reply(200, {"count": 10_000, "next": link, "results": [{"pk": offset + 1}]})

    transport = FakeTransport(respond)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    with pytest.raises(InvenTreeError) as info:
        client.paginate("/api/part/", limit=limit)
    assert info.value.code == "bad_response"
    assert len(transport.calls) == expected_calls


@pytest.mark.parametrize(
    ("rows", "limit", "expected_truncated"),
    [
        ([1, 2], 10, False),  # partial page, empty next link: nothing more to fetch
        ([1, 2], 2, False),  # full page, but the empty next link means it is not truncated
        ([], 10, False),  # empty page with an empty next link is a complete, empty answer
    ],
)
def test_empty_next_link_is_no_next_link(rows: list[int], limit: int, expected_truncated: bool) -> None:
    def respond(_call: Call) -> tuple[int, bytes]:
        return reply(
            200,
            {"count": 5, "next": "", "previous": None, "results": [{"pk": pk} for pk in rows]},
        )

    transport = FakeTransport(respond)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    items, truncated = client.paginate_with_flag("/api/part/", limit=limit)
    assert [row["pk"] for row in items] == rows
    assert truncated is expected_truncated
    # The empty link is not followed, so it cannot be mistaken for a loop back to the same page.
    assert len(transport.calls) == 1


def test_empty_page_with_next_link_ends_as_bad_response() -> None:
    def respond(call: Call) -> tuple[int, bytes]:
        # Every page is empty but names a fresh next link, which would loop without the guard.
        link = f"{BASE}/api/part/?limit=50&offset={_offset_of(call) + 50}"
        return reply(200, {"count": 3, "next": link, "results": []})

    transport = FakeTransport(respond)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    with pytest.raises(InvenTreeError) as info:
        client.paginate("/api/part/", limit=50)
    assert info.value.code == "bad_response"
    assert len(transport.calls) == 1
    # An empty page with no next link is still a normal, complete answer.
    empty = InvenTreeClient(BASE, TOKEN, transport=constant(200, b'{"count": 0, "next": null, "results": []}'))
    assert empty.paginate_with_flag("/api/part/", limit=50) == ([], False)


# ---------------------------------------------------------------- version and roles


def test_check_version_returns_json_object() -> None:
    transport = FakeTransport(lambda _call: reply(200, {"apiVersion": 1}))
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    assert client.check_version() == {"apiVersion": 1}
    assert transport.calls[0].url == f"{BASE}/api/"


@pytest.mark.parametrize("body", [b"<html>login</html>", b"[1, 2]"])
def test_check_version_raises_version_unknown_when_not_an_object(body: bytes) -> None:
    client = InvenTreeClient(BASE, TOKEN, transport=constant(200, body))
    with pytest.raises(InvenTreeError) as info:
        client.check_version()
    assert info.value.code == "version_unknown"


def test_roles_returns_list_from_user_roles_endpoint() -> None:
    transport = FakeTransport(lambda _call: reply(200, [{"name": "fixture-role"}]))
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    assert client.roles() == [{"name": "fixture-role"}]
    assert transport.calls[0].url == f"{BASE}/api/user/roles/"


def test_roles_non_list_response_is_bad_response() -> None:
    # VERIFY-LIVE: the roles response shape is unconfirmed; this pins the current contract.
    client = InvenTreeClient(BASE, TOKEN, transport=FakeTransport(lambda _c: reply(200, {"roles": {}})))
    with pytest.raises(InvenTreeError) as info:
        client.roles()
    assert info.value.code == "bad_response"


@pytest.mark.parametrize("payload", [{"results": [], "next": 5}, {"results": "nope"}, "a string"])
def test_unexpected_page_shape_is_bad_response(payload: object) -> None:
    client = InvenTreeClient(BASE, TOKEN, transport=FakeTransport(lambda _c: reply(200, payload)))
    with pytest.raises(InvenTreeError) as info:
        client.paginate("/api/part/")
    assert info.value.code == "bad_response"


def test_pagination_link_with_invalid_port_is_refused() -> None:
    def respond(_call: Call) -> tuple[int, bytes]:
        return reply(
            200,
            {"count": 2, "next": "https://inventree.example.com:notaport/api/part/?offset=1", "results": [{"pk": 1}]},
        )

    transport = FakeTransport(respond)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    with pytest.raises(InvenTreeError) as info:
        client.paginate("/api/part/", limit=10)
    assert info.value.code == "bad_response"
    assert len(transport.calls) == 1


def test_base_url_with_invalid_port_is_insecure() -> None:
    with pytest.raises(InvenTreeError) as info:
        InvenTreeClient("https://inventree.example.com:notaport", TOKEN, transport=constant(200))
    assert info.value.code == "insecure_url"


def test_explicit_port_zero_is_refused_not_treated_as_default_port() -> None:
    # Port 0 is not the scheme's default port, so neither a base URL nor a next link may use it.
    for base_url in ("https://inventree.example.com:0", "http://localhost:0"):
        transport = constant(200)
        with pytest.raises(InvenTreeError) as info:
            InvenTreeClient(base_url, TOKEN, transport=transport)
        assert info.value.code == "insecure_url"
        assert transport.calls == []

    # A next link on the same host with port 0 is refused, not followed as port 443.
    def respond(_call: Call) -> tuple[int, bytes]:
        link = "https://inventree.example.com:0/api/part/?offset=1"
        return reply(200, {"count": 2, "next": link, "results": [{"pk": 1}]})

    transport = FakeTransport(respond)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    with pytest.raises(InvenTreeError) as link_info:
        client.paginate("/api/part/", limit=10)
    assert link_info.value.code == "bad_response"
    assert len(transport.calls) == 1


def test_relative_path_gets_leading_slash_and_booleans_encode_as_lowercase() -> None:
    transport = constant(200)
    client = InvenTreeClient(BASE, TOKEN, transport=transport)
    client.get("api/part/", params={"active": True, "category": None, "inactive": False})
    assert transport.calls[0].url == f"{BASE}/api/part/?active=true&inactive=false"


# ---------------------------------------------------------------- default transport


class _FakeResponse:
    """Answers like an HTTP response. `body` is bytes, or any object with a read(size) method."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._stream = io.BytesIO(body) if isinstance(body, bytes) else body

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _EndlessStream:
    """Serves bytes forever and counts them, so a test can see how far the client read."""

    def __init__(self) -> None:
        self.bytes_served = 0

    def read(self, size: int = -1) -> bytes:
        assert size > 0, "the client must read a response in bounded chunks"
        self.bytes_served += size
        return b"x" * size


class _RecordingOpener:
    """Stands in for urllib's opener. Nothing is sent anywhere."""

    def __init__(self) -> None:
        self.handlers: tuple[object, ...] = ()
        self.requests: list[tuple[urllib.request.Request, float | None]] = []
        self.outcome: Any = None

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch) -> _RecordingOpener:
    recorder = _RecordingOpener()

    def fake_build_opener(*handlers: object) -> _RecordingOpener:
        recorder.handlers = handlers
        return recorder

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    return recorder


def test_default_transport_sends_auth_and_refuses_redirects(opener: _RecordingOpener) -> None:
    opener.outcome = _FakeResponse(200, b'{"ok": true}')

    assert InvenTreeClient(BASE, TOKEN).get("/api/") == {"ok": True}

    request, timeout = opener.requests[0]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == f"Token {TOKEN}"
    assert headers["accept"] == "application/json"
    assert request.get_method() == "GET"
    assert request.data is None
    assert timeout == DEFAULT_TIMEOUT_SECONDS

    redirect_handlers = [h for h in opener.handlers if isinstance(h, client_module._NoRedirect)]
    assert len(redirect_handlers) == 1
    assert redirect_handlers[0].redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example.net/") is None


def test_default_transport_sends_json_body_on_post(opener: _RecordingOpener) -> None:
    opener.outcome = _FakeResponse(200, b"{}")
    InvenTreeClient(BASE, TOKEN).post("/api/part/", {"example": 1})
    request, _timeout = opener.requests[0]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert request.get_method() == "POST"
    assert headers["content-type"] == "application/json"
    assert json.loads(request.data or b"") == {"example": 1}


def test_default_transport_ignores_environment_and_system_proxies(
    opener: _RecordingOpener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://proxy.example.net:3128")
    opener.outcome = _FakeResponse(200, b"{}")

    InvenTreeClient(BASE, TOKEN).get("/api/")

    # An empty ProxyHandler is the only proxy handler, so no proxy is ever used.
    proxy_handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


def test_oversized_response_bodies_are_refused_without_being_kept(opener: _RecordingOpener) -> None:
    cap = client_module.MAX_RESPONSE_BYTES
    # The default transport must stop at the cap rather than read an endless body to the end.
    stream = _EndlessStream()
    opener.outcome = _FakeResponse(200, stream)
    with pytest.raises(InvenTreeError) as info:
        InvenTreeClient(BASE, TOKEN).get("/api/part/")
    assert info.value.code == "bad_response"
    assert stream.bytes_served <= cap + client_module._READ_CHUNK_BYTES
    assert TOKEN not in info.value.message

    # A body of exactly the cap is accepted in full; one byte more is refused.
    assert len(client_module._read_capped(io.BytesIO(bytes(cap)))) == cap
    with pytest.raises(InvenTreeError) as exact:
        client_module._read_capped(io.BytesIO(bytes(cap + 1)))
    assert exact.value.code == "bad_response"

    # An injected transport that returns an oversized body is refused the same way.
    with pytest.raises(InvenTreeError) as injected:
        InvenTreeClient(BASE, TOKEN, transport=constant(200, bytes(cap + 1))).get("/api/part/")
    assert injected.value.code == "bad_response"


def test_default_transport_maps_http_error_status(opener: _RecordingOpener) -> None:
    url = f"{BASE}/api/part/"
    opener.outcome = urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), io.BytesIO(b"{}"))
    with pytest.raises(InvenTreeError) as info:
        InvenTreeClient(BASE, TOKEN).get("/api/part/")
    assert info.value.code == "not_found"
    assert info.value.status == 404


def test_default_transport_maps_connection_failure_to_unreachable(opener: _RecordingOpener) -> None:
    opener.outcome = urllib.error.URLError("no route to host")
    with pytest.raises(InvenTreeError) as info:
        InvenTreeClient(BASE, TOKEN).get("/api/")
    assert info.value.code == "unreachable"
