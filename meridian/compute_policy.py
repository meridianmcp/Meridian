"""Project policy for compute choices: what to optimise, the limits, and a history that can be reverted.

This sits on :mod:`meridian.compute_options`, which ranks offers for a job. Here a project has a :class:`Policy`
that says what matters (cost, memory, throughput, or a weighted balance), which precisions may be used, and the
hard limits. :func:`plan_options` runs the model at each allowed precision, so a lower-memory precision can win
when the policy allows it. :func:`choose` returns the pick, the best option under each lens, and whether a person
has to confirm it.

A person or a chat can always override the pick. Policies are stored as a revision history, and :func:`revert`
adds a new revision that restores an old policy. Nothing is ever deleted, so a change of mind is itself recorded.

Assumptions:
- An offer's throughput is taken as the same at every precision. If a machine runs a quantised model at a different
  speed, supply a separate offer for that precision.
- Lower precision is only used when the policy lists it in ``allowed_precisions``. The default is the baseline
  precisions, so memory minimisation never quietly changes model quality.

Pure logic: no network, no credentials, nothing is provisioned or billed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from meridian import compute_options as co

OBJECTIVES = ("cost", "memory", "throughput", "balanced")
BASELINE_PRECISIONS = ("fp16", "bf16")
_LIMIT_FIELDS = ("max_price_per_hour", "max_hours", "min_throughput", "max_vram_gb")


@dataclass(frozen=True)
class Policy:
    """What a project wants from a compute choice. The defaults are the safe ones."""

    objective: str = "cost"
    max_price_per_hour: float | None = None
    max_hours: float | None = None
    min_throughput: float | None = None
    max_vram_gb: float | None = None
    allowed_precisions: tuple[str, ...] = BASELINE_PRECISIONS
    providers: tuple[str, ...] | None = None
    weights: tuple[float, float, float] = (1.0, 1.0, 1.0)  # cost, memory, time; used by "balanced" only

    def validate(self) -> None:
        if self.objective not in OBJECTIVES:
            raise ValueError(f"objective must be one of {list(OBJECTIVES)}, got {self.objective!r}")
        if not self.allowed_precisions:
            raise ValueError("allowed_precisions cannot be empty")
        unknown = [p for p in self.allowed_precisions if p not in co.PRECISION_BYTES]
        if unknown:
            raise ValueError(f"unknown precisions {unknown}; known: {sorted(co.PRECISION_BYTES)}")
        for name in _LIMIT_FIELDS:
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be greater than zero when set")
        if len(self.weights) != 3 or any(w < 0 for w in self.weights) or sum(self.weights) <= 0:
            raise ValueError("weights must be three non-negative numbers with a positive sum")

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "max_price_per_hour": self.max_price_per_hour,
            "max_hours": self.max_hours,
            "min_throughput": self.min_throughput,
            "max_vram_gb": self.max_vram_gb,
            "allowed_precisions": list(self.allowed_precisions),
            "providers": None if self.providers is None else list(self.providers),
            "weights": list(self.weights),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        policy = cls(
            objective=str(data.get("objective", "cost")),
            max_price_per_hour=data.get("max_price_per_hour"),
            max_hours=data.get("max_hours"),
            min_throughput=data.get("min_throughput"),
            max_vram_gb=data.get("max_vram_gb"),
            allowed_precisions=tuple(data.get("allowed_precisions", BASELINE_PRECISIONS)),
            providers=None if data.get("providers") is None else tuple(data["providers"]),
            weights=tuple(float(w) for w in data.get("weights", (1.0, 1.0, 1.0))),
        )
        policy.validate()
        return policy


@dataclass(frozen=True)
class ModelSpec:
    """The model being run. The architecture fields are optional; without them the KV cache is not counted."""

    params_b: float
    n_layers: int | None = None
    n_kv_heads: int | None = None
    head_dim: int | None = None


@dataclass(frozen=True)
class Workload:
    """How the model is used. ``work_units`` is the total work in the same unit as the offers' throughput."""

    context_tokens: int = 0
    batch_size: int = 1
    work_units: float | None = None


@dataclass(frozen=True)
class PlanOption:
    """One machine at one precision, with the estimates behind it."""

    precision: str
    vram_needed_gb: float
    ranked: co.Ranked

    @property
    def offer(self) -> co.Offer:
        return self.ranked.offer

    @property
    def est_cost(self) -> float | None:
        return self.ranked.est_cost

    @property
    def est_hours(self) -> float | None:
        return self.ranked.est_hours


@dataclass(frozen=True)
class Choice:
    """The result of :func:`choose`.

    ``chosen`` is the pick under the policy's objective, or ``None`` when nothing meets the policy. The lens fields
    show the best option under each single goal, so a person can see the trade-off. ``needs_human`` is set when a
    confirmation is needed, and ``reasons`` says why.
    """

    chosen: PlanOption | None
    ranked: tuple[PlanOption, ...]
    rejected: tuple[co.Rejected, ...]
    cheapest: PlanOption | None
    lowest_memory: PlanOption | None
    fastest: PlanOption | None
    needs_human: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _with_note(ranked: co.Ranked, note: str) -> co.Ranked:
    return replace(ranked, notes=(*ranked.notes, note))


def plan_options(
    model: ModelSpec,
    offers: Iterable[co.Offer],
    policy: Policy,
    workload: Workload = Workload(),
) -> tuple[list[PlanOption], list[co.Rejected]]:
    """Every feasible (precision, offer) pair under ``policy``, plus the offers that failed at each precision."""
    policy.validate()
    offers = list(offers)
    options: list[PlanOption] = []
    rejected: list[co.Rejected] = []
    for precision in policy.allowed_precisions:
        vram = co.estimate_llm_vram_gb(
            model.params_b,
            precision,
            context_tokens=workload.context_tokens,
            batch_size=workload.batch_size,
            n_layers=model.n_layers,
            n_kv_heads=model.n_kv_heads,
            head_dim=model.head_dim,
        )
        if policy.max_vram_gb is not None and vram > policy.max_vram_gb:
            continue  # this precision does not fit the project's VRAM cap at all
        job = co.Job(
            min_vram_gb=vram,
            providers=policy.providers,
            max_price_per_hour=policy.max_price_per_hour,
            max_hours=policy.max_hours,
            work_units=workload.work_units,
        )
        ranked, failed = co.rank_offers(offers, job)
        rejected.extend(failed)
        for item in ranked:
            if policy.min_throughput is not None:
                if item.offer.throughput is None:
                    item = _with_note(item, "throughput unknown: min_throughput not checked")
                elif item.offer.throughput < policy.min_throughput:
                    rejected.append(
                        co.Rejected(
                            offer=item.offer,
                            reasons=(
                                f"throughput {item.offer.throughput:g} is below the {policy.min_throughput:g} minimum",
                            ),
                        )
                    )
                    continue
            options.append(PlanOption(precision=precision, vram_needed_gb=vram, ranked=item))
    return options, rejected


def _unit(value: float | None, fallback: float) -> float:
    return fallback if value is None else value


def _order(options: list[PlanOption], policy: Policy) -> list[PlanOption]:
    if policy.objective == "cost":
        return sorted(options, key=_cost_key)
    if policy.objective == "memory":
        return sorted(
            options,
            key=lambda o: (o.vram_needed_gb, _unit(o.est_cost, float("inf")), o.offer.price_per_hour, o.offer.id),
        )
    if policy.objective == "throughput":
        return sorted(
            options,
            key=lambda o: (-_unit(o.offer.throughput, 0.0), o.offer.price_per_hour, o.offer.id),
        )
    return _order_balanced(options, policy.weights)


def _cost_key(option: PlanOption) -> tuple[int, float, float, float, str]:
    if option.est_cost is not None:
        return (0, option.est_cost, _unit(option.est_hours, 0.0), option.offer.price_per_hour, option.offer.id)
    return (1, option.offer.price_per_hour, 0.0, 0.0, option.offer.id)


def _normalise(values: list[float]) -> list[float]:
    low, high = min(values), max(values)
    if high == low:
        return [0.0 for _ in values]
    return [(v - low) / (high - low) for v in values]


def _order_balanced(options: list[PlanOption], weights: tuple[float, float, float]) -> list[PlanOption]:
    """Weighted score where 0 is best on each term. Cost uses the estimated total, or the hourly price when the time
    is unknown. Time with no estimate sits in the middle, so it neither wins nor loses by default."""
    if not options:
        return []
    costs = [_unit(o.est_cost, o.offer.price_per_hour) for o in options]
    memory = [o.vram_needed_gb for o in options]
    times = [_unit(o.est_hours, 0.5) if o.est_hours is not None else 0.5 for o in options]
    nc, nm, nt = _normalise(costs), _normalise(memory), _normalise(times)
    total = sum(weights)
    scores = [(weights[0] * c + weights[1] * m + weights[2] * t) / total for c, m, t in zip(nc, nm, nt)]
    return [o for _, o in sorted(zip(scores, options), key=lambda pair: (pair[0], pair[1].offer.id))]


def choose(
    model: ModelSpec,
    offers: Iterable[co.Offer],
    policy: Policy,
    workload: Workload = Workload(),
) -> Choice:
    """Pick the option the policy favours, show the best option under each single goal, and say whether a person
    must confirm the pick."""
    options, rejected = plan_options(model, offers, policy, workload)
    ordered = _order(options, policy)
    chosen = ordered[0] if ordered else None

    priced = [o for o in options if o.est_cost is not None]
    cheapest = min(priced, key=_cost_key) if priced else (
        min(options, key=lambda o: (o.offer.price_per_hour, o.offer.id)) if options else None
    )
    lowest_memory = min(options, key=lambda o: (o.vram_needed_gb, o.offer.id)) if options else None
    with_throughput = [o for o in options if o.offer.throughput is not None]
    fastest = max(with_throughput, key=lambda o: (o.offer.throughput, -o.offer.price_per_hour)) if with_throughput else None

    reasons: list[str] = []
    needs_human = False
    if chosen is None:
        reasons.append("no offer meets the policy")
        needs_human = True
    else:
        if chosen.precision not in BASELINE_PRECISIONS:
            reasons.append(f"runs at {chosen.precision}, not a baseline precision: confirm the quality trade-off")
            needs_human = True
        if policy.objective in ("memory", "throughput", "balanced") and chosen.offer.throughput is None:
            reasons.append("throughput is unknown for the chosen option, so the pick is a guess: measure it first")
            needs_human = True

    return Choice(
        chosen=chosen,
        ranked=tuple(ordered),
        rejected=tuple(rejected),
        cheapest=cheapest,
        lowest_memory=lowest_memory,
        fastest=fastest,
        needs_human=needs_human,
        reasons=tuple(reasons),
    )


# --- project policy history --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Revision:
    """One entry in a project's policy history. Revisions are never edited or removed."""

    version: int
    at: str
    actor: str
    reason: str
    policy: dict[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_text(value: str, name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is required so the change is attributable")
    return text


def current(history: Sequence[Revision]) -> Policy:
    """The policy in force. An empty history means the defaults."""
    if not history:
        return Policy()
    return Policy.from_dict(history[-1].policy)


def set_policy(
    history: Sequence[Revision],
    policy: Policy,
    *,
    actor: str,
    reason: str,
    at: str | None = None,
) -> list[Revision]:
    """Return the history with a new revision for ``policy``. ``actor`` and ``reason`` are required."""
    policy.validate()
    entry = Revision(
        version=len(history) + 1,
        at=at or _now(),
        actor=_require_text(actor, "actor"),
        reason=_require_text(reason, "reason"),
        policy=policy.to_dict(),
    )
    return [*history, entry]


def revert(
    history: Sequence[Revision],
    version: int,
    *,
    actor: str,
    reason: str,
    at: str | None = None,
) -> list[Revision]:
    """Restore the policy from an earlier revision. This adds a new revision, so the change of mind stays recorded."""
    match = next((r for r in history if r.version == version), None)
    if match is None:
        raise ValueError(f"no revision {version} in this project's history")
    return set_policy(history, Policy.from_dict(match.policy), actor=actor,
                      reason=f"revert to revision {version}: {_require_text(reason, 'reason')}", at=at)


def load_history(path: str | Path) -> list[Revision]:
    """Read a project's history file. A missing file is an empty history."""
    file = Path(path)
    if not file.exists():
        return []
    data = json.loads(file.read_text(encoding="utf-8"))
    return [Revision(**entry) for entry in data]


def save_history(path: str | Path, history: Sequence[Revision]) -> None:
    """Write a project's history file, creating its folder if needed."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps([r.__dict__ for r in history], indent=2), encoding="utf-8")
