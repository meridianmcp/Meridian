"""RunPod GPU catalog client (meridian_desktop/gpu_catalog.py). No socket opens: a fake transport answers every request.

The sample reply has the field names RunPod's v2 catalog returned when it was read with include=AVAILABILITY. The key
in these tests is a made-up string, and the tests check that it appears only in the Authorization header.
"""

import json
import urllib.error
import urllib.request

import pytest

from meridian_desktop import gpu_catalog
from meridian_desktop.gpu_catalog import (
    BAD_SHAPE_MESSAGE,
    NO_KEY_MESSAGE,
    TOO_LARGE_MESSAGE,
    UNREACHABLE_MESSAGE,
    CATALOG_QUERY,
    CATALOG_URL,
    DataCenterStock,
    GpuCatalogError,
    fetch_catalog,
    parse_catalog,
)

SECRET_KEY = "rp_test_NOT_A_REAL_KEY_123"

SAMPLE = {
    "gpus": [
        {
            "id": "NVIDIA A100 80GB PCIe",
            "name": "A100 PCIe",
            "manufacturer": "NVIDIA",
            "memory": 80,
            "availability": "LOW",
            "community": True,
            "secure": True,
            "maxCount": {"community": 6, "secure": 8},
            "price": {"community": 1.19, "secure": 2.09, "serverless": 2.99},
            "dataCenters": [{"id": "CA-MTL-3", "name": "CA-MTL-3", "availability": "LOW"}],
            "cudaVersions": [{"version": "13.2", "available": True}],
        },
        {
            "id": "AMD Instinct MI300X OAM",
            "name": "MI300X",
            "manufacturer": "AMD",
            "memory": 192,
            "availability": "NONE",
            "community": False,
            "secure": True,
            "maxCount": {"community": 0, "secure": 8},
            "price": {"community": 0.5, "secure": 2.39},
        },
        "not an object",
        {"name": "no id, so skipped"},
    ]
}


class _Reply:
    """Stands in for the transport. It records each request and answers with a fixed status and body."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else json.dumps(SAMPLE).encode("utf-8")
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        return self.status, self.body


def test_parse_reads_each_gpu_with_its_stock_prices_and_data_centers():
    first, second = parse_catalog(SAMPLE)
    assert [offer.gpu_id for offer in (first, second)] == ["NVIDIA A100 80GB PCIe", "AMD Instinct MI300X OAM"]
    assert first.name == "A100 PCIe"
    assert first.memory_gb == 80
    assert first.availability == "LOW"
    assert first.price_secure == 2.09
    assert first.price_community == 1.19
    assert first.max_count_secure == 8
    assert first.data_centers == (DataCenterStock(id="CA-MTL-3", availability="LOW"),)
    assert second.data_centers == ()
    assert second.price_secure == 2.39


def test_a_missing_price_or_count_is_none_not_zero():
    offers = parse_catalog({"gpus": [{"id": "X", "availability": "NONE"}]})
    assert offers[0].price_secure is None
    assert offers[0].max_count_secure is None
    assert offers[0].memory_gb is None


@pytest.mark.parametrize("payload", [{"other": 1}, ["a", "list"], {"gpus": "not-a-list"}])
def test_a_reply_without_a_gpu_list_is_refused_with_fixed_text(payload):
    with pytest.raises(GpuCatalogError) as caught:
        parse_catalog(payload)
    assert str(caught.value) == BAD_SHAPE_MESSAGE


def test_the_request_goes_to_runpod_over_https_with_the_key_only_in_the_authorization_header():
    reply = _Reply()
    offers = fetch_catalog(SECRET_KEY, reply)
    assert len(offers) == 2
    (request, timeout), = reply.calls
    assert request.full_url == f"{CATALOG_URL}?{CATALOG_QUERY}"
    assert request.full_url.startswith("https://api.runpod.io/")
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {SECRET_KEY}"
    assert SECRET_KEY not in request.full_url
    assert timeout == gpu_catalog.TIMEOUT_SECONDS


@pytest.mark.parametrize("key", [None, ""])
def test_no_saved_key_means_no_request_at_all(key):
    reply = _Reply()
    with pytest.raises(GpuCatalogError) as caught:
        fetch_catalog(key, reply)
    assert str(caught.value) == NO_KEY_MESSAGE
    assert reply.calls == []


def test_a_refused_request_names_the_status_and_never_the_reply_body():
    reply = _Reply(status=401, body=b"secret-body-text")
    with pytest.raises(GpuCatalogError) as caught:
        fetch_catalog(SECRET_KEY, reply)
    assert "HTTP 401" in str(caught.value)
    assert "secret-body-text" not in str(caught.value)
    assert SECRET_KEY not in str(caught.value)


@pytest.mark.parametrize("failure", [urllib.error.URLError("refused " + SECRET_KEY), TimeoutError("slow " + SECRET_KEY)])
def test_a_network_failure_gives_the_fixed_unreachable_text_and_no_key(failure):
    def boom(request, timeout):
        raise failure

    with pytest.raises(GpuCatalogError) as caught:
        fetch_catalog(SECRET_KEY, boom)
    assert str(caught.value) == UNREACHABLE_MESSAGE
    assert SECRET_KEY not in str(caught.value)


def test_an_oversized_reply_is_refused():
    reply = _Reply(body=b"x" * (gpu_catalog.MAX_REPLY_BYTES + 1))
    with pytest.raises(GpuCatalogError) as caught:
        fetch_catalog(SECRET_KEY, reply)
    assert str(caught.value) == TOO_LARGE_MESSAGE


def test_a_reply_that_is_not_json_is_refused_without_echoing_it():
    reply = _Reply(body=b"<html>secret page</html>")
    with pytest.raises(GpuCatalogError) as caught:
        fetch_catalog(SECRET_KEY, reply)
    assert str(caught.value) == BAD_SHAPE_MESSAGE
    assert "secret page" not in str(caught.value)


def test_redirects_are_refused_so_the_key_never_reaches_another_host():
    handler = gpu_catalog._NoRedirect()
    request = urllib.request.Request(CATALOG_URL)
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.example/") is None
