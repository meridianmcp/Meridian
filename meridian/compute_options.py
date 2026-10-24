"""Compute option selection: rank the machines that can run a job by estimated cost.

Given what a job needs (VRAM, system RAM, vCPUs, a price cap, a time limit) and a set of offers (a
machine, its price per hour, and optionally its measured throughput), this module keeps the offers that can
run the job and orders them by estimated total cost. It also estimates how much GPU memory an LLM needs.

Pure logic: no network, no credentials, no provider SDK. The caller supplies the offers, either from a JSON
catalog (:func:`load_catalog`) or from a provider adapter it injects, the same boundary the RunPod research
provider uses (:mod:`meridian.research.providers.runpod`). Nothing here creates, starts or bills anything.

Cost model: when both the job and the offer have a throughput figure, the estimated hours are
``work_units / throughput`` and the estimated cost is ``hours * price_per_hour``. Without a throughput figure
the time is unknown, so those offers are ranked by price per hour and the result says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

#: Bytes per parameter for the weight precisions this module knows about.
PRECISION_BYTES: dict[str, float] = {"fp32": 4.0, "fp16": 2.0, "bf16": 2.0, "int8": 1.0, "int4": 0.5}


@dataclass(frozen=True)
class Offer:
    """One machine a job could run on.

    ``vram_gb`` and ``ram_gb`` are the totals for the offer, not per GPU. ``throughput`` is in whatever work
    unit the caller uses for :attr:`Job.work_units` (tokens per second, samples per second, and so on), and is
    ``None`` when it has not been measured.
    """

    id: str
    provider: str
    price_per_hour: float
    vram_gb: float = 0.0
    ram_gb: float = 0.0
    vcpu: int = 0
    gpu: str | None = None
    throughput: float | None = None
    available: bool = True
    price_as_of: str | None = None


@dataclass(frozen=True)
class Job:
    """What a job needs. Every field is optional; an unset field places no constraint."""

    min_vram_gb: float = 0.0
    min_ram_gb: float = 0.0
    min_vcpu: int = 0
    work_units: float | None = None
    max_price_per_hour: float | None = None
    max_hours: float | None = None
    providers: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Ranked:
    """A feasible offer with its estimates. ``est_hours`` and ``est_cost`` are ``None`` when the time is unknown."""

    offer: Offer
    est_hours: float | None
    est_cost: float | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejected:
    """An offer that cannot run the job, with every reason it fails."""

    offer: Offer
    reasons: tuple[str, ...] = field(default_factory=tuple)


def estimate_llm_vram_gb(
    params_b: float,
    precision: str = "fp16",
    *,
    context_tokens: int = 0,
    batch_size: int = 1,
    n_layers: int | None = None,
    n_kv_heads: int | None = None,
    head_dim: int | None = None,
    kv_precision: str = "fp16",
    overhead: float = 1.2,
) -> float:
    """Estimate the GPU memory, in GB, that inference of a transformer LLM needs.

    Weights take ``params_b * bytes_per_param``. The KV cache takes ``2 * layers * kv_heads * head_dim *
    context_tokens * batch_size`` values (keys and values), and is counted only when the architecture is given.
    ``overhead`` covers activations, buffers and fragmentation. This is an estimate for planning, not a
    guarantee; the caller should leave headroom.
    """
    if params_b <= 0:
        raise ValueError("params_b must be greater than zero")
    if precision not in PRECISION_BYTES:
        raise ValueError(f"precision must be one of {sorted(PRECISION_BYTES)}, got {precision!r}")
    if kv_precision not in PRECISION_BYTES:
        raise ValueError(f"kv_precision must be one of {sorted(PRECISION_BYTES)}, got {kv_precision!r}")
    if context_tokens < 0 or batch_size < 1:
        raise ValueError("context_tokens must be zero or more and batch_size at least 1")
    if overhead < 1.0:
        raise ValueError("overhead must be at least 1.0")

    weights_gb = params_b * PRECISION_BYTES[precision]
    kv_gb = 0.0
    if n_layers and n_kv_heads and head_dim and context_tokens:
        kv_values = 2 * n_layers * n_kv_heads * head_dim * context_tokens * batch_size
        kv_gb = kv_values * PRECISION_BYTES[kv_precision] / 1e9
    return round((weights_gb + kv_gb) * overhead, 2)


def _feasibility(offer: Offer, job: Job) -> list[str]:
    reasons: list[str] = []
    if not offer.available:
        reasons.append("offer is not available")
    if job.providers is not None and offer.provider not in job.providers:
        reasons.append(f"provider {offer.provider!r} is not allowed")
    if offer.vram_gb < job.min_vram_gb:
        reasons.append(f"VRAM {offer.vram_gb:g} GB is below the {job.min_vram_gb:g} GB needed")
    if offer.ram_gb < job.min_ram_gb:
        reasons.append(f"RAM {offer.ram_gb:g} GB is below the {job.min_ram_gb:g} GB needed")
    if offer.vcpu < job.min_vcpu:
        reasons.append(f"{offer.vcpu} vCPU is below the {job.min_vcpu} needed")
    if job.max_price_per_hour is not None and offer.price_per_hour > job.max_price_per_hour:
        reasons.append(f"price {offer.price_per_hour:g}/h is above the {job.max_price_per_hour:g}/h cap")
    return reasons


def rank_offers(offers: Iterable[Offer], job: Job) -> tuple[list[Ranked], list[Rejected]]:
    """Split ``offers`` into the ones that can run ``job``, ordered best first, and the ones that cannot.

    Ordering: offers with a known estimated cost come first, cheapest total first, with the shorter run breaking
    ties. Offers without a known time follow, ordered by price per hour, and each carries a note saying the time
    is unknown. A ``max_hours`` limit drops offers whose estimated run is longer.
    """
    feasible: list[Ranked] = []
    rejected: list[Rejected] = []
    for offer in offers:
        reasons = _feasibility(offer, job)
        est_hours: float | None = None
        est_cost: float | None = None
        notes: list[str] = []
        if job.work_units is not None and offer.throughput:
            est_hours = job.work_units / offer.throughput
            est_cost = est_hours * offer.price_per_hour
            if job.max_hours is not None and est_hours > job.max_hours:
                reasons.append(f"estimated {est_hours:.2f} h is above the {job.max_hours:g} h limit")
        elif job.work_units is not None:
            notes.append("time unknown: no throughput figure, ranked by price per hour")
        if reasons:
            rejected.append(Rejected(offer=offer, reasons=tuple(reasons)))
        else:
            feasible.append(Ranked(offer=offer, est_hours=est_hours, est_cost=est_cost, notes=tuple(notes)))

    feasible.sort(key=_sort_key)
    return feasible, rejected


def _sort_key(ranked: Ranked) -> tuple[int, float, float, float, str]:
    if ranked.est_cost is not None:
        return (0, ranked.est_cost, ranked.est_hours or 0.0, ranked.offer.price_per_hour, ranked.offer.id)
    return (1, ranked.offer.price_per_hour, 0.0, 0.0, ranked.offer.id)


def cheapest(offers: Iterable[Offer], job: Job) -> Ranked | None:
    """The best feasible offer for ``job``, or ``None`` when nothing can run it."""
    ranked, _ = rank_offers(offers, job)
    return ranked[0] if ranked else None


def offer_from_dict(data: dict[str, Any]) -> Offer:
    """Build an :class:`Offer` from a catalog entry, rejecting malformed values."""
    try:
        offer = Offer(
            id=str(data["id"]),
            provider=str(data["provider"]),
            price_per_hour=float(data["price_per_hour"]),
            vram_gb=float(data.get("vram_gb", 0.0)),
            ram_gb=float(data.get("ram_gb", 0.0)),
            vcpu=int(data.get("vcpu", 0)),
            gpu=None if data.get("gpu") is None else str(data["gpu"]),
            throughput=None if data.get("throughput") is None else float(data["throughput"]),
            available=bool(data.get("available", True)),
            price_as_of=None if data.get("price_as_of") is None else str(data["price_as_of"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed catalog entry {data.get('id', '<no id>')!r}: {exc}") from exc
    if not offer.id or not offer.provider:
        raise ValueError("catalog entries need a non-empty id and provider")
    if offer.price_per_hour <= 0:
        raise ValueError(f"offer {offer.id!r}: price_per_hour must be greater than zero")
    if offer.vram_gb < 0 or offer.ram_gb < 0 or offer.vcpu < 0:
        raise ValueError(f"offer {offer.id!r}: VRAM, RAM and vCPU cannot be negative")
    if offer.throughput is not None and offer.throughput <= 0:
        raise ValueError(f"offer {offer.id!r}: throughput must be greater than zero when given")
    return offer


def load_catalog(path: str | Path) -> list[Offer]:
    """Read a catalog file: ``{"as_of": "...", "offers": [ {...}, ... ]}``. Each offer is checked on load."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        raise ValueError("a catalog is an object with an 'offers' list")
    as_of = data.get("as_of")
    offers = []
    for entry in data["offers"]:
        if not isinstance(entry, dict):
            raise ValueError("every catalog offer must be an object")
        if as_of is not None and entry.get("price_as_of") is None:
            entry = {**entry, "price_as_of": as_of}
        offers.append(offer_from_dict(entry))
    return offers


def offers_from_entries(entries: Sequence[dict[str, Any]]) -> list[Offer]:
    """Build offers from already-loaded dicts, for callers that get their catalog from a provider adapter."""
    return [offer_from_dict(entry) for entry in entries]
