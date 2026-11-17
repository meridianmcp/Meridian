"""GPU offer catalog: provider-neutral types, GPU normalisation, machine totals, filters, sorts, and an
in-process TTL cache for live offer sources.

Nothing here calls a provider. A source (for example :mod:`meridian.research.providers.runpod_catalog`) makes
the provider call and returns a :class:`CatalogFetch`. :class:`OffersCache` decides when a source is called:
it serves the cached result inside the TTL, keeps the last good data when a refresh fails, and waits out a
backoff (or the provider's ``Retry-After``) before it tries again.

Rules the code keeps (see the design spec):

* Unknown is ``None``, never ``0``. A stock value of ``None`` is "unknown", which is not "unavailable".
* Cached rows are base rows, one GPU each. Machine totals come from :func:`for_gpu_count`, never from the cache.
* Sorting puts ``None`` last in both directions. Ties break on provider, GPU model, cloud type, then data center.
* Errors carry a fixed code and fixed text. A provider's own error text is never copied into a result.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from meridian.compute_options import Offer

logger = logging.getLogger(__name__)

#: Provider stock values, in increasing order of availability. ``None`` is unknown.
STOCK_VALUES: tuple[str, ...] = ("NONE", "LOW", "MEDIUM", "HIGH")
STOCK_RANK: dict[str, int] = {value: rank for rank, value in enumerate(STOCK_VALUES)}
#: Cloud types a catalog row can carry. ``spot`` is reserved for AWS and is never emitted for RunPod.
CLOUD_TYPES: tuple[str, ...] = ("secure", "community", "spot")
SORT_KEYS: tuple[str, ...] = ("price_per_gpu_hour", "price_per_hour", "vram_gb_per_gpu", "provider", "stock")
ORDER_VALUES: tuple[str, ...] = ("asc", "desc")
#: Largest GPU count a view accepts.
MAX_GPU_COUNT = 8

DEFAULT_TTL_SECONDS = 300
DEFAULT_BACKOFF_SECONDS = 60
#: A provider's Retry-After is honoured up to this many seconds.
MAX_RETRY_AFTER_SECONDS = 3600

#: Fixed text for each catalog error code. Provider text is never used.
ERROR_MESSAGES: dict[str, str] = {
    "not_configured": "RunPod is not configured on this machine. Set RUNPOD_API_KEY, then refresh.",
    "auth_failed": "RunPod rejected the API key (HTTP 401). Check that the key is current.",
    "forbidden": "RunPod refused catalog reads for this API key (HTTP 403). The key may lack permission.",
    "rate_limited": "RunPod rate-limited the catalog request (HTTP 429). Wait before refreshing.",
    "upstream_error": "RunPod returned an error for the catalog request. Try again later.",
    "timeout": "The RunPod catalog request timed out. Try again later.",
    "unreachable": "Could not reach the RunPod API. Check this machine's network connection.",
    "unexpected_shape": "RunPod returned a catalog response this view could not read.",
    "unexpected_error": "The offer source failed unexpectedly. Details were withheld.",
}

_VENDOR_WORDS = re.compile(r"\b(nvidia|geforce|tesla|quadro)\b", re.IGNORECASE)
_VARIANT_TOKEN = re.compile(r"\b(sxm\d*|pcie|nvl)\b", re.IGNORECASE)
_MEMORY_TOKEN = re.compile(r"\b(\d{1,3})\s*gb\b", re.IGNORECASE)
_MEMORY_TECHNOLOGY = re.compile(r"\bhbm\d*e?\b|\bgddr\d+x?\b", re.IGNORECASE)
_NON_SLUG = re.compile(r"[^a-z0-9]+")

#: Hand-reviewed cross-provider equivalents, keyed by the normalised model. Empty until a second provider lands.
GPU_ALIASES: dict[str, str] = {}


# ----------------------------------------------------------------------------
# Types shared by sources and the cache
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class OfferCapabilities:
    """What a source's rows contain. ``capacity_only`` sources report capacity, not dollar prices."""

    has_price: bool
    capacity_only: bool


@dataclass(frozen=True)
class CatalogError:
    """A fixed error code and fixed text, plus the provider's ``Retry-After`` in seconds when it sent one."""

    code: str
    message: str
    retry_after_s: int | None = None


@dataclass(frozen=True)
class CatalogFetch:
    """The result of one source call.

    ``offers`` are base rows (one GPU each). ``prices_as_of`` and ``stock_as_of`` are UTC times taken when the
    provider responded; ``None`` when that part was not read. ``partial`` is true when some stock could not be
    read, so every row for that cloud has stock ``None``. ``credential_source`` is the kind of credential used
    (``env``, ``keyring`` or ``runpodctl``), never a value.
    """

    offers: tuple[Offer, ...]
    prices_as_of: datetime | None
    stock_as_of: datetime | None
    warnings: tuple[str, ...] = ()
    error: CatalogError | None = None
    partial: bool = False
    credential_source: str | None = None


class OffersSource(Protocol):
    """An offer source. ``fetch_offers`` is synchronous; the route runs it on a worker thread."""

    name: str
    capabilities: OfferCapabilities

    def fetch_offers(self) -> CatalogFetch: ...


@dataclass(frozen=True)
class ProviderSnapshot:
    """What the cache knows about one provider right now.

    ``status`` is ``ok``, ``partial``, ``stale`` (last good data plus a failed refresh), ``not_configured`` or
    ``error`` (no data at all). ``fetch`` is the last good result, and ``error`` the latest failure, if any.
    """

    name: str
    status: str
    fetch: CatalogFetch | None
    error: CatalogError | None
    checked_at: datetime | None
    credential_source: str | None
    from_cache: bool

    @property
    def offers(self) -> tuple[Offer, ...]:
        return self.fetch.offers if self.fetch is not None else ()


# ----------------------------------------------------------------------------
# Time stamps
# ----------------------------------------------------------------------------


def utc_now() -> datetime:
    """The current time as an aware UTC datetime. The default clock for sources and the cache."""
    return datetime.now(timezone.utc)


def format_utc_stamp(value: datetime | None) -> str | None:
    """An aware datetime as ISO 8601 UTC, for example ``2026-11-09T14:03:00Z``. ``None`` stays ``None``."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("stamps must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------
# GPU normalisation
# ----------------------------------------------------------------------------


def _valid_memory(memory_gb: object) -> int | None:
    if isinstance(memory_gb, bool):
        return None
    if isinstance(memory_gb, int) and memory_gb > 0:
        return memory_gb
    if isinstance(memory_gb, float) and memory_gb.is_integer() and memory_gb > 0:
        return int(memory_gb)
    return None


def normalize_gpu(name: object, memory_gb: object) -> tuple[str | None, str | None]:
    """Return ``(gpu_model, gpu_variant)`` for a provider GPU name and its VRAM in GB.

    The model is ``family-{memory}gb-{variant}``, for example ``h100-80gb-sxm``. Vendor words are dropped. The
    variant (SXM, PCIe, NVL) is kept, so SXM and PCIe rows never merge. When the VRAM field is missing, a
    ``NN GB`` token in the name is used. A name with no VRAM anywhere gives a family-only model, and that group
    is not merged with any sized one. Returns ``(None, None)`` for a blank name.
    """
    if not isinstance(name, str) or not name.strip():
        return None, None
    text = _VENDOR_WORDS.sub(" ", name)
    variant: str | None = None
    variant_match = _VARIANT_TOKEN.search(text)
    if variant_match is not None:
        token = variant_match.group(1).lower()
        variant = "sxm" if token.startswith("sxm") else token
        text = f"{text[:variant_match.start()]} {text[variant_match.end():]}"
    text = _MEMORY_TECHNOLOGY.sub(" ", text)
    memory = _valid_memory(memory_gb)
    name_memory = _MEMORY_TOKEN.search(text)
    if name_memory is not None:
        if memory is None:
            memory = int(name_memory.group(1))
        text = _MEMORY_TOKEN.sub(" ", text)
    family = _NON_SLUG.sub("-", text.lower()).strip("-")
    if not family:
        return None, variant
    parts = [family]
    if memory is not None:
        parts.append(f"{memory}gb")
    if variant is not None:
        parts.append(variant)
    model = "-".join(parts)
    return GPU_ALIASES.get(model, model), variant


# ----------------------------------------------------------------------------
# Machine totals, filters, sorts, grouping
# ----------------------------------------------------------------------------


def for_gpu_count(offer: Offer, gpu_count: int) -> Offer:
    """A copy of a base row scaled to ``gpu_count`` GPUs in one machine.

    Price and VRAM are per-GPU values multiplied by the count. A row with an unknown per-GPU price has an unknown
    machine price. The input is never changed. Use this on base rows (``gpu_count`` 1 or unset).
    """
    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 1:
        raise ValueError("gpu_count must be an integer of 1 or more")
    if offer.gpu_count not in (None, 1):
        raise ValueError("for_gpu_count takes a base row with gpu_count 1")
    per_gpu_price = offer.price_per_gpu_hour
    per_gpu_vram = offer.vram_gb_per_gpu
    return dataclasses.replace(
        offer,
        gpu_count=gpu_count,
        price_per_hour=None if per_gpu_price is None else per_gpu_price * gpu_count,
        vram_gb=None if per_gpu_vram is None else float(per_gpu_vram * gpu_count),
    )


def filter_offers(
    offers: Sequence[Offer],
    *,
    gpu_model: str | None = None,
    cloud_type: str | None = None,
    in_stock_only: bool = False,
    max_price_per_gpu_hour: float | None = None,
    gpu_count: int | None = None,
) -> list[Offer]:
    """Keep the offers that match every filter that is set.

    ``in_stock_only`` drops unknown stock, because unknown is not known to be in stock. A price cap drops
    unknown prices. ``max_gpu_count`` is a ceiling, not stock: an offer is dropped only when its ceiling is
    known and below ``gpu_count``. An unknown ceiling is kept.
    """
    kept: list[Offer] = []
    for offer in offers:
        if gpu_model is not None and offer.gpu_model != gpu_model:
            continue
        if cloud_type is not None and offer.cloud_type != cloud_type:
            continue
        if in_stock_only and offer.stock_status not in ("LOW", "MEDIUM", "HIGH"):
            continue
        if max_price_per_gpu_hour is not None and (
            offer.price_per_gpu_hour is None or offer.price_per_gpu_hour > max_price_per_gpu_hour
        ):
            continue
        if gpu_count is not None and offer.max_gpu_count is not None and offer.max_gpu_count < gpu_count:
            continue
        kept.append(offer)
    return kept


def _sort_value(key: str, offer: Offer) -> float | str | None:
    if key == "price_per_gpu_hour":
        return offer.price_per_gpu_hour
    if key == "price_per_hour":
        return offer.price_per_hour
    if key == "vram_gb_per_gpu":
        return offer.vram_gb_per_gpu
    if key == "provider":
        return offer.provider
    # key == "stock": rank the enum, and an unknown stock sorts last like any unknown value.
    return STOCK_RANK.get(offer.stock_status) if offer.stock_status is not None else None


def sort_offers(offers: Sequence[Offer], key: str = "price_per_gpu_hour", order: str = "asc") -> list[Offer]:
    """Sort offers by ``key`` in ``order``. Unknown values go last in both directions.

    Ties break on provider, GPU model, cloud type and data center, always ascending. The tie-break order is set
    first, and the stable sort then keeps it, so a descending sort does not reverse the ties.
    """
    if key not in SORT_KEYS:
        raise ValueError(f"sort must be one of {', '.join(SORT_KEYS)}")
    if order not in ORDER_VALUES:
        raise ValueError("order must be asc or desc")
    ties_sorted = sorted(
        offers,
        key=lambda o: (o.provider or "", o.gpu_model or "", o.cloud_type or "", o.data_center or ""),
    )
    known = [o for o in ties_sorted if _sort_value(key, o) is not None]
    unknown = [o for o in ties_sorted if _sort_value(key, o) is None]
    known.sort(key=lambda o: _sort_value(key, o), reverse=(order == "desc"))  # type: ignore[arg-type,return-value]
    return known + unknown


def group_by_gpu_model(offers: Sequence[Offer]) -> dict[str | None, list[Offer]]:
    """Offers grouped by ``gpu_model``. Rows with no model form their own ``None`` group."""
    groups: dict[str | None, list[Offer]] = {}
    for offer in offers:
        groups.setdefault(offer.gpu_model, []).append(offer)
    return groups


def summarize_gpu_models(offers: Sequence[Offer]) -> list[dict[str, Any]]:
    """One entry per GPU model, sorted by model: the row count and the lowest known per-GPU price."""
    summary: list[dict[str, Any]] = []
    groups = group_by_gpu_model(offers)
    for model in sorted(groups, key=lambda m: (m is None, m or "")):
        members = groups[model]
        prices = [o.price_per_gpu_hour for o in members if o.price_per_gpu_hour is not None]
        summary.append(
            {
                "gpu_model": model,
                "count": len(members),
                "min_price_per_gpu_hour": min(prices) if prices else None,
            }
        )
    return summary


# ----------------------------------------------------------------------------
# TTL cache
# ----------------------------------------------------------------------------


@dataclass
class _ProviderState:
    last_good: CatalogFetch | None = None
    fetched_at: datetime | None = None
    checked_at: datetime | None = None
    last_error: CatalogError | None = None
    next_allowed_at: datetime | None = None
    credential_source: str | None = None


class OffersCache:
    """In-process cache of offer sources, one entry per provider.

    * Inside ``ttl_seconds`` of a good fetch, :meth:`get` returns the cached result and makes no provider call.
    * A failed fetch keeps the last good result and sets a backoff of ``backoff_seconds``, or the provider's
      ``Retry-After`` when that is longer. No call is made before the backoff ends.
    * A failure with no earlier good result is status ``error`` (or ``not_configured``). It is a response state,
      not an exception.
    * Concurrent callers for one provider share one fetch. A per-provider lock makes the second caller wait,
      then read the cache the first one filled.

    The clock is injected and must return an aware UTC datetime.
    """

    def __init__(
        self,
        sources: Mapping[str, OffersSource],
        *,
        clock: Callable[[], datetime] = utc_now,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        if ttl_seconds < 0 or backoff_seconds < 0:
            raise ValueError("ttl_seconds and backoff_seconds cannot be negative")
        self._sources: dict[str, OffersSource] = dict(sources)
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)
        self._backoff = timedelta(seconds=backoff_seconds)
        self._locks: dict[str, threading.Lock] = {name: threading.Lock() for name in self._sources}
        self._states: dict[str, _ProviderState] = {name: _ProviderState() for name in self._sources}

    def provider_names(self) -> tuple[str, ...]:
        return tuple(self._sources)

    def get(self, name: str) -> ProviderSnapshot:
        """The snapshot for ``name``. Raises ``KeyError`` for an unknown provider."""
        source = self._sources[name]
        with self._locks[name]:
            state = self._states[name]
            now = self._clock()
            if state.last_error is None and state.fetched_at is not None and now < state.fetched_at + self._ttl:
                return self._snapshot(name, state, from_cache=True)
            if state.next_allowed_at is not None and now < state.next_allowed_at:
                return self._snapshot(name, state, from_cache=True)
            fetch = self._call_source(name, source)
            done = self._clock()
            state.checked_at = done
            state.credential_source = fetch.credential_source
            if fetch.error is None:
                state.last_good = fetch
                state.fetched_at = done
                state.last_error = None
                state.next_allowed_at = None
            else:
                state.last_error = fetch.error
                wait = self._backoff
                if fetch.error.retry_after_s is not None:
                    capped = min(max(fetch.error.retry_after_s, 0), MAX_RETRY_AFTER_SECONDS)
                    wait = max(wait, timedelta(seconds=capped))
                state.next_allowed_at = done + wait
            return self._snapshot(name, state, from_cache=False)

    @staticmethod
    def _call_source(name: str, source: OffersSource) -> CatalogFetch:
        try:
            result = source.fetch_offers()
        except Exception as exc:  # noqa: BLE001 -- a source bug must not leak its text; only the type is logged
            logger.warning("offer source %s failed with %s", name, type(exc).__name__)
            return CatalogFetch(
                offers=(),
                prices_as_of=None,
                stock_as_of=None,
                error=CatalogError("unexpected_error", ERROR_MESSAGES["unexpected_error"]),
            )
        if not isinstance(result, CatalogFetch):
            return CatalogFetch(
                offers=(),
                prices_as_of=None,
                stock_as_of=None,
                error=CatalogError("unexpected_error", ERROR_MESSAGES["unexpected_error"]),
            )
        return result

    @staticmethod
    def _snapshot(name: str, state: _ProviderState, *, from_cache: bool) -> ProviderSnapshot:
        if state.last_error is None:
            if state.last_good is None:
                status = "error"
            else:
                status = "partial" if state.last_good.partial else "ok"
        elif state.last_good is not None:
            status = "stale"
        elif state.last_error.code == "not_configured":
            status = "not_configured"
        else:
            status = "error"
        return ProviderSnapshot(
            name=name,
            status=status,
            fetch=state.last_good,
            error=state.last_error,
            checked_at=state.checked_at,
            credential_source=state.credential_source,
            from_cache=from_cache,
        )


# ----------------------------------------------------------------------------
# Query parsing and the response view
# ----------------------------------------------------------------------------

ALLOWED_OFFER_PARAMS: tuple[str, ...] = (
    "provider",
    "gpu_model",
    "cloud_type",
    "gpu_count",
    "in_stock_only",
    "max_price_per_gpu_hour",
    "sort",
    "order",
)
PROVIDER_NAMES: tuple[str, ...] = ("runpod",)
MAX_GPU_MODEL_CHARS = 64
_GPU_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9.\-]{0,63}")
_INTEGER_RE = re.compile(r"[0-9]{1,3}")
_PRICE_RE = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6})?")


class QueryError(ValueError):
    """A request parameter the offers view will not accept. ``code`` is ``unknown_parameter`` or
    ``invalid_filter``. The message is fixed text and never echoes the value supplied."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class OffersQuery:
    provider: str = "runpod"
    gpu_model: str | None = None
    cloud_type: str | None = None
    gpu_count: int = 1
    in_stock_only: bool = False
    max_price_per_gpu_hour: float | None = None
    sort: str = "price_per_gpu_hour"
    order: str = "asc"


def _single(params: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = params[name]
    if len(values) != 1:
        raise QueryError("invalid_filter", f"{name} must be given once")
    return values[0]


def parse_offers_query(params: Mapping[str, Sequence[str]]) -> OffersQuery:
    """Validate the query parameters of ``GET /compute/offers``.

    ``params`` maps each name to its list of values. A name outside :data:`ALLOWED_OFFER_PARAMS` raises
    ``unknown_parameter``, which is how ``api_key=`` and similar typos are refused. Every other problem raises
    ``invalid_filter``. Messages name the parameter and the allowed values, never the value supplied.
    """
    unknown = sorted(name for name in params if name not in ALLOWED_OFFER_PARAMS)
    if unknown:
        raise QueryError(
            "unknown_parameter",
            "Unknown query parameter. Allowed parameters: " + ", ".join(ALLOWED_OFFER_PARAMS) + ".",
        )
    fields: dict[str, Any] = {}
    if "provider" in params:
        value = _single(params, "provider")
        if value not in PROVIDER_NAMES:
            raise QueryError("invalid_filter", "provider must be one of: " + ", ".join(PROVIDER_NAMES) + ".")
        fields["provider"] = value
    if "gpu_model" in params:
        value = (_single(params, "gpu_model") or "").lower()
        if len(value) > MAX_GPU_MODEL_CHARS or not _GPU_MODEL_RE.fullmatch(value):
            raise QueryError("invalid_filter", "gpu_model must be a GPU model key of up to 64 characters.")
        fields["gpu_model"] = value
    if "cloud_type" in params:
        value = _single(params, "cloud_type")
        if value not in CLOUD_TYPES:
            raise QueryError("invalid_filter", "cloud_type must be one of: " + ", ".join(CLOUD_TYPES) + ".")
        fields["cloud_type"] = value
    if "gpu_count" in params:
        value = _single(params, "gpu_count") or ""
        if not _INTEGER_RE.fullmatch(value) or not 1 <= int(value) <= MAX_GPU_COUNT:
            raise QueryError("invalid_filter", f"gpu_count must be an integer from 1 to {MAX_GPU_COUNT}.")
        fields["gpu_count"] = int(value)
    if "in_stock_only" in params:
        value = (_single(params, "in_stock_only") or "").lower()
        if value not in ("true", "false", "1", "0"):
            raise QueryError("invalid_filter", "in_stock_only must be true or false.")
        fields["in_stock_only"] = value in ("true", "1")
    if "max_price_per_gpu_hour" in params:
        value = _single(params, "max_price_per_gpu_hour") or ""
        if not _PRICE_RE.fullmatch(value) or float(value) <= 0:
            raise QueryError("invalid_filter", "max_price_per_gpu_hour must be a number greater than zero.")
        fields["max_price_per_gpu_hour"] = float(value)
    if "sort" in params:
        value = _single(params, "sort")
        if value not in SORT_KEYS:
            raise QueryError("invalid_filter", "sort must be one of: " + ", ".join(SORT_KEYS) + ".")
        fields["sort"] = value
    if "order" in params:
        value = _single(params, "order")
        if value not in ORDER_VALUES:
            raise QueryError("invalid_filter", "order must be asc or desc.")
        fields["order"] = value
    return OffersQuery(**fields)


_STATUS_RANK: dict[str, int] = {"ok": 0, "partial": 1, "stale": 2, "not_configured": 3, "error": 4}


def offer_to_dict(offer: Offer) -> dict[str, Any]:
    """The JSON shape of one offer row. Provider strings are copied as data, and are escaped by the dashboard."""
    return {
        "id": offer.id,
        "provider": offer.provider,
        "gpu": offer.gpu,
        "gpu_model": offer.gpu_model,
        "gpu_variant": offer.gpu_variant,
        "gpu_count": offer.gpu_count,
        "max_gpu_count": offer.max_gpu_count,
        "cloud_type": offer.cloud_type,
        "data_center": offer.data_center,
        "stock_status": offer.stock_status,
        "available": offer.available,
        "price_per_gpu_hour": offer.price_per_gpu_hour,
        "price_per_hour": offer.price_per_hour,
        "vram_gb_per_gpu": offer.vram_gb_per_gpu,
        "vram_gb": offer.vram_gb,
        "ram_gb": offer.ram_gb,
        "vcpu": offer.vcpu,
        "price_as_of": offer.price_as_of,
    }


def build_offers_view(snapshots: Sequence[ProviderSnapshot], query: OffersQuery) -> dict[str, Any]:
    """Filter, scale and sort the snapshots' rows for ``query``, and describe each provider's state.

    The GPU model summary is computed before the user's filters, so the selector keeps every model. The overall
    ``status`` is the worst provider status. ``ok`` is true for ``ok``, ``partial`` and ``stale``, because those
    all carry data.
    """
    providers: list[dict[str, Any]] = []
    base_rows: list[Offer] = []
    for snapshot in snapshots:
        fetch = snapshot.fetch
        providers.append(
            {
                "name": snapshot.name,
                "status": snapshot.status,
                "prices_as_of": format_utc_stamp(fetch.prices_as_of) if fetch is not None else None,
                "stock_as_of": format_utc_stamp(fetch.stock_as_of) if fetch is not None else None,
                "checked_at": format_utc_stamp(snapshot.checked_at),
                "error": (
                    {"code": snapshot.error.code, "message": snapshot.error.message}
                    if snapshot.error is not None
                    else None
                ),
                "warnings": list(fetch.warnings) if fetch is not None else [],
            }
        )
        base_rows.extend(snapshot.offers)

    summary = summarize_gpu_models(base_rows)
    kept = filter_offers(
        base_rows,
        gpu_model=query.gpu_model,
        cloud_type=query.cloud_type,
        in_stock_only=query.in_stock_only,
        max_price_per_gpu_hour=query.max_price_per_gpu_hour,
        gpu_count=query.gpu_count,
    )
    scaled = [for_gpu_count(offer, query.gpu_count) for offer in kept]
    ordered = sort_offers(scaled, key=query.sort, order=query.order)

    if providers:
        status = max((p["status"] for p in providers), key=lambda s: _STATUS_RANK.get(s, _STATUS_RANK["error"]))
    else:
        status = "error"
    ok = status in ("ok", "partial", "stale")
    top_error = next((p["error"] for p in providers if p["error"] is not None), None)
    return {
        "ok": ok,
        "status": status,
        "error": None if top_error is None else top_error,
        "providers": providers,
        "offers": [offer_to_dict(offer) for offer in ordered],
        "count": len(ordered),
        "gpu_models": summary,
        "filters_applied": {
            "provider": query.provider,
            "gpu_model": query.gpu_model,
            "cloud_type": query.cloud_type,
            "gpu_count": query.gpu_count,
            "in_stock_only": query.in_stock_only,
            "max_price_per_gpu_hour": query.max_price_per_gpu_hour,
            "sort": query.sort,
            "order": query.order,
        },
    }
