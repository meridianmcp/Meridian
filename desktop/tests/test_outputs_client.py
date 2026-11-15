"""Tests for the Outputs and Research client (meridian_desktop/outputs_client.py). Pure Python, no Qt.

Most tests inject a fake transport, so no request leaves the test. The real loopback GET is exercised
through a fake urllib opener (local_client._opener), so a 404, a refused connection and a timeout take
the same path they take in the app, with no socket opened.
"""

import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from meridian_desktop import local_client, outputs_client as oc
from meridian_desktop.local_client import LocalClientError

LOCAL = "http://127.0.0.1:8471"


class FakeTransport:
    """Records each call. Returns the reply it was given, or raises the error it was given."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        if self.error is not None:
            raise self.error
        return self.reply


def ok(payload):
    return oc.Reply(200, json.dumps(payload).encode("utf-8"))


class _Opener:
    """Stands in for local_client's opener. Records the request and raises the error it was given."""

    def __init__(self, error):
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request.get_method(), request.full_url))
        raise self.error


def _http_404(url):
    return urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"error":{"code":"not_found"}}'))


def _hit(**overrides):
    hit = {
        "path": "C:/outputs/figures/fit.csv",
        "score": 0.912,
        "bm25": 1.8,
        "kind": "csv",
        "is_archival": False,
        "canonical_path": None,
        "generating_script": "make_table.py",
        "csv_columns": ["x", "y", "err"],
        "json_keys": None,
        "size": 2048,
        "mtime": "2026-10-18T10:00:00Z",
    }
    hit.update(overrides)
    return hit


def _query_of(path):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)


# ----------------------------------------------------------------- query handling


@pytest.mark.parametrize("query", ["", "   ", "\n\t", None, 5, ["deep"]])
def test_blank_or_non_text_query_is_refused_before_any_request(query):
    transport = FakeTransport(reply=ok({"hits": []}))
    result = oc.search_outputs(LOCAL, query, transport=transport)
    assert result.state == oc.INVALID
    assert result.message == oc.BLANK_QUERY_MESSAGE
    assert transport.calls == []


def test_over_long_query_is_refused_before_any_request():
    transport = FakeTransport(reply=ok({"hits": []}))
    result = oc.search_outputs(LOCAL, "x" * (oc.MAX_QUERY_CHARS + 1), transport=transport)
    assert result.state == oc.INVALID
    assert result.message == oc.QUERY_TOO_LONG_MESSAGE
    assert transport.calls == []


def test_query_is_normalised_and_encoded_into_the_search_path():
    transport = FakeTransport(reply=ok({"hits": []}))
    oc.search_outputs(LOCAL, "  a&b   c\n", transport=transport)
    base_url, path, timeout = transport.calls[0]
    assert base_url == LOCAL
    assert path.startswith("/desktop/outputs/search?")
    assert _query_of(path) == {"q": ["a&b c"], "limit": [str(oc.DEFAULT_LIMIT)]}
    assert timeout == oc.SEARCH_TIMEOUT


@pytest.mark.parametrize(
    "limit, sent",
    [(0, "1"), (5, "5"), (10000, str(oc.MAX_LIMIT)), ("7", str(oc.DEFAULT_LIMIT)), (True, str(oc.DEFAULT_LIMIT)),
     (None, str(oc.DEFAULT_LIMIT)), (2.5, str(oc.DEFAULT_LIMIT))],
)
def test_limit_is_clamped_and_a_non_whole_number_gets_the_default(limit, sent):
    transport = FakeTransport(reply=ok({"hits": []}))
    oc.search_outputs(LOCAL, "q", limit, transport=transport)
    assert _query_of(transport.calls[0][1])["limit"] == [sent]


# ----------------------------------------------------------------- outputs: results and provenance


def test_hits_carry_the_provenance_fields_the_route_returns():
    transport = FakeTransport(reply=ok({"hits": [_hit()], "total_indexed": 1}))
    result = oc.search_outputs(LOCAL, "fit", transport=transport)

    assert result.state == oc.RESULTS
    assert result.message == oc.OUTPUTS_RESULTS_MESSAGE.format(1, "fit")
    assert result.query == "fit"
    (hit,) = result.hits
    assert hit == oc.OutputHit(
        path="C:/outputs/figures/fit.csv",
        score=0.912,
        kind="csv",
        is_archival=False,
        canonical_path="",
        generating_script="make_table.py",
        csv_columns=("x", "y", "err"),
        json_keys=(),
        size=2048,
        modified="2026-10-18T10:00:00Z",
    )


def test_a_path_is_kept_exactly_including_its_spaces():
    path = "C:/out puts/two  spaces/fit (1).csv"
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=ok({"hits": [_hit(path=path)]})))
    assert result.hits[0].path == path


def test_a_hit_without_a_path_or_with_a_control_character_in_it_is_dropped():
    payload = {
        "hits": [
            _hit(path=""),
            _hit(path="C:/a\nb.csv"),
            _hit(path=None),
            "not a hit",
            _hit(path="C:/keep.csv"),
        ]
    }
    result = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok(payload)))
    assert [hit.path for hit in result.hits] == ["C:/keep.csv"]


def test_text_fields_are_collapsed_and_capped():
    long_kind = "k" * 500
    payload = {"hits": [_hit(kind=long_kind, generating_script="  make\n  table.py  ")]}
    hit = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok(payload))).hits[0]
    assert len(hit.kind) == oc.MAX_FIELD_CHARS
    assert hit.kind.endswith("...")
    assert hit.generating_script == "make table.py"


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("score", "0.9", None),
        ("score", True, None),
        ("score", None, None),
        ("size", -1, None),
        ("size", "2048", None),
        ("is_archival", "yes", False),
    ],
)
def test_values_of_the_wrong_type_read_as_not_reported(field, value, expected):
    hit = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok({"hits": [_hit(**{field: value})]}))).hits[0]
    attr = {"score": "score", "size": "size", "is_archival": "is_archival"}[field]
    assert getattr(hit, attr) == expected


def test_a_non_finite_score_reads_as_not_reported():
    body = b'{"hits": [{"path": "C:/a.csv", "score": NaN}]}'
    result = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=oc.Reply(200, body)))
    assert result.state == oc.RESULTS
    assert result.hits[0].score is None


def test_name_lists_are_capped_and_non_lists_read_as_empty():
    payload = {"hits": [_hit(csv_columns=[f"c{i}" for i in range(80)], json_keys="not a list")]}
    hit = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok(payload))).hits[0]
    assert len(hit.csv_columns) == oc.MAX_LIST_ITEMS
    assert hit.json_keys == ()


# ----------------------------------------------------------------- outputs: index state


def test_index_state_is_read_from_the_convergence_object():
    payload = {
        "hits": [_hit()],
        "convergence": {
            "partial_index": True,
            "pending_count": 3,
            "degraded": True,
            "walk_errors": [],
            "db_write_error": None,
            "last_rebuilt_at": "2026-10-18T10:00:00Z",
        },
    }
    index = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok(payload))).index
    assert index == oc.IndexState(
        degraded=True,
        partial=True,
        pending_count=3,
        db_write_failed=False,
        walk_error_count=0,
        last_rebuilt_at="2026-10-18T10:00:00Z",
    )


def test_index_state_falls_back_to_the_top_level_fields_of_the_search_response():
    payload = {"hits": [_hit()], "partial": True, "pending_count": 2}
    index = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok(payload))).index
    assert index.degraded is True
    assert index.partial is True
    assert index.pending_count == 2


def test_the_server_cannot_clear_degraded_while_the_index_is_partial():
    payload = {"hits": [_hit()], "convergence": {"degraded": False, "partial_index": True}}
    index = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok(payload))).index
    assert index.degraded is True


def test_unreadable_walk_and_write_failures_make_the_index_degraded():
    payload = {
        "hits": [_hit()],
        "convergence": {"walk_errors": ["permission denied"], "db_write_error": "disk full"},
    }
    index = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok(payload))).index
    assert index.degraded is True
    assert index.walk_error_count == 1
    assert index.db_write_failed is True


def test_a_response_with_no_index_fields_reports_no_index_state():
    result = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok({"hits": [_hit()]})))
    assert result.index is None


def test_a_complete_index_is_not_degraded():
    payload = {"hits": [_hit()], "convergence": {"degraded": False, "partial_index": False, "pending_count": 0}}
    index = oc.search_outputs(LOCAL, "x", transport=FakeTransport(reply=ok(payload))).index
    assert index.degraded is False


# ----------------------------------------------------------------- outputs: empty and missing


def test_no_matches_is_the_empty_state_and_names_the_query():
    result = oc.search_outputs(LOCAL, "nothing", transport=FakeTransport(reply=ok({"hits": []})))
    assert result.state == oc.EMPTY
    assert result.message == oc.OUTPUTS_EMPTY_MESSAGE.format("nothing")
    assert result.hits == ()


def test_no_matches_while_the_index_is_degraded_says_it_is_not_proof():
    payload = {"hits": [], "convergence": {"partial_index": True, "pending_count": 9}}
    result = oc.search_outputs(LOCAL, "nothing", transport=FakeTransport(reply=ok(payload)))
    assert result.state == oc.EMPTY
    assert result.message == oc.OUTPUTS_EMPTY_DEGRADED_MESSAGE.format("nothing")
    assert "not proof" in result.message


def test_a_404_is_a_missing_route_and_not_an_empty_result(monkeypatch):
    opener = _Opener(_http_404(LOCAL))
    monkeypatch.setattr(local_client, "_opener", lambda: opener)
    result = oc.search_outputs(LOCAL, "fit")
    assert result.state == oc.MISSING_ROUTE
    assert result.message == oc.OUTPUTS_MISSING_ROUTE_MESSAGE
    assert result.hits == ()
    assert len(opener.requests) == 1
    method, url = opener.requests[0]
    assert method == "GET"
    assert url.startswith(LOCAL + "/desktop/outputs/search?")


def test_a_refused_connection_is_not_running_through_the_real_loopback_get(monkeypatch):
    opener = _Opener(urllib.error.URLError(ConnectionRefusedError(111, "refused")))
    monkeypatch.setattr(local_client, "_opener", lambda: opener)
    result = oc.search_outputs(LOCAL, "fit")
    assert result.state == oc.NOT_RUNNING
    assert result.message == oc.NOT_RUNNING_MESSAGE
    assert result.message.startswith("Not connected")


def test_a_timeout_through_the_real_loopback_get_is_an_error(monkeypatch):
    monkeypatch.setattr(local_client, "_opener", lambda: _Opener(TimeoutError("read timed out")))
    result = oc.search_outputs(LOCAL, "fit")
    assert result.state == oc.ERROR
    assert result.message == oc.TIMEOUT_MESSAGE


# ----------------------------------------------------------------- outputs: errors


@pytest.mark.parametrize("base_url", ["http://example.com:8471", "https://127.0.0.1:8471", "http://127.0.0.1.example.com:8471",
                                      "http://user:pw@127.0.0.1:8471", "", None, "ftp://127.0.0.1"])
def test_a_non_loopback_address_sends_nothing(base_url):
    transport = FakeTransport(reply=ok({"hits": []}))
    result = oc.search_outputs(base_url, "fit", transport=transport)
    assert result.state == oc.ERROR
    assert result.message == oc.NOT_LOOPBACK_MESSAGE
    assert transport.calls == []


def test_a_200_that_is_not_json_is_an_error():
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=oc.Reply(200, b"<html>not json</html>")))
    assert result.state == oc.ERROR
    assert result.message == oc.UNREADABLE_MESSAGE


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"null", b"\xff\xfe"])
def test_a_200_that_is_not_a_json_object_is_an_error(body):
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=oc.Reply(200, body)))
    assert result.state == oc.ERROR
    assert result.message == oc.UNREADABLE_MESSAGE


def test_a_200_without_a_hits_list_is_an_error_not_an_empty_list():
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=ok({"total_indexed": 0})))
    assert result.state == oc.ERROR
    assert result.message == oc.UNREADABLE_MESSAGE


def test_a_server_error_field_is_an_error_that_carries_its_own_short_text():
    payload = {"hits": [], "error": "outputs_dir does not exist"}
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=ok(payload)))
    assert result.state == oc.ERROR
    assert result.message == oc.SERVER_ERROR_MESSAGE.format("outputs_dir does not exist")


def test_a_server_error_with_no_readable_text_says_no_detail_was_given():
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=ok({"error": {"nested": 1}})))
    assert result.state == oc.ERROR
    assert result.message == oc.SERVER_ERROR_MESSAGE.format(oc.NO_DETAIL)


@pytest.mark.parametrize("status", [500, 401, 403, 418])
def test_another_http_status_is_an_error_that_names_the_status(status):
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=oc.Reply(status, b"")))
    assert result.state == oc.ERROR
    assert result.message == oc.HTTP_STATUS_MESSAGE.format(status)


def test_a_redirect_is_an_error_and_is_not_followed():
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=oc.Reply(302, b"")))
    assert result.state == oc.ERROR
    assert result.message == oc.REDIRECT_MESSAGE


def test_the_error_message_never_carries_the_response_body():
    secret = "TOKEN-abc123-do-not-show"
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(reply=oc.Reply(500, secret.encode())))
    assert secret not in result.message


@pytest.mark.parametrize(
    "error, state, message",
    [
        (LocalClientError("timeout", "x"), oc.ERROR, oc.TIMEOUT_MESSAGE),
        (LocalClientError("too_large", "x"), oc.ERROR, oc.TOO_LARGE_MESSAGE),
        (LocalClientError("unreachable", "x"), oc.ERROR, oc.UNREACHABLE_MESSAGE),
        (LocalClientError("not_running", "x"), oc.NOT_RUNNING, oc.NOT_RUNNING_MESSAGE),
        (LocalClientError("some_new_code", "x"), oc.ERROR, oc.UNCHECKED_MESSAGE),
        (RuntimeError("private detail with C:/secret"), oc.ERROR, oc.UNCHECKED_MESSAGE),
    ],
)
def test_transport_failures_map_to_a_typed_state_and_fixed_text(error, state, message):
    result = oc.search_outputs(LOCAL, "fit", transport=FakeTransport(error=error))
    assert result.state == state
    assert result.message == message
    assert "secret" not in result.message


def test_the_default_transport_is_the_loopback_get(monkeypatch):
    seen = []

    def fake_get(base_url, path, timeout):
        seen.append((base_url, path, timeout))
        return local_client._Reply(200, json.dumps({"hits": []}).encode())

    monkeypatch.setattr(local_client, "_get", fake_get)
    result = oc.search_outputs(LOCAL, "fit")
    assert result.state == oc.EMPTY
    assert seen[0][0] == LOCAL
    assert seen[0][1].startswith("/desktop/outputs/search?")


# ----------------------------------------------------------------- research runs


def _run(**overrides):
    run = {
        "id": "run-1",
        "project_id": "p",
        "status": "active",
        "mode": "read_only",
        "provider": "provider-a",
        "started_at": "2026-10-18T09:00:00Z",
        "updated_at": "2026-10-18T09:05:00Z",
        "completed_at": None,
    }
    run.update(overrides)
    return run


def test_research_runs_map_state_provider_and_timestamps():
    transport = FakeTransport(reply=ok({"runs": [_run(), _run(id="run-2", status="completed", completed_at="2026-10-18T10:00:00Z")], "count": 2}))
    result = oc.list_research_runs(LOCAL, transport=transport)

    assert transport.calls[0][1] == "/desktop/research/runs?limit=50"
    assert result.state == oc.RESULTS
    assert result.message == oc.RUNS_RESULTS_MESSAGE.format(2)
    assert result.runs == (
        oc.ResearchRun("run-1", "active", "provider-a", "2026-10-18T09:00:00Z", "2026-10-18T09:05:00Z", ""),
        oc.ResearchRun("run-2", "completed", "provider-a", "2026-10-18T09:00:00Z", "2026-10-18T09:05:00Z", "2026-10-18T10:00:00Z"),
    )


def test_a_run_without_a_provider_reads_as_empty_text_for_the_panel_to_say_not_reported():
    result = oc.list_research_runs(LOCAL, transport=FakeTransport(reply=ok({"runs": [_run(provider=None)]})))
    assert result.runs[0].provider == ""


def test_state_falls_back_to_a_state_key_when_status_is_missing():
    run = _run(status=None, state="failed")
    result = oc.list_research_runs(LOCAL, transport=FakeTransport(reply=ok({"runs": [run]})))
    assert result.runs[0].state == "failed"


def test_runs_without_an_id_are_skipped():
    result = oc.list_research_runs(LOCAL, transport=FakeTransport(reply=ok({"runs": [_run(id=""), "x", _run(id="ok")]})))
    assert [run.run_id for run in result.runs] == ["ok"]


def test_no_runs_is_the_empty_state():
    result = oc.list_research_runs(LOCAL, transport=FakeTransport(reply=ok({"runs": [], "count": 0})))
    assert result.state == oc.EMPTY
    assert result.message == oc.RUNS_EMPTY_MESSAGE
    assert result.runs == ()


def test_research_run_limit_is_clamped_into_the_path():
    transport = FakeTransport(reply=ok({"runs": []}))
    oc.list_research_runs(LOCAL, 500, transport=transport)
    assert _query_of(transport.calls[0][1]) == {"limit": [str(oc.MAX_LIMIT)]}


def test_research_runs_without_a_list_are_an_error_not_an_empty_list():
    result = oc.list_research_runs(LOCAL, transport=FakeTransport(reply=ok({"count": 0})))
    assert result.state == oc.ERROR
    assert result.message == oc.UNREADABLE_MESSAGE


def test_research_runs_server_error_field_is_an_error():
    result = oc.list_research_runs(LOCAL, transport=FakeTransport(reply=ok({"runs": [], "error": "db busy"})))
    assert result.state == oc.ERROR
    assert result.message == oc.SERVER_ERROR_MESSAGE.format("db busy")


def test_research_runs_with_no_server_is_not_connected(monkeypatch):
    monkeypatch.setattr(local_client, "_opener", lambda: _Opener(urllib.error.URLError(ConnectionRefusedError(111, "x"))))
    result = oc.list_research_runs(LOCAL)
    assert result.state == oc.NOT_RUNNING
    assert result.message == oc.NOT_RUNNING_MESSAGE
    assert result.runs == ()


def test_research_runs_on_a_404_is_a_missing_route(monkeypatch):
    opener = _Opener(_http_404(LOCAL))
    monkeypatch.setattr(local_client, "_opener", lambda: opener)
    result = oc.list_research_runs(LOCAL)
    assert result.state == oc.MISSING_ROUTE
    assert result.message == oc.RUNS_MISSING_ROUTE_MESSAGE
    assert opener.requests[0][0] == "GET"


def test_research_runs_refuse_a_non_loopback_address_before_any_request():
    transport = FakeTransport(reply=ok({"runs": []}))
    result = oc.list_research_runs("http://example.com", transport=transport)
    assert result.state == oc.ERROR
    assert result.message == oc.NOT_LOOPBACK_MESSAGE
    assert transport.calls == []


def test_research_runs_unexpected_transport_failure_shows_fixed_text():
    result = oc.list_research_runs(LOCAL, transport=FakeTransport(error=ValueError("leak C:/x")))
    assert result.state == oc.ERROR
    assert result.message == oc.UNCHECKED_MESSAGE


# ----------------------------------------------------------------- module rules


def test_the_client_module_imports_no_qt():
    source = Path(oc.__file__).read_text(encoding="utf-8")
    assert "PySide6" not in source
    assert "PyQt" not in source
