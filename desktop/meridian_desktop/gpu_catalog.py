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


__all__ = [
    "CATALOG_QUERY",
    "CATALOG_URL",
    "DataCenterStock",
    "GpuCatalogError",
    "GpuOffer",
    "fetch_catalog",
    "parse_catalog",
]
