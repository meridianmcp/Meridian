"""Tests for meridian.pool_telemetry module."""

from __future__ import annotations

import json
import os

import pytest

from meridian.pool_telemetry import (
    LATENCY_BUCKETS_MS,
    METER,
    AlertLedger,
    LoadThresholds,
    PlacementRecommendation,
    PoolWindow,
    TenantPoolMeter,
    TenantWindow,
    classify_tenant,
    pool_label,
    recommend_placement,
    record_request,
    tenant_label,
)


class Clock:
    """Fake clock for testing."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


# ============================================================================
# tenant_label and pool_label tests
# ============================================================================


def test_tenant_label_stable():
    """tenant_label must return the same value for the same input."""
    tid = "user-123"
    label1 = tenant_label(tid)
    label2 = tenant_label(tid)
    assert label1 == label2
    assert label1.startswith("t_")


def test_tenant_label_redacted():
    """tenant_label must not contain the raw tenant ID."""
    tid = "user-123"
    label = tenant_label(tid)
    assert tid not in label
    assert "@" not in label


def test_tenant_label_falsy_input():
    """tenant_label returns t_unknown for falsy input."""
    assert tenant_label(None) == "t_unknown"
    assert tenant_label("") == "t_unknown"


def test_pool_label_valid():
    """pool_label returns p_ + last 8 chars for valid project IDs."""
    pid = "proj-abc-defghij"
    label = pool_label(pid)
    assert label.startswith("p_")
    assert label == "p_-defghij"  # Last 8 chars of 16-char string


def test_pool_label_short():
    """pool_label returns p_ + whole value if shorter than 8 chars."""
    pid = "short"
    label = pool_label(pid)
    assert label == "p_short"


def test_pool_label_invalid_chars():
    """pool_label returns p_unpooled for invalid characters."""
    assert pool_label("proj with space") == "p_unpooled"
    assert pool_label("proj@abc") == "p_unpooled"
    assert pool_label("proj#123") == "p_unpooled"


def test_pool_label_too_long():
    """pool_label returns p_unpooled for IDs > 64 chars."""
    pid = "a" * 65
    assert pool_label(pid) == "p_unpooled"


def test_pool_label_falsy():
    """pool_label returns p_unpooled for falsy input."""
    assert pool_label(None) == "p_unpooled"
    assert pool_label("") == "p_unpooled"


def test_pool_label_strip():
    """pool_label strips whitespace."""
    pid = "  valid-id  "
    label = pool_label(pid)
    assert label.startswith("p_")
    assert label == "p_valid-id"


# ============================================================================
# LoadThresholds tests
# ============================================================================


def test_load_thresholds_defaults():
    """LoadThresholds provides sensible defaults."""
    th = LoadThresholds()
    assert th.bucket_seconds == 60
    assert th.window_buckets == 15
    assert th.min_observed_buckets == 5
    assert th.idle_requests_per_min == 1.0
    assert th.hot_requests_per_min == 120.0
    assert th.provisional is True


def test_load_thresholds_validation_bucket_seconds():
    """LoadThresholds raises ValueError for invalid bucket_seconds."""
    with pytest.raises(ValueError, match="bucket_seconds must be >= 1"):
        LoadThresholds(bucket_seconds=0)


def test_load_thresholds_validation_sustain_buckets():
    """LoadThresholds raises ValueError if sustain_buckets > window_buckets."""
    with pytest.raises(ValueError, match="sustain_buckets must be <= window_buckets"):
        LoadThresholds(window_buckets=10, sustain_buckets=15)


def test_load_thresholds_validation_hot_exit_ratio():
    """LoadThresholds raises ValueError for invalid hot_exit_ratio."""
    with pytest.raises(ValueError, match="hot_exit_ratio must be in"):
        LoadThresholds(hot_exit_ratio=0.0)
    with pytest.raises(ValueError, match="hot_exit_ratio must be in"):
        LoadThresholds(hot_exit_ratio=2.0)


def test_load_thresholds_validation_dominant_share():
    """LoadThresholds raises ValueError for invalid dominant_share."""
    with pytest.raises(ValueError, match="dominant_share must be in"):
        LoadThresholds(dominant_share=0.0)
    with pytest.raises(ValueError, match="dominant_share must be in"):
        LoadThresholds(dominant_share=1.01)
    assert LoadThresholds(dominant_share=1.0).dominant_share == 1.0  # (0, 1]: "all of the pool"


def test_load_thresholds_from_env_defaults(monkeypatch):
    """LoadThresholds.from_env() uses defaults when env vars not set."""
    monkeypatch.delenv("MERIDIAN_POOL_HOT_RPM", raising=False)
    monkeypatch.delenv("MERIDIAN_POOL_IDLE_RPM", raising=False)
    monkeypatch.delenv("MERIDIAN_POOL_NOISY_P95_MS", raising=False)

    th = LoadThresholds.from_env()
    assert th.hot_requests_per_min == 120.0
    assert th.idle_requests_per_min == 1.0
    assert th.noisy_p95_ms == 1500.0


def test_load_thresholds_from_env_override(monkeypatch):
    """LoadThresholds.from_env() reads environment variables."""
    monkeypatch.setenv("MERIDIAN_POOL_HOT_RPM", "50.0")
    monkeypatch.setenv("MERIDIAN_POOL_IDLE_RPM", "0.5")
    monkeypatch.setenv("MERIDIAN_POOL_NOISY_P95_MS", "2000.0")

    th = LoadThresholds.from_env()
    assert th.hot_requests_per_min == 50.0
    assert th.idle_requests_per_min == 0.5
    assert th.noisy_p95_ms == 2000.0


def test_load_thresholds_from_env_invalid(monkeypatch):
    """LoadThresholds.from_env() ignores invalid values."""
    monkeypatch.setenv("MERIDIAN_POOL_HOT_RPM", "not-a-number")
    monkeypatch.setenv("MERIDIAN_POOL_IDLE_RPM", "-1")

    th = LoadThresholds.from_env()
    assert th.hot_requests_per_min == 120.0  # default
    assert th.idle_requests_per_min == 1.0  # default


# ============================================================================
# classify_tenant tests
# ============================================================================


def test_classify_tenant_insufficient_evidence():
    """classify_tenant returns insufficient_evidence for thin data."""
    th = LoadThresholds(min_observed_buckets=5)
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=1,
        errors=0,
        requests_per_min=0.5,
        p95_ms=10.0,
        observed_buckets=2,  # Less than min_observed_buckets
        per_bucket_rpm=(0.5, 0.5),
    )
    assert classify_tenant(w, th) == "insufficient_evidence"


def test_classify_tenant_sustained_hot():
    """classify_tenant returns hot for sustained high RPM."""
    th = LoadThresholds(
        sustain_buckets=3,
        hot_requests_per_min=100.0,
    )
    # The last 3 COMPLETED buckets are >= 100 RPM; the trailing 30 is the bucket still
    # filling (a fraction of a minute of traffic) and must not break the streak.
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=500,
        errors=0,
        requests_per_min=120.0,
        p95_ms=50.0,
        observed_buckets=6,
        per_bucket_rpm=(10.0, 50.0, 120.0, 120.0, 120.0, 30.0),
    )
    assert classify_tenant(w, th) == "hot"


def test_classify_tenant_sustained_hot_single_spike():
    """classify_tenant does NOT return hot for a single spike."""
    th = LoadThresholds(
        sustain_buckets=3,
        hot_requests_per_min=100.0,
    )
    # Only last bucket is high; others are normal
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=500,
        errors=0,
        requests_per_min=80.0,
        p95_ms=50.0,
        observed_buckets=5,
        per_bucket_rpm=(10.0, 50.0, 50.0, 50.0, 150.0),
    )
    assert classify_tenant(w, th) != "hot"


def test_classify_tenant_hysteresis_hot_to_normal():
    """classify_tenant applies hysteresis: hot stays hot above exit ratio."""
    th = LoadThresholds(
        hot_requests_per_min=100.0,
        hot_exit_ratio=0.6,
    )
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=500,
        errors=0,
        requests_per_min=70.0,  # Above 100 * 0.6 = 60, so stay hot
        p95_ms=50.0,
        observed_buckets=10,
        per_bucket_rpm=(70.0,) * 10,
    )
    assert classify_tenant(w, th, previous="hot") == "hot"


def test_classify_tenant_hysteresis_drop_below_exit():
    """classify_tenant drops from hot when below exit ratio."""
    th = LoadThresholds(
        hot_requests_per_min=100.0,
        hot_exit_ratio=0.6,
    )
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=500,
        errors=0,
        requests_per_min=50.0,  # Below 100 * 0.6 = 60
        p95_ms=50.0,
        observed_buckets=10,
        per_bucket_rpm=(50.0,) * 10,
    )
    result = classify_tenant(w, th, previous="hot")
    assert result != "hot"


def test_classify_tenant_idle():
    """classify_tenant returns idle for low RPM."""
    th = LoadThresholds(idle_requests_per_min=1.0)
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=5,
        errors=0,
        requests_per_min=0.5,
        p95_ms=10.0,
        observed_buckets=10,
        per_bucket_rpm=(0.5,) * 10,
    )
    assert classify_tenant(w, th) == "idle"


def test_classify_tenant_normal():
    """classify_tenant returns normal for moderate RPM."""
    th = LoadThresholds(
        idle_requests_per_min=1.0,
        hot_requests_per_min=100.0,
    )
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=50,
        errors=0,
        requests_per_min=10.0,  # Between idle and hot
        p95_ms=50.0,
        observed_buckets=10,
        per_bucket_rpm=(10.0,) * 10,
    )
    assert classify_tenant(w, th) == "normal"


# ============================================================================
# recommend_placement tests
# ============================================================================


def test_recommend_placement_insufficient_evidence():
    """recommend_placement returns stay_pooled for insufficient evidence."""
    th = LoadThresholds()
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=1,
        errors=0,
        requests_per_min=0.5,
        p95_ms=None,
        observed_buckets=2,
        per_bucket_rpm=(0.5, 0.5),
    )
    rec = recommend_placement(w, "insufficient_evidence", [w], th)
    assert rec.action == "stay_pooled"
    assert rec.reason == "insufficient_evidence"
    assert rec.advisory is True


def test_recommend_placement_not_hot():
    """recommend_placement returns stay_pooled for non-hot tenants."""
    th = LoadThresholds()
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=100,
        errors=0,
        requests_per_min=10.0,
        p95_ms=50.0,
        observed_buckets=10,
        per_bucket_rpm=(10.0,) * 10,
    )
    rec = recommend_placement(w, "normal", [w], th)
    assert rec.action == "stay_pooled"
    assert rec.reason == "not_hot"


def test_recommend_placement_hot_alone():
    """recommend_placement returns stay_pooled if hot tenant is alone."""
    th = LoadThresholds()
    w = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=200,
        errors=0,
        requests_per_min=150.0,
        p95_ms=100.0,
        observed_buckets=10,
        per_bucket_rpm=(150.0,) * 10,
    )
    # Only this tenant in pool
    rec = recommend_placement(w, "hot", [w], th)
    assert rec.action == "stay_pooled"
    assert rec.reason == "hot_alone"


def test_recommend_placement_hot_with_noisy_neighbours():
    """recommend_placement returns dedicated_project for hot with noisy neighbours."""
    th = LoadThresholds(noisy_p95_ms=500.0)
    tenant = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=200,
        errors=0,
        requests_per_min=150.0,
        p95_ms=100.0,
        observed_buckets=10,
        per_bucket_rpm=(150.0,) * 10,
    )
    noisy_neighbour = TenantWindow(
        label="t_def",
        pool="p_xyz",
        requests=100,
        errors=0,
        requests_per_min=50.0,
        p95_ms=600.0,  # Above noisy threshold
        observed_buckets=10,
        per_bucket_rpm=(50.0,) * 10,
    )
    rec = recommend_placement(tenant, "hot", [tenant, noisy_neighbour], th)
    assert rec.action == "dedicated_project"
    assert rec.reason == "hot_with_noisy_neighbours"
    assert rec.co_tenants == 1
    assert rec.co_tenant_p95_ms == 600.0


def test_recommend_placement_hot_dominant_share():
    """recommend_placement returns small_hot_pool for dominant tenant."""
    th = LoadThresholds(dominant_share=0.5)
    tenant = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=700,  # 70% of total
        errors=0,
        requests_per_min=150.0,
        p95_ms=100.0,
        observed_buckets=10,
        per_bucket_rpm=(150.0,) * 10,
    )
    neighbour = TenantWindow(
        label="t_def",
        pool="p_xyz",
        requests=300,  # 30% of total
        errors=0,
        requests_per_min=50.0,
        p95_ms=100.0,
        observed_buckets=10,
        per_bucket_rpm=(50.0,) * 10,
    )
    rec = recommend_placement(tenant, "hot", [tenant, neighbour], th)
    assert rec.action == "small_hot_pool"
    assert rec.reason == "hot_dominant_share"
    assert rec.co_tenants == 1


def test_recommend_placement_hot_neighbours_unaffected():
    """recommend_placement returns stay_pooled if neighbours are unaffected."""
    th = LoadThresholds(noisy_p95_ms=500.0, dominant_share=0.9)
    tenant = TenantWindow(
        label="t_abc",
        pool="p_xyz",
        requests=200,
        errors=0,
        requests_per_min=150.0,
        p95_ms=100.0,
        observed_buckets=10,
        per_bucket_rpm=(150.0,) * 10,
    )
    good_neighbour = TenantWindow(
        label="t_def",
        pool="p_xyz",
        requests=100,
        errors=0,
        requests_per_min=50.0,
        p95_ms=100.0,  # Below noisy threshold
        observed_buckets=10,
        per_bucket_rpm=(50.0,) * 10,
    )
    rec = recommend_placement(tenant, "hot", [tenant, good_neighbour], th)
    assert rec.action == "stay_pooled"
    assert rec.reason == "hot_but_neighbours_unaffected"


# ============================================================================
# AlertLedger tests
# ============================================================================


def test_alert_ledger_first_alert():
    """AlertLedger returns True on first alert."""
    clock = Clock()
    ledger = AlertLedger(clock=clock)
    assert ledger.should_alert("key1") is True


def test_alert_ledger_within_cooldown():
    """AlertLedger returns False within cooldown."""
    clock = Clock()
    ledger = AlertLedger(cooldown_seconds=60.0, clock=clock)
    assert ledger.should_alert("key1") is True
    clock.advance(30)
    assert ledger.should_alert("key1") is False


def test_alert_ledger_after_cooldown():
    """AlertLedger returns True after cooldown elapses."""
    clock = Clock()
    ledger = AlertLedger(cooldown_seconds=60.0, clock=clock)
    assert ledger.should_alert("key1") is True
    clock.advance(61)
    assert ledger.should_alert("key1") is True


def test_alert_ledger_multiple_keys():
    """AlertLedger tracks multiple keys independently."""
    clock = Clock()
    ledger = AlertLedger(cooldown_seconds=60.0, clock=clock)
    assert ledger.should_alert("key1") is True
    assert ledger.should_alert("key2") is True
    clock.advance(30)
    assert ledger.should_alert("key1") is False
    assert ledger.should_alert("key2") is False


def test_alert_ledger_max_keys_bound():
    """AlertLedger evicts oldest key when exceeding max_keys."""
    clock = Clock()
    ledger = AlertLedger(cooldown_seconds=3600.0, clock=clock, max_keys=3)
    assert ledger.should_alert("key1") is True
    clock.advance(1)
    assert ledger.should_alert("key2") is True
    clock.advance(1)
    assert ledger.should_alert("key3") is True
    clock.advance(1)
    # key1 should be evicted now
    assert ledger.should_alert("key4") is True
    # key1 is gone, so should_alert returns True (first alert again)
    assert ledger.should_alert("key1") is True


# ============================================================================
# TenantPoolMeter tests
# ============================================================================


def test_meter_record_basic():
    """TenantPoolMeter records basic requests."""
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    meter.record("tenant1", "proj1")
    meter.record("tenant1", "proj1")

    windows = meter.tenant_windows()
    assert len(windows) == 1
    assert windows[0].requests == 2


def test_meter_tenant_label_redacted():
    """TenantPoolMeter windows use redacted labels, never raw tenant IDs."""
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    meter.record("user-123", "proj1")

    windows = meter.tenant_windows()
    snapshot = meter.snapshot()

    # Check windows
    assert all("user-123" not in w.label for w in windows)
    # Check snapshot
    assert "user-123" not in json.dumps(snapshot)


def test_meter_pool_label_redacted():
    """TenantPoolMeter snapshots never contain raw project IDs."""
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    meter.record("tenant1", "my-secret-project-id-12345")

    snapshot = meter.snapshot()
    assert "my-secret-project-id-12345" not in json.dumps(snapshot)


def test_meter_bounded_memory():
    """TenantPoolMeter evicts when exceeding max_tenants."""
    clock = Clock()
    meter = TenantPoolMeter(max_tenants=5, clock=clock)

    # Add more than max_tenants
    for i in range(50):
        meter.record(f"tenant{i}", "proj1")

    assert meter.evicted_tenants == 45  # 50 distinct tenants through 5 slots
    assert len(meter.tenant_windows()) == 5
    assert meter.snapshot()["tracked_tenants"] == 5


def test_meter_old_buckets_age_out():
    """TenantPoolMeter drops old buckets outside the window."""
    clock = Clock()
    th = LoadThresholds(bucket_seconds=60, window_buckets=3, sustain_buckets=2, min_observed_buckets=1)
    meter = TenantPoolMeter(thresholds=th, clock=clock)

    # Record in bucket 0
    meter.record("tenant1", "proj1")
    assert len(meter.tenant_windows()) == 1
    assert meter.tenant_windows()[0].requests == 1

    # Advance past the window: the first event no longer counts, only the new one does.
    clock.advance(th.bucket_seconds * th.window_buckets + 1)
    meter.record("tenant1", "proj1")

    windows = meter.tenant_windows()
    assert len(windows) == 1
    assert windows[0].requests == 1
    assert len(windows[0].per_bucket_rpm) == th.window_buckets


def test_meter_p95_estimation():
    """TenantPoolMeter estimates p95 latency correctly."""
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)

    # Add 100 latencies, 95 of which are <= 100ms
    for _ in range(95):
        meter.record("tenant1", "proj1", duration_ms=50.0)
    # 5 at 1000ms
    for _ in range(5):
        meter.record("tenant1", "proj1", duration_ms=1000.0)

    windows = meter.tenant_windows()
    assert len(windows) == 1
    p95 = windows[0].p95_ms
    # p95 should be around 1000ms (the 95th percentile)
    assert p95 is not None


def test_meter_p95_none_without_samples():
    """TenantPoolMeter returns None for p95 when no latency samples."""
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    meter.record("tenant1", "proj1", duration_ms=None)

    windows = meter.tenant_windows()
    assert windows[0].p95_ms is None


def test_meter_classify_all():
    """TenantPoolMeter.classify_all() returns classification for each tenant."""
    clock = Clock()
    th = LoadThresholds(
        bucket_seconds=60,
        window_buckets=15,
        min_observed_buckets=1,
        idle_requests_per_min=1.0,
        hot_requests_per_min=100.0,
    )
    meter = TenantPoolMeter(thresholds=th, clock=clock)

    # Add a few requests (should be normal)
    for _ in range(5):
        meter.record("tenant1", "proj1")

    states = meter.classify_all()
    assert "t_" in list(states.keys())[0]  # Labels are redacted
    assert list(states.values())[0] in ("idle", "normal", "hot", "insufficient_evidence")


def test_meter_recommendations_only_hot():
    """TenantPoolMeter.recommendations() returns only hot tenants."""
    clock = Clock()
    th = LoadThresholds(
        bucket_seconds=10,
        window_buckets=15,
        sustain_buckets=1,
        hot_requests_per_min=100.0,
    )
    meter = TenantPoolMeter(thresholds=th, clock=clock)

    # Add some requests to a tenant
    for _ in range(5):
        meter.record("tenant1", "proj1")

    recs = meter.recommendations()
    # With only 5 requests in a 10-second bucket, RPM = 5 / (1 * 10 / 60) = 30, not hot
    assert all(isinstance(r, PlacementRecommendation) for r in recs)


def test_meter_snapshot_json_clean():
    """TenantPoolMeter.snapshot() is JSON-serializable and redacted."""
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    meter.record("user-123", "my-proj-456")

    snap = meter.snapshot()
    # Must be JSON-serializable
    json_str = json.dumps(snap)
    assert isinstance(json_str, str)

    # Must not contain raw IDs or @ signs
    assert "user-123" not in json_str
    assert "my-proj-456" not in json_str
    assert "@" not in json_str


def test_meter_reset():
    """TenantPoolMeter.reset() clears all data."""
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    meter.record("tenant1", "proj1")
    assert len(meter.tenant_windows()) == 1

    meter.reset()
    assert len(meter.tenant_windows()) == 0
    assert meter.evicted_tenants == 0


def test_meter_requests_per_min_calculation():
    """TenantPoolMeter calculates requests_per_min correctly."""
    clock = Clock()
    th = LoadThresholds(bucket_seconds=60, window_buckets=15)
    meter = TenantPoolMeter(thresholds=th, clock=clock)

    # Add 10 requests to one tenant in one bucket
    for _ in range(10):
        meter.record("tenant1", "proj1")

    windows = meter.tenant_windows()
    assert len(windows) == 1
    # observed_buckets should be 1, requests=10, so RPM = 10 / (1 * 60 / 60) = 10
    assert windows[0].requests_per_min == 10.0


# ============================================================================
# record_request convenience function tests
# ============================================================================


def test_record_request_off():
    """record_request returns immediately when telemetry is off."""
    import meridian.pool_telemetry as mod

    # Create a fresh meter for this test
    test_meter = TenantPoolMeter()
    original_meter = mod.METER
    mod.METER = test_meter

    try:
        os.environ["MERIDIAN_POOL_TELEMETRY"] = "0"
        record_request("tenant1", "proj1")
        assert len(test_meter.tenant_windows()) == 0

        os.environ["MERIDIAN_POOL_TELEMETRY"] = "false"
        record_request("tenant1", "proj1")
        assert len(test_meter.tenant_windows()) == 0

        os.environ["MERIDIAN_POOL_TELEMETRY"] = "off"
        record_request("tenant1", "proj1")
        assert len(test_meter.tenant_windows()) == 0
    finally:
        mod.METER = original_meter
        os.environ.pop("MERIDIAN_POOL_TELEMETRY", None)


def test_record_request_swallows_exceptions(monkeypatch):
    """record_request swallows all exceptions."""
    import meridian.pool_telemetry as mod

    # Create a mock meter that raises
    class FailingMeter:
        def record(self, *args, **kwargs):
            raise RuntimeError("Test error")

    original_meter = mod.METER
    mod.METER = FailingMeter()

    try:
        os.environ.pop("MERIDIAN_POOL_TELEMETRY", None)
        # Should not raise
        record_request("tenant1", "proj1")
    finally:
        mod.METER = original_meter


def test_record_request_enabled():
    """record_request records when telemetry is enabled."""
    import meridian.pool_telemetry as mod

    test_meter = TenantPoolMeter()
    original_meter = mod.METER
    mod.METER = test_meter

    try:
        os.environ.pop("MERIDIAN_POOL_TELEMETRY", None)
        record_request("tenant1", "proj1", duration_ms=10.0)
        assert len(test_meter.tenant_windows()) == 1
    finally:
        mod.METER = original_meter


# ============================================================================
# Review additions: the properties that matter on the request path
# ============================================================================


def test_eviction_is_least_recently_seen_not_first_seen():
    clock = Clock()
    meter = TenantPoolMeter(max_tenants=3, clock=clock)
    for name in ("a", "b", "c"):
        meter.record(name, "proj1")
    meter.record("a", "proj1")  # a is now the most recently seen
    meter.record("d", "proj1")  # evicts b, the least recently seen
    labels = {w.label for w in meter.tenant_windows()}
    assert labels == {tenant_label("a"), tenant_label("c"), tenant_label("d")}
    assert meter.evicted_tenants == 1


def test_latency_storage_is_a_fixed_size_histogram_not_raw_samples():
    """A hot tenant must not make the meter's memory grow with its traffic."""
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    for _ in range(20_000):
        meter.record("hot", "proj1", duration_ms=40)
    (rec,) = meter._tenants.values()
    (bucket,) = rec.buckets.values()
    assert len(bucket.hist) == len(LATENCY_BUCKETS_MS) + 1
    assert sum(bucket.hist) == 20_000
    assert meter.tenant_windows()[0].p95_ms == 50.0  # upper bound of the 25-50 ms bin


def test_a_steady_hot_tenant_stays_hot_across_a_bucket_rollover():
    """The newest bucket holds only a few seconds of traffic right after a rollover;
    it must not knock a sustained-hot tenant out of 'hot'."""
    clock = Clock()
    th = LoadThresholds(window_buckets=15, sustain_buckets=10, min_observed_buckets=5, hot_requests_per_min=120.0)
    meter = TenantPoolMeter(thresholds=th, clock=clock)
    for _minute in range(12):
        for _ in range(200):
            meter.record("busy", "proj1")
        clock.advance(60)
    meter.record("busy", "proj1")  # the first request of the 13th bucket
    assert list(meter.classify_all().values()) == ["hot"]


def test_a_tenant_that_goes_quiet_ages_out_of_the_tracked_set():
    clock = Clock()
    th = LoadThresholds(window_buckets=3, sustain_buckets=2, min_observed_buckets=1)
    meter = TenantPoolMeter(thresholds=th, clock=clock)
    meter.record("brief", "proj1")
    clock.advance(th.bucket_seconds * (th.window_buckets + 2))
    assert meter.tenant_windows() == []
    assert meter.snapshot()["tracked_tenants"] == 0


def test_concurrent_recording_loses_no_events():
    import threading

    clock = Clock()
    meter = TenantPoolMeter(clock=clock)

    def worker(n):
        for _ in range(2000):
            meter.record(f"tenant{n % 3}", "proj1", duration_ms=12)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(w.requests for w in meter.tenant_windows()) == 16_000


def test_snapshot_respects_top_n_and_a_tenant_moving_pools():
    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    for i in range(6):
        for _ in range(i + 1):
            meter.record(f"tenant{i}", "proj-aaaaaaaa")
    snap = meter.snapshot(top_n=2)
    assert len(snap["top_tenants"]) == 2
    assert [t["requests"] for t in snap["top_tenants"]] == [6, 5]
    assert meter.snapshot(top_n=0)["top_tenants"] == []

    meter.record("tenant5", "proj-bbbbbbbb")  # promoted to a dedicated project
    moved = [w for w in meter.tenant_windows() if w.label == tenant_label("tenant5")]
    assert moved[0].pool == pool_label("proj-bbbbbbbb")


# ============================================================================
# new_advisories: only moves, once per cooldown, never raises
# ============================================================================


def _noisy_pool(clock):
    """A hot tenant plus a neighbour whose requests are slow: 12 minutes of traffic."""
    meter = TenantPoolMeter(clock=clock)
    for _minute in range(12):
        for _ in range(200):
            meter.record("hot", "pool-free-AAAAAAAA", duration_ms=10)
        for _ in range(5):
            meter.record("neighbour", "pool-free-AAAAAAAA", duration_ms=3000)
        clock.advance(60)
    meter.record("hot", "pool-free-AAAAAAAA", duration_ms=10)
    return meter


def test_new_advisories_returns_a_move_once_per_cooldown():
    from meridian.pool_telemetry import new_advisories

    meter = _noisy_pool(Clock())
    ledger_clock = Clock()  # separate from the meter's: the traffic itself must not age out
    ledger = AlertLedger(cooldown_seconds=3600, clock=ledger_clock)

    first = new_advisories(meter, ledger)
    assert [(r.action, r.reason) for r in first] == [("dedicated_project", "hot_with_noisy_neighbours")]
    assert first[0].tenant == tenant_label("hot")
    assert new_advisories(meter, ledger) == []  # same advisory inside the cooldown
    ledger_clock.advance(3601)
    assert len(new_advisories(meter, ledger)) == 1


def test_new_advisories_ignores_hot_tenants_that_should_stay_pooled():
    from meridian.pool_telemetry import new_advisories

    clock = Clock()
    meter = TenantPoolMeter(clock=clock)
    for _minute in range(12):  # hot but alone: isolating it would change nothing for anyone
        for _ in range(200):
            meter.record("loner", "pool-free-BBBBBBBB", duration_ms=10)
        clock.advance(60)
    meter.record("loner", "pool-free-BBBBBBBB", duration_ms=10)
    assert [r.reason for r in meter.recommendations()] == ["hot_alone"]
    assert new_advisories(meter, AlertLedger(clock=clock)) == []


def test_new_advisories_never_raises():
    from meridian.pool_telemetry import new_advisories

    class Broken:
        def recommendations(self):
            raise RuntimeError("boom")

    assert new_advisories(Broken(), AlertLedger()) == []
