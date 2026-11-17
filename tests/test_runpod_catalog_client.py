"""Tests for the RunPod catalog methods on RunPodClient: get_gpu_prices and get_gpu_availability.

Every request goes through httpx.MockTransport, so nothing reaches the network. The placeholder key is never a
real key. It must not appear in a URL, a query string, a response, or an error message.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from meridian.research.providers import runpod_client as rc
from meridian.research.providers.runpod_client import (
    CATALOG_TIMEOUT,
    DEFAULT_TIMEOUT_SECONDS,
    GPU_CATALOG_PATH,
    RunPodAPIError,
    RunPodClient,
)

KEY = "TEST-KEY-DO-NOT-LEAK-7d2e4f"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise RuntimeError("network access is disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


class Recorder:
    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)


def _client(responder) -> tuple[RunPodClient, Recorder]:
    recorder = Recorder(responder)
    return RunPodClient(KEY, transport=httpx.MockTransport(recorder)), recorder


def _ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _timeout_of(request: httpx.Request) -> dict:
    return request.extensions["timeout"]


# --- get_gpu_prices ---------------------------------------------------------------------------------


def test_prices_call_is_a_get_on_the_v2_catalog_path_with_no_query_parameters():
    client, rec = _client(lambda r: _ok({"gpus": []}))
    assert client.get_gpu_prices() == {"gpus": []}
    (request,) = rec.requests
    assert request.method == "GET"
    assert request.url.path == "/v2/" + GPU_CATALOG_PATH
    assert request.url.query == b""


def test_the_key_is_sent_only_as_the_bearer_header_never_in_the_url():
    client, rec = _client(lambda r: _ok({"gpus": []}))
    client.get_gpu_prices()
    (request,) = rec.requests
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in str(request.url)
    assert b"api_key" not in request.url.query


def test_prices_call_uses_the_explicit_catalog_timeouts():
    client, rec = _client(lambda r: _ok({"gpus": []}))
    client.get_gpu_prices()
    assert _timeout_of(rec.requests[0]) == {"connect": 5.0, "read": 15.0, "write": 10.0, "pool": 5.0}
    assert CATALOG_TIMEOUT.connect == 5.0 and CATALOG_TIMEOUT.read == 15.0


def test_pod_endpoints_keep_the_default_timeout_so_the_catalog_change_does_not_leak_into_them():
    client, rec = _client(lambda r: _ok({"pods": [], "pagination": {}}))
    client.list_pods()
    assert _timeout_of(rec.requests[0])["read"] == DEFAULT_TIMEOUT_SECONDS


# --- get_gpu_availability ---------------------------------------------------------------------------


@pytest.mark.parametrize("cloud", ["SECURE", "COMMUNITY"])
def test_availability_call_sends_exactly_the_documented_parameters(cloud):
    client, rec = _client(lambda r: _ok({"gpus": []}))
    client.get_gpu_availability(cloud)
    (request,) = rec.requests
    assert request.url.path == "/v2/" + GPU_CATALOG_PATH
    params = dict(request.url.params)
    assert params == {"include": "AVAILABILITY", "product": "POD", "count": "1", "cloud": cloud}


def test_availability_call_never_sends_the_key_in_a_parameter_and_uses_the_catalog_timeouts():
    client, rec = _client(lambda r: _ok({"gpus": []}))
    client.get_gpu_availability("SECURE")
    (request,) = rec.requests
    assert KEY not in str(request.url)
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert _timeout_of(request)["connect"] == 5.0


@pytest.mark.parametrize("cloud", ["secure", "spot", "", "SECURE&api_key=x"])
def test_availability_refuses_any_cloud_other_than_secure_or_community_before_sending(cloud):
    client, rec = _client(lambda r: _ok({"gpus": []}))
    with pytest.raises(ValueError):
        client.get_gpu_availability(cloud)
    assert rec.requests == []


def test_the_generic_request_helper_is_unchanged_when_no_timeout_is_given():
    # The helper must not pass timeout=None (that disables the timeout in httpx). Pods keep the default.
    client, rec = _client(lambda r: _ok({"pods": [], "pagination": {}}))
    client.list_pods()
    assert _timeout_of(rec.requests[0])["read"] == DEFAULT_TIMEOUT_SECONDS


# --- error mapping at the client boundary -----------------------------------------------------------


def _expect_error(responder) -> RunPodAPIError:
    client, _ = _client(responder)
    with pytest.raises(RunPodAPIError) as info:
        client.get_gpu_prices()
    return info.value


def test_401_becomes_an_api_error_with_status_401():
    err = _expect_error(lambda r: httpx.Response(401, json={"title": "unauthorized", "detail": "bad key"}))
    assert err.status_code == 401 and err.code == "unauthorized"


def test_403_and_500_carry_their_status():
    assert _expect_error(lambda r: httpx.Response(403, json={"detail": "no"})).status_code == 403
    assert _expect_error(lambda r: httpx.Response(503, text="down")).status_code == 503


def test_429_carries_retry_after_seconds():
    err = _expect_error(lambda r: httpx.Response(429, headers={"Retry-After": "30"}, json={"detail": "slow"}))
    assert err.status_code == 429 and err.retry_after == 30.0


def test_a_non_numeric_retry_after_is_not_interpreted():
    err = _expect_error(lambda r: httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    assert err.retry_after is None


def test_a_read_timeout_is_a_transport_error_whose_message_starts_with_the_exception_name():
    def raise_timeout(_request):
        raise httpx.ReadTimeout("timed out")

    err = _expect_error(raise_timeout)
    assert err.status_code is None and err.code == "TRANSPORT_ERROR"
    assert err.message.startswith("ReadTimeout")


def test_a_connection_error_is_a_transport_error():
    def refuse(_request):
        raise httpx.ConnectError("refused")

    err = _expect_error(refuse)
    assert err.status_code is None and err.message.startswith("ConnectError")


def test_a_server_that_echoes_the_key_cannot_leak_it_through_an_error_message():
    err = _expect_error(lambda r: httpx.Response(400, json={"title": "bad", "detail": f"got {KEY} back"}))
    assert KEY not in err.message
    assert KEY not in str(err)
    assert KEY not in repr(err.__dict__)


def test_a_non_json_success_body_is_an_invalid_response_error():
    client, _ = _client(lambda r: httpx.Response(200, content=b"<html>not json</html>"))
    with pytest.raises(RunPodAPIError) as info:
        client.get_gpu_prices()
    assert info.value.code == "INVALID_RESPONSE"


def test_the_placeholder_key_is_not_accepted_when_it_has_whitespace():
    with pytest.raises(ValueError):
        RunPodClient("TEST KEY WITH SPACES")


def test_the_module_constant_for_the_catalog_path_is_the_v2_relative_path():
    assert rc.GPU_CATALOG_PATH == "catalog/gpus"
