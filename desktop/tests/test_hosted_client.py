"""Tests for meridian_desktop/hosted_client.py.

Most tests inject a fake transport, so nothing here opens a socket. The few that use the real urllib
transport call a loopback stub on 127.0.0.1, which is allowed. FAKE_TOKEN is an obviously fake value,
so a leak check can search every error for it. No test reads the keychain or the real network.
"""

from __future__ import annotations

import http.client
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from _fakes import QuietServer
from meridian_desktop import hosted_client
from meridian_desktop.hosted_client import (
    BAD_RESPONSE_TEXT,
    FORBIDDEN_TEXT,
    MAX_RESPONSE_BYTES,
    NOT_FOUND_TEXT,
    NOT_SIGNED_IN_TEXT,
    REJECTED_TEXT,
    TIMEOUT_TEXT,
    UNREACHABLE_TEXT,
    BadBaseUrl,
    BadToken,
    Forbidden,
    HostedError,
    HostedTimeout,
    HostedUnreachable,
    NotFound,
    NotSignedIn,
    Reply,
    list_projects,
    usable_token,
    validate_base_url,
)

REPO = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "sk_meridian_fake-hosted-client-0005"
HTTPS_BASE = "https://hosted.test"
PROJECT_ROWS = [
    {"id": "p-1", "name": "Alpha", "status": "active", "priority": "P1", "created_at": "2026-01-02 10:00:00"},
    {"id": "p-2", "name": "Beta", "status": "parked", "priority": "P2", "created_at": "2026-02-03 11:00:00"},
]
BODY_LEAK = "BODY-SECRET-DO-NOT-SHOW"


class FakeTransport:
    """A transport that returns one canned reply, or raises one canned error. It records each request."""

    def __init__(self, reply: Reply | None = None, error: BaseException | None = None) -> None:
        self.reply = reply if reply is not None else Reply(200, json.dumps(PROJECT_ROWS).encode("utf-8"))
        self.error = error
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Reply:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.reply


def _assert_no_token_in(*errors: BaseException) -> None:
    for error in errors:
        assert FAKE_TOKEN not in str(error)
        assert FAKE_TOKEN not in repr(error)


def test_hosted_client_sends_bearer_and_never_leaks_token():
    transport = FakeTransport()

    rows = list_projects(HTTPS_BASE, FAKE_TOKEN, transport=transport)

    assert rows == PROJECT_ROWS
    assert len(transport.requests) == 1, "one request, no retries"
    request = transport.requests[0]
    assert request.get_method() == "GET"
    assert request.full_url == "https://hosted.test/projects"
    assert request.get_header("Authorization") == f"Bearer {FAKE_TOKEN}"
    assert transport.timeouts == [10.0]


@pytest.mark.parametrize("token", [None, "", "   ", "\t\n"])
def test_empty_token_raises_not_signed_in_and_sends_no_request(token):
    transport = FakeTransport()

    with pytest.raises(NotSignedIn) as info:
        list_projects(HTTPS_BASE, token, transport=transport)

    assert str(info.value) == NOT_SIGNED_IN_TEXT
    assert transport.requests == []


def test_401_raises_not_signed_in_with_status_and_no_body_text():
    transport = FakeTransport(Reply(401, f'{{"detail": "invalid API token {BODY_LEAK}"}}'.encode("utf-8")))

    with pytest.raises(NotSignedIn) as info:
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=transport)

    error = info.value
    assert error.status == 401
    assert error.code == "not_signed_in"
    assert str(error) == REJECTED_TEXT
    assert BODY_LEAK not in str(error)
    _assert_no_token_in(error)


def test_403_and_404_raise_their_own_typed_errors():
    with pytest.raises(Forbidden) as forbidden:
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=FakeTransport(Reply(403, b"")))
    assert forbidden.value.status == 403
    assert str(forbidden.value) == FORBIDDEN_TEXT

    with pytest.raises(NotFound) as missing:
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=FakeTransport(Reply(404, b"")))
    assert missing.value.status == 404
    assert str(missing.value) == NOT_FOUND_TEXT
    assert all(isinstance(error, HostedError) for error in (forbidden.value, missing.value))


@pytest.mark.parametrize("status", [500, 503, 418])
def test_other_non_2xx_is_hosted_error_carrying_only_the_status_code(status):
    transport = FakeTransport(Reply(status, f"{BODY_LEAK} {FAKE_TOKEN}".encode("utf-8")))

    with pytest.raises(HostedError) as info:
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=transport)

    error = info.value
    assert type(error) is HostedError
    assert error.status == status
    assert str(error) == f"Hosted Meridian returned HTTP {status}."
    assert BODY_LEAK not in str(error)
    _assert_no_token_in(error)


def test_redirect_reply_is_an_error_and_is_not_followed():
    transport = FakeTransport(Reply(302, b""))

    with pytest.raises(HostedError) as info:
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=transport)

    assert info.value.code == "redirect_refused"
    assert info.value.status == 302
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b'{"id": 1}', b"[1, 2]", b'[{"id": 1}, "x"]', b"\xff\xfe"],
)
def test_2xx_body_that_is_not_a_list_of_objects_is_bad_response(body):
    with pytest.raises(HostedError) as info:
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=FakeTransport(Reply(200, body)))

    assert info.value.code == "bad_response"
    assert info.value.status == 200
    assert str(info.value) == BAD_RESPONSE_TEXT


def test_hosted_client_rejects_insecure_base_and_hides_token():
    refused = [
        "http://usemeridian.us",
        "ftp://usemeridian.us",
        "https://user:pw-not-real@hosted.test",
        "https://hosted.test/?next=1",
        "https://hosted.test/#frag",
        "http://127.0.0.1.example.com",
        "https://",
        "",
        "   ",
    ]
    for url in refused:
        transport = FakeTransport()
        with pytest.raises(ValueError) as info:  # BadBaseUrl is a ValueError
            list_projects(url, FAKE_TOKEN, transport=transport)
        assert isinstance(info.value, BadBaseUrl), url
        assert transport.requests == [], f"a request was made for {url!r}"
        _assert_no_token_in(info.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:7878", "http://localhost:9000", "http://[::1]:8000"])
def test_http_is_allowed_only_on_a_loopback_host(url):
    transport = FakeTransport()

    rows = list_projects(url, FAKE_TOKEN, transport=transport)

    assert rows == PROJECT_ROWS
    assert transport.requests[0].full_url.startswith(url + "/projects")


def test_validate_base_url_strips_space_and_trailing_slash():
    assert validate_base_url("  https://usemeridian.us/  ") == "https://usemeridian.us"
    assert validate_base_url("https://hosted.test/api/") == "https://hosted.test/api"


@pytest.mark.parametrize("value", ["sk_meridian_a b", "sk_meridian_x\ny", "sk_meridian_é"])
def test_token_with_space_control_or_non_ascii_is_refused_before_any_request(value):
    transport = FakeTransport()

    with pytest.raises(BadToken) as info:
        list_projects(HTTPS_BASE, value, transport=transport)

    assert transport.requests == []
    assert value not in str(info.value)
    assert value not in repr(info.value)


def test_usable_token_strips_surrounding_space():
    assert usable_token(f"  {FAKE_TOKEN}\r\n") == FAKE_TOKEN


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(f"refused while sending {FAKE_TOKEN}"),
        OSError(f"network down {FAKE_TOKEN}"),
        http.client.RemoteDisconnected(f"closed {FAKE_TOKEN}"),
        ValueError(f"bad header {FAKE_TOKEN}"),
    ],
)
def test_network_failure_maps_to_unreachable_without_the_cause(error):
    transport = FakeTransport(error=error)

    with pytest.raises(HostedUnreachable) as info:
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=transport)

    assert info.value.code == "unreachable"
    assert str(info.value) == UNREACHABLE_TEXT
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    _assert_no_token_in(info.value)
    assert len(transport.requests) == 1


def test_network_failure_is_logged_with_its_class_and_host_but_never_the_token(caplog):
    caplog.set_level(logging.WARNING, logger="meridian_desktop.hosted_client")
    transport = FakeTransport(error=urllib.error.URLError(OSError(f"refused {FAKE_TOKEN}")))

    with pytest.raises(HostedUnreachable):
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=transport)

    host = urllib.parse.urlsplit(HTTPS_BASE).hostname
    assert "hosted request failed: unreachable" in caplog.text
    assert f"host={host}" in caplog.text
    assert "URLError(reason=OSError, errno=None)" in caplog.text
    assert FAKE_TOKEN not in caplog.text


def test_timeout_maps_to_hosted_timeout_and_is_not_retried():
    transport = FakeTransport(error=TimeoutError(f"deadline passed for {FAKE_TOKEN}"))

    with pytest.raises(HostedTimeout) as info:
        list_projects(HTTPS_BASE, FAKE_TOKEN, transport=transport)

    assert info.value.code == "timeout"
    assert str(info.value) == TIMEOUT_TEXT
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    _assert_no_token_in(info.value)
    assert len(transport.requests) == 1, "a timeout is not retried"


def test_every_typed_error_is_a_hosted_error():
    for cls in (NotSignedIn, Forbidden, NotFound, HostedTimeout, HostedUnreachable, BadToken, BadBaseUrl):
        assert issubclass(cls, HostedError)
    assert issubclass(BadBaseUrl, ValueError)


def test_bounded_size_limit_is_four_mebibytes():
    assert MAX_RESPONSE_BYTES == 4 * 1024 * 1024


# ----------------------------------------------------------- real urllib transport, loopback stub only


class _Stub:
    """Canned routes for the loopback stub. Each route is (status, headers, body, delay in seconds)."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], bytes, float]] = {}
        self.hits: list[tuple[str, str | None]] = []


@pytest.fixture
def stub():
    state = _Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            state.hits.append((self.path, self.headers.get("Authorization")))
            status, headers, body, delay = state.routes.get(self.path, (404, {}, b"", 0.0))
            if delay:
                time.sleep(delay)
            self.send_response(status)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep test output quiet
            pass

    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        state.base = f"http://127.0.0.1:{server.server_address[1]}"
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_default_transport_sends_the_bearer_header_to_a_loopback_stub(stub):
    stub.routes["/projects"] = (200, {"Content-Type": "application/json"}, json.dumps(PROJECT_ROWS).encode("utf-8"), 0.0)

    rows = list_projects(stub.base, FAKE_TOKEN)

    assert rows == PROJECT_ROWS
    assert stub.hits == [("/projects", f"Bearer {FAKE_TOKEN}")]


def test_default_transport_keeps_the_status_and_a_short_body_of_an_error_reply(stub):
    # The device flow reads the error name from a 400 body, so the transport keeps it. A body is bounded.
    body = json.dumps({"error": "access_denied"}).encode("utf-8")
    stub.routes["/oauth/token"] = (400, {"Content-Type": "application/json"}, body, 0.0)
    request = urllib.request.Request(stub.base + "/oauth/token")

    reply = hosted_client._urllib_transport(request, 5.0)

    assert reply.status == 400
    assert reply.body == body


def test_default_transport_bounds_the_body_of_an_error_reply(stub):
    stub.routes["/oauth/token"] = (400, {"Content-Type": "application/json"}, b"x" * (64 * 1024), 0.0)
    request = urllib.request.Request(stub.base + "/oauth/token")

    reply = hosted_client._urllib_transport(request, 5.0)

    assert reply.status == 400
    assert len(reply.body) == hosted_client.MAX_ERROR_BYTES


def test_default_transport_does_not_follow_a_redirect(stub):
    stub.routes["/projects"] = (302, {"Location": "/elsewhere"}, b"", 0.0)
    stub.routes["/elsewhere"] = (200, {}, b"[]", 0.0)

    with pytest.raises(HostedError) as info:
        list_projects(stub.base, FAKE_TOKEN)

    assert info.value.code == "redirect_refused"
    assert info.value.status == 302
    paths = [path for path, _auth in stub.hits]
    assert paths == ["/projects"], "the redirect target must not be requested"
    _assert_no_token_in(info.value)


def test_default_transport_times_out_at_the_given_limit(stub):
    stub.routes["/projects"] = (200, {}, b"[]", 1.0)

    with pytest.raises(HostedTimeout) as info:
        list_projects(stub.base, FAKE_TOKEN, timeout=0.2)

    assert str(info.value) == TIMEOUT_TEXT
    _assert_no_token_in(info.value)


def test_default_transport_refuses_a_body_declared_over_the_limit(stub):
    stub.routes["/projects"] = (200, {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b"", 0.0)

    with pytest.raises(HostedError) as info:
        list_projects(stub.base, FAKE_TOKEN)

    assert info.value.code == "too_large"
    _assert_no_token_in(info.value)


def test_hosted_client_module_imports_no_qt_and_no_keyring():
    code = (
        "import sys\n"
        "import meridian_desktop.hosted_client\n"
        "assert 'keyring' not in sys.modules, 'keyring imported at module import'\n"
        "assert not any(name.startswith('PySide6') for name in sys.modules), 'Qt imported'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
