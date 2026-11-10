"""Tests for the in-process offers cache in meridian.compute_catalog.

The clock is injected, so the TTL, the backoff and Retry-After are exact. Sources are fakes that count their
calls. No network is used: DNS and outbound connections are refused for every test in this module.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from meridian import compute_catalog as cc
from meridian import compute_options as co

SENTINEL = "SENTINEL-CACHE-KEY-DO-NOT-LEAK-91af"
T0 = datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise RuntimeError("network access is disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeSource:
    """Returns the queued results in order, then repeats the last one. Counts every call."""

    name = "runpod"
    capabilities = cc.OfferCapabilities(has_price=True, capacity_only=False)

    def __init__(self, *results, delay: float = 0.0) -> None:
        self._results = list(results)
        self._lock = threading.Lock()
        self.calls = 0
        self.delay = delay

    def fetch_offers(self) -> cc.CatalogFetch:
        with self._lock:
            self.calls += 1
            index = min(self.calls, len(self._results)) - 1
            result = self._results[index]
        if self.delay:
            time.sleep(self.delay)
        if isinstance(result, BaseException):
            raise result
        return result


def _good(*, partial: bool = False, offers=None) -> cc.CatalogFetch:
    row = co.Offer(id="runpod:gpu:secure:-", provider="runpod", price_per_hour=2.0, price_per_gpu_hour=2.0)
    return cc.CatalogFetch(
        offers=tuple(offers if offers is not None else [row]),
        prices_as_of=T0,
        stock_as_of=None if partial else T0,
        partial=partial,
        credential_source="env",
    )


def _fail(code: str, retry_after_s: int | None = None) -> cc.CatalogFetch:
    error = cc.CatalogError(code, cc.ERROR_MESSAGES[code], retry_after_s)
    return cc.CatalogFetch(offers=(), prices_as_of=None, stock_as_of=None, error=error)


def _cache(source, clock, **kwargs) -> cc.OffersCache:
    return cc.OffersCache({"runpod": source}, clock=clock, **kwargs)


# --- TTL --------------------------------------------------------------------------------------------


def test_inside_the_ttl_the_cached_result_is_served_with_no_upstream_call():
    clock = Clock()
    source = FakeSource(_good())
    cache = _cache(source, clock, ttl_seconds=300)
    first = cache.get("runpod")
    clock.advance(299)
    second = cache.get("runpod")
    assert source.calls == 1
    assert first.status == "ok" and second.status == "ok"
    assert second.from_cache is True and first.from_cache is False


def test_after_the_ttl_the_source_is_called_again():
    clock = Clock()
    source = FakeSource(_good())
    cache = _cache(source, clock, ttl_seconds=300)
    cache.get("runpod")
    clock.advance(300)
    snap = cache.get("runpod")
    assert source.calls == 2
    assert snap.from_cache is False


def test_a_partial_fetch_is_reported_as_partial_status():
    source = FakeSource(_good(partial=True))
    snap = _cache(source, Clock()).get("runpod")
    assert snap.status == "partial"
    assert snap.fetch is not None and snap.fetch.partial is True


def test_the_snapshot_carries_the_checked_time_and_the_credential_kind():
    clock = Clock()
    snap = _cache(FakeSource(_good()), clock).get("runpod")
    assert snap.checked_at == T0
    assert snap.credential_source == "env"
    assert snap.offers and snap.offers[0].id == "runpod:gpu:secure:-"


# --- single flight ----------------------------------------------------------------------------------


def test_concurrent_callers_share_one_upstream_fetch():
    clock = Clock()
    source = FakeSource(_good(), delay=0.2)
    cache = _cache(source, clock, ttl_seconds=300)
    barrier = threading.Barrier(8)
    results: list[cc.ProviderSnapshot] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        snap = cache.get("runpod")
        with results_lock:
            results.append(snap)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert source.calls == 1
    assert len(results) == 8
    assert all(snap.status == "ok" for snap in results)


# --- stale, backoff and Retry-After -----------------------------------------------------------------


def test_a_failed_refresh_keeps_the_last_good_data_and_marks_it_stale():
    clock = Clock()
    source = FakeSource(_good(), _fail("upstream_error"))
    cache = _cache(source, clock, ttl_seconds=300, backoff_seconds=60)
    cache.get("runpod")
    clock.advance(300)
    snap = cache.get("runpod")
    assert snap.status == "stale"
    assert snap.error is not None and snap.error.code == "upstream_error"
    assert snap.offers and snap.offers[0].provider == "runpod"


def test_no_upstream_call_is_made_during_the_backoff_after_a_failure():
    clock = Clock()
    source = FakeSource(_good(), _fail("timeout"))
    cache = _cache(source, clock, ttl_seconds=300, backoff_seconds=60)
    cache.get("runpod")
    clock.advance(300)
    cache.get("runpod")
    assert source.calls == 2
    clock.advance(59)
    stale = cache.get("runpod")
    assert source.calls == 2
    assert stale.status == "stale"


def test_the_backoff_ends_and_the_source_is_tried_again():
    clock = Clock()
    source = FakeSource(_good(), _fail("timeout"), _good())
    cache = _cache(source, clock, ttl_seconds=300, backoff_seconds=60)
    cache.get("runpod")
    clock.advance(300)
    cache.get("runpod")
    clock.advance(60)
    recovered = cache.get("runpod")
    assert source.calls == 3
    assert recovered.status == "ok"
    assert recovered.error is None


def test_retry_after_is_respected_when_it_is_longer_than_the_backoff():
    clock = Clock()
    source = FakeSource(_fail("rate_limited", retry_after_s=300), _good())
    cache = _cache(source, clock, backoff_seconds=60)
    first = cache.get("runpod")
    assert first.status == "error" and first.error.retry_after_s == 300
    clock.advance(120)
    cache.get("runpod")
    assert source.calls == 1
    clock.advance(181)
    later = cache.get("runpod")
    assert source.calls == 2
    assert later.status == "ok"


def test_the_backoff_is_used_when_retry_after_is_shorter():
    clock = Clock()
    source = FakeSource(_fail("rate_limited", retry_after_s=5), _good())
    cache = _cache(source, clock, backoff_seconds=60)
    cache.get("runpod")
    clock.advance(30)
    cache.get("runpod")
    assert source.calls == 1


def test_a_huge_retry_after_is_capped():
    clock = Clock()
    source = FakeSource(_fail("rate_limited", retry_after_s=10**9), _good())
    cache = _cache(source, clock, backoff_seconds=60)
    cache.get("runpod")
    clock.advance(cc.MAX_RETRY_AFTER_SECONDS + 1)
    cache.get("runpod")
    assert source.calls == 2


# --- states without any good data -----------------------------------------------------------------


def test_a_failure_with_no_good_data_is_status_error_not_an_exception():
    snap = _cache(FakeSource(_fail("unreachable")), Clock()).get("runpod")
    assert snap.status == "error"
    assert snap.error.code == "unreachable"
    assert snap.offers == ()


def test_not_configured_with_no_good_data_is_its_own_status_and_is_throttled_by_the_backoff():
    clock = Clock()
    source = FakeSource(_fail("not_configured"), _good())
    cache = _cache(source, clock, backoff_seconds=60)
    first = cache.get("runpod")
    assert first.status == "not_configured"
    assert first.error.message == cc.ERROR_MESSAGES["not_configured"]
    for _ in range(5):
        clock.advance(5)
        cache.get("runpod")
    assert source.calls == 1
    clock.advance(60)
    assert cache.get("runpod").status == "ok"


def test_recovery_clears_the_error_and_returns_to_ok():
    clock = Clock()
    source = FakeSource(_fail("upstream_error"), _good())
    cache = _cache(source, clock, backoff_seconds=60)
    cache.get("runpod")
    clock.advance(60)
    assert cache.get("runpod").status == "ok"


def test_a_source_that_raises_gives_a_fixed_error_and_never_its_text(caplog):
    clock = Clock()
    source = FakeSource(RuntimeError(f"boom {SENTINEL}"))
    with caplog.at_level(logging.WARNING, logger="meridian.compute_catalog"):
        snap = _cache(source, clock).get("runpod")
    assert snap.status == "error"
    assert snap.error.code == "unexpected_error"
    assert snap.error.message == cc.ERROR_MESSAGES["unexpected_error"]
    assert SENTINEL not in repr(snap)
    assert SENTINEL not in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_source_that_returns_the_wrong_type_is_an_unexpected_error():
    class BadSource(FakeSource):
        def fetch_offers(self):
            return {"not": "a fetch"}

    snap = _cache(BadSource(), Clock()).get("runpod")
    assert snap.error.code == "unexpected_error"


# --- construction and lookups -----------------------------------------------------------------------


def test_an_unknown_provider_raises_key_error():
    with pytest.raises(KeyError):
        _cache(FakeSource(_good()), Clock()).get("aws")


def test_negative_durations_are_refused():
    with pytest.raises(ValueError):
        _cache(FakeSource(_good()), Clock(), ttl_seconds=-1)
    with pytest.raises(ValueError):
        _cache(FakeSource(_good()), Clock(), backoff_seconds=-1)


def test_provider_names_lists_the_registered_sources():
    assert _cache(FakeSource(_good()), Clock()).provider_names() == ("runpod",)


def test_the_default_clock_is_aware_utc():
    now = cc.utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_the_default_ttl_and_backoff_match_the_spec():
    assert cc.DEFAULT_TTL_SECONDS == 300
    assert cc.DEFAULT_BACKOFF_SECONDS == 60
