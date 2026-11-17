"""Tests for the RunPod offer adapter (meridian.research.providers.runpod_catalog).

The end-to-end cases drive a real RunPodClient through httpx.MockTransport, using the synthetic fixtures under
tests/fixtures/runpod_catalog/. Those fixtures follow the documented schema and were not recorded from RunPod, so
the tests check the mapping, not the live wire shape. Nothing reaches the network.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from meridian import compute_catalog as cc
from meridian.research.providers import runpod_catalog as rcat
from meridian.research.providers.runpod_client import RunPodAPIError, RunPodClient

KEY = "TEST-KEY-DO-NOT-LEAK-a91c"
FIXTURES = Path(__file__).parent / "fixtures" / "runpod_catalog"
STAMP = datetime(2026, 11, 9, 14, 3, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise RuntimeError("network access is disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


@pytest.fixture(autouse=True)
def _env_key(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", KEY)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Clock:
    def __init__(self, start: datetime = STAMP) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _handler(
    *,
    prices=None,
    secure=None,
    community=None,
    price_status: int = 200,
    secure_status: int = 200,
    community_status: int = 200,
    price_headers=None,
    seen: list | None = None,
):
    """A MockTransport handler: the price call (no include) and the two availability calls (include)."""
    prices = _fixture("synthetic_gpus_prices.json") if prices is None else prices
    secure = _fixture("synthetic_gpus_availability_secure.json") if secure is None else secure
    community = _fixture("synthetic_gpus_availability_community.json") if community is None else community

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        params = dict(request.url.params)
        if "include" not in params:
            return _respond(price_status, prices, price_headers)
        if params.get("cloud") == "SECURE":
            return _respond(secure_status, secure)
        return _respond(community_status, community)

    return handle


def _respond(status: int, body, headers=None) -> httpx.Response:
    if status == 200:
        return httpx.Response(200, json=body)
    return httpx.Response(status, json=body if body is not None else {"detail": "x"}, headers=headers)


def _source(handler, *, clock=None, monotonic=None, deadline=25.0):
    def factory(api_key: str) -> RunPodClient:
        return RunPodClient(api_key, transport=httpx.MockTransport(handler))

    return rcat.RunPodOffersSource(
        credential_refs=("env:RUNPOD_API_KEY",),
        client_factory=factory,
        clock=clock or Clock(),
        monotonic=monotonic or (lambda: 0.0),
        deadline_seconds=deadline,
    )


def _rows(fetch) -> dict[str, object]:
    return {offer.id: offer for offer in fetch.offers}


# --- end to end: fixtures to base rows --------------------------------------------------------------


def test_a_full_refresh_gives_base_rows_one_per_data_center_or_aggregate():
    fetch = _source(_handler()).fetch_offers()
    assert fetch.error is None and fetch.partial is False
    rows = _rows(fetch)

    # H100 80GB HBM3 in secure: two data centers from the availability response.
    us = rows["runpod:NVIDIA H100 80GB HBM3:secure:US-TX-3"]
    eu = rows["runpod:NVIDIA H100 80GB HBM3:secure:EU-RO-1"]
    assert us.available is True and us.stock_status == "LOW"
    assert eu.available is False and eu.stock_status == "NONE"

    # H100 PCIe has no data centers in the availability response, so it gets one aggregate row.
    pcie = rows["runpod:NVIDIA H100 PCIe:secure:-"]
    assert pcie.data_center is None and pcie.stock_status == "LOW"

    # GeForce RTX 4090 is community-only: its secure flag is false, so no secure row.
    assert not any("RTX 4090" in key and ":secure:" in key for key in rows)
    assert rows["runpod:NVIDIA GeForce RTX 4090:community:CA-MTL-1"].stock_status == "HIGH"
    assert rows["runpod:NVIDIA GeForce RTX 4090:community:US-OR-1"].stock_status == "LOW"


def test_per_gpu_price_is_the_machine_price_at_one_gpu_and_the_vram_is_the_memory_field():
    rows = _rows(_source(_handler()).fetch_offers())
    row = rows["runpod:NVIDIA H100 80GB HBM3:secure:US-TX-3"]
    assert row.price_per_gpu_hour == pytest.approx(2.99)
    assert row.price_per_hour == pytest.approx(2.99)
    assert row.gpu_count == 1
    assert row.vram_gb_per_gpu == 80 and row.vram_gb == pytest.approx(80.0)
    assert row.price_as_of == "2026-11-09T14:03:00Z"


def test_gpu_model_variant_and_cloud_type_come_from_the_name_and_the_cloud():
    rows = _rows(_source(_handler()).fetch_offers())
    assert rows["runpod:NVIDIA H100 80GB HBM3:secure:US-TX-3"].gpu_model == "h100-80gb"
    assert rows["runpod:NVIDIA H100 PCIe:secure:-"].gpu_model == "h100-80gb-pcie"
    assert rows["runpod:NVIDIA H100 PCIe:secure:-"].gpu_variant == "pcie"
    assert rows["runpod:NVIDIA A100-SXM4-80GB:secure:US-GA-1"].gpu_model == "a100-80gb-sxm"
    assert rows["runpod:NVIDIA GeForce RTX 4090:community:CA-MTL-1"].gpu_model == "rtx-4090-24gb"
    assert rows["runpod:NVIDIA L40S:community:US-NC-1"].cloud_type == "community"


def test_the_pcie_and_sxm_variants_are_separate_models():
    rows = _rows(_source(_handler()).fetch_offers())
    pcie = rows["runpod:NVIDIA H100 PCIe:secure:-"].gpu_model
    sxm = rows["runpod:NVIDIA A100-SXM4-80GB:secure:US-GA-1"].gpu_model
    assert pcie != sxm and "pcie" in pcie and "sxm" in sxm


def test_max_gpu_count_is_read_from_the_integer_maxcount():
    rows = _rows(_source(_handler()).fetch_offers())
    assert rows["runpod:NVIDIA H100 PCIe:secure:-"].max_gpu_count == 4
    assert rows["runpod:NVIDIA L40S:community:US-NC-1"].max_gpu_count == 4


def test_ram_and_vcpu_stay_unknown_until_the_live_read_confirms_their_units():
    for row in _source(_handler()).fetch_offers().offers:
        assert row.ram_gb is None and row.vcpu is None


def test_serverless_prices_are_never_rows():
    fetch = _source(_handler()).fetch_offers()
    assert all(row.cloud_type in ("secure", "community") for row in fetch.offers)


def test_the_stamps_are_the_fetch_times_and_the_stock_stamp_is_the_earliest_successful_stock_call():
    clock_values = iter(
        [
            datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc),  # prices
            datetime(2026, 11, 9, 14, 1, tzinfo=timezone.utc),  # secure stock
            datetime(2026, 11, 9, 14, 2, tzinfo=timezone.utc),  # community stock
        ]
    )
    source = _source(_handler(), clock=lambda: next(clock_values))
    fetch = source.fetch_offers()
    assert fetch.prices_as_of == datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)
    assert fetch.stock_as_of == datetime(2026, 11, 9, 14, 1, tzinfo=timezone.utc)


def test_the_request_sequence_is_one_price_call_then_one_availability_call_per_cloud():
    seen: list = []
    _source(_handler(seen=seen)).fetch_offers()
    assert len(seen) == 3
    assert "include" not in dict(seen[0].url.params)
    assert [dict(r.url.params).get("cloud") for r in seen[1:]] == ["SECURE", "COMMUNITY"]


def test_the_key_is_not_in_any_url_or_in_the_fetch_result():
    seen: list = []
    fetch = _source(_handler(seen=seen)).fetch_offers()
    assert all(KEY not in str(r.url) for r in seen)
    assert KEY not in repr(fetch)
    assert fetch.credential_source == "env"


# --- prices that cannot be used -----------------------------------------------------------------------


def test_zero_negative_and_non_numeric_prices_become_none_with_a_counted_warning():
    prices = {
        "gpus": [
            {
                "id": "NVIDIA L40S",
                "name": "L40S",
                "memory": 48,
                "secure": True,
                "community": True,
                "price": {"secure": 0, "community": "1.20"},
            },
            {
                "id": "NVIDIA RTX A6000",
                "name": "RTX A6000",
                "memory": 48,
                "secure": True,
                "community": False,
                "price": {"secure": -2.5},
            },
        ]
    }
    fetch = _source(_handler(prices=prices)).fetch_offers()
    assert fetch.error is None
    assert all(row.price_per_gpu_hour is None and row.price_per_hour is None for row in fetch.offers)
    assert any("3 price value(s)" in warning for warning in fetch.warnings)


def test_a_missing_price_is_unknown_without_a_warning():
    # The companion GPU carries the documented price keys, so this response is not the renamed-field case.
    companion = {"id": "NVIDIA T4", "memory": 16, "secure": False, "community": False, "price": {"secure": None}}
    prices = {
        "gpus": [
            {"id": "NVIDIA L40S", "memory": 48, "secure": True, "community": None, "price": {}},
            companion,
        ]
    }
    fetch = _source(_handler(prices=prices)).fetch_offers()
    (row,) = [r for r in fetch.offers if r.data_center is None]
    assert row.price_per_gpu_hour is None
    assert not any("price value" in w for w in fetch.warnings)


# --- shape ------------------------------------------------------------------------------------------


def test_a_price_response_with_no_gpus_list_is_unexpected_shape_with_no_offers():
    fetch = _source(_handler(prices=_fixture("synthetic_gpus_unexpected_shape.json"))).fetch_offers()
    assert fetch.error is not None and fetch.error.code == "unexpected_shape"
    assert fetch.offers == ()
    assert fetch.prices_as_of is None


def test_a_gpu_entry_with_no_string_id_fails_the_whole_price_response():
    prices = {"gpus": [{"id": "NVIDIA L40S"}, {"name": "no id here"}]}
    fetch = _source(_handler(prices=prices)).fetch_offers()
    assert fetch.error is not None and fetch.error.code == "unexpected_shape"


@pytest.mark.parametrize(
    "bad_secure",
    [
        {"gpus": "not a list"},
        {"gpus": [{"id": "NVIDIA L40S", "availability": "LOW", "dataCenters": "nope"}]},
        {"gpus": [{"id": "NVIDIA L40S", "availability": "LOW", "dataCenters": [{"availability": "LOW"}]}]},
    ],
)
def test_a_malformed_availability_response_only_makes_that_cloud_unknown(bad_secure):
    fetch = _source(_handler(secure=bad_secure)).fetch_offers()
    assert fetch.error is None and fetch.partial is True
    secure_rows = [r for r in fetch.offers if r.cloud_type == "secure"]
    assert secure_rows and all(r.stock_status is None and r.available is None for r in secure_rows)
    assert any("secure cloud" in w and "not recognised" in w for w in fetch.warnings)
    community_rows = [r for r in fetch.offers if r.cloud_type == "community"]
    assert any(r.stock_status is not None for r in community_rows)


def test_the_price_response_is_not_partly_read_when_one_entry_is_malformed():
    prices = {"gpus": [{"id": "NVIDIA L40S", "price": "not an object"}]}
    fetch = _source(_handler(prices=prices)).fetch_offers()
    assert fetch.error is not None and fetch.error.code == "unexpected_shape"
    assert fetch.offers == ()


def test_renamed_price_keys_fail_the_whole_refresh_instead_of_showing_blank_prices():
    prices = {
        "gpus": [
            {
                "id": "NVIDIA H100 80GB HBM3",
                "name": "H100 80GB HBM3",
                "memory": 80,
                "secure": True,
                "community": False,
                "maxCount": 8,
                "price": {"secureCloud": 2.99, "communityCloud": None},
            }
        ]
    }
    fetch = _source(_handler(prices=prices)).fetch_offers()
    assert fetch.error is not None and fetch.error.code == "unexpected_shape"
    assert fetch.offers == () and fetch.prices_as_of is None


def test_a_price_response_with_no_gpus_is_empty_and_not_a_shape_error():
    assert rcat.parse_prices({"gpus": []}) == []
    assert rcat.parse_availability({"gpus": []}) == {}


def test_a_null_price_key_counts_as_documented_so_the_response_is_read():
    (gpu,) = rcat.parse_prices({"gpus": [{"id": "A", "price": {"secure": None, "community": None}}]})
    assert gpu.prices == {"secure": None, "community": None}
    assert gpu.invalid_prices == {"secure": False, "community": False}


@pytest.mark.parametrize(
    "renamed",
    [
        # The stock key is renamed on the GPU entry and on its data center.
        {
            "gpus": [
                {
                    "id": "NVIDIA H100 80GB HBM3",
                    "stockStatus": "HIGH",
                    "dataCenters": [{"id": "US-TX-3", "stockStatus": "HIGH"}],
                }
            ]
        },
        # The stock key is renamed on every GPU, and the first GPU has no data centers.
        {"gpus": [{"id": "NVIDIA H100 80GB HBM3", "dataCenters": []}, {"id": "NVIDIA L40S", "stockStatus": "LOW"}]},
    ],
)
def test_renamed_stock_keys_make_only_that_cloud_unknown(renamed):
    fetch = _source(_handler(secure=renamed)).fetch_offers()
    assert fetch.error is None and fetch.partial is True
    secure = [r for r in fetch.offers if r.cloud_type == "secure"]
    assert secure and all(r.stock_status is None and r.available is None for r in secure)
    assert any("secure cloud" in w and "not recognised" in w for w in fetch.warnings)
    assert any(r.stock_status is not None for r in fetch.offers if r.cloud_type == "community")


# --- availability failures (partial) ----------------------------------------------------------------


def test_an_availability_error_gives_partial_with_unknown_stock_for_that_cloud_only():
    fetch = _source(_handler(secure_status=500)).fetch_offers()
    assert fetch.error is None and fetch.partial is True
    secure = [r for r in fetch.offers if r.cloud_type == "secure"]
    community = [r for r in fetch.offers if r.cloud_type == "community"]
    assert secure and all(r.stock_status is None and r.available is None and r.data_center is None for r in secure)
    assert any(r.stock_status is not None for r in community)
    assert any(w.startswith("Stock is unavailable for the secure cloud") for w in fetch.warnings)


def test_when_every_availability_call_fails_every_row_has_unknown_stock_and_no_row_says_none():
    fetch = _source(_handler(secure_status=503, community_status=429)).fetch_offers()
    assert fetch.partial is True and fetch.stock_as_of is None
    assert fetch.offers and all(r.stock_status is None and r.available is None for r in fetch.offers)


def test_a_rate_limited_availability_call_names_the_fixed_message_and_not_the_provider_text():
    handler = _handler(community_status=429)
    fetch = _source(handler).fetch_offers()
    text = " ".join(fetch.warnings)
    assert "rate-limited" in text and "HTTP 429" in text


def test_the_deadline_skips_a_stock_call_that_has_not_started_in_time():
    # Clock reads: the start of the refresh, the check before the secure stock call, then before community.
    ticks = iter([0.0, 0.0, 30.0])

    def monotonic() -> float:
        return next(ticks)

    seen: list = []
    fetch = _source(_handler(seen=seen), monotonic=monotonic, deadline=25.0).fetch_offers()
    assert fetch.partial is True
    assert len(seen) == 2  # the price call and the secure stock call; community was skipped
    assert any("community cloud was not refreshed before the deadline" in w for w in fetch.warnings)
    community = [r for r in fetch.offers if r.cloud_type == "community"]
    secure = [r for r in fetch.offers if r.cloud_type == "secure"]
    assert community and all(r.stock_status is None for r in community)
    assert any(r.stock_status is not None for r in secure)


def test_the_deadline_skips_both_stock_calls_when_the_price_call_used_it_all():
    ticks = iter([0.0, 40.0, 40.0])

    def monotonic() -> float:
        return next(ticks)

    seen: list = []
    fetch = _source(_handler(seen=seen), monotonic=monotonic, deadline=25.0).fetch_offers()
    assert len(seen) == 1  # only the price call was sent
    assert fetch.partial is True and all(r.stock_status is None for r in fetch.offers)
    assert len([w for w in fetch.warnings if "before the deadline" in w]) == 2


# --- price call failures ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "auth_failed"), (403, "forbidden"), (500, "upstream_error"), (404, "upstream_error")],
)
def test_a_price_status_maps_to_its_fixed_code_and_no_stock_call_is_made(status, code):
    seen: list = []
    fetch = _source(_handler(price_status=status, seen=seen)).fetch_offers()
    assert fetch.error is not None and fetch.error.code == code
    assert fetch.offers == () and fetch.partial is False
    assert len(seen) == 1  # the availability calls are never reached


def test_a_rate_limited_price_call_carries_retry_after_seconds():
    fetch = _source(
        _handler(price_status=429, price_headers={"Retry-After": "45"}, prices={"detail": "slow"})
    ).fetch_offers()
    assert fetch.error.code == "rate_limited" and fetch.error.retry_after_s == 45


def test_a_retry_after_larger_than_the_cap_is_capped():
    fetch = _source(
        _handler(price_status=429, price_headers={"Retry-After": "999999"}, prices={"detail": "slow"})
    ).fetch_offers()
    assert fetch.error.retry_after_s == rcat._MAX_RETRY_AFTER_S


def test_a_read_timeout_on_the_price_call_is_a_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    fetch = _source(handler).fetch_offers()
    assert fetch.error.code == "timeout"


def test_a_connection_error_on_the_price_call_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    fetch = _source(handler).fetch_offers()
    assert fetch.error.code == "unreachable"


def test_a_server_echoing_the_key_in_its_error_never_reaches_the_fetch():
    fetch = _source(
        _handler(price_status=401, prices={"title": "unauthorized", "detail": f"key {KEY} rejected"})
    ).fetch_offers()
    assert fetch.error.code == "auth_failed"
    assert KEY not in fetch.error.message and KEY not in repr(fetch)


# --- credentials ------------------------------------------------------------------------------------


def test_with_no_key_configured_the_state_is_not_configured_and_no_http_call_is_made(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    seen: list = []
    fetch = _source(_handler(seen=seen)).fetch_offers()
    assert fetch.error is not None and fetch.error.code == "not_configured"
    assert fetch.error.message == cc.ERROR_MESSAGES["not_configured"]
    assert seen == []
    assert fetch.credential_source is None


def test_an_unusable_key_is_not_configured_and_is_not_sent(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "has spaces in it")
    seen: list = []
    fetch = _source(_handler(seen=seen)).fetch_offers()
    assert fetch.error.code == "not_configured"
    assert seen == []
    assert "has spaces" not in repr(fetch)


def test_an_unsupported_reference_is_skipped_without_echoing_it(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", KEY)
    kind, secret = rcat.resolve_secret(("pasted-secret-value", "env:RUNPOD_API_KEY"), hosted=False)
    assert kind == "env" and secret == KEY


def test_resolution_returns_no_kind_and_no_secret_when_nothing_resolves(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    assert rcat.resolve_secret(("env:RUNPOD_API_KEY",), hosted=False) == (None, None)


def test_a_hosted_call_refuses_env_references_before_reading_anything(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", KEY)
    assert rcat.resolve_secret(("env:RUNPOD_API_KEY",), hosted=True) == (None, None)


# --- error mapping table ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RunPodAPIError(None, "INVALID_RESPONSE", "x"), "unexpected_shape"),
        (RunPodAPIError(None, "TRANSPORT_ERROR", "ReadTimeout: timed out"), "timeout"),
        (RunPodAPIError(None, "TRANSPORT_ERROR", "ConnectTimeout: x"), "timeout"),
        (RunPodAPIError(None, "TRANSPORT_ERROR", "ConnectError: refused"), "unreachable"),
        (RunPodAPIError(401, "unauthorized", "x"), "auth_failed"),
        (RunPodAPIError(403, "forbidden", "x"), "forbidden"),
        (RunPodAPIError(429, "rate", "x", retry_after=12.7), "rate_limited"),
        (RunPodAPIError(502, "bad gateway", "x"), "upstream_error"),
    ],
)
def test_every_client_error_maps_to_a_fixed_catalog_code(error, code):
    mapped = rcat.catalog_error_from(error)
    assert mapped.code == code
    assert mapped.message == cc.ERROR_MESSAGES[code]


def test_a_429_keeps_the_whole_seconds_of_retry_after():
    assert rcat.catalog_error_from(RunPodAPIError(429, "rate", "x", retry_after=12.7)).retry_after_s == 12


# --- parsing helpers --------------------------------------------------------------------------------


def test_stock_values_are_normalised_to_the_enum_or_unknown():
    body = {"gpus": [{"id": "A", "availability": "low"}, {"id": "B", "availability": "SOLD OUT"}]}
    entries = rcat.parse_availability(body)
    assert entries["A"][0] == "LOW"
    assert entries["B"][0] is None


def test_a_non_boolean_flag_and_an_unnamed_gpu_are_handled_without_crashing():
    body = {
        "gpus": [
            {"id": "A", "secure": "yes", "community": None, "memory": "80", "name": "", "price": {"secure": None}}
        ]
    }
    (gpu,) = rcat.parse_prices(body)
    assert gpu.flags["secure"] is None and gpu.flags["community"] is None
    assert gpu.memory_gb is None and gpu.name is None


def test_max_count_accepts_an_integer_or_a_per_cloud_object_and_otherwise_is_unknown():
    assert rcat._max_count(8, "secure") == 8
    assert rcat._max_count({"secure": 4, "community": 2}, "community") == 2
    assert rcat._max_count({"SECURE": 3}, "secure") == 3
    assert rcat._max_count("eight", "secure") is None
    assert rcat._max_count(True, "secure") is None
    assert rcat._max_count({"community": 2}, "secure") is None


def test_build_offers_never_emits_the_same_row_id_twice():
    gpu = rcat.RunPodGpu(
        gpu_id="NVIDIA L40S",
        name="L40S",
        memory_gb=48,
        flags={"secure": True, "community": None},
        prices={"secure": 0.8, "community": None},
        invalid_prices={"secure": False, "community": False},
        max_counts={"secure": 4, "community": None},
    )
    offers, _ = rcat.build_offers([gpu, gpu], {"secure": None, "community": None}, STAMP)
    assert len(offers) == len({o.id for o in offers}) == 1


def test_a_gpu_missing_from_the_availability_response_has_unknown_stock():
    gpu = rcat.RunPodGpu(
        gpu_id="NVIDIA L40S",
        name="L40S",
        memory_gb=48,
        flags={"secure": True, "community": False},
        prices={"secure": 0.8, "community": None},
        invalid_prices={"secure": False, "community": False},
        max_counts={"secure": None, "community": None},
    )
    offers, _ = rcat.build_offers([gpu], {"secure": {}}, STAMP)
    assert offers[0].stock_status is None and offers[0].available is None
