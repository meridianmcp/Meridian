"""The Zotero bridge transport and reader (meridian/zotero_bridge_transport.py).

Every test injects a fake opener, so nothing reaches a socket. The token file lives in a temporary state folder. The
tests never print the token. They compare it only to values read back from the same file.
"""

from __future__ import annotations

import io
import json
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from meridian import zotero_bridge_api as bridge_api
from meridian import zotero_bridge_transport as transport_mod
from meridian.zotero_bridge_client import ZoteroBridgeClient


class FakeResponse:
    """What ``opener.open`` returns for a 2xx reply: a context manager with a status and a body."""

    def __init__(self, status: int, payload: bytes = b""):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]


class FakeOpener:
    """Stands in for an OpenerDirector. Records each request and answers from a script, in order."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[urllib.request.Request, float | None]] = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def http_error(code: int, payload: bytes) -> urllib.error.HTTPError:
    """A refusal the way urllib raises it: an HTTPError whose body is the bridge's JSON."""
    return urllib.error.HTTPError("http://127.0.0.1:23125/", code, "refused", Message(), io.BytesIO(payload))


def _headers(request: urllib.request.Request) -> dict[str, str]:
    return {name.lower(): value for name, value in request.header_items()}


@pytest.fixture
def token_path(monkeypatch, tmp_path):
    """A usable token file in an isolated state folder. Returns its path."""
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "state"))
    path = bridge_api.token_file_path()
    bridge_api.ensure_api_token(path)
    return path


def _token(path) -> str:
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# The write transport: what it sends
# ---------------------------------------------------------------------------


def test_write_transport_sends_loopback_host_bearer_token_and_the_grant_header(token_path):
    grant = secrets.token_hex(32)
    opener = FakeOpener(FakeResponse(200, b'{"key": "ABCD1234"}'))
    transport = transport_mod.make_write_transport(opener=opener)

    status, reply = transport(
        "POST",
        "/v1/collections",
        {"Meridian-Write-Grant": grant, "Content-Type": "application/json"},
        {"name": "Reading list"},
    )

    request, timeout = opener.calls[0]
    assert request.full_url == "http://127.0.0.1:23125/v1/collections"
    assert request.get_method() == "POST"
    headers = _headers(request)
    assert headers["host"] == "127.0.0.1:23125"
    assert headers["authorization"] == f"Bearer {_token(token_path)}"
    assert headers["meridian-write-grant"] == grant
    assert json.loads(request.data) == {"name": "Reading list"}
    assert timeout == transport_mod.WRITE_TIMEOUT_SECONDS
    assert (status, reply) == (200, {"key": "ABCD1234"})


def test_the_client_cannot_override_the_authorization_header(token_path):
    opener = FakeOpener(FakeResponse(200, b"{}"))
    transport = transport_mod.make_write_transport(opener=opener)

    transport("GET", "/v1/grants/x", {"Authorization": "Bearer attacker"}, None)

    headers = _headers(opener.calls[0][0])
    assert headers["authorization"] == f"Bearer {_token(token_path)}"


def test_the_token_is_read_from_the_file_on_every_call(token_path):
    opener = FakeOpener(FakeResponse(200, b"{}"), FakeResponse(200, b"{}"))
    transport = transport_mod.make_write_transport(opener=opener)
    transport("GET", "/v1/grants/x", {}, None)

    replacement = bridge_api.TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_path.write_text(replacement + "\n", encoding="utf-8")
    transport("GET", "/v1/grants/x", {}, None)

    assert _headers(opener.calls[0][0])["authorization"] != _headers(opener.calls[1][0])["authorization"]
    assert _headers(opener.calls[1][0])["authorization"] == f"Bearer {replacement}"


def test_a_refusal_from_the_bridge_is_returned_as_the_reply_not_raised(token_path):
    body = b'{"error": {"code": "grant_required", "message": "A grant is needed."}}'
    opener = FakeOpener(http_error(403, body))
    transport = transport_mod.make_write_transport(opener=opener)

    status, reply = transport("POST", "/v1/collections", {}, {"name": "X"})

    assert status == 403
    assert reply["error"]["code"] == "grant_required"


def test_a_reply_that_is_not_json_comes_back_as_none(token_path):
    opener = FakeOpener(FakeResponse(200, b"<html>not json</html>"))
    transport = transport_mod.make_write_transport(opener=opener)

    assert transport("GET", "/v1/grants/x", {}, None) == (200, None)


def test_a_missing_token_file_is_a_fixed_error_and_nothing_is_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "empty"))
    opener = FakeOpener()
    transport = transport_mod.make_write_transport(opener=opener)

    with pytest.raises(transport_mod.BridgeTransportError, match="token file is missing or unusable"):
        transport("GET", "/v1/grants/x", {}, None)
    assert opener.calls == []


# ---------------------------------------------------------------------------
# Loopback only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:23125/v1/items",
        "http://127.0.0.1.example.com:23125/v1/items",
        "http://192.168.1.10:23125/v1/items",
        "http://[::1]:23125/v1/items",
        "http://127.0.0.1:9999/v1/items",
        "https://127.0.0.1:23125/v1/items",
        "http://127.0.0.1@evil.example:23125/v1/items",
    ],
)
def test_a_url_that_is_not_loopback_on_the_expected_port_is_refused_before_sending(url):
    opener = FakeOpener()

    with pytest.raises(transport_mod.BridgeTransportError):
        transport_mod._send(opener, "GET", url, 23125, {}, None, 1.0)
    assert opener.calls == []


@pytest.mark.parametrize("path", ["@evil.example/v1/items", "v1/items?x=1@evil.example"])
def test_a_path_cannot_move_the_write_request_to_another_host(token_path, path):
    opener = FakeOpener()
    transport = transport_mod.make_write_transport(opener=opener)

    with pytest.raises(transport_mod.BridgeTransportError):
        transport("POST", path, {}, {"name": "X"})
    assert opener.calls == []


def test_the_reader_refuses_a_path_that_moves_it_off_loopback():
    opener = FakeOpener()
    reader = transport_mod.make_reader(opener=opener)

    with pytest.raises(transport_mod.BridgeTransportError):
        reader("@evil.example/api/users/0/items")
    assert opener.calls == []


# ---------------------------------------------------------------------------
# The reader: GET only, no token
# ---------------------------------------------------------------------------


def test_the_reader_is_get_only_on_loopback_port_23119_and_sends_no_token(token_path):
    opener = FakeOpener(FakeResponse(200, b"[]"))
    reader = transport_mod.make_reader(opener=opener)

    status, body = reader("/api/users/0/items/ABCD1234/children?format=json&limit=100&start=0")

    request, _ = opener.calls[0]
    assert request.get_method() == "GET"
    assert request.full_url == (
        "http://127.0.0.1:23119/api/users/0/items/ABCD1234/children?format=json&limit=100&start=0"
    )
    headers = _headers(request)
    assert headers["host"] == "127.0.0.1:23119"
    assert "authorization" not in headers
    assert request.data is None
    assert (status, body) == (200, [])


def test_a_reader_failure_is_a_fixed_error_that_never_carries_the_exception_text(token_path):
    opener = FakeOpener(OSError("connection refused on 127.0.0.1:23119"))
    reader = transport_mod.make_reader(opener=opener)

    with pytest.raises(transport_mod.BridgeTransportError) as info:
        reader("/api/users/0/items/ABCD1234/children")

    assert str(info.value) == "The local Zotero endpoint did not answer."
    assert info.value.__context__ is None
    assert info.value.__cause__ is None


# ---------------------------------------------------------------------------
# Secrets never reach an exception
# ---------------------------------------------------------------------------


def test_exception_text_that_holds_the_token_never_reaches_the_caller(token_path):
    token = _token(token_path)
    opener = FakeOpener(ValueError(f"Invalid header value 'Bearer {token}'"))
    transport = transport_mod.make_write_transport(opener=opener)

    with pytest.raises(transport_mod.BridgeTransportError) as info:
        transport("GET", "/v1/grants/x", {}, None)

    assert token not in str(info.value)
    assert token not in repr(info.value)
    assert info.value.__context__ is None
    assert info.value.__cause__ is None


def test_the_client_built_on_the_real_transport_keeps_the_token_out_of_its_errors(token_path):
    token = _token(token_path)
    opener = FakeOpener(OSError(f"socket error while sending Bearer {token}"))
    client = ZoteroBridgeClient(
        transport=transport_mod.make_write_transport(opener=opener),
        reader=transport_mod.make_reader(opener=FakeOpener()),
    )

    with pytest.raises(Exception) as info:
        client.create_collection("Reading list")

    assert info.value.code == "transport_failed"
    assert token not in str(info.value)


# ---------------------------------------------------------------------------
# The whole write path, through the real transport and the real client
# ---------------------------------------------------------------------------


def test_a_grant_then_a_write_runs_through_the_real_transport_and_client(token_path):
    grant_id = "0" * 32
    write_key = secrets.token_hex(32)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    opener = FakeOpener(
        FakeResponse(200, json.dumps({"grant_id": grant_id}).encode()),
        FakeResponse(200, json.dumps({"status": "granted", "write_key": write_key, "expires_at": expires}).encode()),
        FakeResponse(201, b'{"key": "ABCD1234"}'),
    )
    client = ZoteroBridgeClient(
        transport=transport_mod.make_write_transport(opener=opener),
        reader=transport_mod.make_reader(opener=FakeOpener()),
        sleep=lambda seconds: None,
    )

    key = client.create_collection("Reading list")

    assert key == "ABCD1234"
    assert [request.get_method() for request, _ in opener.calls] == ["POST", "GET", "POST"]
    assert _headers(opener.calls[2][0])["meridian-write-grant"] == write_key


# ---------------------------------------------------------------------------
# Opener policy and the process-wide client
# ---------------------------------------------------------------------------


def test_the_default_opener_follows_no_redirects():
    opener = transport_mod.default_opener()

    assert not any(type(handler) is urllib.request.HTTPRedirectHandler for handler in opener.handlers)
    redirect = next(handler for handler in opener.handlers if isinstance(handler, transport_mod._NoRedirect))
    assert redirect.redirect_request(None, None, 302, "Found", {}, "http://evil.example/") is None


def test_the_default_opener_ignores_proxy_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    # The stock opener would pick the environment proxy up, so the check is meaningful.
    stock = urllib.request.build_opener()
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in stock.handlers)

    opener = transport_mod.default_opener()

    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in opener.handlers)


def test_the_process_wide_client_is_built_once_and_reset_drops_it():
    transport_mod.reset_client_for_tests()
    try:
        first = transport_mod.get_client()
        assert isinstance(first, ZoteroBridgeClient)
        assert transport_mod.get_client() is first

        transport_mod.reset_client_for_tests()
        assert transport_mod.get_client() is not first
    finally:
        transport_mod.reset_client_for_tests()
