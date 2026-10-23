"""Tests for the RunPod v2 REST client (meridian.research.providers.runpod_client).

Every test drives the client through httpx.MockTransport, so nothing reaches
the network. The API key below is a placeholder, not a credential, and the
tests assert that it never appears in any error the client raises.
"""
from __future__ import annotations

import json

import httpx
import pytest

from meridian.research.providers.runpod import RunPodClientProtocol
from meridian.research.providers.runpod_client import (
    DEFAULT_BASE_URL,
    RunPodAPIError,
    RunPodClient,
    build_create_pod_body,
)

_KEY = "rpa_TESTONLY_placeholder_0123456789"
_POD = {"id": "pod-abc", "name": "meridian-x", "status": "RUNNING"}


class _Recorder:
    """Records every request the client sends and answers with ``responder``."""

    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _client(responder) -> tuple[RunPodClient, _Recorder]:
    recorder = _Recorder(responder)
    return RunPodClient(_KEY, transport=httpx.MockTransport(recorder)), recorder


def _json_response(payload, status: int = 200, headers: "dict[str, str] | None" = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def _empty_page(_request: httpx.Request) -> httpx.Response:
    return _json_response({"pods": [], "pagination": {"hasNextPage": False, "nextCursor": None}})


def test_default_base_url_is_v2_and_paths_resolve_under_it():
    client, rec = _client(_empty_page)
    client.list_pods()
    assert DEFAULT_BASE_URL == "https://api.runpod.io/v2"
    assert str(rec.requests[0].url) == "https://api.runpod.io/v2/pods"


def test_list_pods_sends_cursor_and_limit():
    client, rec = _client(
        lambda r: _json_response({"pods": [_POD], "pagination": {"nextCursor": None, "hasNextPage": False}})
    )
    page = client.list_pods(cursor="c1", limit=50)
    req = rec.requests[0]
    assert req.method == "GET"
    assert req.url.path == "/v2/pods"
    assert req.url.params["cursor"] == "c1"
    assert req.url.params["limit"] == "50"
    assert page["pods"] == [_POD]


def test_list_pods_rejects_out_of_range_limit_without_request():
    client, rec = _client(_empty_page)
    with pytest.raises(ValueError):
        client.list_pods(limit=0)
    with pytest.raises(ValueError):
        client.list_pods(limit=1001)
    assert rec.requests == []


def test_include_cluster_pods_is_not_sent_and_raises():
    client, rec = _client(_empty_page)
    with pytest.raises(NotImplementedError, match="includeClusterPods"):
        client.list_pods(include_cluster_pods=True)
    assert rec.requests == []


def test_get_pod_path_and_method():
    client, rec = _client(lambda r: _json_response(_POD))
    assert client.get_pod("pod-abc") == _POD
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("GET", "/v2/pods/pod-abc")


def test_get_pod_percent_encodes_pod_id_characters_that_are_allowed_through():
    client, rec = _client(lambda r: _json_response(_POD))
    client.get_pod("pod abc%")
    req = rec.requests[0]
    assert req.url.raw_path == b"/v2/pods/pod%20abc%25"
    assert req.url.query == b""


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_empty_pod_id_is_refused_without_request(bad_id):
    client, rec = _client(lambda r: _json_response(_POD))
    with pytest.raises(ValueError):
        client.get_pod(bad_id)
    assert rec.requests == []


@pytest.mark.parametrize("bad_id", [".", "..", "../billing?x=1", "pod/x", "a/../b", "pod?x=1", "pod#frag"])
@pytest.mark.parametrize("method", ["get_pod", "stop_pod", "terminate_pod", "pod_logs"])
def test_dot_segment_or_path_or_query_delimiter_pod_id_is_refused_without_request(method, bad_id):
    # "." and ".." survive percent-quoting and would resolve to /v2 or /v2/pods.
    client, rec = _client(lambda r: _json_response(_POD))
    with pytest.raises(ValueError):
        getattr(client, method)(bad_id)
    assert rec.requests == []


def test_create_pod_posts_confirmed_body_fields_only():
    client, rec = _client(lambda r: _json_response(_POD, status=201))
    client.create_pod(
        name="meridian-x",
        image_name="runpod/pytorch:2.4",
        env={"A": "1"},
        gpu_type_id="NVIDIA A100 80GB",
        gpu_count=2,
        command=("python", "train.py"),
    )
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("POST", "/v2/pods")
    assert json.loads(req.content) == {
        "name": "meridian-x",
        "image": "runpod/pytorch:2.4",
        "env": {"A": "1"},
        "gpu": {"id": "NVIDIA A100 80GB", "count": 2},
        "cmd": ["python", "train.py"],
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "image_name": "img"},
        {"name": "n", "image_name": ""},
        {"name": "n", "image_name": "img", "gpu_count": 1},
        {"name": "n", "image_name": "img", "gpu_type_id": "A100", "gpu_count": 0},
        {"name": "n", "image_name": "img", "gpu_type_id": "A100", "gpu_count": -1},
        # The spec requires exactly one of gpu or cpu; neither must not be sent.
        {"name": "n", "image_name": "img"},
        {"name": "n", "image_name": "img", "env": {"A": "1"}, "command": ("x",)},
    ],
)
def test_create_pod_validates_before_any_request(kwargs):
    client, rec = _client(lambda r: _json_response(_POD, status=201))
    with pytest.raises(ValueError):
        client.create_pod(**kwargs)
    assert rec.requests == []


def test_create_pod_has_no_cpu_or_memory_parameter_so_they_cannot_be_silently_accepted():
    client, rec = _client(lambda r: _json_response(_POD, status=201))
    with pytest.raises(TypeError):
        client.create_pod(name="n", image_name="img", cpu=4)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        client.create_pod(name="n", image_name="img", memory_gb=16)  # type: ignore[call-arg]
    assert rec.requests == []


def test_build_create_pod_body_matches_create_request():
    body = build_create_pod_body(name="n", image_name="img", gpu_type_id="A100", gpu_count=1)
    assert body == {"name": "n", "image": "img", "gpu": {"id": "A100", "count": 1}}


def test_stop_pod_posts_stop_action():
    client, rec = _client(lambda r: _json_response({"id": "pod-abc", "status": "EXITED"}))
    client.stop_pod("pod-abc")
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("POST", "/v2/pods/pod-abc/action")
    assert json.loads(req.content) == {"action": "stop"}


def test_terminate_pod_uses_delete_and_accepts_204_with_no_body():
    client, rec = _client(lambda r: httpx.Response(204))
    assert client.terminate_pod("pod-abc") == {}
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("DELETE", "/v2/pods/pod-abc")


def test_pod_logs_streams_sse_with_source_and_resume_header():
    body = 'data: {"source":"container","line":"hello","ts":"t"}\n\n'
    client, rec = _client(
        lambda r: httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
    )
    text = client.pod_logs("pod-abc", source="system", last_event_id="42")
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("GET", "/v2/pods/pod-abc/logs")
    assert req.url.params["source"] == "system"
    assert req.headers["accept"] == "text/event-stream"
    assert req.headers["last-event-id"] == "42"
    assert text == body


def test_pod_logs_caps_bytes_read():
    client, _ = _client(lambda r: httpx.Response(200, content=("x" * 5000).encode()))
    assert len(client.pod_logs("pod-abc", max_bytes=100)) == 100


def test_pod_logs_refuses_unconfirmed_query_params_and_bad_source_without_request():
    client, rec = _client(lambda r: httpx.Response(200, content=b""))
    with pytest.raises(NotImplementedError, match="tail"):
        client.pod_logs("pod-abc", tail=10)
    with pytest.raises(NotImplementedError, match="since"):
        client.pod_logs("pod-abc", since="2026-01-01")
    with pytest.raises(ValueError):
        client.pod_logs("pod-abc", source="other")
    assert rec.requests == []


def test_account_billing_gets_billing_route_with_no_window_params():
    client, rec = _client(lambda r: _json_response({"records": [], "metadata": {}}))
    assert client.account_billing() == {"records": [], "metadata": {}}
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("GET", "/v2/billing")
    assert req.url.query == b""


def test_authorization_header_is_bearer_on_every_method_and_key_never_in_url_or_body():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/logs"):
            return httpx.Response(200, content=b"data: {}\n\n")
        return _json_response({"pods": [], "pagination": {"hasNextPage": False}, **_POD})

    client, rec = _client(responder)
    client.list_pods()
    client.get_pod("pod-abc")
    client.create_pod(name="n", image_name="img", env={"A": "1"}, gpu_type_id="A100", gpu_count=1)
    client.stop_pod("pod-abc")
    client.terminate_pod("pod-abc")
    client.pod_logs("pod-abc")
    client.account_billing()
    client.find_pod_by_name("n")

    assert len(rec.requests) >= 8
    for req in rec.requests:
        assert req.headers["authorization"] == f"Bearer {_KEY}"
        assert _KEY not in str(req.url)
        assert _KEY.encode() not in req.content


def test_401_error_carries_no_key_material_even_when_server_echoes_the_key():
    leaky = {"title": "Unauthorized", "status": 401, "detail": f"Invalid API key {_KEY}"}
    client, _ = _client(lambda r: _json_response(leaky, status=401))
    with pytest.raises(RunPodAPIError) as exc_info:
        client.get_pod("pod-abc")
    err = exc_info.value
    assert err.status_code == 401
    assert err.code == "Unauthorized"
    assert _KEY not in str(err)
    assert _KEY not in repr(err)
    assert _KEY not in repr(err.args)
    assert _KEY not in " ".join(str(v) for v in vars(err).values())
    assert "REDACTED" in str(err)


def test_5xx_with_non_json_body_uses_generic_code_and_no_key_material():
    client, _ = _client(lambda r: httpx.Response(503, content=b"<html>down</html>"))
    with pytest.raises(RunPodAPIError) as exc_info:
        client.get_pod("pod-abc")
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "HTTP_503"
    assert _KEY not in str(exc_info.value)


def test_5xx_with_problem_body_uses_title_as_code():
    client, _ = _client(
        lambda r: _json_response({"title": "Internal Server Error", "status": 500, "detail": "boom"}, status=500)
    )
    with pytest.raises(RunPodAPIError) as exc_info:
        client.list_pods()
    assert exc_info.value.status_code == 500
    assert exc_info.value.code == "Internal Server Error"
    assert "boom" in exc_info.value.message


def test_429_exposes_numeric_retry_after():
    client, _ = _client(
        lambda r: _json_response(
            {"title": "Too Many Requests", "status": 429, "detail": "slow down"},
            status=429,
            headers={"Retry-After": "7"},
        )
    )
    with pytest.raises(RunPodAPIError) as exc_info:
        client.list_pods()
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 7.0


def test_transport_failure_is_wrapped_without_key_material():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed for {request.url}")

    client, _ = _client(boom)
    with pytest.raises(RunPodAPIError) as exc_info:
        client.get_pod("pod-abc")
    assert exc_info.value.status_code is None
    assert exc_info.value.code == "TRANSPORT_ERROR"
    assert _KEY not in str(exc_info.value)


def test_error_outside_httpx_error_passes_through_unwrapped():
    # httpx.InvalidURL is not an httpx.HTTPError, so it is not wrapped as RunPodAPIError (see the module docstring).
    def bad_url(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("malformed URL component")

    client, _ = _client(bad_url)
    with pytest.raises(httpx.InvalidURL):
        client.get_pod("pod-abc")
    assert not issubclass(httpx.InvalidURL, httpx.HTTPError)


def _walk_chain(exc: BaseException):
    """Yield ``exc`` and every exception reachable from it through ``__context__`` or ``__cause__``."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend(link for link in (current.__context__, current.__cause__) if link is not None)


def _request_of(exc: BaseException) -> "httpx.Request | None":
    try:
        return getattr(exc, "request", None)
    except RuntimeError:  # httpx raises RuntimeError from .request when no request was set
        return None


def _assert_key_absent_from_chain(exc: BaseException) -> None:
    for link in _walk_chain(exc):
        name = type(link).__name__
        texts = [str(link), repr(link), repr(link.args), *map(str, link.args), *map(str, vars(link).values())]
        assert not any(_KEY in text for text in texts), f"{name} carries the key"
        request = _request_of(link)
        if request is not None:
            assert _KEY not in request.headers.get("authorization", ""), f"{name} request carries the key"


@pytest.mark.parametrize(
    "call",
    [lambda client: client.get_pod("pod-abc"), lambda client: client.pod_logs("pod-abc")],
    ids=["get_pod", "pod_logs"],
)
def test_transport_failure_chain_never_carries_the_key(call):
    # httpx attaches the request, and so the Authorization header, to its transport errors.
    seen: list[httpx.Request] = []

    def boom(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.ConnectError("connect failed", request=request)

    client, _ = _client(boom)
    with pytest.raises(RunPodAPIError) as exc_info:
        call(client)
    assert _KEY in seen[0].headers["authorization"]  # the fixture really does carry the key
    err = exc_info.value
    assert err.code == "TRANSPORT_ERROR"
    assert err.__context__ is None
    assert err.__cause__ is None
    _assert_key_absent_from_chain(err)


def test_invalid_json_body_chain_never_carries_the_key():
    # JSONDecodeError keeps the whole document in .doc, so a body that echoes the key must not be chained.
    client, _ = _client(lambda r: httpx.Response(200, content=f"<html>{_KEY}</html>".encode()))
    with pytest.raises(RunPodAPIError) as exc_info:
        client.get_pod("pod-abc")
    err = exc_info.value
    assert err.code == "INVALID_RESPONSE"
    assert err.__context__ is None
    assert err.__cause__ is None
    _assert_key_absent_from_chain(err)


def test_non_json_success_body_is_an_invalid_response_error():
    client, _ = _client(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(RunPodAPIError) as exc_info:
        client.get_pod("pod-abc")
    assert exc_info.value.code == "INVALID_RESPONSE"


@pytest.mark.parametrize("bad_key", ["", "   ", "rpa key", "rpa\nkey", "rpa\tkey", "rpa\x00key", "rpa\x7fkey"])
def test_api_key_with_whitespace_or_empty_is_refused_without_echoing_it(bad_key):
    with pytest.raises(ValueError) as exc_info:
        RunPodClient(bad_key)
    assert "rpa" not in str(exc_info.value)


def test_non_ascii_api_key_is_refused_before_httpx_and_does_not_echo_it():
    bad_key = "rpa_café_TESTONLY"
    with pytest.raises(ValueError) as exc_info:
        RunPodClient(bad_key)
    # UnicodeEncodeError (what httpx raises for this key) is a ValueError subclass,
    # so the exact type is checked to prove the client's own guard refused it.
    assert type(exc_info.value) is ValueError
    message = repr(exc_info.value)
    assert "rpa" not in message
    assert "caf" not in message
    assert "é" not in message


def test_find_pod_by_name_pages_until_the_match():
    pages = {
        None: {"pods": [{"id": "p1", "name": "other"}], "pagination": {"hasNextPage": True, "nextCursor": "c2"}},
        "c2": {"pods": [{"id": "p2", "name": "meridian-target"}], "pagination": {"hasNextPage": False}},
    }
    client, rec = _client(lambda r: _json_response(pages[r.url.params.get("cursor")]))
    found = client.find_pod_by_name("meridian-target")
    assert found is not None and found["id"] == "p2"
    assert [r.url.params.get("cursor") for r in rec.requests] == [None, "c2"]


def test_find_pod_by_name_matches_exact_name_only():
    client, _ = _client(
        lambda r: _json_response(
            {
                "pods": [{"id": "p1", "name": "meridian-target-extra"}, {"id": "p2", "name": "meridian-targe"}],
                "pagination": {"hasNextPage": False},
            }
        )
    )
    assert client.find_pod_by_name("meridian-target") is None


def test_find_pod_by_name_refuses_ambiguous_pagination_instead_of_reporting_absence():
    client, _ = _client(
        lambda r: _json_response({"pods": [], "pagination": {"hasNextPage": True, "nextCursor": None}})
    )
    with pytest.raises(RunPodAPIError) as exc_info:
        client.find_pod_by_name("x")
    assert exc_info.value.code == "PAGINATION_INCONSISTENT"


def test_find_pod_by_name_stops_on_a_repeated_cursor_instead_of_looping():
    client, rec = _client(
        lambda r: _json_response({"pods": [], "pagination": {"hasNextPage": True, "nextCursor": "same"}})
    )
    with pytest.raises(RunPodAPIError) as exc_info:
        client.find_pod_by_name("x")
    assert exc_info.value.code == "PAGINATION_INCONSISTENT"
    assert len(rec.requests) == 2


def test_client_exposes_every_method_the_adapter_protocol_requires():
    required = sorted(name for name in vars(RunPodClientProtocol) if not name.startswith("_"))
    assert required == ["create_pod", "find_pod_by_name", "get_pod", "stop_pod", "terminate_pod"]
    for name in required:
        assert callable(getattr(RunPodClient, name))


@pytest.mark.asyncio
async def test_provider_submit_and_cancel_through_real_client_send_expected_wire_requests():
    # The adapter's fake client accepts **kwargs, so only this test checks that
    # the adapter's create arguments match RunPodClient's real signature.
    from meridian.research.providers.base import JobSpec, ResourceRequest
    from meridian.research.providers.runpod import RunPodProvider

    created: dict[str, dict] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v2/pods":
            return _json_response({"pods": list(created.values()), "pagination": {"hasNextPage": False}})
        if request.method == "POST" and path == "/v2/pods":
            body = json.loads(request.content)
            pod = {"id": "pod-wire-1", "name": body["name"], "status": "CREATED"}
            created[body["name"]] = pod
            return _json_response(pod, status=201)
        if request.method == "POST" and path == "/v2/pods/pod-wire-1/action":
            return _json_response({"id": "pod-wire-1", "status": "EXITED"})
        if request.method == "DELETE" and path == "/v2/pods/pod-wire-1":
            return httpx.Response(204)
        return httpx.Response(404, json={"title": "Not Found", "status": 404, "detail": "unexpected request"})

    client, rec = _client(responder)
    spec = JobSpec(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a",
        idempotency_key="wire-job-1", command=("python", "train.py"), image="runpod/pytorch:2.4",
        env={"A": "1"}, resources=ResourceRequest(gpu_count=1, gpu_type="NVIDIA A100 80GB"),
    )
    handle = await RunPodProvider(client).submit(spec)
    assert handle.external_id == "pod-wire-1"

    # A fresh provider (empty handle map) resubmitting the same key finds the pod.
    again = await RunPodProvider(client).submit(spec)
    assert again.external_id == "pod-wire-1"

    status = await RunPodProvider(client).cancel(handle)
    assert status.state == "cancelled"

    creates = [r for r in rec.requests if r.method == "POST" and r.url.path == "/v2/pods"]
    assert len(creates) == 1
    assert json.loads(creates[0].content) == {
        "name": next(iter(created)),
        "image": "runpod/pytorch:2.4",
        "env": {"A": "1"},
        "gpu": {"id": "NVIDIA A100 80GB", "count": 1},
        "cmd": ["python", "train.py"],
    }
    wire = [(r.method, r.url.path) for r in rec.requests]
    assert ("POST", "/v2/pods/pod-wire-1/action") in wire
    assert ("DELETE", "/v2/pods/pod-wire-1") in wire


@pytest.mark.asyncio
async def test_negative_gpu_count_sends_no_wire_request_not_even_the_duplicate_lookup():
    from meridian.research.providers.base import JobSpec, ResourceRequest
    from meridian.research.providers.runpod import RunPodProvider

    client, rec = _client(_empty_page)
    spec = JobSpec(
        project_id="p", experiment_id="e", run_id="r", attempt_id="a",
        idempotency_key="wire-negative-gpu", command=("python", "train.py"), image="runpod/pytorch:2.4",
        resources=ResourceRequest(gpu_count=-1, gpu_type="NVIDIA A100 80GB"),
    )
    with pytest.raises(ValueError, match="gpu_count must be >= 0"):
        await RunPodProvider(client).submit(spec)
    assert rec.requests == []
