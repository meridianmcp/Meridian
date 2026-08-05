"""Retrieval evaluation suite (sprint 116c75c9, item_group handoff-retrieval-v026).

A reproducible, machine-readable evaluation harness for Meridian's search
backends, built from a project's REAL notes/sprint-items/pointers/handoffs
rather than hand-picked fixtures.

Design
------
``run_evaluation(db, project_id)`` copies a read-only sample of the target
project's real content (notes, sprint items, sprint-item pointers, the
latest handoff body) into a disposable **shadow project** (plus one sibling
project used only as a cross-project leakage canary). All eval queries run
against the shadow/sibling projects — the caller's real project is never
written to. Both scratch projects are deleted at the end of the run unless
``cleanup=False``.

Materializing everything as ``project_notes`` rows (regardless of original
source kind) keeps every backend comparable on one apples-to-apples table
instead of fighting the fact that :func:`meridian.db.search_all` returns
four separately-ranked groups (tasks/notes/decisions/sprint_items) that
can't be merged into one global rank order. The original source kind is
kept as the note's ``tags`` value for traceability.

Case kinds (see the notes on sprint item 116c75c9)
---------------------------------------------------
* ``exact_match``         — query is a real record's own text verbatim;
  the record must come back at rank 1. The simplest possible control.
* ``paraphrase``           — query is a deterministic, non-identical
  reordering/synonym-substitution of a real record's title; the record
  must appear in the top ``k`` (recall@k, not recall@1).
* ``adversarial_lexical``  — two real records that share salient tokens by
  chance; the query targets one of them and the OTHER (irrelevant) record
  must not win rank 1. Measures lexical-overlap false positives.
* ``leakage``              — a distinctive nonce note lives ONLY in the
  sibling scratch project; querying the shadow project for that nonce must
  never surface it. Regression guard for project-scoping bugs.
* ``ambiguous_abstention`` — two synthetic near-duplicate notes are added to
  the shadow project; a correct backend surfaces BOTH in the top results
  rather than silently discarding one in favor of an overconfident single
  pick. (This is the specific, testable definition of "abstention" used
  here — see :func:`_compute_backend_metrics`.)

Backends
--------
* ``keyword_fts``     — always available; wraps :func:`meridian.db.search_all`.
* ``model2vec_rerank`` — only run when ``include_model2vec=True`` AND
  :func:`meridian.semantic_search.is_available` is True (i.e. the caller has
  explicitly opted in via ``MERIDIAN_SEMANTIC_ENABLED`` and model2vec is
  importable). Never force-loads the model otherwise.
* ``duckdb_vss`` / ``pgvector`` — pluggable extension points. Neither backend
  is implemented in this codebase yet (tracked by sprint item e1475682); the
  harness looks for an optional ``meridian.vector_index`` /
  ``meridian.pgvector_index`` module exposing
  ``search(db, project_id, query, k) -> list[tuple[id, score]]`` (sync or
  async) and reports a precise, honest "unavailable" reason when absent
  rather than silently skipping. Once either module exists, this harness
  picks it up with no changes here.

Metrics recorded per backend: ``exact_match_recall_at_1``,
``paraphrase_recall_at_k``, ``adversarial_false_positive_rate``,
``leakage_count``/``leakage_rate``, ``ambiguous_abstention_rate``,
``wall_time_ms_mean``/``wall_time_ms_total``, ``rss_delta_mb`` (reuses
:func:`meridian.semantic_search.rss_mb`, the existing portable RSS reader),
and ``remote_round_trips`` (a proxy count, NOT a billed dollar figure —
Meridian has no provider billing integration to draw a real cost from).

``evaluate_gate(report)`` turns the report into a pass/fail decision per
backend against configurable :class:`GateThresholds`, with a zero-leakage
hard requirement, and recommends a default backend (``keyword_fts`` whenever
it passes, since it is always available and carries no extra RSS/latency
cost) — this is the "gate for defaults" the sprint item asks for.
"""

from __future__ import annotations

import importlib
import inspect
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from meridian import db as db_module
from meridian import semantic_search as _semantic_search

# ---------------------------------------------------------------------------
# Dataset primitives
# ---------------------------------------------------------------------------

_CASE_KINDS = (
    "exact_match",
    "paraphrase",
    "adversarial_lexical",
    "leakage",
    "ambiguous_abstention",
)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "is", "are", "this", "that", "it", "its", "be", "as", "at", "by",
}

_SYNONYMS = {
    "add": "create", "fix": "repair", "remove": "delete", "get": "retrieve",
    "make": "build", "check": "verify", "use": "utilize", "show": "display",
    "update": "revise", "start": "begin", "stop": "halt",
}


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    kind: str
    project_id: str
    query: str
    expected_ids: tuple[str, ...] = ()
    forbidden_ids: tuple[str, ...] = ()
    description: str = ""


@dataclass
class BackendMetrics:
    backend: str
    available: bool
    skip_reason: str | None
    cases_run: int
    exact_match_recall_at_1: float | None
    paraphrase_recall_at_k: float | None
    adversarial_false_positive_rate: float | None
    leakage_count: int
    leakage_rate: float | None
    ambiguous_abstention_rate: float | None
    wall_time_ms_mean: float | None
    wall_time_ms_total: float | None
    rss_delta_mb: float | None
    remote_round_trips: int
    remote_db_cost_note: str


@dataclass
class GateThresholds:
    min_exact_match_recall_at_1: float = 0.9
    min_paraphrase_recall_at_k: float = 0.5
    max_adversarial_false_positive_rate: float = 0.34
    max_wall_time_ms_mean: float = 2000.0
    require_zero_leakage: bool = True


@dataclass
class GateDecision:
    passed_backends: list[str]
    failed_backends: dict[str, list[str]]
    skipped_backends: list[str]
    recommended_default: str | None
    reasons: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    project_id: str
    generated_at: str
    k: int
    dataset_size: int
    cases_by_kind: dict[str, int]
    backends: dict[str, BackendMetrics]


# ---------------------------------------------------------------------------
# Small deterministic text helpers (no external NLP deps — reproducible).
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    return [
        w.lower().strip(".,;:!?()[]{}\"'")
        for w in (text or "").split()
        if w.strip()
    ]


def _salient_tokens(text: str) -> list[str]:
    return [t for t in _tokenize(text) if len(t) >= 4 and t not in _STOPWORDS]


def _paraphrase(text: str, limit: int = 120) -> str:
    """Deterministic, non-identical rewrite: synonym-substitute then reverse
    token order. Not a real paraphraser — a reproducible lexical-variant
    probe for recall@k, distinct from the exact-match control string."""
    toks = _tokenize(text)[: max(3, limit // 6)]
    toks = [_SYNONYMS.get(t, t) for t in toks]
    return " ".join(reversed(toks))[:limit]


def _best_overlap_pair(
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], list[str]] | None:
    """Pick the two DISTINCT records with the largest salient-token overlap."""
    best: tuple[dict[str, Any], dict[str, Any], list[str]] | None = None
    for i, a in enumerate(records):
        a_tokens = set(_salient_tokens(a["text"]))
        if not a_tokens:
            continue
        for j, b in enumerate(records):
            if i == j:
                continue
            shared = a_tokens & set(_salient_tokens(b["text"]))
            if not shared:
                continue
            if best is None or len(shared) > len(best[2]):
                best = (a, b, sorted(shared))
    return best


def _first_meaningful_excerpt(body: str, max_chars: int = 300) -> str:
    for line in (body or "").splitlines():
        stripped = line.strip().lstrip("#>*- ").strip()
        if len(stripped) >= 20:
            return stripped[:max_chars]
    return (body or "").strip()[:max_chars]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


# ---------------------------------------------------------------------------
# Real-corpus collection (READ-ONLY on the caller's project).
# ---------------------------------------------------------------------------


async def _collect_real_corpus(
    db: Any,
    project_id: str,
    *,
    max_notes: int = 8,
    max_sprint_items: int = 8,
    max_pointers: int = 4,
) -> list[dict[str, Any]]:
    """Pull a bounded, read-only sample of real project content.

    Never mutates ``project_id`` — every row fetched here is only ever
    COPIED into a disposable shadow project by the caller.
    """
    records: list[dict[str, Any]] = []

    try:
        notes = await db_module.get_project_notes(
            db, project_id, bodies=True, limit=max_notes
        )
    except Exception:  # noqa: BLE001 - best-effort corpus collection
        notes = []
    for n in notes:
        text = f"{n.get('title') or ''} {n.get('body') or ''}".strip()
        if text:
            records.append({
                "source_kind": "note", "source_id": n.get("id"),
                "title": n.get("title") or "", "text": text,
            })

    try:
        sprint_items = await db_module.get_sprint_items(
            db, project_id, include_human=True
        )
    except Exception:  # noqa: BLE001
        sprint_items = []
    sprint_items = sprint_items[:max_sprint_items]
    for s in sprint_items:
        text = f"{s.get('title') or ''} {s.get('notes') or ''}".strip()
        if text:
            records.append({
                "source_kind": "sprint_item", "source_id": s.get("id"),
                "title": s.get("title") or "", "text": text,
            })

    pointer_budget = max_pointers
    for s in sprint_items:
        if pointer_budget <= 0:
            break
        try:
            pointers = await db_module.get_sprint_item_pointers(db, s["id"])
        except Exception:  # noqa: BLE001
            pointers = []
        for p in pointers:
            if pointer_budget <= 0:
                break
            label = (p.get("label") or "").strip()
            if not label:
                targets = p.get("targets") or []
                label = "; ".join(
                    str(t.get("uri", "")) for t in targets if isinstance(t, dict)
                )[:200]
            text = f"pointer {p.get('source_type', '')} {label}".strip()
            if text and label:
                records.append({
                    "source_kind": "pointer", "source_id": p.get("id"),
                    "title": (label[:60] or p.get("source_type", "")), "text": text,
                })
                pointer_budget -= 1

    try:
        handoff = await db_module.get_latest_handoff(db, project_id)
    except Exception:  # noqa: BLE001
        handoff = None
    if handoff and handoff.get("body"):
        excerpt = _first_meaningful_excerpt(handoff["body"])
        if excerpt:
            records.append({
                "source_kind": "handoff", "source_id": handoff.get("id"),
                "title": "handoff excerpt", "text": excerpt,
            })

    return records


# ---------------------------------------------------------------------------
# Case construction.
# ---------------------------------------------------------------------------


def _build_corpus_cases(shadow_records: list[dict[str, Any]], k: int) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for rec in shadow_records:
        q = rec["text"][:200].strip()
        if len(q) < 4:
            continue
        cases.append(EvalCase(
            case_id=f"exact-{rec['shadow_id']}",
            kind="exact_match",
            project_id=rec["shadow_project_id"],
            query=q,
            expected_ids=(rec["shadow_id"],),
            description=f"exact-match control from {rec['source_kind']}",
        ))
    for rec in shadow_records:
        para = _paraphrase(rec["title"] or rec["text"])
        if len(para) < 4:
            continue
        cases.append(EvalCase(
            case_id=f"paraphrase-{rec['shadow_id']}",
            kind="paraphrase",
            project_id=rec["shadow_project_id"],
            query=para,
            expected_ids=(rec["shadow_id"],),
            description=f"paraphrase recall@{k} probe",
        ))
    pair = _best_overlap_pair(shadow_records)
    if pair:
        target, distractor, shared = pair
        query = " ".join((*shared[:2], *_salient_tokens(target["text"])[:2]))
        if query.strip():
            cases.append(EvalCase(
                case_id=f"adversarial-{target['shadow_id']}-{distractor['shadow_id']}",
                kind="adversarial_lexical",
                project_id=target["shadow_project_id"],
                query=query,
                expected_ids=(target["shadow_id"],),
                forbidden_ids=(distractor["shadow_id"],),
                description="shared-token distractor must not outrank the true target",
            ))
    return cases


async def _build_control_cases(
    db: Any, shadow_project_id: str, sibling_project_id: str
) -> list[EvalCase]:
    """Synthesize the two controls that need deliberately-constructed fixtures
    rather than naturally-occurring real data: an ambiguous near-duplicate
    pair, and a cross-project leakage canary."""
    cases: list[EvalCase] = []

    n1 = await db_module.add_project_note(
        db, shadow_project_id, "Rate limiting for the public API",
        "Token bucket rate limiter guarding the public REST API.",
        tags="eval_control",
    )
    n2 = await db_module.add_project_note(
        db, shadow_project_id, "Rate limiting for the public API v2",
        "Sliding-window rate limiter guarding the public REST API, v2 rollout.",
        tags="eval_control",
    )
    cases.append(EvalCase(
        case_id="ambiguous-rate-limit-pair",
        kind="ambiguous_abstention",
        project_id=shadow_project_id,
        query="rate limiting for the public api",
        expected_ids=(n1["id"], n2["id"]),
        description=(
            "near-duplicate pair; a correct backend surfaces both rather than "
            "silently dropping one for an overconfident single pick"
        ),
    ))

    nonce = f"leakcheck-{uuid.uuid4().hex[:12]}"
    leak_note = await db_module.add_project_note(
        db, sibling_project_id, "Leakage canary",
        f"Distinctive token {nonce} must never surface outside its own project.",
        tags="eval_control",
    )
    cases.append(EvalCase(
        case_id="leakage-cross-project",
        kind="leakage",
        project_id=shadow_project_id,
        query=nonce,
        forbidden_ids=(leak_note["id"],),
        description="cross-project leakage canary — must never appear in a same-scope search",
    ))
    return cases


# ---------------------------------------------------------------------------
# Backend runners. Each returns (ranked_hit_ids, latency_ms) for one case.
# ---------------------------------------------------------------------------


async def _run_keyword_fts(db: Any, case: EvalCase, k: int) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    result = await db_module.search_all(db, case.project_id, case.query, limit=k)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    hit_ids = [n["id"] for n in result.get("notes", [])]
    return hit_ids, latency_ms


async def _run_model2vec(
    db: Any, case: EvalCase, k: int, note_cache: dict[str, list[tuple[str, str]]]
) -> tuple[list[str], float]:
    if case.project_id not in note_cache:
        rows = await db_module.get_project_notes(db, case.project_id, bodies=True)
        note_cache[case.project_id] = [
            (r["id"], f"{r.get('title') or ''} {r.get('body') or ''}".strip())
            for r in rows
        ]
    candidates = note_cache[case.project_id]
    t0 = time.perf_counter()
    ranked = _semantic_search.rank(case.query, candidates)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    return [cid for cid, _score in ranked[:k]], latency_ms


async def _call_pluggable_backend(
    search_fn: Callable[..., Any], db: Any, case: EvalCase, k: int
) -> tuple[list[str], float]:
    t0 = time.perf_counter()
    result = search_fn(db, case.project_id, case.query, k)
    if inspect.isawaitable(result):
        result = await result
    latency_ms = (time.perf_counter() - t0) * 1000.0
    hit_ids = [item[0] if isinstance(item, (tuple, list)) else item for item in (result or [])]
    return hit_ids, latency_ms


def _detect_pluggable_backend(module_name: str) -> tuple[Callable[..., Any] | None, str | None]:
    """Documented extension point: a module at ``module_name`` exposing
    ``search(db, project_id, query, k) -> list[tuple[id, score]] | list[id]``
    (sync or async) is picked up automatically. Neither ``meridian.vector_index``
    (DuckDB VSS) nor ``meridian.pgvector_index`` exists yet in this codebase —
    see sprint item e1475682 — so this currently always reports unavailable
    with a precise reason."""
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return None, (
            f"{module_name} module not present — this backend is not yet "
            "implemented in this codebase (tracked by sprint item e1475682)"
        )
    search_fn = getattr(mod, "search", None)
    if search_fn is None:
        return None, f"{module_name} has no search() entry point"
    return search_fn, None


async def _pgvector_backend_probe(db: Any) -> tuple[Callable[..., Any] | None, str | None]:
    if not hasattr(db, "_pool"):
        return None, "not running on a Postgres backend"
    search_fn, reason = _detect_pluggable_backend("meridian.pgvector_index")
    if search_fn is None:
        return None, reason
    try:
        async with db.execute(
            "SELECT 1 FROM pg_extension WHERE extname = ?", ("vector",)
        ) as cur:
            row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - missing catalog access, etc.
        return None, f"pgvector extension check failed: {exc}"
    if not row:
        return None, "pgvector extension not installed on this Postgres instance"
    return search_fn, None


# ---------------------------------------------------------------------------
# Metrics + gate.
# ---------------------------------------------------------------------------


def _compute_backend_metrics(
    name: str,
    outcomes: list[tuple[EvalCase, list[str], float]],
    k: int,
    *,
    available: bool,
    skip_reason: str | None,
    rss_delta: float | None,
    remote_round_trips: int,
    remote_db_cost_note: str,
) -> BackendMetrics:
    if not available:
        return BackendMetrics(
            backend=name, available=False, skip_reason=skip_reason, cases_run=0,
            exact_match_recall_at_1=None, paraphrase_recall_at_k=None,
            adversarial_false_positive_rate=None, leakage_count=0,
            leakage_rate=None, ambiguous_abstention_rate=None,
            wall_time_ms_mean=None, wall_time_ms_total=None, rss_delta_mb=None,
            remote_round_trips=0, remote_db_cost_note="backend unavailable — not run",
        )

    by_kind: dict[str, list[tuple[EvalCase, list[str], float]]] = {}
    for outcome in outcomes:
        by_kind.setdefault(outcome[0].kind, []).append(outcome)

    exact = by_kind.get("exact_match", [])
    exact_scores = [
        1.0 if hits and hits[0] in case.expected_ids else 0.0
        for case, hits, _lat in exact
    ]

    para = by_kind.get("paraphrase", [])
    para_scores = [
        1.0 if any(eid in hits[:k] for eid in case.expected_ids) else 0.0
        for case, hits, _lat in para
    ]

    adversarial = by_kind.get("adversarial_lexical", [])
    fp_scores = [
        1.0 if hits and hits[0] in case.forbidden_ids else 0.0
        for case, hits, _lat in adversarial
    ]

    leakage_cases = by_kind.get("leakage", [])
    leakage_hits = [
        1 if any(fid in hits for fid in case.forbidden_ids) else 0
        for case, hits, _lat in leakage_cases
    ]
    leakage_count = sum(leakage_hits)

    ambiguous = by_kind.get("ambiguous_abstention", [])
    ambiguous_top = max(k, 2)
    abstention_scores = [
        1.0 if all(eid in hits[:ambiguous_top] for eid in case.expected_ids) else 0.0
        for case, hits, _lat in ambiguous
    ]

    all_latencies = [lat for _c, _h, lat in outcomes]

    return BackendMetrics(
        backend=name,
        available=True,
        skip_reason=None,
        cases_run=len(outcomes),
        exact_match_recall_at_1=_safe_mean(exact_scores),
        paraphrase_recall_at_k=_safe_mean(para_scores),
        adversarial_false_positive_rate=_safe_mean(fp_scores),
        leakage_count=leakage_count,
        leakage_rate=_safe_mean([float(x) for x in leakage_hits]),
        ambiguous_abstention_rate=_safe_mean(abstention_scores),
        wall_time_ms_mean=_safe_mean(all_latencies),
        wall_time_ms_total=sum(all_latencies) if all_latencies else None,
        rss_delta_mb=rss_delta,
        remote_round_trips=remote_round_trips,
        remote_db_cost_note=remote_db_cost_note,
    )


async def _evaluate_backend(
    name: str,
    cases: list[EvalCase],
    k: int,
    *,
    runner: Callable[[EvalCase], Awaitable[tuple[list[str], float]]],
    is_remote: bool,
) -> BackendMetrics:
    rss_before = _semantic_search.rss_mb()
    outcomes: list[tuple[EvalCase, list[str], float]] = []
    for case in cases:
        t0 = time.perf_counter()
        try:
            hit_ids, latency_ms = await runner(case)
        except Exception:  # noqa: BLE001 - a broken backend must not crash the run
            hit_ids, latency_ms = [], (time.perf_counter() - t0) * 1000.0
        outcomes.append((case, hit_ids, latency_ms))
    rss_after = _semantic_search.rss_mb()
    rss_delta = (
        (rss_after - rss_before) if (rss_before is not None and rss_after is not None) else None
    )
    remote_round_trips = len(outcomes) if is_remote else 0
    remote_note = (
        "proxy metric: count of remote round trips, NOT a billed dollar figure — "
        "Meridian has no provider billing integration"
        if is_remote else
        "local backend — no remote database cost applies"
    )
    return _compute_backend_metrics(
        name, outcomes, k,
        available=True, skip_reason=None, rss_delta=rss_delta,
        remote_round_trips=remote_round_trips, remote_db_cost_note=remote_note,
    )


def _skipped_backend_metrics(name: str, reason: str | None) -> BackendMetrics:
    return _compute_backend_metrics(
        name, [], 1, available=False, skip_reason=reason,
        rss_delta=None, remote_round_trips=0, remote_db_cost_note="",
    )


async def _best_effort_delete_project(db: Any, project_id: str) -> None:
    try:
        await db_module.delete_project(db, project_id)
    except Exception:  # noqa: BLE001 - cleanup is best-effort, never fatal
        pass


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


async def run_evaluation(
    db: Any,
    project_id: str,
    *,
    sample_size: int = 8,
    k: int = 5,
    include_model2vec: bool = False,
    cleanup: bool = True,
) -> EvalReport:
    """Build a reproducible eval dataset from ``project_id``'s real content
    and run every available retrieval backend against it. Never mutates
    ``project_id`` — all writes land in disposable shadow/sibling projects
    that are deleted before returning unless ``cleanup=False``."""
    shadow = await db_module.create_project(
        db, f"__retrieval_eval__{project_id[:8]}__{uuid.uuid4().hex[:8]}"
    )
    sibling = await db_module.create_project(
        db, f"__retrieval_eval_sibling__{uuid.uuid4().hex[:8]}"
    )
    try:
        real_records = await _collect_real_corpus(
            db, project_id, max_notes=sample_size, max_sprint_items=sample_size
        )
        shadow_records = []
        for rec in real_records:
            note = await db_module.add_project_note(
                db, shadow["id"], rec["title"] or rec["source_kind"], rec["text"],
                tags=rec["source_kind"],
            )
            shadow_records.append({
                **rec, "shadow_id": note["id"], "shadow_project_id": shadow["id"],
            })

        cases = _build_corpus_cases(shadow_records, k)
        cases += await _build_control_cases(db, shadow["id"], sibling["id"])

        cases_by_kind: dict[str, int] = {}
        for c in cases:
            cases_by_kind[c.kind] = cases_by_kind.get(c.kind, 0) + 1

        is_pg = hasattr(db, "_pool")
        backends: dict[str, BackendMetrics] = {}

        backends["keyword_fts"] = await _evaluate_backend(
            "keyword_fts", cases, k,
            runner=lambda case: _run_keyword_fts(db, case, k),
            is_remote=is_pg,
        )

        if include_model2vec and _semantic_search.is_available():
            note_cache: dict[str, list[tuple[str, str]]] = {}
            backends["model2vec_rerank"] = await _evaluate_backend(
                "model2vec_rerank", cases, k,
                runner=lambda case: _run_model2vec(db, case, k, note_cache),
                is_remote=False,
            )
        else:
            backends["model2vec_rerank"] = _skipped_backend_metrics(
                "model2vec_rerank",
                "model2vec disabled or not requested — set MERIDIAN_SEMANTIC_ENABLED=1 "
                "and pass include_model2vec=True to evaluate it"
                if not include_model2vec else
                "model2vec unavailable (disabled, not importable, or circuit breaker tripped)",
            )

        duckdb_search, duckdb_reason = _detect_pluggable_backend("meridian.vector_index")
        if duckdb_search is not None:
            backends["duckdb_vss"] = await _evaluate_backend(
                "duckdb_vss", cases, k,
                runner=lambda case: _call_pluggable_backend(duckdb_search, db, case, k),
                is_remote=False,
            )
        else:
            backends["duckdb_vss"] = _skipped_backend_metrics("duckdb_vss", duckdb_reason)

        pgvector_search, pgvector_reason = await _pgvector_backend_probe(db)
        if pgvector_search is not None:
            backends["pgvector"] = await _evaluate_backend(
                "pgvector", cases, k,
                runner=lambda case: _call_pluggable_backend(pgvector_search, db, case, k),
                is_remote=True,
            )
        else:
            backends["pgvector"] = _skipped_backend_metrics("pgvector", pgvector_reason)

        return EvalReport(
            project_id=project_id,
            generated_at=_utc_now_iso(),
            k=k,
            dataset_size=len(cases),
            cases_by_kind=cases_by_kind,
            backends=backends,
        )
    finally:
        if cleanup:
            await _best_effort_delete_project(db, shadow["id"])
            await _best_effort_delete_project(db, sibling["id"])


def evaluate_gate(
    report: EvalReport, thresholds: GateThresholds | None = None
) -> GateDecision:
    """Turn an :class:`EvalReport` into a pass/fail decision per backend and
    recommend a default. Leakage is a hard requirement regardless of
    ``thresholds`` overrides for every other metric."""
    thresholds = thresholds or GateThresholds()
    passed: list[str] = []
    failed: dict[str, list[str]] = {}
    skipped: list[str] = []
    reasons: list[str] = []

    for name, m in report.backends.items():
        if not m.available:
            skipped.append(name)
            reasons.append(f"{name}: skipped ({m.skip_reason})")
            continue
        backend_reasons: list[str] = []
        if thresholds.require_zero_leakage and (m.leakage_count or 0) > 0:
            backend_reasons.append(
                f"leakage_count={m.leakage_count} > 0 (hard requirement)"
            )
        if (
            m.exact_match_recall_at_1 is not None
            and m.exact_match_recall_at_1 < thresholds.min_exact_match_recall_at_1
        ):
            backend_reasons.append(
                f"exact_match_recall_at_1={m.exact_match_recall_at_1:.2f} < "
                f"{thresholds.min_exact_match_recall_at_1}"
            )
        if (
            m.paraphrase_recall_at_k is not None
            and m.paraphrase_recall_at_k < thresholds.min_paraphrase_recall_at_k
        ):
            backend_reasons.append(
                f"paraphrase_recall_at_k={m.paraphrase_recall_at_k:.2f} < "
                f"{thresholds.min_paraphrase_recall_at_k}"
            )
        if (
            m.adversarial_false_positive_rate is not None
            and m.adversarial_false_positive_rate > thresholds.max_adversarial_false_positive_rate
        ):
            backend_reasons.append(
                f"adversarial_false_positive_rate={m.adversarial_false_positive_rate:.2f} > "
                f"{thresholds.max_adversarial_false_positive_rate}"
            )
        if (
            m.wall_time_ms_mean is not None
            and m.wall_time_ms_mean > thresholds.max_wall_time_ms_mean
        ):
            backend_reasons.append(
                f"wall_time_ms_mean={m.wall_time_ms_mean:.1f} > "
                f"{thresholds.max_wall_time_ms_mean}"
            )
        if backend_reasons:
            failed[name] = backend_reasons
        else:
            passed.append(name)

    recommended = "keyword_fts" if "keyword_fts" in passed else (passed[0] if passed else None)
    return GateDecision(
        passed_backends=passed, failed_backends=failed, skipped_backends=skipped,
        recommended_default=recommended, reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Machine-readable publishing.
# ---------------------------------------------------------------------------


def to_json(report: EvalReport) -> str:
    """Serialize an :class:`EvalReport` to deterministic, indented JSON."""
    payload = asdict(report)
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def write_report(report: EvalReport, path: str | Path) -> Path:
    """Publish ``report`` as machine-readable JSON at ``path``. Returns the
    resolved path so callers can log/attach it."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_json(report), encoding="utf-8")
    return out
