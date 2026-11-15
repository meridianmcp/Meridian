"""RunPod GPU catalog: the GPU types, prices and stock RunPod reports now. Pure Python: no Qt import.

The desktop calls RunPod itself, with the key from the operating system credential store (service meridian-desktop,
account runpod-api-key). The one request is an https GET to RunPod's v2 catalog. The key goes only in the
Authorization header. Redirects are refused, so the key is never sent to another host. A reply that is not the
expected shape, or any failure, gives a fixed message and never echoes the reply body.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CATALOG_URL = "https://api.runpod.io/v2/catalog/gpus"
CATALOG_QUERY = "include=AVAILABILITY&product=POD"
MAX_REPLY_BYTES = 2_000_000
MAX_OFFERS = 500
TIMEOUT_SECONDS = 20.0

NO_KEY_MESSAGE = "No RunPod key is saved on this computer, so the GPU list cannot be read."
UNREACHABLE_MESSAGE = "RunPod could not be reached."
TOO_LARGE_MESSAGE = "RunPod sent a reply that is too large to read."
BAD_SHAPE_MESSAGE = "RunPod sent a reply in a shape the desktop does not read."


class GpuCatalogError(Exception):
    """A failed read. The message is fixed text that never includes the reply body or the key."""


@dataclass(frozen=True)
class DataCenterStock:
    """One data center and its stock level as RunPod reports it (NONE, LOW, MEDIUM or HIGH)."""

    id: str
    availability: str


@dataclass(frozen=True)
class GpuOffer:
    """One GPU type from RunPod's catalog. Prices are US dollars per GPU hour, as RunPod reports them."""

    gpu_id: str
    name: str
    manufacturer: str
    memory_gb: int | None
    availability: str
    price_secure: float | None
    price_community: float | None
    max_count_secure: int | None
    max_count_community: int | None
    data_centers: tuple[DataCenterStock, ...]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the bearer key is only ever sent to api.runpod.io."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any, kind: type) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return kind(value)


def _data_centers(value: Any) -> tuple[DataCenterStock, ...]:
    if not isinstance(value, list):
        return ()
    rows = []
    for entry in value:
        if isinstance(entry, dict) and _text(entry.get("id")):
            rows.append(DataCenterStock(id=_text(entry["id"]), availability=_text(entry.get("availability")).upper()))
    return tuple(rows)


def parse_catalog(payload: Any) -> tuple[GpuOffer, ...]:
    """Read RunPod's catalog reply. Items that are not objects, or have no id, are skipped."""
    if not isinstance(payload, dict) or not isinstance(payload.get("gpus"), list):
        raise GpuCatalogError(BAD_SHAPE_MESSAGE)
    offers = []
    for entry in payload["gpus"][:MAX_OFFERS]:
        if not isinstance(entry, dict) or not _text(entry.get("id")):
            continue
        price = entry.get("price") if isinstance(entry.get("price"), dict) else {}
        max_count = entry.get("maxCount") if isinstance(entry.get("maxCount"), dict) else {}
        offers.append(
            GpuOffer(
                gpu_id=_text(entry["id"]),
                name=_text(entry.get("name")) or _text(entry["id"]),
                manufacturer=_text(entry.get("manufacturer")),
                memory_gb=_number(entry.get("memory"), int),
                availability=_text(entry.get("availability")).upper() or "UNKNOWN",
                price_secure=_number(price.get("secure"), float),
                price_community=_number(price.get("community"), float),
                max_count_secure=_number(max_count.get("secure"), int),
                max_count_community=_number(max_count.get("community"), int),
                data_centers=_data_centers(entry.get("dataCenters")),
            )
        )
    return tuple(offers)


def _default_open(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """Send the request through the no-redirect opener. Returns (status, body), with the body capped."""
    try:
        with _OPENER.open(request, timeout=timeout) as reply:
            return reply.status, reply.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def fetch_catalog(
    key: str | None,
    open_url: Callable[[urllib.request.Request, float], tuple[int, bytes]] = _default_open,
) -> tuple[GpuOffer, ...]:
    """Read the GPU catalog from RunPod with the saved key. ``open_url`` is replaced in tests so no socket opens."""
    if not key:
        raise GpuCatalogError(NO_KEY_MESSAGE)
    request = urllib.request.Request(
        f"{CATALOG_URL}?{CATALOG_QUERY}",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        status, body = open_url(request, TIMEOUT_SECONDS)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise GpuCatalogError(UNREACHABLE_MESSAGE) from None
    if status != 200:
        raise GpuCatalogError(f"RunPod refused the GPU list request (HTTP {status}).")
    if len(body) > MAX_REPLY_BYTES:
        raise GpuCatalogError(TOO_LARGE_MESSAGE)
    try:
        payload = json.loads(body)
    except ValueError:
        raise GpuCatalogError(BAD_SHAPE_MESSAGE) from None
    return parse_catalog(payload)


TIER_ANY = "any"
TIER_SECURE = "secure"
TIER_COMMUNITY = "community"


def offered_secure(offer: GpuOffer) -> bool:
    """RunPod lists a secure-cloud price for this GPU and has not capped it at zero."""
    return offer.price_secure is not None and offer.max_count_secure != 0


def offered_community(offer: GpuOffer) -> bool:
    """RunPod lists a community-cloud price for this GPU and has not capped it at zero."""
    return offer.price_community is not None and offer.max_count_community != 0


def tier_price(offer: GpuOffer, tier: str) -> float | None:
    """The price per GPU hour for one tier, or None when the tier does not offer the GPU.

    With TIER_ANY it is the lowest price among the tiers that offer the GPU.
    """
    if tier == TIER_SECURE:
        return offer.price_secure if offered_secure(offer) else None
    if tier == TIER_COMMUNITY:
        return offer.price_community if offered_community(offer) else None
    prices = [p for p in (tier_price(offer, TIER_SECURE), tier_price(offer, TIER_COMMUNITY)) if p is not None]
    return min(prices) if prices else None


def filter_offers(
    offers: tuple[GpuOffer, ...] | list[GpuOffer],
    *,
    text: str = "",
    min_memory_gb: int = 0,
    tier: str = TIER_ANY,
    data_center: str = "",
    max_price: float = 0.0,
) -> tuple[GpuOffer, ...]:
    """The offers that pass every filter, in their order. A zero minimum or price, or an empty text or data
    center, means no filter on that field."""
    needle = text.strip().casefold()
    kept = []
    for offer in offers:
        if needle and needle not in " ".join((offer.gpu_id, offer.name, offer.manufacturer)).casefold():
            continue
        if min_memory_gb and (offer.memory_gb is None or offer.memory_gb < min_memory_gb):
            continue
        if tier == TIER_SECURE and not offered_secure(offer):
            continue
        if tier == TIER_COMMUNITY and not offered_community(offer):
            continue
        if data_center and not any(dc.id == data_center for dc in offer.data_centers):
            continue
        if max_price:
            price = tier_price(offer, tier)
            if price is None or price > max_price:
                continue
        kept.append(offer)
    return tuple(kept)


__all__ = [
    "CATALOG_QUERY",
    "CATALOG_URL",
    "DataCenterStock",
    "GpuCatalogError",
    "GpuOffer",
    "TIER_ANY",
    "TIER_COMMUNITY",
    "TIER_SECURE",
    "fetch_catalog",
    "filter_offers",
    "offered_community",
    "offered_secure",
    "parse_catalog",
    "tier_price",
]
