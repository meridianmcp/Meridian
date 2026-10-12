"""Model price table and cache-aware cost formula for the usage ledger (824acdc6).

Pure data and arithmetic: no DB, no network, no model call. Consumed by
``meridian.usage_ledger``. Slice 1 of the host-neutral usage ledger.

Rates are USD per one million tokens, held as Decimal. The table is data, not
code: callers load it from a mapping (for example a fetched or refreshed price
file) through ``load_price_table``, which stamps a version string so every
report can say which prices produced its numbers.

This module ships NO prices. ``EMPTY_PRICE_TABLE`` prices nothing, so every
cost is reported as unpriced (None) until a table is loaded. Do not type rates
in from memory. The item notes name MIT LiteLLM's
model_prices_and_context_window.json as the candidate source; fetch or refresh
it with attribution rather than vendoring it.

Cost is recomputed from tokens and the table, never taken from host-reported
cost. Claude transcripts report one output_tokens figure that already includes
thinking tokens, so there is no separate reasoning rate.

Units: 1 USD = 1,000,000 micros and a rate is USD per 1,000,000 tokens, so
``tokens * rate`` is already in micros. The total is rounded half-up to whole
micros once, not per field.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # type-only: usage_ledger imports this module at runtime
    from meridian.usage_ledger import UsageRecord

PRICE_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
EMPTY_TABLE_VERSION = "unpopulated"


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens for each billable token class."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal


@dataclass(frozen=True)
class PriceTable:
    """A version-stamped mapping of model id to ModelPrice."""

    version: str
    models: Mapping[str, ModelPrice] = field(default_factory=dict)

    def lookup(self, model: str) -> ModelPrice | None:
        """Exact model-id match only. Fuzzy matching could misprice silently."""
        return self.models.get(model)


EMPTY_PRICE_TABLE = PriceTable(version=EMPTY_TABLE_VERSION, models={})


def _as_rate(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{where}: rate must be a number, got {type(value).__name__}")
    try:
        rate = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{where}: rate {value!r} is not a number") from exc
    if not rate.is_finite() or rate < 0:
        raise ValueError(f"{where}: rate must be finite and >= 0")
    return rate


def load_price_table(data: Mapping[str, Any]) -> PriceTable:
    """Build a PriceTable from ``{"version": str, "models": {id: {field: rate}}}``.

    Fails loudly on a malformed table. A caller who supplies prices should never
    get a silently partial table: a missing or unknown field raises ValueError.
    """
    if not isinstance(data, Mapping):
        raise ValueError("price table must be a mapping")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("price table needs a non-empty string 'version' stamp")
    raw_models = data.get("models")
    if not isinstance(raw_models, Mapping):
        raise ValueError("price table needs a 'models' mapping")
    models: dict[str, ModelPrice] = {}
    for model_id, entry in raw_models.items():
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model ids must be non-empty strings")
        if not isinstance(entry, Mapping):
            raise ValueError(f"{model_id}: entry must be a mapping")
        unknown = sorted(set(entry) - set(PRICE_FIELDS))
        if unknown:
            raise ValueError(f"{model_id}: unknown price fields {unknown}")
        missing = [name for name in PRICE_FIELDS if name not in entry]
        if missing:
            raise ValueError(f"{model_id}: missing price fields {missing}")
        models[model_id] = ModelPrice(
            **{name: _as_rate(entry[name], f"{model_id}.{name}") for name in PRICE_FIELDS}
        )
    return PriceTable(version=version.strip(), models=models)


def cost_micros(record: UsageRecord, table: PriceTable) -> int | None:
    """Cache-aware cost of one usage record in micros, or None if unpriced.

    Cache reads bill at the cache_read rate, never at the input rate. Cache
    writes bill at the rate for their TTL bucket (5m or 1h).
    """
    price = table.lookup(record.model)
    if price is None:
        return None
    total = (
        Decimal(record.input_tokens) * price.input
        + Decimal(record.output_tokens) * price.output
        + Decimal(record.cache_read_tokens) * price.cache_read
        + Decimal(record.cache_write_5m_tokens) * price.cache_write_5m
        + Decimal(record.cache_write_1h_tokens) * price.cache_write_1h
    )
    return int(total.to_integral_value(rounding=ROUND_HALF_UP))
