"""RunPod GPU offer source: the REST v2 catalog as base :class:`~meridian.compute_options.Offer` rows.

Calls per refresh: one price call (``GET /v2/catalog/gpus``), then one availability call per cloud
(``include=AVAILABILITY``, ``product=POD``, ``count=1``). Each call has explicit timeouts (see
:data:`meridian.research.providers.runpod_client.CATALOG_TIMEOUT`). Calls that have not started when the refresh
deadline passes are skipped, and their stock is reported as unknown.

Rules:

* The key comes from :mod:`meridian.credentials` (the same refs as the MCP tools). It is sent only as the
  ``Authorization`` header. A missing key is the ``not_configured`` state, with no HTTP call.
* Provider text (names, ids, data center ids) is copied as data. Errors are mapped to fixed codes and fixed text.
* A price that is zero, negative or not a number becomes ``None`` and is counted in a warning.
* A price response with the wrong shape fails the whole refresh (``unexpected_shape``). So does one where GPUs are
  present but none has a documented price key, which is what a renamed field looks like. An availability response
  with the wrong shape, or with GPUs but no stock key, only makes that cloud's stock unknown, so the response is
  never partly read.
* Pass 1 covers the GPU price fields the spec names. ``maxCount`` is read as an integer or a per-cloud object,
  and anything else is ``None``, because its live shape is not confirmed.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from meridian.compute_catalog import (
    CatalogError,
    CatalogFetch,
    ERROR_MESSAGES,
    STOCK_VALUES,
    OfferCapabilities,
    format_utc_stamp,
    normalize_gpu,
    utc_now,
)
from meridian.compute_options import Offer
from meridian.credentials import CredentialError, describe_source, resolve_credential
from meridian.research.providers.runpod_client import RunPodAPIError, RunPodClient

RUNPOD_PROVIDER = "runpod"
#: Refresh deadline in seconds. Calls not started before it are skipped (spec 5.3).
DEFAULT_DEADLINE_SECONDS = 25.0
#: (cloud_type, the API's cloud value). Rows use the lower-case cloud type.
CLOUDS: tuple[tuple[str, str], ...] = (("secure", "SECURE"), ("community", "COMMUNITY"))
_INVALID_KEY_MESSAGE = "The RunPod key resolved but is not a usable API key. The value was not shown."
_TIMEOUT_PREFIXES = ("ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException")
_MAX_RETRY_AFTER_S = 3600
_NAME_CAP = 200


class CatalogShapeError(ValueError):
    """A catalog response did not have the documented shape. The message is internal and never shown."""


@dataclass(frozen=True)
class RunPodGpu:
    """One GPU type from the price response. ``flags`` and ``prices`` are keyed by cloud type."""

    gpu_id: str
    name: str | None
    memory_gb: int | None
    flags: Mapping[str, bool | None] = field(default_factory=dict)
    prices: Mapping[str, float | None] = field(default_factory=dict)
    invalid_prices: Mapping[str, bool] = field(default_factory=dict)
    max_counts: Mapping[str, int | None] = field(default_factory=dict)


#: Stock for one GPU in one cloud: its enum and its data centers as ``(dc_id, stock)`` pairs.
StockEntry = tuple[str | None, tuple[tuple[str, str | None], ...]]


# ----------------------------------------------------------------------------
# Error mapping
# ----------------------------------------------------------------------------


def _error(code: str, retry_after_s: int | None = None) -> CatalogError:
    return CatalogError(code, ERROR_MESSAGES[code], retry_after_s)


def catalog_error_from(exc: RunPodAPIError) -> CatalogError:
    """Map a client error to a fixed code and text. The client's own message is never copied."""
    if exc.code == "INVALID_RESPONSE":
        return _error("unexpected_shape")
    if exc.status_code is None:
        if exc.code == "TRANSPORT_ERROR" and str(exc.message).startswith(_TIMEOUT_PREFIXES):
            return _error("timeout")
        return _error("unreachable")
    if exc.status_code == 401:
        return _error("auth_failed")
    if exc.status_code == 403:
        return _error("forbidden")
    if exc.status_code == 429:
        retry = None
        if exc.retry_after is not None:
            retry = int(min(max(exc.retry_after, 0), _MAX_RETRY_AFTER_S))
        return _error("rate_limited", retry)
    return _error("upstream_error")


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------


def _gpu_items(body: object) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        raise CatalogShapeError("response is not an object")
    gpus = body.get("gpus")
    if not isinstance(gpus, list):
        raise CatalogShapeError("response has no gpus list")
    items: list[dict[str, Any]] = []
    for item in gpus:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise CatalogShapeError("a GPU entry has no string id")
        items.append(item)
    return items


def _positive_int(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, float) and raw.is_integer() and raw > 0:
        return int(raw)
    return None


def _flag(raw: object) -> bool | None:
    return raw if isinstance(raw, bool) else None


def _text(raw: object) -> str | None:
    return raw[:_NAME_CAP] if isinstance(raw, str) and raw else None


def _stock(raw: object) -> str | None:
    if isinstance(raw, str) and raw.upper() in STOCK_VALUES:
        return raw.upper()
    return None


def _price_value(raw: object) -> tuple[float | None, bool]:
    """``(price, invalid)``. A missing price is ``(None, False)``. A zero, negative, non-finite or non-numeric
    value is ``(None, True)``."""
    if raw is None:
        return None, False
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, True
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        return None, True
    return value, False


def _max_count(raw: object, cloud_type: str) -> int | None:
    """``maxCount`` as an int for both clouds, or the value for this cloud from an object. Anything else is None."""
    if isinstance(raw, dict):
        raw = raw.get(cloud_type, raw.get(cloud_type.upper()))
    return _positive_int(raw)


def _has_price_key(price_block: Mapping[str, Any] | None) -> bool:
    """True when the price object has a key for a cloud this adapter reads. A null value still counts, because
    RunPod marks a cloud with no price as null."""
    return price_block is not None and any(cloud_type in price_block for cloud_type, _api in CLOUDS)


def parse_prices(body: object) -> list[RunPodGpu]:
    """Read the price response. Any malformed entry fails the whole response, so none is partly read.

    GPUs with no documented price key at all are a renamed field, not a missing price. That fails the whole response
    (``CatalogShapeError``), so the view cannot show every price as unknown under an ok status (spec R1).
    """
    items = _gpu_items(body)
    gpus: list[RunPodGpu] = []
    price_key_seen = False
    for item in items:
        price_block = item.get("price")
        if price_block is not None and not isinstance(price_block, dict):
            raise CatalogShapeError("a GPU price is not an object")
        price_key_seen = price_key_seen or _has_price_key(price_block)
        flags: dict[str, bool | None] = {}
        prices: dict[str, float | None] = {}
        invalid: dict[str, bool] = {}
        max_counts: dict[str, int | None] = {}
        for cloud_type, _api in CLOUDS:
            flags[cloud_type] = _flag(item.get(cloud_type))
            value, bad = _price_value((price_block or {}).get(cloud_type))
            prices[cloud_type] = value
            invalid[cloud_type] = bad
            max_counts[cloud_type] = _max_count(item.get("maxCount"), cloud_type)
        gpus.append(
            RunPodGpu(
                gpu_id=item["id"],
                name=_text(item.get("name")),
                memory_gb=_positive_int(item.get("memory")),
                flags=flags,
                prices=prices,
                invalid_prices=invalid,
                max_counts=max_counts,
            )
        )
    if items and not price_key_seen:
        raise CatalogShapeError("no GPU has a documented price key")
    return gpus


def parse_availability(body: object) -> dict[str, StockEntry]:
    """Read one cloud's availability response, keyed by GPU id. Any malformed entry fails the whole response.

    GPUs with no stock key on the entry or on any data center are a renamed field. That fails this cloud's stock
    (the caller marks it unknown and the refresh is partial), not the whole refresh.
    """
    items = _gpu_items(body)
    entries: dict[str, StockEntry] = {}
    stock_key_seen = False
    for item in items:
        raw_centers = item.get("dataCenters")
        centers: list[tuple[str, str | None]] = []
        if raw_centers is not None:
            if not isinstance(raw_centers, list):
                raise CatalogShapeError("dataCenters is not a list")
            for center in raw_centers:
                if not isinstance(center, dict) or not isinstance(center.get("id"), str) or not center["id"]:
                    raise CatalogShapeError("a data center has no string id")
                stock_key_seen = stock_key_seen or "availability" in center
                centers.append((center["id"], _stock(center.get("availability"))))
        stock_key_seen = stock_key_seen or "availability" in item
        entries[item["id"]] = (_stock(item.get("availability")), tuple(centers))
    if items and not stock_key_seen:
        raise CatalogShapeError("no availability entry has a stock key")
    return entries


# ----------------------------------------------------------------------------
# Building base rows
# ----------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


def _available(stock: str | None) -> bool | None:
    if stock is None:
        return None
    return stock != "NONE"


def _rows_for_cloud(stock_map: Mapping[str, StockEntry] | None, gpu_id: str) -> list[tuple[str | None, str | None]]:
    """``(data_center, stock)`` pairs for one GPU in one cloud. Stock is unknown when the cloud's availability
    call failed, or when the GPU is absent from its response. A GPU with no data centers gets one aggregate row."""
    if stock_map is None or gpu_id not in stock_map:
        return [(None, None)]
    stock, centers = stock_map[gpu_id]
    if centers:
        return [(dc_id, dc_stock) for dc_id, dc_stock in centers]
    return [(None, stock)]


def build_offers(
    gpus: Sequence[RunPodGpu],
    stock_by_cloud: Mapping[str, Mapping[str, StockEntry] | None],
    prices_as_of: datetime,
) -> tuple[list[Offer], int]:
    """Base rows (one GPU each) from the price list and the per-cloud stock.

    Returns the rows and the number of prices that were present but invalid. Row ids follow
    ``runpod:{gpu}:{cloud}:{data_center or '-'}``, so one id is never produced twice.
    """
    offers: list[Offer] = []
    seen: set[str] = set()
    invalid_count = 0
    stamp = format_utc_stamp(prices_as_of)
    for gpu in gpus:
        model, variant = normalize_gpu(gpu.name or gpu.gpu_id, gpu.memory_gb)
        if model is None:
            model = _slug(gpu.gpu_id)
        for cloud_type, _api in CLOUDS:
            flag = gpu.flags.get(cloud_type)
            price = gpu.prices.get(cloud_type)
            bad = bool(gpu.invalid_prices.get(cloud_type))
            if flag is False:
                continue
            if flag is None and price is None and not bad:
                continue
            if bad:
                invalid_count += 1
            for dc_id, stock in _rows_for_cloud(stock_by_cloud.get(cloud_type), gpu.gpu_id):
                offer_id = f"{RUNPOD_PROVIDER}:{gpu.gpu_id}:{cloud_type}:{dc_id or '-'}"
                if offer_id in seen:
                    continue
                seen.add(offer_id)
                offers.append(
                    Offer(
                        id=offer_id,
                        provider=RUNPOD_PROVIDER,
                        price_per_hour=price,
                        vram_gb=None if gpu.memory_gb is None else float(gpu.memory_gb),
                        ram_gb=None,
                        vcpu=None,
                        gpu=gpu.gpu_id,
                        throughput=None,
                        available=_available(stock),
                        price_as_of=stamp,
                        gpu_model=model,
                        gpu_variant=variant,
                        gpu_count=1,
                        max_gpu_count=gpu.max_counts.get(cloud_type),
                        price_per_gpu_hour=price,
                        vram_gb_per_gpu=gpu.memory_gb,
                        cloud_type=cloud_type,
                        data_center=dc_id,
                        stock_status=stock,
                    )
                )
    return offers, invalid_count


# ----------------------------------------------------------------------------
# The source
# ----------------------------------------------------------------------------


def resolve_secret(refs: Sequence[str], *, hosted: bool) -> tuple[str | None, str | None]:
    """``(kind, secret)`` from the first reference that resolves, or ``(None, None)``.

    A failed reference is skipped without its message, because resolver messages can name variables, services and
    paths. The secret is returned to the caller and never logged.
    """
    for ref in refs:
        try:
            kind = describe_source(ref)
        except CredentialError:
            continue
        try:
            credential = resolve_credential(ref, hosted=hosted)
        except CredentialError:
            continue
        return kind, credential.secret
    return None, None


def _failure(code: str, kind: str | None = None, message: str | None = None) -> CatalogFetch:
    error = CatalogError(code, message or ERROR_MESSAGES[code])
    return CatalogFetch(offers=(), prices_as_of=None, stock_as_of=None, error=error, credential_source=kind)


class RunPodOffersSource:
    """RunPod's catalog as base offer rows. Satisfies :class:`meridian.compute_catalog.OffersSource`.

    ``client_factory`` receives the key and returns a client with ``get_gpu_prices``,
    ``get_gpu_availability`` and ``close``. Tests inject a fake or an ``httpx.MockTransport`` client.
    """

    name = RUNPOD_PROVIDER
    capabilities = OfferCapabilities(has_price=True, capacity_only=False)

    def __init__(
        self,
        *,
        credential_refs: Sequence[str],
        hosted: bool = False,
        client_factory: Callable[[str], Any] = RunPodClient,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        self._refs = tuple(credential_refs)
        self._hosted = hosted
        self._client_factory = client_factory
        self._clock = clock
        self._monotonic = monotonic
        self._deadline = deadline_seconds

    def fetch_offers(self) -> CatalogFetch:
        kind, secret = resolve_secret(self._refs, hosted=self._hosted)
        if secret is None:
            return _failure("not_configured")
        try:
            client = self._client_factory(secret)
        except ValueError:
            return _failure("not_configured", kind, _INVALID_KEY_MESSAGE)
        try:
            return self._fetch_with(client, kind)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 -- a failed close must not replace the result
                    pass

    def _fetch_with(self, client: Any, kind: str | None) -> CatalogFetch:
        started = self._monotonic()
        try:
            price_body = client.get_gpu_prices()
        except RunPodAPIError as exc:
            error = catalog_error_from(exc)
            return CatalogFetch(
                offers=(), prices_as_of=None, stock_as_of=None, error=error, credential_source=kind
            )
        prices_as_of = self._clock()
        try:
            gpus = parse_prices(price_body)
        except CatalogShapeError:
            return _failure("unexpected_shape", kind)

        warnings: list[str] = []
        stock_by_cloud: dict[str, dict[str, StockEntry] | None] = {}
        stock_times: list[datetime] = []
        for cloud_type, api_cloud in CLOUDS:
            if self._monotonic() - started > self._deadline:
                stock_by_cloud[cloud_type] = None
                warnings.append(f"Stock for the {cloud_type} cloud was not refreshed before the deadline.")
                continue
            try:
                stock_by_cloud[cloud_type] = parse_availability(client.get_gpu_availability(api_cloud))
                stock_times.append(self._clock())
            except RunPodAPIError as exc:
                stock_by_cloud[cloud_type] = None
                warnings.append(
                    f"Stock is unavailable for the {cloud_type} cloud: {catalog_error_from(exc).message}"
                )
            except CatalogShapeError:
                stock_by_cloud[cloud_type] = None
                warnings.append(
                    f"Stock is unavailable for the {cloud_type} cloud: the response was not recognised."
                )

        offers, invalid_count = build_offers(gpus, stock_by_cloud, prices_as_of)
        if invalid_count:
            warnings.append(
                f"{invalid_count} price value(s) were zero, negative or not a number and are shown as not reported."
            )
        partial = any(stock_by_cloud.get(cloud_type) is None for cloud_type, _api in CLOUDS)
        return CatalogFetch(
            offers=tuple(offers),
            prices_as_of=prices_as_of,
            stock_as_of=min(stock_times) if stock_times else None,
            warnings=tuple(warnings),
            error=None,
            partial=partial,
            credential_source=kind,
        )
