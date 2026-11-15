"""Tests for meridian_desktop/code_client.py. Fake transports answer most cases. Only the default-transport
tests open a socket, and only to a loopback stub or to a loopback port that nothing listens on. No test
contacts another host, reads a real key or touches the real user files."""

import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler

import pytest

from _fakes import QuietServer
from meridian_desktop import local_client
from meridian_desktop.code_client import (
    BAD_RESPONSE_MESSAGE,
    BLANK_QUERY_MESSAGE,
    DEFAULT_LIMIT,
    HTTP_STATUS_MESSAGE,
    MAX_LIMIT,
    MAX_PATH_CHARS,
    MAX_QUERY_CHARS,
    MAX_SNIPPET_CHARS,
    REDIRECT_MESSAGE,
    STATE_BLANK_QUERY,
    STATE_ERROR,
    STATE_NOT_INDEXED,
    STATE_NOT_RUNNING,
    STATE_OK,
    STATE_ROUTE_MISSING,
    UNCHECKED_MESSAGE,
    CodeHit,
    Reply,
    search,
)
from meridian_desktop.local_client import (
    NOT_RUNNING_MESSAGE,
    TIMEOUT_MESSAGE,
    TOO_LARGE_MESSAGE,
    UNREACHABLE_MESSAGE,
    LocalClientError,
)

BASE = "http://127.0.0.1:7878"
SNIPPET = "def search(base_url, query):"


class FakeTransport:
    """Records each call and the thread it ran on. Answers with ``reply``, or raises ``error``."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []
        self.threads = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        self.threads.append(threading.current_thread())
        if self.error is not None:
            raise self.error
        return self.reply


def answer(status, body=b""):
    return Reply(status, body)


def results(payload):
    return Reply(200, json.dumps(payload).encode("utf-8"))


def _closed_port_url() -> str:
    """A loopback URL on a port that nothing listens on right now."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


class Stub:
    """A loopback HTTP server that answers every GET the same way and records each request path."""

    def __init__(self, status, body=b"", headers=None):
        self.paths = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server naming
                stub.paths.append(self.path)
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep test output quiet
                return

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def serve():
    """Start loopback stubs for one test, and stop them afterwards."""
    started = []

    def start(status, body=b"", headers=None):
        stub = Stub(status, body, headers)
        stub.start()
        started.append(stub)
        return stub

    yield start
    for stub in started:
        stub.stop()


# ----------------------------------------------------------------- the answers


def test_hits_are_read_with_path_line_score_and_snippet():
    fake = FakeTransport(results({"results": [{"path": "meridian_desktop/code_client.py", "line": 12, "score": 0.9123, "snippet": SNIPPET}]}))
    result = search(BASE, "search", transport=fake)
    assert result.state == STATE_OK
    assert result.hits == (CodeHit("meridian_desktop/code_client.py", 12, 0.9123, SNIPPET),)


def test_an_empty_result_list_is_ok_with_no_hits():
    result = search(BASE, "nothing", transport=FakeTransport(results({"results": []})))
    assert result.state == STATE_OK
    assert result.hits == ()


def test_request_is_a_get_to_the_search_route_with_query_and_limit():
    fake = FakeTransport(results({"results": []}))
    search(BASE, "a b&c", transport=fake)
    base_url, path, timeout = fake.calls[0]
    assert base_url == BASE
    assert path == "/desktop/code/search?q=a+b%26c&limit=25"
    assert timeout == local_client.DEFAULT_TIMEOUT


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(0, 1), (-5, 1), (7, 7), (1000, MAX_LIMIT), ("x", DEFAULT_LIMIT), (True, DEFAULT_LIMIT), (None, DEFAULT_LIMIT)],
)
def test_limit_is_clamped_and_falls_back_when_it_is_not_an_int(limit, expected):
    fake = FakeTransport(results({"results": []}))
    search(BASE, "q", limit=limit, transport=fake)
    assert f"limit={expected}" in fake.calls[0][1]


def test_query_is_trimmed_and_capped_before_it_is_sent():
    fake = FakeTransport(results({"results": []}))
    search(BASE, "  " + "x" * 500 + "  ", transport=fake)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0][1]).query)["q"][0]
    assert query == "x" * MAX_QUERY_CHARS


@pytest.mark.parametrize("query", ["", "   ", None, 42])
def test_blank_query_returns_before_any_request(query):
    fake = FakeTransport(results({"results": []}))
    result = search(BASE, query, transport=fake)
    assert result.state == STATE_BLANK_QUERY
    assert result.detail == BLANK_QUERY_MESSAGE
    assert fake.calls == []


@pytest.mark.parametrize(
    "base",
    [
        "http://example.com:7878",
        "https://127.0.0.1:7878",
        "http://user:secret@127.0.0.1:7878",
        "http://127.0.0.1.example.com:7878",
        "http://127.0.0.1:7878/?x=1",
        "",
        None,
        42,
        "ftp://127.0.0.1",
    ],
)
def test_non_loopback_base_is_refused_before_any_request(base, monkeypatch):
    fake = FakeTransport(results({"results": []}))
    default_calls = []

    def recording_default(*args, **kwargs):
        default_calls.append(args)
        return Reply(200, b'{"results": []}')

    monkeypatch.setattr(local_client, "_get", recording_default)
    result = search(base, "q", transport=fake)
    assert result.state == STATE_ERROR
    assert "secret" not in result.detail
    assert fake.calls == []
    assert search(base, "q").state == STATE_ERROR  # the default transport is refused as well
    assert default_calls == []


def test_no_server_is_the_not_running_state():
    fake = FakeTransport(error=LocalClientError("not_running", NOT_RUNNING_MESSAGE))
    assert search(BASE, "q", transport=fake).state == STATE_NOT_RUNNING


@pytest.mark.parametrize(
    ("code", "message"),
    [("timeout", TIMEOUT_MESSAGE), ("too_large", TOO_LARGE_MESSAGE), ("unreachable", UNREACHABLE_MESSAGE)],
)
def test_other_transport_failures_keep_the_fixed_message(code, message):
    result = search(BASE, "q", transport=FakeTransport(error=LocalClientError(code, message)))
    assert result.state == STATE_ERROR
    assert result.detail == message


def test_an_unexpected_transport_exception_is_reduced_to_fixed_text():
    result = search(BASE, "q", transport=FakeTransport(error=RuntimeError("secret detail inside the failure")))
    assert result.state == STATE_ERROR
    assert result.detail == UNCHECKED_MESSAGE
    assert "secret" not in result.detail


def test_a_404_is_the_route_missing_state():
    result = search(BASE, "q", transport=FakeTransport(answer(404, b'{"detail": "Not Found"}')))
    assert result.state == STATE_ROUTE_MISSING


@pytest.mark.parametrize("status", [409, 503])
def test_not_indexed_is_read_from_the_error_code(status):
    body = json.dumps({"error": {"code": "not_indexed", "message": "no index yet"}}).encode("utf-8")
    result = search(BASE, "q", transport=FakeTransport(answer(status, body)))
    assert result.state == STATE_NOT_INDEXED
    assert result.hits == ()


def test_an_error_code_that_is_not_a_safe_slug_is_not_trusted_as_not_indexed():
    body = json.dumps({"error": {"code": "NOT_INDEXED"}}).encode("utf-8")
    result = search(BASE, "q", transport=FakeTransport(answer(409, body)))
    assert result.state == STATE_ERROR


def test_another_error_status_reports_the_status_and_never_the_body():
    body = json.dumps({"error": {"code": "boom"}, "secret": "leak-me"}).encode("utf-8")
    result = search(BASE, "q", transport=FakeTransport(answer(500, body)))
    assert result.state == STATE_ERROR
    assert result.detail == HTTP_STATUS_MESSAGE.format(500)
    assert "leak-me" not in result.detail


def test_a_redirect_is_refused_with_its_own_message():
    result = search(BASE, "q", transport=FakeTransport(answer(302, b"")))
    assert result.state == STATE_ERROR
    assert result.detail == REDIRECT_MESSAGE


@pytest.mark.parametrize("body", [b"not json", b"[]", b"null", b'{"results": "nope"}', b'{"hits": []}', b"\xff\xfe"])
def test_a_200_that_is_not_a_results_object_is_a_bad_response(body):
    result = search(BASE, "q", transport=FakeTransport(answer(200, body)))
    assert result.state == STATE_ERROR
    assert result.detail == BAD_RESPONSE_MESSAGE


# ----------------------------------------------------------------- reading the hits


def test_malformed_entries_are_skipped_and_bad_fields_are_blanked():
    body = (
        b'{"results": ['
        b'"a string entry", '
        b'{"line": 3}, '
        b'{"path": "   "}, '
        b'{"path": "a.py", "line": true, "score": true, "snippet": null}, '
        b'{"path": "b.py", "line": 0, "score": "0.5", "snippet": "x"}, '
        b'{"path": "c.py", "line": 5, "score": NaN, "snippet": "y"}, '
        b'{"path": "d.py", "line": 2, "score": 1e400, "snippet": ""}, '
        b'{"path": "e.py", "line": 1, "score": 1' + b"0" * 400 + b"}"
        b"]}"
    )
    result = search(BASE, "q", transport=FakeTransport(answer(200, body)))
    assert result.hits == (
        CodeHit("a.py", 0, None, ""),
        CodeHit("b.py", 0, None, "x"),
        CodeHit("c.py", 5, None, "y"),
        CodeHit("d.py", 2, None, ""),
        CodeHit("e.py", 1, None, ""),
    )


def test_path_and_snippet_are_capped_and_the_snippet_keeps_its_indent():
    payload = {
        "results": [
            {"path": "  a.py  ", "line": 1, "score": 0.1, "snippet": "    def f():\n  return 1  \n"},
            {"path": "p" * 400, "line": 2, "score": 0.2, "snippet": "s" * 5000},
        ]
    }
    hits = search(BASE, "q", transport=FakeTransport(results(payload))).hits
    assert hits[0].path == "a.py"
    assert hits[0].snippet == "    def f():\n  return 1"
    assert len(hits[1].path) == MAX_PATH_CHARS and hits[1].path.endswith("...")
    assert len(hits[1].snippet) == MAX_SNIPPET_CHARS and hits[1].snippet.endswith("...")


def test_no_more_than_the_max_limit_entries_are_read():
    payload = {"results": [{"path": f"f{i}.py", "line": 1, "score": 1.0, "snippet": ""} for i in range(150)]}
    hits = search(BASE, "q", transport=FakeTransport(results(payload))).hits
    assert len(hits) == MAX_LIMIT


# ----------------------------------------------------------------- the default transport


def test_default_transport_reads_a_loopback_stub_answer(serve):
    stub = serve(200, json.dumps({"results": [{"path": "x.py", "line": 4, "score": 2, "snippet": "hi"}]}).encode("utf-8"))
    result = search(stub.url, "hello")
    assert result.state == STATE_OK
    assert result.hits == (CodeHit("x.py", 4, 2.0, "hi"),)
    assert stub.paths == ["/desktop/code/search?q=hello&limit=25"]


def test_default_transport_maps_a_refused_connection_to_not_running():
    assert search(_closed_port_url(), "q").state == STATE_NOT_RUNNING


def test_default_transport_does_not_follow_a_redirect(serve):
    stub = serve(302, b"", headers={"Location": "http://127.0.0.1:1/elsewhere"})
    result = search(stub.url, "q")
    assert result.state == STATE_ERROR
    assert result.detail == REDIRECT_MESSAGE
    assert len(stub.paths) == 1


def test_default_transport_maps_a_loopback_404_to_route_missing(serve):
    stub = serve(404, b'{"detail": "Not Found"}')
    assert search(stub.url, "q").state == STATE_ROUTE_MISSING
