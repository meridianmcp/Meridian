"""Meridian pool telemetry: in-memory, bounded, redacted per-tenant load tracking.

Purpose: In shared Neon projects (up to 8 tenants on one endpoint), noisy tenants
can degrade others. This module provides low-cardinality, redacted telemetry to
classify tenant load (idle/normal/hot) with hysteresis, recommend placement actions
(stay_pooled / dedicated_project / small_hot_pool), and deduplicate alerts.

Constraints:
- In-memory only (telemetry that writes to Postgres/Redis would itself wake Neon).
- Per-process only (not shared across servers).
- Bounded memory (evicts least-recently-seen tenants and old time buckets).
- Redacted (tenants as salted-hash labels, pools as truncated IDs; no emails/URLs/secrets).
- Advisory only (never migrates a tenant or changes quotas).

Key classes:
- TenantPoolMeter: main instrumentation, time-bucketing, per-tenant/pool windows.
- LoadThresholds: configuration for classification (provisional, subject to calibration).
- AlertLedger: deduplication for alerts.

Module exports METER (module-level singleton) and record_request() convenience function.

Item 1b2fbebe. Nothing here is wired to act: thresholds are provisional placeholders (see
LoadThresholds) and every recommendation is advisory. See
docs/infra-neon-tenant-pool-isolation.md for the design, baseline and runbook.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Sequence


def tenant_label(tenant_id: str | None) -> str:
    """Return a redacted, stable label for a tenant ID.

    Uses SHA256(f"meridian-pool-telemetry:{tenant_id}") salted hash.
    Falsy input returns "t_unknown".
    """
    if not tenant_id:
        return "t_unknown"
    h = hashlib.sha256(f"meridian-pool-telemetry:{tenant_id}".encode()).hexdigest()
    return f"t_{h[:10]}"


def pool_label(neon_project_id: str | None) -> str:
    """Return a redacted label for a Neon project ID.

    If the stripped value matches ^[A-Za-z0-9_-]{1,64}$, returns "p_" + last 8 chars
    (or the whole value if shorter). Otherwise returns "p_unpooled".
    """
    if not neon_project_id:
        return "p_unpooled"
    s = neon_project_id.strip()
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", s):
        return "p_unpooled"
    suffix = s[-8:] if len(s) >= 8 else s
    return f"p_{suffix}"


LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)


@dataclass(frozen=True)
class LoadThresholds:
    """Configuration for load classification.

    All thresholds are PROVISIONAL, unmeasured placeholders that must be calibrated
    from >=7 days of real telemetry before any automated action is taken.
    """

    bucket_seconds: int = 60
    window_buckets: int = 15
    min_observed_buckets: int = 5
    idle_requests_per_min: float = 1.0
    hot_requests_per_min: float = 120.0
    hot_exit_ratio: float = 0.6
    sustain_buckets: int = 10
    noisy_p95_ms: float = 1500.0
    dominant_share: float = 0.7
    provisional: bool = True

    def __post_init__(self):
        """Validate thresholds."""
        if self.bucket_seconds < 1:
            raise ValueError("bucket_seconds must be >= 1")
        if self.window_buckets < 1:
            raise ValueError("window_buckets must be >= 1")
        if self.sustain_buckets > self.window_buckets:
            raise ValueError("sustain_buckets must be <= window_buckets")
        if self.min_observed_buckets > self.window_buckets:
            raise ValueError("min_observed_buckets must be <= window_buckets")
        if not (0 < self.hot_exit_ratio <= 1):
            raise ValueError("hot_exit_ratio must be in (0, 1]")
        if not (0 < self.dominant_share <= 1):
            raise ValueError("dominant_share must be in (0, 1]")

    @classmethod
    def from_env(cls) -> LoadThresholds:
        """Create thresholds from optional environment variables.

        Reads:
        - MERIDIAN_POOL_HOT_RPM (float, default 120.0)
        - MERIDIAN_POOL_IDLE_RPM (float, default 1.0)
        - MERIDIAN_POOL_NOISY_P95_MS (float, default 1500.0)

        Ignores unparsable or non-positive values, falls back to defaults.
        """
        hot_rpm = 120.0
        idle_rpm = 1.0
        noisy_p95 = 1500.0

        for var, dest_name in [
            ("MERIDIAN_POOL_HOT_RPM", "hot_rpm"),
            ("MERIDIAN_POOL_IDLE_RPM", "idle_rpm"),
            ("MERIDIAN_POOL_NOISY_P95_MS", "noisy_p95"),
        ]:
            val_str = os.environ.get(var)
            if val_str:
                try:
                    val = float(val_str)
                    if val > 0:
                        if dest_name == "hot_rpm":
                            hot_rpm = val
                        elif dest_name == "idle_rpm":
                            idle_rpm = val
                        elif dest_name == "noisy_p95":
                            noisy_p95 = val
                except (ValueError, TypeError):
                    pass

        return cls(
            hot_requests_per_min=hot_rpm,
            idle_requests_per_min=idle_rpm,
            noisy_p95_ms=noisy_p95,
            provisional=True,
        )


@dataclass(frozen=True)
class TenantWindow:
    """A sliding window view of a single tenant's load."""

    label: str
    pool: str
    requests: int
    errors: int
    requests_per_min: float
    p95_ms: float | None
    observed_buckets: int
    per_bucket_rpm: tuple[float, ...] = ()


@dataclass(frozen=True)
class PoolWindow:
    """A snapshot of a pool's aggregate load."""

    pool: str
    tenants: int
    requests: int
    requests_per_min: float
    p95_ms: float | None


def classify_tenant(
    window: TenantWindow,
    th: LoadThresholds,
    previous: str | None = None,
) -> str:
    """Classify a tenant's load state with hysteresis.

    Returns one of: "insufficient_evidence", "idle", "normal", "hot".

    Rules (in order):
    1. If observed_buckets < min_observed_buckets: "insufficient_evidence"
    2. If the last sustain_buckets COMPLETED entries of per_bucket_rpm (all but the newest,
       in-progress one) are ALL >= hot_requests_per_min: "hot"
    3. If previous=="hot" and requests_per_min >= hot_requests_per_min * hot_exit_ratio: "hot"
    4. If requests_per_min < idle_requests_per_min: "idle"
    5. Else: "normal"
    """
    if window.observed_buckets < th.min_observed_buckets:
        return "insufficient_evidence"

    # Sustained hot: the last sustain_buckets COMPLETED buckets are all >= the hot rate. The
    # newest bucket is still filling (it holds a fraction of a minute of traffic), so a
    # steady hot tenant would otherwise drop out of "hot" at every bucket rollover.
    completed = window.per_bucket_rpm[:-1]
    if len(completed) >= th.sustain_buckets:
        sustained = completed[-th.sustain_buckets :]
        if all(rpm >= th.hot_requests_per_min for rpm in sustained):
            return "hot"

    # Hysteresis: stay hot if above exit ratio
    if (
        previous == "hot"
        and window.requests_per_min >= th.hot_requests_per_min * th.hot_exit_ratio
    ):
        return "hot"

    # Idle
    if window.requests_per_min < th.idle_requests_per_min:
        return "idle"

    return "normal"


@dataclass(frozen=True)
class PlacementRecommendation:
    """An advisory recommendation for tenant placement."""

    action: str  # "stay_pooled", "dedicated_project", "small_hot_pool"
    tenant: str  # label
    pool: str
    reason: str  # fixed vocabulary
    co_tenants: int
    co_tenant_p95_ms: float | None
    advisory: bool = True


def recommend_placement(
    tenant: TenantWindow,
    state: str,
    pool_tenants: Sequence[TenantWindow],
    th: LoadThresholds,
) -> PlacementRecommendation:
    """Recommend placement for a tenant given its load state.

    Args:
        tenant: the tenant's window
        state: the result of classify_tenant() for this tenant
        pool_tenants: ALL tenants in the pool (may include tenant itself)
        th: load thresholds

    Returns:
        PlacementRecommendation with action and reason.

    Rules:
    - state=="insufficient_evidence": stay_pooled, reason="insufficient_evidence"
    - state!="hot": stay_pooled, reason="not_hot"
    - state=="hot" and no co-tenants with requests>0: stay_pooled, reason="hot_alone"
    - state=="hot" with co-tenants:
      - co_p95 = max p95_ms of co-tenants with requests>0 and non-None p95
      - if co_p95 >= noisy_p95_ms: dedicated_project, reason="hot_with_noisy_neighbours"
      - elif tenant.requests / sum(all requests incl. tenant) >= dominant_share:
            small_hot_pool, reason="hot_dominant_share"
      - else: stay_pooled, reason="hot_but_neighbours_unaffected"
    """
    if state == "insufficient_evidence":
        return PlacementRecommendation(
            action="stay_pooled",
            tenant=tenant.label,
            pool=tenant.pool,
            reason="insufficient_evidence",
            co_tenants=0,
            co_tenant_p95_ms=None,
        )

    if state != "hot":
        return PlacementRecommendation(
            action="stay_pooled",
            tenant=tenant.label,
            pool=tenant.pool,
            reason="not_hot",
            co_tenants=0,
            co_tenant_p95_ms=None,
        )

    # state == "hot"
    co_tenants_list = [t for t in pool_tenants if t.label != tenant.label and t.requests > 0]

    if not co_tenants_list:
        return PlacementRecommendation(
            action="stay_pooled",
            tenant=tenant.label,
            pool=tenant.pool,
            reason="hot_alone",
            co_tenants=0,
            co_tenant_p95_ms=None,
        )

    # Hot with co-tenants
    co_p95_values = [t.p95_ms for t in co_tenants_list if t.p95_ms is not None]
    co_p95 = max(co_p95_values) if co_p95_values else None

    if co_p95 is not None and co_p95 >= th.noisy_p95_ms:
        return PlacementRecommendation(
            action="dedicated_project",
            tenant=tenant.label,
            pool=tenant.pool,
            reason="hot_with_noisy_neighbours",
            co_tenants=len(co_tenants_list),
            co_tenant_p95_ms=co_p95,
        )

    # Check dominant share
    total_requests = max(1, sum(t.requests for t in pool_tenants))
    share = tenant.requests / total_requests

    if share >= th.dominant_share:
        return PlacementRecommendation(
            action="small_hot_pool",
            tenant=tenant.label,
            pool=tenant.pool,
            reason="hot_dominant_share",
            co_tenants=len(co_tenants_list),
            co_tenant_p95_ms=co_p95,
        )

    return PlacementRecommendation(
        action="stay_pooled",
        tenant=tenant.label,
        pool=tenant.pool,
        reason="hot_but_neighbours_unaffected",
        co_tenants=len(co_tenants_list),
        co_tenant_p95_ms=co_p95,
    )


class AlertLedger:
    """De-duplication ledger for alerts: one alert per key per cooldown window.

    The existing capacity alert (hosted._send_capacity_alert) has no de-duplication; a
    hot-tenant alert built on that pattern would email on every evaluation.
    """

    def __init__(
        self,
        cooldown_seconds: float = 3600.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 500,
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self.max_keys = max(1, max_keys)
        self._last: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def should_alert(self, key: str) -> bool:
        """True the first time a key is seen, and again only after the cooldown."""
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.cooldown_seconds:
                return False
            self._last[key] = now
            self._last.move_to_end(key)
            while len(self._last) > self.max_keys:
                self._last.popitem(last=False)
            return True


# One histogram bin per latency bound plus an overflow bin: memory per bucket is fixed,
# however many requests a hot tenant sends (raw samples would grow with its traffic).
_N_BINS = len(LATENCY_BUCKETS_MS) + 1


class _Bucket:
    __slots__ = ("requests", "errors", "hist")

    def __init__(self) -> None:
        self.requests = 0
        self.errors = 0
        self.hist = [0] * _N_BINS


class _TenantRec:
    __slots__ = ("pool", "first_idx", "last_idx", "buckets")

    def __init__(self, pool: str, idx: int) -> None:
        self.pool = pool
        self.first_idx = idx
        self.last_idx = idx
        self.buckets: dict[int, _Bucket] = {}


def _p95_from_hist(hist: Sequence[int]) -> float | None:
    """Upper bound of the histogram bin holding the 95th percentile (2x the last bound for
    the overflow bin); None when there are no latency samples."""
    total = sum(hist)
    if total == 0:
        return None
    target = total * 0.95
    running = 0
    for i, n in enumerate(hist):
        running += n
        if running >= target:
            if i < len(LATENCY_BUCKETS_MS):
                return float(LATENCY_BUCKETS_MS[i])
            return float(LATENCY_BUCKETS_MS[-1] * 2)
    return float(LATENCY_BUCKETS_MS[-1] * 2)


class TenantPoolMeter:
    """In-memory, bounded, redacted per-tenant / per-pool request telemetry.

    ``record`` sits on the request path, so it is O(1) amortised and never scans the tenant
    table: tenants live in an LRU ``OrderedDict`` (capacity eviction pops the oldest entry),
    each holding at most ``window_buckets`` fixed-size buckets. A lock makes it safe for the
    sync (threadpool) and async request paths alike.
    """

    def __init__(
        self,
        thresholds: LoadThresholds | None = None,
        *,
        max_tenants: int = 2000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._th = thresholds if thresholds is not None else LoadThresholds()
        self.max_tenants = max(1, max_tenants)
        self._clock = clock
        self._tenants: OrderedDict[str, _TenantRec] = OrderedDict()
        self._previous_state: dict[str, str] = {}
        self._lock = threading.Lock()
        self.evicted_tenants = 0

    # -- recording ---------------------------------------------------------------

    def _idx(self) -> int:
        return int(self._clock() // self._th.bucket_seconds)

    def record(
        self,
        tenant_id: str | None,
        pool_project_id: str | None,
        *,
        duration_ms: float | None = None,
        error: bool = False,
    ) -> None:
        """Count one event for a tenant in a pool. O(1) amortised."""
        label = tenant_label(tenant_id)
        pool = pool_label(pool_project_id)
        idx = self._idx()
        with self._lock:
            rec = self._tenants.get(label)
            if rec is None:
                if len(self._tenants) >= self.max_tenants:
                    old_label, _ = self._tenants.popitem(last=False)
                    self._previous_state.pop(old_label, None)
                    self.evicted_tenants += 1
                rec = _TenantRec(pool, idx)
                self._tenants[label] = rec
            else:
                self._tenants.move_to_end(label)
            rec.pool = pool
            rec.last_idx = idx
            bucket = rec.buckets.get(idx)
            if bucket is None:
                bucket = rec.buckets[idx] = _Bucket()
                floor = idx - self._th.window_buckets + 1
                for stale in [b for b in rec.buckets if b < floor]:
                    del rec.buckets[stale]
            bucket.requests += 1
            if error:
                bucket.errors += 1
            if duration_ms is not None:
                bucket.hist[bisect_left(LATENCY_BUCKETS_MS, duration_ms)] += 1

    # -- reading -----------------------------------------------------------------

    def _collect(self) -> list[tuple[TenantWindow, list[int]]]:
        """(window, merged latency histogram) per tenant active in the window. Tenants whose
        last event aged out of the window are dropped here, which keeps memory bounded by
        *active* tenants rather than every tenant ever seen."""
        th = self._th
        idx = self._idx()
        floor = idx - th.window_buckets + 1
        per_bucket_minutes = th.bucket_seconds / 60.0
        rows: list[tuple[TenantWindow, list[int]]] = []
        with self._lock:
            for label in [l for l, r in self._tenants.items() if r.last_idx < floor]:
                del self._tenants[label]
                self._previous_state.pop(label, None)
            for label, rec in self._tenants.items():
                hist = [0] * _N_BINS
                requests = errors = 0
                per_bucket: list[float] = []
                for b in range(floor, idx + 1):
                    bucket = rec.buckets.get(b)
                    if bucket is None:
                        per_bucket.append(0.0)
                        continue
                    requests += bucket.requests
                    errors += bucket.errors
                    for i, n in enumerate(bucket.hist):
                        hist[i] += n
                    per_bucket.append(bucket.requests / per_bucket_minutes)
                observed = max(1, min(idx - rec.first_idx + 1, th.window_buckets))
                rpm = requests / (observed * per_bucket_minutes)
                rows.append(
                    (
                        TenantWindow(
                            label=label,
                            pool=rec.pool,
                            requests=requests,
                            errors=errors,
                            requests_per_min=rpm,
                            p95_ms=_p95_from_hist(hist),
                            observed_buckets=observed,
                            per_bucket_rpm=tuple(per_bucket),
                        ),
                        hist,
                    )
                )
        return rows

    def tenant_windows(self) -> list[TenantWindow]:
        return [w for w, _ in self._collect()]

    def tenant_window(self, label: str) -> TenantWindow | None:
        for w in self.tenant_windows():
            if w.label == label:
                return w
        return None

    def pool_windows(self) -> list[PoolWindow]:
        th = self._th
        agg: dict[str, dict] = {}
        for w, hist in self._collect():
            slot = agg.setdefault(w.pool, {"tenants": 0, "requests": 0, "hist": [0] * _N_BINS, "obs": 1})
            slot["tenants"] += 1
            slot["requests"] += w.requests
            slot["obs"] = max(slot["obs"], w.observed_buckets)
            for i, n in enumerate(hist):
                slot["hist"][i] += n
        out = []
        for pool, slot in sorted(agg.items()):
            minutes = slot["obs"] * th.bucket_seconds / 60.0
            out.append(
                PoolWindow(
                    pool=pool,
                    tenants=slot["tenants"],
                    requests=slot["requests"],
                    requests_per_min=slot["requests"] / minutes,
                    p95_ms=_p95_from_hist(slot["hist"]),
                )
            )
        return out

    def classify_all(self) -> dict[str, str]:
        """label -> idle/normal/hot/insufficient_evidence; remembers each state so the
        hysteresis in classify_tenant works across calls."""
        windows = self.tenant_windows()
        states: dict[str, str] = {}
        with self._lock:
            for w in windows:
                state = classify_tenant(w, self._th, self._previous_state.get(w.label))
                self._previous_state[w.label] = state
                states[w.label] = state
        return states

    def _recommendations(self, windows: list[TenantWindow], states: dict[str, str]) -> list[PlacementRecommendation]:
        by_pool: dict[str, list[TenantWindow]] = {}
        for w in windows:
            by_pool.setdefault(w.pool, []).append(w)
        return [
            recommend_placement(w, "hot", by_pool[w.pool], self._th)
            for w in windows
            if states.get(w.label) == "hot"
        ]

    def recommendations(self) -> list[PlacementRecommendation]:
        """Advisory placement for each HOT tenant only."""
        windows = self.tenant_windows()
        return self._recommendations(windows, self.classify_all())

    def snapshot(self, top_n: int = 10) -> dict:
        """JSON-serialisable, redacted view: never a raw tenant id, project id or email."""
        windows = self.tenant_windows()
        states = self.classify_all()
        th = self._th
        top = sorted(windows, key=lambda w: (-w.requests, w.label))[: max(0, top_n)]
        with self._lock:
            tracked = len(self._tenants)
        return {
            "thresholds": {
                "bucket_seconds": th.bucket_seconds,
                "window_buckets": th.window_buckets,
                "min_observed_buckets": th.min_observed_buckets,
                "idle_requests_per_min": th.idle_requests_per_min,
                "hot_requests_per_min": th.hot_requests_per_min,
                "hot_exit_ratio": th.hot_exit_ratio,
                "sustain_buckets": th.sustain_buckets,
                "noisy_p95_ms": th.noisy_p95_ms,
                "dominant_share": th.dominant_share,
                "provisional": th.provisional,
            },
            "pools": [
                {
                    "pool": p.pool,
                    "tenants": p.tenants,
                    "requests": p.requests,
                    "requests_per_min": round(p.requests_per_min, 2),
                    "p95_ms": p.p95_ms,
                }
                for p in self.pool_windows()
            ],
            "top_tenants": [
                {
                    "tenant": w.label,
                    "pool": w.pool,
                    "requests": w.requests,
                    "errors": w.errors,
                    "requests_per_min": round(w.requests_per_min, 2),
                    "p95_ms": w.p95_ms,
                    "state": states.get(w.label, "insufficient_evidence"),
                }
                for w in top
            ],
            "recommendations": [
                {
                    "action": r.action,
                    "tenant": r.tenant,
                    "pool": r.pool,
                    "reason": r.reason,
                    "co_tenants": r.co_tenants,
                    "co_tenant_p95_ms": r.co_tenant_p95_ms,
                    "advisory": r.advisory,
                }
                for r in self._recommendations(windows, states)
            ],
            "tracked_tenants": tracked,
            "evicted_tenants": self.evicted_tenants,
        }

    def reset(self) -> None:
        with self._lock:
            self._tenants.clear()
            self._previous_state.clear()
            self.evicted_tenants = 0


METER = TenantPoolMeter()
ALERTS = AlertLedger()


def new_advisories(
    meter: TenantPoolMeter | None = None,
    ledger: AlertLedger | None = None,
) -> list[PlacementRecommendation]:
    """Advisory placement changes that have not already been alerted within the cooldown.

    Only recommendations that would MOVE a tenant (action != "stay_pooled") are returned,
    each at most once per ledger cooldown. Nothing is sent from here: the caller decides
    whether to log, email or just show it, and a failure here must never reach a request,
    so any error yields an empty list.
    """
    try:
        meter = meter if meter is not None else METER
        ledger = ledger if ledger is not None else ALERTS
        return [
            rec
            for rec in meter.recommendations()
            if rec.action != "stay_pooled"
            and ledger.should_alert(f"{rec.pool}:{rec.tenant}:{rec.action}")
        ]
    except Exception:
        return []


def record_request(
    tenant_id: str | None,
    pool_project_id: str | None,
    *,
    duration_ms: float | None = None,
    error: bool = False,
) -> None:
    """Record one request on the process-wide METER.

    Telemetry must never break a request: every failure is swallowed. Set
    MERIDIAN_POOL_TELEMETRY=0 (or false/off) to turn recording off; it is read per call so
    the switch needs no restart.
    """
    if os.environ.get("MERIDIAN_POOL_TELEMETRY", "").strip().lower() in ("0", "false", "off"):
        return
    try:
        METER.record(tenant_id, pool_project_id, duration_ms=duration_ms, error=error)
    except Exception:
        pass
