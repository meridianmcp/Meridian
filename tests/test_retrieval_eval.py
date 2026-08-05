"""Tests for the retrieval evaluation suite (sprint 116c75c9).

Covers: real-corpus collection (notes/sprint items/pointers/handoffs),
non-mutation of the caller's project, case construction (exact/paraphrase/
adversarial/leakage/ambiguous), the keyword_fts backend end-to-end, graceful
skip behavior for model2vec/duckdb_vss/pgvector when unavailable, the
pluggable-backend detection contract, the gate's hard zero-leakage rule and
its default recommendation, and machine-readable JSON publishing.

CI-SAFE: no real model2vec load, no real DuckDB/pgvector dependency — those
backends are exercised via monkeypatched fakes only.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from meridian import db as db_module
from meridian.retrieval_eval import (
    EvalCase,
    EvalReport,
    BackendMetrics,
    GateThresholds,
    _best_overlap_pair,
    _build_corpus_cases,
    _collect_real_corpus,
    _detect_pluggable_backend,
    _paraphrase,
    evaluate_gate,
    run_evaluation,
    to_json,
    write_report,
)


# ---------------------------------------------------------------------------
# Deterministic text helpers.
# ---------------------------------------------------------------------------


def test_paraphrase_is_deterministic_and_differs_from_raw_text():
    title = "Rate limiting for the public API"
    p1 = _paraphrase(title)
    p2 = _paraphrase(title)
    assert p1 == p2  # deterministic
    assert p1 != title.lower()  # not a no-op identity transform
    assert p1.strip()


def test_paraphrase_handles_empty_text():
    assert _paraphrase("") == ""


def test_best_overlap_pair_picks_largest_shared_token_set():
    records = [
        {"text": "deploy pipeline configuration for staging"},
        {"text": "totally unrelated topic about coffee brewing"},
        {"text": "deploy pipeline configuration for production release"},
    ]
    pair = _best_overlap_pair(records)
    assert pair is not None
    a, b, shared = pair
    assert {"deploy", "pipeline", "configuration"}.issubset(set(shared))
    assert a is not b


def test_best_overlap_pair_none_when_no_shared_tokens():
    records = [{"text": "aaaa bbbb"}, {"text": "cccc dddd"}]
    assert _best_overlap_pair(records) is None


# ---------------------------------------------------------------------------
# Real-corpus collection — read-only.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_real_corpus_pulls_notes_sprint_items_and_pointers(db):
    p = await db_module.create_project(db, "eval-corpus-src")
    await db_module.add_project_note(
        db, p["id"], "Deploy pipeline", "GitHub Actions deploys to Fly.io"
    )
    item = await db_module.add_sprint_item(
        db, p["id"], "v0.1", "Add retry logic to deploy step"
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "file",
        [{
            "uri": "meridian/deploy.py",
            "selector": {"type": "symbol", "qualified_name": "deploy.retry"},
            "target_kind": "planned_new",
        }],
        label="deploy.py retry block",
    )

    records = await _collect_real_corpus(db, p["id"])
    kinds = {r["source_kind"] for r in records}
    assert "note" in kinds
    assert "sprint_item" in kinds
    assert "pointer" in kinds
    note_rec = next(r for r in records if r["source_kind"] == "note")
    assert "Deploy pipeline" in note_rec["text"]


@pytest.mark.asyncio
async def test_collect_real_corpus_empty_project_returns_empty_list(db):
    p = await db_module.create_project(db, "eval-corpus-empty")
    records = await _collect_real_corpus(db, p["id"])
    assert records == []


# ---------------------------------------------------------------------------
# Case construction from a corpus (pure — no DB).
# ---------------------------------------------------------------------------


def test_build_corpus_cases_produces_exact_paraphrase_and_adversarial():
    shadow_records = [
        {
            "source_kind": "note", "shadow_id": "id-1", "shadow_project_id": "proj-a",
            "title": "Deploy pipeline configuration",
            "text": "Deploy pipeline configuration for staging environments",
        },
        {
            "source_kind": "note", "shadow_id": "id-2", "shadow_project_id": "proj-a",
            "title": "Deploy pipeline rollback",
            "text": "Deploy pipeline rollback configuration for production incidents",
        },
    ]
    cases = _build_corpus_cases(shadow_records, k=5)
    kinds = [c.kind for c in cases]
    assert kinds.count("exact_match") == 2
    assert kinds.count("paraphrase") == 2
    assert kinds.count("adversarial_lexical") == 1

    exact = next(c for c in cases if c.kind == "exact_match" and c.expected_ids == ("id-1",))
    assert exact.query.startswith("Deploy pipeline configuration")

    adversarial = next(c for c in cases if c.kind == "adversarial_lexical")
    assert adversarial.expected_ids[0] != adversarial.forbidden_ids[0]


def test_build_corpus_cases_empty_corpus_returns_no_cases():
    assert _build_corpus_cases([], k=5) == []


# ---------------------------------------------------------------------------
# End-to-end run_evaluation — keyword_fts is always available.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_evaluation_never_mutates_source_project(db):
    p = await db_module.create_project(db, "eval-no-mutate")
    await db_module.add_project_note(db, p["id"], "Auth flow", "OAuth login handling")

    async with db.execute("SELECT COUNT(*) AS c FROM projects") as cur:
        before_row = await cur.fetchone()
    before_count = before_row["c"] if isinstance(before_row, dict) else before_row[0]
    notes_before = await db_module.get_project_notes(db, p["id"])

    await run_evaluation(db, p["id"], sample_size=4, k=5, cleanup=True)

    notes_after = await db_module.get_project_notes(db, p["id"])
    assert len(notes_after) == len(notes_before)

    async with db.execute("SELECT COUNT(*) AS c FROM projects") as cur:
        after_row = await cur.fetchone()
    after_count = after_row["c"] if isinstance(after_row, dict) else after_row[0]
    # Two scratch projects (shadow + sibling) were created AND cleaned up.
    assert after_count == before_count


@pytest.mark.asyncio
async def test_run_evaluation_keyword_fts_recalls_exact_match(db):
    p = await db_module.create_project(db, "eval-keyword-recall")
    await db_module.add_project_note(
        db, p["id"], "Continuous deployment pipeline",
        "Configures GitHub Actions to deploy the service to Fly.io on merge to dev",
    )

    report = await run_evaluation(db, p["id"], sample_size=4, k=5, cleanup=True)
    assert report.dataset_size > 0
    kw = report.backends["keyword_fts"]
    assert kw.available is True
    assert kw.exact_match_recall_at_1 == 1.0
    assert kw.leakage_count == 0
    assert kw.wall_time_ms_mean is not None


@pytest.mark.asyncio
async def test_run_evaluation_includes_ambiguous_and_leakage_cases_even_when_empty(db):
    p = await db_module.create_project(db, "eval-empty-project")
    report = await run_evaluation(db, p["id"], sample_size=4, k=5, cleanup=True)
    assert report.cases_by_kind.get("ambiguous_abstention") == 1
    assert report.cases_by_kind.get("leakage") == 1
    kw = report.backends["keyword_fts"]
    assert kw.leakage_count == 0
    assert kw.ambiguous_abstention_rate is not None
    # No real notes were seeded, so no exact/paraphrase cases exist.
    assert "exact_match" not in report.cases_by_kind
    assert kw.exact_match_recall_at_1 is None


@pytest.mark.asyncio
async def test_model2vec_backend_skipped_by_default(db, monkeypatch):
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "eval-model2vec-off")
    await db_module.add_project_note(db, p["id"], "Rate limiting", "token bucket algorithm")

    report = await run_evaluation(db, p["id"], sample_size=4, k=5, cleanup=True)
    m2v = report.backends["model2vec_rerank"]
    assert m2v.available is False
    assert m2v.cases_run == 0
    assert "disabled" in (m2v.skip_reason or "") or "not requested" in (m2v.skip_reason or "")


@pytest.mark.asyncio
async def test_model2vec_backend_runs_when_available_and_requested(db, monkeypatch):
    from meridian import semantic_search

    p = await db_module.create_project(db, "eval-model2vec-on")
    await db_module.add_project_note(db, p["id"], "Rate limiting", "token bucket algorithm")

    monkeypatch.setattr(semantic_search, "is_available", lambda: True)
    monkeypatch.setattr(
        semantic_search, "rank",
        lambda query, candidates, floor=None: [(cid, 0.9) for cid, _t in candidates],
    )

    report = await run_evaluation(
        db, p["id"], sample_size=4, k=5, include_model2vec=True, cleanup=True
    )
    m2v = report.backends["model2vec_rerank"]
    assert m2v.available is True
    assert m2v.cases_run == report.dataset_size


@pytest.mark.asyncio
async def test_duckdb_and_pgvector_report_precise_unavailable_reasons(db):
    p = await db_module.create_project(db, "eval-pluggable-off")
    await db_module.add_project_note(db, p["id"], "Note", "body text")

    report = await run_evaluation(db, p["id"], sample_size=4, k=5, cleanup=True)

    duckdb = report.backends["duckdb_vss"]
    assert duckdb.available is False
    assert "not yet implemented" in (duckdb.skip_reason or "")

    pgvector = report.backends["pgvector"]
    assert pgvector.available is False
    # This test DB is SQLite (no TEST_DATABASE_URL in CI), so the reason must
    # name that, not a generic failure.
    if not hasattr(db, "_pool"):
        assert "Postgres" in (pgvector.skip_reason or "")


# ---------------------------------------------------------------------------
# Pluggable-backend detection contract (DuckDB VSS / pgvector extension point).
# ---------------------------------------------------------------------------


def test_detect_pluggable_backend_missing_module_has_precise_reason():
    search_fn, reason = _detect_pluggable_backend("meridian._does_not_exist_vector_module")
    assert search_fn is None
    assert "not present" in reason


def test_detect_pluggable_backend_finds_installed_fake_module(monkeypatch):
    fake = types.ModuleType("meridian._fake_vector_index")

    async def _fake_search(db, project_id, query, k):
        return [("id-1", 0.99)]

    fake.search = _fake_search
    monkeypatch.setitem(sys.modules, "meridian._fake_vector_index", fake)

    search_fn, reason = _detect_pluggable_backend("meridian._fake_vector_index")
    assert search_fn is _fake_search
    assert reason is None


@pytest.mark.asyncio
async def test_run_evaluation_wires_pluggable_duckdb_backend_when_present(db, monkeypatch):
    fake = types.ModuleType("meridian.vector_index")

    async def _fake_search(db_, project_id, query, k):
        return [("fake-hit", 0.5)]

    fake.search = _fake_search
    monkeypatch.setitem(sys.modules, "meridian.vector_index", fake)

    p = await db_module.create_project(db, "eval-duckdb-plugged")
    await db_module.add_project_note(db, p["id"], "Note", "body text")

    report = await run_evaluation(db, p["id"], sample_size=4, k=5, cleanup=True)
    duckdb = report.backends["duckdb_vss"]
    assert duckdb.available is True
    assert duckdb.cases_run == report.dataset_size


# ---------------------------------------------------------------------------
# Gate evaluation.
# ---------------------------------------------------------------------------


def _metrics(**overrides) -> BackendMetrics:
    base = dict(
        backend="test_backend", available=True, skip_reason=None, cases_run=10,
        exact_match_recall_at_1=1.0, paraphrase_recall_at_k=0.9,
        adversarial_false_positive_rate=0.0, leakage_count=0, leakage_rate=0.0,
        ambiguous_abstention_rate=1.0, wall_time_ms_mean=5.0, wall_time_ms_total=50.0,
        rss_delta_mb=0.0, remote_round_trips=0, remote_db_cost_note="local",
    )
    base.update(overrides)
    return BackendMetrics(**base)


def test_gate_hard_fails_on_any_leakage_regardless_of_other_metrics():
    report = EvalReport(
        project_id="p1", generated_at="now", k=5, dataset_size=10,
        cases_by_kind={"leakage": 1},
        backends={"keyword_fts": _metrics(backend="keyword_fts", leakage_count=1)},
    )
    decision = evaluate_gate(report)
    assert "keyword_fts" not in decision.passed_backends
    assert "keyword_fts" in decision.failed_backends
    assert any("leakage" in r for r in decision.failed_backends["keyword_fts"])
    assert decision.recommended_default is None


def test_gate_recommends_keyword_fts_when_it_passes():
    report = EvalReport(
        project_id="p1", generated_at="now", k=5, dataset_size=10,
        cases_by_kind={"exact_match": 10},
        backends={
            "keyword_fts": _metrics(backend="keyword_fts"),
            "model2vec_rerank": BackendMetrics(
                backend="model2vec_rerank", available=False, skip_reason="disabled",
                cases_run=0, exact_match_recall_at_1=None, paraphrase_recall_at_k=None,
                adversarial_false_positive_rate=None, leakage_count=0, leakage_rate=None,
                ambiguous_abstention_rate=None, wall_time_ms_mean=None,
                wall_time_ms_total=None, rss_delta_mb=None, remote_round_trips=0,
                remote_db_cost_note="backend unavailable — not run",
            ),
        },
    )
    decision = evaluate_gate(report)
    assert decision.recommended_default == "keyword_fts"
    assert "model2vec_rerank" in decision.skipped_backends


def test_gate_fails_backend_below_recall_threshold():
    report = EvalReport(
        project_id="p1", generated_at="now", k=5, dataset_size=10,
        cases_by_kind={"exact_match": 10},
        backends={"weak_backend": _metrics(backend="weak_backend", exact_match_recall_at_1=0.2)},
    )
    decision = evaluate_gate(report, GateThresholds(min_exact_match_recall_at_1=0.9))
    assert "weak_backend" in decision.failed_backends
    assert decision.recommended_default is None


# ---------------------------------------------------------------------------
# Machine-readable publishing.
# ---------------------------------------------------------------------------


def test_to_json_produces_valid_deterministic_json():
    report = EvalReport(
        project_id="p1", generated_at="now", k=5, dataset_size=1,
        cases_by_kind={"exact_match": 1},
        backends={"keyword_fts": _metrics(backend="keyword_fts")},
    )
    text = to_json(report)
    payload = json.loads(text)
    assert payload["project_id"] == "p1"
    assert payload["backends"]["keyword_fts"]["backend"] == "keyword_fts"
    assert to_json(report) == text  # deterministic (sort_keys)


def test_write_report_writes_json_file(tmp_path):
    report = EvalReport(
        project_id="p1", generated_at="now", k=5, dataset_size=0,
        cases_by_kind={}, backends={},
    )
    out_path = tmp_path / "nested" / "eval_report.json"
    result_path = write_report(report, out_path)
    assert result_path == out_path
    assert out_path.exists()
    assert json.loads(out_path.read_text(encoding="utf-8"))["project_id"] == "p1"
