"""Tests for meridian_desktop/local_client.py. A real loopback http.server stub answers the
requests. No test contacts a host other than 127.0.0.1, and no test reads a real key."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from _fakes import QuietServer
from meridian_desktop import local_client
from meridian_desktop.local_client import (
    HEALTH_ERROR,
    HEALTH_NOT_MERIDIAN,
    HEALTH_NOT_RUNNING,
    HEALTH_RUNNING,
    MAX_RESPONSE_BYTES,
    USER_AGENT,
    LocalClientError,
    assert_loopback,
    probe_health,
    runpod_check,
    runpod_list_pods,
)
from meridian_desktop.projects_reader import PROJECT_COLUMNS

FAKE_KEY = "test-key-not-real"
HEALTH_OK = {"status": "ok", "service": "meridian", "git_sha": "abc123", "version": "0.0.0-test"}


class Stub:
    """A loopback HTTP server. Each route is a callable that writes its own response."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server naming
                stub.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                    }
                )
                route = stub.routes.get(self.path)
                if route is None:
                    raw(404, b'{"detail": "Not Found"}')(self)
                else:
                    route(self)

            def log_message(self, *args):  # keep test output quiet
                return

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._stopped = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            self._server.shutdown()
            self._server.server_close()

    def paths(self) -> list[str]:
        return [request["path"] for request in self.requests]


def raw(status: int, body: bytes, headers: dict | None = None):
    """A route that writes ``body`` with the given status. Content-Length is set unless given."""

    def route(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        sent = {"Content-Type": "application/json"}
        sent.update(headers or {})
        if "Content-Length" not in sent:
            sent["Content-Length"] = str(len(body))
        for name, value in sent.items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)

    return route


def as_json(status: int, payload: object):
    return raw(status, json.dumps(payload).encode("utf-8"))


def trickle(total_bytes: int, gap: float):
    """A route that declares a 100000-byte body, then sends one byte every ``gap`` seconds.

    Each byte arrives well inside the per-read socket timeout, so only a total deadline
    stops a client that waits for the body to finish.
    """

    def route(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "100000")
        handler.end_headers()
        for _ in range(total_bytes):
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(gap)

    return route


@pytest.fixture
def stub():
    server = Stub()
    server.start()
    yield server
    server.stop()


def _closed_port_url() -> str:
    """A loopback URL on a port that nothing listens on right now."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


# ----------------------------------------------------------------- the loopback guard


ACCEPTED = [
    "http://127.0.0.1",
    "http://127.0.0.1:7878",
    "http://127.0.0.1:7878/",
    "http://localhost:8471",
    "http://LOCALHOST:7878",
    "http://[::1]:7878",
    "http://[::1]",
]

REFUSED = [
    "https://127.0.0.1:7878",
    "ftp://127.0.0.1:7878",
    "file:///etc/hosts",
    "127.0.0.1:7878",
    "http://user:test-key-not-real@127.0.0.1:7878",
    "http://user@127.0.0.1:7878",
    "http://127.0.0.1@evil.example:7878",
    "http://127.0.0.1.example.com:7878",
    "http://localhost.example.com:7878",
    "http://127.0.0.1.nip.io",
    "http://evil.example:7878",
    "http://127.0.0.2:7878",
    "http://0.0.0.0:7878",
    "http://10.0.0.5:7878",
    "http://[::ffff:127.0.0.1]:7878",
    "http://127.0.0.1:0",
    "http://127.0.0.1:70000",
    "http://127.0.0.1:7878?x=1",
    "http://127.0.0.1:7878#fragment",
    "http://[::1",
    "",
    None,
]


@pytest.mark.parametrize("url", ACCEPTED)
def test_guard_accepts_loopback_http(url):
    assert_loopback(url)


@pytest.mark.parametrize("url", REFUSED)
def test_guard_refuses_everything_else(url):
    with pytest.raises(LocalClientError) as info:
        assert_loopback(url)
    assert info.value.code == "not_loopback"
    # The refusal names the rule, never the URL it refused.
    assert "test-key-not-real" not in str(info.value)


@pytest.mark.parametrize("url", ["https://example.invalid", "http://evil.example"])
def test_probe_refuses_an_unsafe_url_without_sending(stub, url):
    assert probe_health(url) == HEALTH_ERROR
    assert stub.requests == []


def test_unsafe_url_is_refused_by_runpod_calls_without_sending(stub):
    with pytest.raises(LocalClientError) as info:
        runpod_check("http://127.0.0.1.example.com:7878")
    assert info.value.code == "not_loopback"
    with pytest.raises(LocalClientError):
        runpod_list_pods("http://user:pass@127.0.0.1:7878")
    assert stub.requests == []


# ----------------------------------------------------------------- probe_health


def test_probe_running_when_health_body_is_meridian(stub):
    stub.routes["/health"] = as_json(200, HEALTH_OK)
    assert probe_health(stub.url) == HEALTH_RUNNING


def test_probe_not_meridian_when_another_service_answers(stub):
    stub.routes["/health"] = as_json(200, {"status": "ok", "service": "other"})
    assert probe_health(stub.url) == HEALTH_NOT_MERIDIAN


def test_probe_not_meridian_on_a_404(stub):
    assert probe_health(stub.url) == HEALTH_NOT_MERIDIAN  # no /health route: the stub answers 404


def test_probe_not_meridian_on_non_json_html(stub):
    stub.routes["/health"] = raw(200, b"<html>hello</html>", {"Content-Type": "text/html"})
    assert probe_health(stub.url) == HEALTH_NOT_MERIDIAN


def test_probe_not_meridian_on_a_200_that_is_not_ok(stub):
    stub.routes["/health"] = as_json(200, {"status": "down", "service": "meridian"})
    assert probe_health(stub.url) == HEALTH_NOT_MERIDIAN


def test_probe_does_not_follow_a_redirect(stub):
    stub.routes["/health"] = raw(302, b"", {"Location": f"{stub.url}/elsewhere", "Content-Type": "text/plain"})
    stub.routes["/elsewhere"] = as_json(200, HEALTH_OK)
    assert probe_health(stub.url) == HEALTH_NOT_MERIDIAN
    assert "/elsewhere" not in stub.paths()


def test_probe_not_running_when_nothing_listens():
    assert probe_health(_closed_port_url(), timeout=2.0) == HEALTH_NOT_RUNNING


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (ConnectionRefusedError("refused"), "not_running"),
        (TimeoutError("connect timed out"), "not_running"),
        (OSError("no route"), "unreachable"),
    ],
)
def test_connection_failures_map_to_codes(reason, code):
    assert local_client._transport_error(reason).code == code


def test_probe_error_on_timeout(stub):
    def slow(handler):
        time.sleep(1.0)
        raw(200, json.dumps(HEALTH_OK).encode())(handler)

    stub.routes["/health"] = slow
    assert probe_health(stub.url, timeout=0.1) == HEALTH_ERROR


def test_probe_error_when_the_health_body_trickles(stub):
    # 30 bytes over about 3 seconds, against a 0.5 second deadline. The probe must not wait it out.
    stub.routes["/health"] = trickle(total_bytes=30, gap=0.1)
    started = time.monotonic()
    assert probe_health(stub.url, timeout=0.5) == HEALTH_ERROR
    assert time.monotonic() - started < 2.0


def test_probe_error_on_oversize_health_body(stub):
    stub.routes["/health"] = raw(200, b"x" * (MAX_RESPONSE_BYTES + 1))
    assert probe_health(stub.url) == HEALTH_ERROR


def test_probe_running_over_ipv6_loopback():
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.bind(("::1", 0))
        probe.close()
    except OSError:
        pytest.skip("IPv6 loopback is not available here")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(HEALTH_OK).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    server = IPv6Server(("::1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert probe_health(f"http://[::1]:{server.server_address[1]}") == HEALTH_RUNNING
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------- RunPod routes


def test_runpod_check_returns_the_parsed_body_on_200(stub):
    body = {"ok": True, "credential_source": "keyring", "pod_count": 3, "billing_available": True, "error": None}
    stub.routes["/compute/runpod/check"] = as_json(200, body)
    assert runpod_check(stub.url) == body


def test_runpod_check_returns_ok_false_as_data_on_200(stub):
    body = {
        "ok": False,
        "credential_source": None,
        "pod_count": 0,
        "billing_available": False,
        "error": {"code": "no_credential", "message": "No key found.", "source_kinds_tried": ["env"]},
    }
    stub.routes["/compute/runpod/check"] = as_json(200, body)
    assert runpod_check(stub.url) == body


def test_runpod_list_pods_returns_the_parsed_body_on_200(stub):
    body = {"ok": True, "credential_source": "env", "pods": [], "pod_count": 0, "has_more_pods": False, "error": None}
    stub.routes["/compute/runpod/pods"] = as_json(200, body)
    assert runpod_list_pods(stub.url) == body


def test_runpod_calls_use_the_expected_paths_only(stub):
    stub.routes["/compute/runpod/check"] = as_json(200, {"ok": True})
    stub.routes["/compute/runpod/pods"] = as_json(200, {"ok": True, "pods": []})
    runpod_check(stub.url)
    runpod_list_pods(stub.url)
    assert stub.paths() == ["/compute/runpod/check", "/compute/runpod/pods"]


def test_403_local_only_maps_to_its_code_and_hides_the_body(stub):
    body = {"ok": False, "error": {"code": "local_only", "message": "SENTINEL-BODY-TEXT"}}
    stub.routes["/compute/runpod/check"] = as_json(403, body)
    with pytest.raises(LocalClientError) as info:
        runpod_check(stub.url)
    assert info.value.code == "local_only"
    assert "SENTINEL-BODY-TEXT" not in str(info.value)


def test_403_without_a_json_code_uses_the_status(stub):
    stub.routes["/compute/runpod/pods"] = raw(403, b"<html>SENTINEL-BODY-TEXT</html>", {"Content-Type": "text/html"})
    with pytest.raises(LocalClientError) as info:
        runpod_list_pods(stub.url)
    assert info.value.code == "http_403"
    assert "SENTINEL-BODY-TEXT" not in str(info.value)


def test_unsafe_server_code_is_not_copied(stub):
    stub.routes["/compute/runpod/check"] = as_json(500, {"error": {"code": "Bad Code!"}})
    with pytest.raises(LocalClientError) as info:
        runpod_check(stub.url)
    assert info.value.code == "http_500"


def test_404_from_an_older_meridian_is_reported_by_status(stub):
    with pytest.raises(LocalClientError) as info:
        runpod_check(stub.url)  # the stub has no RunPod route, so it answers 404
    assert info.value.code == "http_404"


def test_runpod_redirect_is_refused_and_not_followed(stub):
    stub.routes["/compute/runpod/check"] = raw(302, b"", {"Location": f"{stub.url}/elsewhere", "Content-Type": "text/plain"})
    stub.routes["/elsewhere"] = as_json(200, {"ok": True})
    with pytest.raises(LocalClientError) as info:
        runpod_check(stub.url)
    assert info.value.code == "redirect_refused"
    assert "/elsewhere" not in stub.paths()


def test_non_json_200_is_bad_json(stub):
    stub.routes["/compute/runpod/check"] = raw(200, b"not json at all", {"Content-Type": "text/plain"})
    with pytest.raises(LocalClientError) as info:
        runpod_check(stub.url)
    assert info.value.code == "bad_json"


def test_json_array_200_is_bad_json(stub):
    stub.routes["/compute/runpod/check"] = as_json(200, [1, 2, 3])
    with pytest.raises(LocalClientError) as info:
        runpod_check(stub.url)
    assert info.value.code == "bad_json"


def test_runpod_timeout_is_reported(stub):
    def slow(handler):
        time.sleep(1.0)
        as_json(200, {"ok": True})(handler)

    stub.routes["/compute/runpod/pods"] = slow
    with pytest.raises(LocalClientError) as info:
        runpod_list_pods(stub.url, timeout=0.1)
    assert info.value.code == "timeout"


def test_runpod_timeout_when_the_body_trickles(stub):
    # 30 bytes over about 3 seconds, against a 0.5 second deadline. The call must stop at the deadline.
    stub.routes["/compute/runpod/pods"] = trickle(total_bytes=30, gap=0.1)
    started = time.monotonic()
    with pytest.raises(LocalClientError) as info:
        runpod_list_pods(stub.url, timeout=0.5)
    assert info.value.code == "timeout"
    assert time.monotonic() - started < 2.0


def test_runpod_not_running_is_reported():
    with pytest.raises(LocalClientError) as info:
        runpod_check(_closed_port_url(), timeout=2.0)
    assert info.value.code == "not_running"


# ----------------------------------------------------------------- GET /projects

PROJECT_ROW = {
    "id": "p-1",
    "name": "One",
    "creator_human_id": "human-1",
    "icon": "book",
    "status": "active",
    "priority": "P1",
    "parent_project_id": None,
    "created_at": "2026-05-01 10:00:00",
    "executor_config": "EXEC-SECRET-LOCAL",
    "agent_instructions": "AGENT-NOTE-LOCAL",
}


def test_projects_rows_are_keyed_by_the_listed_columns(stub):
    stub.routes["/projects"] = as_json(200, [PROJECT_ROW, {"id": "p-2", "name": "Two"}])
    rows = local_client.list_projects(stub.url)
    assert [list(row) for row in rows] == [list(PROJECT_COLUMNS), list(PROJECT_COLUMNS)]
    assert rows[0] == {key: PROJECT_ROW[key] for key in PROJECT_COLUMNS}
    # A column the server did not send reads as None, the same as the file reader.
    assert rows[1]["name"] == "Two"
    assert rows[1]["status"] is None
    assert "EXEC-SECRET" not in repr(rows)
    assert stub.paths() == ["/projects"]


def test_projects_empty_list_is_no_rows(stub):
    stub.routes["/projects"] = as_json(200, [])
    assert local_client.list_projects(stub.url) == []


@pytest.mark.parametrize("body", [b'{"projects": []}', b"[1, 2]", b'"just text"', b"not json at all"])
def test_projects_non_list_reply_is_refused(stub, body):
    stub.routes["/projects"] = raw(200, body)
    with pytest.raises(LocalClientError) as info:
        local_client.list_projects(stub.url)
    assert info.value.code == "bad_json"
    assert str(info.value) == local_client.BAD_JSON_MESSAGE


def test_projects_redirect_is_refused_and_not_followed(stub):
    stub.routes["/projects"] = raw(302, b"", {"Location": f"{stub.url}/elsewhere", "Content-Type": "text/plain"})
    stub.routes["/elsewhere"] = as_json(200, [PROJECT_ROW])
    with pytest.raises(LocalClientError) as info:
        local_client.list_projects(stub.url)
    assert info.value.code == "redirect_refused"
    assert "/elsewhere" not in stub.paths()


def test_projects_error_status_is_reported_by_its_server_code(stub):
    stub.routes["/projects"] = as_json(500, {"error": {"code": "db_locked", "message": "SENTINEL-BODY-TEXT"}})
    with pytest.raises(LocalClientError) as info:
        local_client.list_projects(stub.url)
    assert info.value.code == "db_locked"
    assert "SENTINEL-BODY-TEXT" not in str(info.value)


def test_projects_timeout_when_the_body_trickles(stub):
    # 30 bytes over about 3 seconds, against a 0.5 second deadline. The call must stop at the deadline.
    stub.routes["/projects"] = trickle(total_bytes=30, gap=0.1)
    started = time.monotonic()
    with pytest.raises(LocalClientError) as info:
        local_client.list_projects(stub.url, timeout=0.5)
    assert info.value.code == "timeout"
    assert time.monotonic() - started < 2.0


def test_projects_request_is_a_plain_get_with_a_ten_second_timeout():
    seen = []

    def transport(request, timeout):
        seen.append((request.get_method(), request.full_url, timeout))
        assert request.get_header("Authorization") is None
        assert request.get_header("User-agent") == USER_AGENT
        return local_client._Reply(200, b"[]")

    assert local_client.list_projects("http://127.0.0.1:7878", transport=transport) == []
    assert seen == [("GET", "http://127.0.0.1:7878/projects", 10.0)]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid",
        "http://evil.example:7878",
        "http://user:test-key-not-real@127.0.0.1:7878",
        "http://127.0.0.1:7878?x=1",
    ],
)
def test_projects_refuses_a_non_loopback_url_before_any_request(url):
    calls = []

    def transport(request, timeout):
        calls.append(request)
        return local_client._Reply(200, b"[]")

    with pytest.raises(LocalClientError) as info:
        local_client.list_projects(url, transport=transport)
    assert info.value.code == "not_loopback"
    assert calls == []
    assert "test-key-not-real" not in str(info.value)


# ----------------------------------------------------------------- size cap


def _padded_json(total_bytes: int) -> bytes:
    prefix = b'{"pad": "'
    suffix = b'"}'
    return prefix + b"x" * (total_bytes - len(prefix) - len(suffix)) + suffix


def test_body_exactly_at_the_cap_is_accepted(stub):
    stub.routes["/compute/runpod/check"] = raw(200, _padded_json(MAX_RESPONSE_BYTES))
    assert len(runpod_check(stub.url)["pad"]) == MAX_RESPONSE_BYTES - len(b'{"pad": ""}')


def test_declared_oversize_body_is_refused_before_reading(stub):
    stub.routes["/compute/runpod/check"] = raw(200, _padded_json(MAX_RESPONSE_BYTES + 1))
    with pytest.raises(LocalClientError) as info:
        runpod_check(stub.url)
    assert info.value.code == "too_large"


def test_undeclared_oversize_body_is_refused_after_the_cap(stub):
    def unbounded(handler):
        # HTTP/1.0 with no Content-Length: the body runs until the connection closes.
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        chunk = b"x" * 65536
        for _ in range((MAX_RESPONSE_BYTES // len(chunk)) + 2):
            handler.wfile.write(chunk)

    stub.routes["/compute/runpod/pods"] = unbounded
    with pytest.raises(LocalClientError) as info:
        runpod_list_pods(stub.url)
    assert info.value.code == "too_large"


def test_error_body_is_capped_too(stub):
    stub.routes["/compute/runpod/check"] = raw(500, b"e" * (MAX_RESPONSE_BYTES + 10), {"Content-Type": "text/plain"})
    with pytest.raises(LocalClientError) as info:
        runpod_check(stub.url)
    assert info.value.code == "too_large"


# ----------------------------------------------------------------- what leaves the process


def test_every_request_is_an_unauthenticated_get_with_fixed_headers(stub, monkeypatch):
    # Put a fake key in the environment. The client must never send it.
    monkeypatch.setenv("RUNPOD_API_KEY", FAKE_KEY)
    stub.routes["/health"] = as_json(200, HEALTH_OK)
    stub.routes["/compute/runpod/check"] = as_json(200, {"ok": True})
    stub.routes["/compute/runpod/pods"] = as_json(200, {"ok": True, "pods": []})

    probe_health(stub.url)
    runpod_check(stub.url)
    runpod_list_pods(stub.url)

    assert len(stub.requests) == 3
    for request in stub.requests:
        assert request["method"] == "GET"
        assert request["headers"]["user-agent"] == USER_AGENT
        assert "authorization" not in request["headers"]
        assert "cookie" not in request["headers"]
        assert "content-length" not in request["headers"]
        assert FAKE_KEY not in request["path"]
        assert all(FAKE_KEY not in value for value in request["headers"].values())


def test_user_agent_is_fixed_and_names_the_desktop():
    assert USER_AGENT.startswith("MeridianDesktop/")
    assert "local client" in USER_AGENT


def test_loopback_requests_bypass_a_proxy(stub, monkeypatch):
    # A proxy that cannot answer would break every request. The client ignores it.
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    stub.routes["/health"] = as_json(200, HEALTH_OK)
    assert probe_health(stub.url) == HEALTH_RUNNING


def test_module_exposes_the_documented_surface():
    for name in ("assert_loopback", "probe_health", "list_projects", "runpod_check", "runpod_list_pods", "LocalClientError"):
        assert hasattr(local_client, name)
