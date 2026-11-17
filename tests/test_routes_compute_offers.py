"""Tests for GET /compute/offers (meridian.routes.compute).

Gate cases use a bare router app, so the hosted route gate of the full app does not answer first. The
end-to-end case uses the full RunPod adapter through httpx.MockTransport. No test reaches the network, and no
test reads a real keyring or runpodctl config: credential references are limited to env: names.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from meridian import compute_catalog as cc
from meridian import compute_options as co
from meridian.research.providers import runpod_catalog as rcat
from meridian.research.providers.runpod_client import RunPodClient

SENTINEL = "SENTINEL-ROUTE-KEY-DO-NOT-LEAK-b13e"
FIXTURES = Path(__file__).parent / "fixtures" / "runpod_catalog"
T0 = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
FORWARDING_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise RuntimeError("network access is disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


@pytest.fixture(autouse=True)
def _self_hosted(monkeypatch):
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _row(**kw) -> co.Offer:
    base = dict(
        id="runpod:NVIDIA H100 80GB HBM3:secure:US-TX-3",
        provider="runpod",
        price_per_hour=2.99,
        vram_gb=80.0,
        ram_gb=None,
        vcpu=None,
        gpu="NVIDIA H100 80GB HBM3",
        available=True,
        price_as_of="2026-11-09T14:00:00Z",
        gpu_model="h100-80gb",
        gpu_variant=None,
        gpu_count=1,
        max_gpu_count=8,
        price_per_gpu_hour=2.99,
        vram_gb_per_gpu=80,
        cloud_type="secure",
        data_center="US-TX-3",
        stock_status="LOW",
    )
    base.update(kw)
    return co.Offer(**base)


def _good_fetch(offers=None, *, partial: bool = False) -> cc.CatalogFetch:
    rows = offers if offers is not None else [
        _row(),
        _row(
            id="runpod:NVIDIA L40S:community:US-NC-1",
            gpu="NVIDIA L40S",
            gpu_model="l40s-48gb",
            price_per_hour=0.79,
            price_per_gpu_hour=0.79,
            vram_gb=48.0,
            vram_gb_per_gpu=48,
            cloud_type="community",
            data_center="US-NC-1",
            stock_status="HIGH",
            available=True,
            max_gpu_count=4,
        ),
    ]
    return cc.CatalogFetch(
        offers=tuple(rows),
        prices_as_of=T0,
        stock_as_of=None if partial else T0,
        partial=partial,
        credential_source="env",
    )


class FakeSource:
    name = "runpod"
    capabilities = cc.OfferCapabilities(has_price=True, capacity_only=False)

    def __init__(self, *results) -> None:
        self._results = list(results)
        self.calls = 0

    def fetch_offers(self) -> cc.CatalogFetch:
        self.calls += 1
        index = min(self.calls, len(self._results)) - 1
        return self._results[index]


@pytest.fixture
def install_cache(monkeypatch):
    """Route the endpoint to a cache over the given source. Returns the cache."""
    from meridian.routes import compute as compute_routes

    def install(source, clock: Clock | None = None) -> cc.OffersCache:
        cache = cc.OffersCache({"runpod": source}, clock=clock or Clock(), ttl_seconds=300, backoff_seconds=60)
        monkeypatch.setattr(compute_routes, "_offers_cache", lambda: cache)
        return cache

    return install


def _client(peer: str = "127.0.0.1"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from meridian.routes import compute as compute_routes

    app = FastAPI()
    app.include_router(compute_routes.router)
    return TestClient(app, client=(peer, 50000))


# --- the documented body ----------------------------------------------------------------------------


def test_loopback_gets_200_with_the_documented_body(install_cache):
    install_cache(FakeSource(_good_fetch()))
    resp = _client().get("/compute/offers")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {
        "ok",
        "status",
        "credential_source",
        "error",
        "providers",
        "offers",
        "count",
        "gpu_models",
        "filters_applied",
    }
    assert body["ok"] is True and body["status"] == "ok"
    assert body["credential_source"] == "env"
    assert body["error"] is None
    assert body["providers"][0]["name"] == "runpod"
    assert body["providers"][0]["prices_as_of"] == "2026-11-09T14:00:00Z"
    assert body["providers"][0]["stock_as_of"] == "2026-11-09T14:00:00Z"
    assert [o["gpu_model"] for o in body["offers"]] == ["l40s-48gb", "h100-80gb"]
    assert body["count"] == 2
    assert {m["gpu_model"] for m in body["gpu_models"]} == {"h100-80gb", "l40s-48gb"}


def test_the_default_filters_are_reported_back(install_cache):
    install_cache(FakeSource(_good_fetch()))
    filters = _client().get("/compute/offers").json()["filters_applied"]
    assert filters == {
        "provider": "runpod",
        "gpu_model": None,
        "cloud_type": None,
        "gpu_count": 1,
        "in_stock_only": False,
        "max_price_per_gpu_hour": None,
        "sort": "price_per_gpu_hour",
        "order": "asc",
    }


def test_filters_and_sort_are_applied_on_the_server(install_cache):
    install_cache(FakeSource(_good_fetch()))
    resp = _client().get(
        "/compute/offers",
        params={
            "gpu_count": "2",
            "sort": "price_per_hour",
            "order": "desc",
            "in_stock_only": "true",
            "cloud_type": "secure",
            "gpu_model": "h100-80gb",
            "max_price_per_gpu_hour": "5",
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert [o["id"] for o in body["offers"]] == ["runpod:NVIDIA H100 80GB HBM3:secure:US-TX-3"]
    assert body["offers"][0]["gpu_count"] == 2
    assert body["offers"][0]["price_per_hour"] == pytest.approx(5.98)
    assert body["offers"][0]["price_per_gpu_hour"] == pytest.approx(2.99)


def test_unknown_stock_is_excluded_by_in_stock_only(install_cache):
    install_cache(FakeSource(_good_fetch([_row(stock_status=None, available=None)])))
    body = _client().get("/compute/offers", params={"in_stock_only": "true"}).json()
    assert body["offers"] == []


def test_the_offer_rows_keep_unknown_fields_as_null(install_cache):
    install_cache(FakeSource(_good_fetch([_row(stock_status=None, available=None, vram_gb_per_gpu=None)])))
    (offer,) = _client().get("/compute/offers").json()["offers"]
    assert offer["stock_status"] is None and offer["available"] is None
    assert offer["vram_gb_per_gpu"] is None and offer["ram_gb"] is None and offer["vcpu"] is None


# --- states ---------------------------------------------------------------------------------------


def test_not_configured_is_a_200_state_with_no_offers_and_a_fixed_message(install_cache):
    not_configured = cc.CatalogFetch(
        offers=(),
        prices_as_of=None,
        stock_as_of=None,
        error=cc.CatalogError("not_configured", cc.ERROR_MESSAGES["not_configured"]),
    )
    install_cache(FakeSource(not_configured))
    resp = _client().get("/compute/offers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["status"] == "not_configured"
    assert body["error"] == {"code": "not_configured", "message": cc.ERROR_MESSAGES["not_configured"]}
    assert body["credential_source"] is None
    assert body["offers"] == []


def test_a_failed_refresh_serves_the_last_good_rows_marked_stale(install_cache):
    clock = Clock()
    error = cc.CatalogFetch(
        offers=(), prices_as_of=None, stock_as_of=None,
        error=cc.CatalogError("upstream_error", cc.ERROR_MESSAGES["upstream_error"]),
    )
    install_cache(FakeSource(_good_fetch(), error), clock=clock)
    first = _client().get("/compute/offers").json()
    assert first["status"] == "ok"
    clock.advance(301)
    stale = _client().get("/compute/offers").json()
    assert stale["status"] == "stale" and stale["ok"] is True
    assert len(stale["offers"]) == 2
    assert stale["error"] == {"code": "upstream_error", "message": cc.ERROR_MESSAGES["upstream_error"]}


def test_a_partial_refresh_is_status_partial_with_unknown_stock_and_a_warning(install_cache):
    partial_rows = [_row(stock_status=None, available=None)]
    fetch = _good_fetch(partial_rows, partial=True)
    fetch = cc.CatalogFetch(
        offers=fetch.offers,
        prices_as_of=T0,
        stock_as_of=None,
        warnings=("Stock is unavailable for the secure cloud: x.",),
        partial=True,
        credential_source="env",
    )
    install_cache(FakeSource(fetch))
    body = _client().get("/compute/offers").json()
    assert body["status"] == "partial" and body["ok"] is True
    assert body["providers"][0]["warnings"] == ["Stock is unavailable for the secure cloud: x."]
    assert body["offers"][0]["stock_status"] is None


# --- invalid and unknown parameters -----------------------------------------------------------------


def test_an_unknown_parameter_such_as_api_key_is_400_and_its_value_is_not_echoed(install_cache):
    install_cache(FakeSource(_good_fetch()))
    resp = _client().get("/compute/offers", params={"api_key": SENTINEL})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "unknown_parameter"
    assert SENTINEL not in resp.text


@pytest.mark.parametrize(
    "params",
    [
        {"sort": "bogus"},
        {"order": "sideways"},
        {"gpu_count": "9"},
        {"in_stock_only": "maybe"},
        {"max_price_per_gpu_hour": "-1"},
        {"provider": "aws"},
    ],
)
def test_an_invalid_filter_value_is_400_invalid_filter(install_cache, params):
    install_cache(FakeSource(_good_fetch()))
    resp = _client().get("/compute/offers", params=params)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_filter"


def test_a_repeated_parameter_is_400_invalid_filter(install_cache):
    install_cache(FakeSource(_good_fetch()))
    resp = _client().get("/compute/offers?gpu_count=1&gpu_count=2")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_filter"


def test_invalid_requests_do_not_call_the_source(install_cache):
    source = FakeSource(_good_fetch())
    install_cache(source)
    _client().get("/compute/offers", params={"sort": "bogus"})
    _client().get("/compute/offers", params={"api_key": SENTINEL})
    assert source.calls == 0


# --- self-hosted loopback gate ----------------------------------------------------------------------


@pytest.mark.parametrize("peer", ["203.0.113.9", "10.0.0.4", "testclient"])
def test_a_non_loopback_caller_gets_403_local_only_and_the_source_is_not_called(install_cache, peer):
    source = FakeSource(_good_fetch())
    install_cache(source)
    resp = _client(peer).get("/compute/offers")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "local_only"
    assert source.calls == 0


@pytest.mark.parametrize("header", FORWARDING_HEADERS)
def test_a_loopback_request_relayed_by_a_proxy_gets_403_local_only(install_cache, header):
    source = FakeSource(_good_fetch())
    install_cache(source)
    resp = _client().get("/compute/offers", headers={header: "203.0.113.9"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "local_only"
    assert source.calls == 0


# --- hosted ------------------------------------------------------------------------------------------


def test_hosted_gets_403_hosted_refused_before_any_credential_or_cache_is_touched(monkeypatch):
    from meridian.routes import compute as compute_routes

    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)

    def must_not_run():
        raise AssertionError("hosted requests must not reach the offers cache")

    monkeypatch.setattr(compute_routes, "_offers_cache", must_not_run)
    resp = _client().get("/compute/offers")
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "hosted_refused"
    assert body["error"]["message"] == "GPU offers are available on local installs only."
    assert body["credential_source"] is None
    assert SENTINEL not in resp.text


def test_the_full_app_mounts_the_route():
    from meridian import server as srv

    paths = {getattr(route, "path", None) for route in srv.app.routes}
    assert "/compute/offers" in paths


def test_the_full_app_hosted_gate_denies_the_offers_route_to_anonymous_callers(client, monkeypatch):
    # The conftest client runs the app lifespan, which the hosted gate needs for its state.
    monkeypatch.setenv("MERIDIAN_HOSTED", "1")
    resp = client.get("/compute/offers")
    assert resp.status_code in (401, 403)
    assert "gpu_models" not in resp.text


# --- end to end through the real adapter -----------------------------------------------------------


def _mock_handler(seen: list[httpx.Request]):
    prices = json.loads((FIXTURES / "synthetic_gpus_prices.json").read_text(encoding="utf-8"))
    secure = json.loads((FIXTURES / "synthetic_gpus_availability_secure.json").read_text(encoding="utf-8"))
    community = json.loads((FIXTURES / "synthetic_gpus_availability_community.json").read_text(encoding="utf-8"))

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = dict(request.url.params)
        if "include" not in params:
            return httpx.Response(200, json=prices)
        return httpx.Response(200, json=secure if params.get("cloud") == "SECURE" else community)

    return handle


def test_the_route_through_the_real_adapter_sends_the_key_only_as_a_bearer_header(monkeypatch, install_cache):
    from meridian.routes import compute as compute_routes

    monkeypatch.setenv("RUNPOD_API_KEY", SENTINEL)
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(_mock_handler(seen))

    def factory(key: str) -> RunPodClient:
        return RunPodClient(key, transport=transport)

    source = rcat.RunPodOffersSource(
        credential_refs=("env:RUNPOD_API_KEY",),
        client_factory=factory,
        clock=lambda: T0,
        monotonic=lambda: 0.0,
    )
    cache = cc.OffersCache({"runpod": source}, clock=lambda: T0)
    monkeypatch.setattr(compute_routes, "_offers_cache", lambda: cache)

    resp = _client().get("/compute/offers", params={"gpu_model": "rtx-4090-24gb"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["credential_source"] == "env"
    assert [o["gpu_model"] for o in body["offers"]] == ["rtx-4090-24gb", "rtx-4090-24gb"]
    assert len(seen) == 3
    assert all(r.headers["Authorization"] == f"Bearer {SENTINEL}" for r in seen)
    assert all(SENTINEL not in str(r.url) for r in seen)
    assert SENTINEL not in resp.text


def test_the_default_cache_reads_the_runpod_key_from_the_environment_only(monkeypatch):
    from meridian.routes import compute as compute_routes

    monkeypatch.setattr(compute_routes, "_offers_cache_instance", None)
    cache = compute_routes._offers_cache()
    assert cache.provider_names() == ("runpod",)
    assert cache._sources["runpod"]._refs == ("env:RUNPOD_API_KEY",)
    assert compute_routes.OFFERS_CREDENTIAL_REFS == ("env:RUNPOD_API_KEY",)
    assert compute_routes._offers_cache() is cache


def test_a_cache_built_by_the_default_factory_makes_no_call_until_it_is_asked(monkeypatch):
    from meridian.routes import compute as compute_routes

    monkeypatch.setattr(compute_routes, "_offers_cache_instance", None)
    cache = compute_routes._offers_cache()
    assert cache._states["runpod"].last_good is None
    assert cache._states["runpod"].checked_at is None


def test_the_offers_payload_has_the_same_keys_as_a_success_body():
    from meridian.routes import compute as compute_routes

    refused = compute_routes._offers_payload(None, {"code": "local_only", "message": "x"})
    assert {"ok", "status", "credential_source", "error", "providers", "offers", "count", "gpu_models"} <= set(refused)
    assert refused["ok"] is False and refused["status"] == "error"


def test_the_offers_view_contains_no_datetime_objects_so_it_serialises():
    view = cc.build_offers_view(
        [cc.ProviderSnapshot("runpod", "ok", _good_fetch(), None, T0, "env", False)], cc.OffersQuery()
    )
    json.dumps(view)  # raises if a datetime or other non-JSON value slipped in
