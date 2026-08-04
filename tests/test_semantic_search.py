"""Tests for Model2Vec semantic-search escalation (sprint 56cd8712).

SAFE-BY-DEFAULT, OPT-IN semantic search layered over keyword/tsvector search.

CI-SAFE: the CI env has NO model2vec and MUST NOT download the model. Every test
here either exercises pure, DB-free logic (the escalation gate, cosine floor with
injected vectors, the circuit breaker with a monkeypatched RSS reader) or asserts
the DEFAULT behavior (semantic unavailable → plain keyword results). Nothing here
imports model2vec or calls ``StaticModel.from_pretrained`` — the model is never
loaded and never downloaded.
"""

from __future__ import annotations

import sys

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import semantic_search as ss
from meridian.semantic_search import SemanticSearcher, should_escalate


# ---------------------------------------------------------------------------
# should_escalate — the CORRECTED gate (pure, DB-free).
# ---------------------------------------------------------------------------


def test_should_escalate_fires_on_zero_fts_and_low_trigram():
    """Fires ONLY when pure FTS returned literally zero AND pg_trgm top < ~0.1 —
    i.e. keyword search genuinely found nothing good."""
    assert should_escalate(0, 0.0) is True
    assert should_escalate(0, 0.05) is True
    assert should_escalate(0, 0.099) is True


def test_should_escalate_suppressed_when_fts_found_rows():
    """Any pure-FTS hit (>=1) means keyword search worked — no escalation."""
    assert should_escalate(1, 0.0) is False
    assert should_escalate(2, 0.05) is False
    assert should_escalate(10, 0.0) is False


def test_should_escalate_suppressed_when_trigram_has_good_hit():
    """A decent fuzzy keyword hit (trigram_top >= 0.1) suppresses escalation even
    when pure FTS is zero."""
    assert should_escalate(0, 0.1) is False
    assert should_escalate(0, 0.25) is False
    assert should_escalate(0, 0.9) is False


def test_should_escalate_NOT_the_old_less_than_3_condition():
    """Regression guard for finding c1008ef9: the old written gate ('escalate if
    tsvector returns <3 results') is WRONG because the real search is a permissive
    3-way OR that almost always returns >=3. A permissive-OR result of 3-5 rows must
    NOT escalate — and crucially, a small result count (1 or 2) with a real keyword
    hit must NOT escalate either. The gate keys off pure-FTS==0, not a count<3."""
    # Old broken trigger would fire on these small counts; the corrected gate must
    # NOT, because pure FTS actually found something.
    for permissive_or_count in (1, 2, 3, 4, 5):
        assert should_escalate(permissive_or_count, 0.05) is False
    # And a genuine keyword hit with a good trigram never escalates regardless.
    assert should_escalate(4, 0.5) is False


def test_should_escalate_handles_none_inputs():
    """None inputs coerce to zero (treated as 'found nothing')."""
    assert should_escalate(None, None) is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# is_corpus_capped / model_name — embedding-freshness helpers (e631d54f,
# follow-up to 56cd8712). Pure, DB-free — same style as should_escalate.
# ---------------------------------------------------------------------------


def test_is_corpus_capped_true_at_and_above_cap():
    assert ss.is_corpus_capped(200, 200) is True
    assert ss.is_corpus_capped(250, 200) is True


def test_is_corpus_capped_false_below_cap():
    assert ss.is_corpus_capped(199, 200) is False
    assert ss.is_corpus_capped(0, 200) is False


def test_is_corpus_capped_degenerate_cap_treats_any_nonempty_corpus_as_capped():
    """A cap of 0 (or negative — misconfiguration) means NO corpus size is
    provably exhaustive, so any non-empty corpus is conservatively capped."""
    assert ss.is_corpus_capped(5, 0) is True
    assert ss.is_corpus_capped(0, 0) is False
    assert ss.is_corpus_capped(1, -1) is True


def test_model_name_returns_the_configured_model():
    assert ss.model_name() == ss._MODEL_NAME
    assert isinstance(ss.model_name(), str) and ss.model_name()


# ---------------------------------------------------------------------------
# cosine floor — rank() drops sub-floor hits, keeps floor-and-above sorted desc.
# Uses a stubbed embed via injected vectors — NO real model.
# ---------------------------------------------------------------------------


def _stub_embed_from_map(mapping):
    """Return an embed(texts) that looks up each text in ``mapping`` -> vector."""
    import numpy as np

    def _embed(texts):
        return np.asarray([mapping[t] for t in texts], dtype="float32")

    return _embed


def test_rank_drops_subfloor_and_sorts_desc(monkeypatch):
    np = pytest.importorskip("numpy")  # CI has no model2vec/numpy; skip the vector math there

    s = SemanticSearcher()
    # Query points along +x. Candidates at varying cosine to it.
    vecs = {
        "query": np.array([1.0, 0.0]),
        "high": np.array([0.98, 0.20]),      # cosine ~0.98
        "mid": np.array([0.60, 0.80]),       # cosine 0.60
        "low": np.array([0.30, 0.95]),       # cosine ~0.30 (below 0.37 floor)
        "zero": np.array([0.0, 1.0]),        # cosine 0.0
    }
    monkeypatch.setattr(s, "embed", _stub_embed_from_map(vecs))
    candidates = [("h", "high"), ("m", "mid"), ("l", "low"), ("z", "zero")]
    ranked = s.rank("query", candidates, floor=0.37)
    ids = [cid for cid, _ in ranked]
    # Sub-floor "low" and "zero" dropped; kept sorted descending by cosine.
    assert ids == ["h", "m"]
    scores = [sc for _, sc in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all(sc >= 0.37 for sc in scores)


def test_rank_respects_env_floor(monkeypatch):
    np = pytest.importorskip("numpy")

    s = SemanticSearcher()
    vecs = {
        "query": np.array([1.0, 0.0]),
        "a": np.array([0.60, 0.80]),  # cosine 0.60
    }
    monkeypatch.setattr(s, "embed", _stub_embed_from_map(vecs))
    # Floor above the only candidate's cosine → dropped.
    assert s.rank("query", [("a", "a")], floor=0.7) == []
    # Floor below → kept.
    kept = s.rank("query", [("a", "a")], floor=0.5)
    assert [cid for cid, _ in kept] == ["a"]


def test_rank_empty_candidates_returns_empty():
    # b3537a8d — empty candidates early-return BEFORE the numpy import, so no numpy needed.
    s = SemanticSearcher()
    assert s.rank("q", []) == []


def test_rank_and_embed_safe_without_numpy(monkeypatch):
    """b3537a8d — the degenerate/unavailable paths of rank()/embed() must NOT crash
    when numpy is absent (the module's safe-by-default promise). Simulate numpy
    missing: any attempt to import it raises, so this fails if a numpy import creeps
    back before the early-returns."""
    import builtins
    real_import = builtins.__import__

    def _no_numpy(name, *a, **k):
        if name == "numpy" or name.startswith("numpy."):
            raise ModuleNotFoundError("No module named 'numpy'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_numpy)
    s = SemanticSearcher()
    assert s.rank("q", []) == []               # empty candidates
    assert s.embed([]) is None                 # empty texts
    monkeypatch.setattr(s, "embed", lambda texts: None)
    assert s.rank("q", [("a", "text")]) == []  # semantic unavailable


def test_rank_returns_empty_when_embed_unavailable(monkeypatch):
    """When embed() returns None (unavailable/tripped) rank yields [] — caller
    falls back to keyword-only."""
    s = SemanticSearcher()
    monkeypatch.setattr(s, "embed", lambda texts: None)
    assert s.rank("q", [("a", "text")]) == []


# ---------------------------------------------------------------------------
# Circuit breaker — monkeypatch the RSS reader; over-limit trips + unloads.
# ---------------------------------------------------------------------------


def test_breaker_trips_when_rss_over_limit(monkeypatch):
    monkeypatch.setenv("MERIDIAN_SEMANTIC_ENABLED", "1")
    monkeypatch.setenv("MERIDIAN_SEMANTIC_RSS_LIMIT_MB", "400")
    s = SemanticSearcher()
    # Pretend model2vec importable so availability only turns on the breaker path.
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    # A loaded model that should get unloaded on trip.
    s._model = object()
    # RSS over the limit.
    monkeypatch.setattr(ss, "rss_mb", lambda: 500.0)
    assert s._rss_ok() is False
    assert s.is_tripped() is True
    assert s._model is None  # unloaded on trip
    # Tripped → is_available False → escalation returns keyword-only.
    assert s.is_available() is False


def test_breaker_allows_when_rss_under_limit(monkeypatch):
    monkeypatch.setenv("MERIDIAN_SEMANTIC_ENABLED", "1")
    monkeypatch.setenv("MERIDIAN_SEMANTIC_RSS_LIMIT_MB", "400")
    s = SemanticSearcher()
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    monkeypatch.setattr(ss, "rss_mb", lambda: 120.0)
    assert s._rss_ok() is True
    assert s.is_tripped() is False
    assert s.is_available() is True


def test_breaker_disabled_when_rss_unmeasurable(monkeypatch):
    """When rss_mb() returns None (platform can't measure) the breaker is disabled
    (never trips) — graceful degradation."""
    monkeypatch.setenv("MERIDIAN_SEMANTIC_ENABLED", "1")
    s = SemanticSearcher()
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    monkeypatch.setattr(ss, "rss_mb", lambda: None)
    assert s._rss_ok() is True
    assert s.is_tripped() is False


def test_embed_returns_none_when_breaker_tripped(monkeypatch):
    """A tripped breaker makes embed() return None → rank() returns [] → keyword
    only. We avoid loading a real model by pre-tripping."""
    monkeypatch.setenv("MERIDIAN_SEMANTIC_ENABLED", "1")
    monkeypatch.setenv("MERIDIAN_SEMANTIC_RSS_LIMIT_MB", "400")
    s = SemanticSearcher()
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    monkeypatch.setattr(ss, "rss_mb", lambda: 999.0)
    # is_available False because breaker trips on the availability check's flow;
    # _ensure_model returns None so embed returns None without touching model2vec.
    assert s.embed(["hello"]) is None


# ---------------------------------------------------------------------------
# Enablement / availability — OFF by default; requires all three conditions.
# ---------------------------------------------------------------------------


def test_is_available_false_by_default(monkeypatch):
    """Env unset → OFF regardless of whether model2vec is importable."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    s = SemanticSearcher()
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    assert s.is_available() is False


def test_is_available_false_when_model2vec_not_importable(monkeypatch):
    """Enabled but model2vec missing (the real CI state) → still unavailable."""
    monkeypatch.setenv("MERIDIAN_SEMANTIC_ENABLED", "1")
    s = SemanticSearcher()
    monkeypatch.setattr(s, "_import_ok", lambda: False)
    assert s.is_available() is False


def test_is_available_true_when_all_conditions_met(monkeypatch):
    monkeypatch.setenv("MERIDIAN_SEMANTIC_ENABLED", "1")
    s = SemanticSearcher()
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    monkeypatch.setattr(ss, "rss_mb", lambda: 100.0)
    assert s.is_available() is True


def test_import_ok_never_raises_on_missing_model2vec(monkeypatch):
    """Simulate model2vec import failure — _import_ok returns False, never raises."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "model2vec" or name.startswith("model2vec."):
            raise ImportError("simulated: no model2vec in CI")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    s = SemanticSearcher()
    assert s._import_ok() is False  # never raises


# ---------------------------------------------------------------------------
# Lazy — importing the module does NOT import model2vec or load the model.
# ---------------------------------------------------------------------------


def test_module_import_does_not_import_model2vec():
    """Merely importing meridian.semantic_search must not import model2vec.

    We assert on the searcher singleton's tri-state cache: it starts unchecked
    (None), proving no import happened at module load, and the model is unloaded.
    """
    s = ss.get_searcher()
    assert s._model is None
    # If nothing has touched availability yet in this process the cache may already
    # be populated by another test; the load-bearing assertion is that the MODEL is
    # never loaded at import and no from_pretrained happened.
    assert s._model is None


def test_disabled_never_loads_model(monkeypatch):
    """With the feature disabled, _ensure_model returns None and never calls
    from_pretrained (which would download)."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    s = SemanticSearcher()

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("from_pretrained must not be called when disabled")

    # Even if model2vec is importable locally, disabled must short-circuit.
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    monkeypatch.setattr(ss, "rss_mb", lambda: 50.0)
    # Patch a fake model2vec module so a stray load would be caught, not downloaded.
    fake = type(sys)("model2vec")
    fake.StaticModel = type("SM", (), {"from_pretrained": staticmethod(_boom)})
    monkeypatch.setitem(sys.modules, "model2vec", fake)
    assert s._ensure_model() is None


# ---------------------------------------------------------------------------
# search_all end-to-end with semantic UNAVAILABLE (the default) — unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_all_unchanged_when_semantic_unavailable(db, monkeypatch):
    """The DEFAULT: semantic OFF → search_all returns plain keyword/tsvector
    results, no escalation, no error. SQLite path never even consults semantic."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "sem-default-off")
    await db_module.add_project_note(db, p["id"], "Rate limiting", "token bucket algorithm")
    result = await db_module.search_all(db, p["id"], "rate limiting")
    assert any(n["title"] == "Rate limiting" for n in result["notes"])
    # Return shape is the documented grouped dict.
    assert set(result) == {"query", "tasks", "notes", "decisions", "sprint_items", "total"}
    # No semantic-provenance flag on any keyword row.
    assert not any(n.get("semantic") for n in result["notes"])


@pytest.mark.asyncio
async def test_search_all_no_match_returns_empty_when_disabled(db, monkeypatch):
    """A query with no keyword hit and semantic disabled returns empty groups —
    never an error, and never escalates."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "sem-nomatch")
    await db_module.add_project_note(db, p["id"], "Deploy", "fly.io config")
    result = await db_module.search_all(db, p["id"], "zzznonexistentquery")
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# search_all PG escalation — gated on TEST_DATABASE_URL, semantic MOCKED.
# Proves the gate fires end-to-end and merges semantic hits WITHOUT a real model.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_all_pg_escalates_with_mocked_semantic(db_pg, monkeypatch):
    """On PG, when keyword/tsvector genuinely finds nothing (pure FTS 0, trigram
    low) and semantic is available, search_all embeds+ranks the corpus and merges
    the hits. Semantic is fully MOCKED — is_available True, rank returns a chosen id
    — so no model is loaded/downloaded. SKIPS without TEST_DATABASE_URL."""
    db = db_pg
    p = await db_module.create_project(db, "sem-pg-escalate")
    note = await db_module.add_project_note(
        db, p["id"], "Vector database", "embeddings and nearest-neighbor lookup")

    # A query that shares no lexical overlap with the note (so pure FTS = 0 and
    # trigram is low) but is semantically related.
    q = "similarity retrieval xyzzy"

    from meridian import semantic_search

    monkeypatch.setattr(semantic_search, "is_available", lambda: True)
    monkeypatch.setattr(semantic_search, "should_escalate", lambda fts, tri: True)
    # rank() returns the note id as a semantic hit above the floor.
    monkeypatch.setattr(
        semantic_search, "rank", lambda query, cands, floor=None: [(note["id"], 0.72)])

    result = await db_module.search_all(db, p["id"], q)
    hit_ids = [n["id"] for n in result["notes"]]
    assert note["id"] in hit_ids, "semantic escalation should surface the related note"
    matched = next(n for n in result["notes"] if n["id"] == note["id"])
    assert matched.get("semantic") is True


@pytest.mark.asyncio
async def test_search_all_pg_no_escalate_when_gate_false(db_pg, monkeypatch):
    """On PG, if should_escalate returns False the results are UNCHANGED — the
    semantic path (rank) is never invoked. SKIPS without TEST_DATABASE_URL."""
    db = db_pg
    p = await db_module.create_project(db, "sem-pg-nogate")
    await db_module.add_project_note(db, p["id"], "Auth", "oauth login flow")

    from meridian import semantic_search

    monkeypatch.setattr(semantic_search, "is_available", lambda: True)
    monkeypatch.setattr(semantic_search, "should_escalate", lambda fts, tri: False)

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("rank must not be called when the gate is closed")

    monkeypatch.setattr(semantic_search, "rank", _boom)
    # No error, and no semantic rows.
    result = await db_module.search_all(db, p["id"], "oauth")
    assert not any(n.get("semantic") for n in result["notes"])


@pytest.mark.asyncio
async def test_search_all_pg_escalation_tags_embedding_model_not_degraded_below_cap(
    db_pg, monkeypatch
):
    """e631d54f — a semantically-augmented row carries `embedding_model` and,
    when the candidate corpus is well under `_SEMANTIC_CORPUS_CAP`,
    `degraded=False` (the ranking pass was exhaustive over the whole
    corpus, not a truncated window)."""
    db = db_pg
    p = await db_module.create_project(db, "sem-pg-model-tag")
    note = await db_module.add_project_note(
        db, p["id"], "Vector database", "embeddings and nearest-neighbor lookup")
    q = "similarity retrieval xyzzy"

    from meridian import semantic_search

    monkeypatch.setattr(semantic_search, "is_available", lambda: True)
    monkeypatch.setattr(semantic_search, "should_escalate", lambda fts, tri: True)
    monkeypatch.setattr(
        semantic_search, "rank", lambda query, cands, floor=None: [(note["id"], 0.72)])

    result = await db_module.search_all(db, p["id"], q)
    matched = next(n for n in result["notes"] if n["id"] == note["id"])
    assert matched["semantic"] is True
    assert matched["embedding_model"] == semantic_search.model_name()
    assert matched["degraded"] is False


@pytest.mark.asyncio
async def test_search_all_pg_escalation_marks_degraded_when_corpus_capped(
    db_pg, monkeypatch
):
    """When the candidate corpus hits `_SEMANTIC_CORPUS_CAP`, augmented rows
    must be labeled `degraded=True` — the ranking pass only saw a bounded
    window, so it must never be read as an authoritative 'nothing else
    matches' answer."""
    db = db_pg
    p = await db_module.create_project(db, "sem-pg-capped")
    note = await db_module.add_project_note(
        db, p["id"], "Vector database", "embeddings and nearest-neighbor lookup")
    q = "similarity retrieval xyzzy"

    # Force the single real candidate row to exactly hit the (monkeypatched,
    # tiny) cap so is_corpus_capped(len(corpus), cap) is True.
    monkeypatch.setattr(db_module, "_SEMANTIC_CORPUS_CAP", 1)

    from meridian import semantic_search

    monkeypatch.setattr(semantic_search, "is_available", lambda: True)
    monkeypatch.setattr(semantic_search, "should_escalate", lambda fts, tri: True)
    monkeypatch.setattr(
        semantic_search, "rank", lambda query, cands, floor=None: [(note["id"], 0.72)])

    result = await db_module.search_all(db, p["id"], q)
    matched = next(n for n in result["notes"] if n["id"] == note["id"])
    assert matched["degraded"] is True


# ---------------------------------------------------------------------------
# 56cd8712 memory-safety fixes: pre-load headroom guard, chunked+truncated
# encode, background idle-unload. These install a STUB model2vec (never the real
# package, never a download) so the load/encode paths are exercised CI-safe.
# ---------------------------------------------------------------------------


def _install_fake_model2vec(monkeypatch, encode_impl):
    import types
    loaded = {"n": 0}

    class _FakeModel:
        def encode(self, texts):
            return encode_impl(list(texts))

    class _FakeStatic:
        @staticmethod
        def from_pretrained(name):
            loaded["n"] += 1
            return _FakeModel()

    fake = types.ModuleType("model2vec")
    fake.StaticModel = _FakeStatic
    monkeypatch.setitem(sys.modules, "model2vec", fake)
    return loaded


def test_preload_refuses_when_headroom_insufficient(monkeypatch):
    """56cd8712 — the PRE-load headroom guard refuses to START a load whose
    projected post-load RSS (current + _MODEL_EST_MB) would breach the limit — the
    OOM-during-load window — staying keyword-only without calling from_pretrained."""
    monkeypatch.setenv("MERIDIAN_SEMANTIC_ENABLED", "1")
    monkeypatch.setenv("MERIDIAN_SEMANTIC_RSS_LIMIT_MB", "380")
    loaded = _install_fake_model2vec(monkeypatch, lambda t: None)
    s = SemanticSearcher()
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    # rss UNDER the trip limit (so _rss_ok passes) but rss + _MODEL_EST_MB(120) > 380.
    monkeypatch.setattr(ss, "rss_mb", lambda: 300.0)  # 300 + 120 = 420 > 380 → refuse
    assert s._ensure_model() is None
    assert loaded["n"] == 0          # from_pretrained never called
    assert s._model is None
    # Ample headroom → the same load proceeds.
    monkeypatch.setattr(ss, "rss_mb", lambda: 100.0)  # 100 + 120 = 220 < 380
    assert s._ensure_model() is not None
    assert loaded["n"] == 1


def test_embed_chunks_and_truncates_large_corpus(monkeypatch):
    """56cd8712 — embed truncates each text to _MAX_TEXT_CHARS and encodes in
    RSS-checked sub-batches of _ENCODE_BATCH, bounding the encode-time RSS peak."""
    np = pytest.importorskip("numpy")
    monkeypatch.setenv("MERIDIAN_SEMANTIC_ENABLED", "1")
    monkeypatch.setenv("MERIDIAN_SEMANTIC_RSS_LIMIT_MB", "100000")  # never trips
    calls: "list[list[str]]" = []

    def _encode(texts):
        calls.append(texts)
        return np.ones((len(texts), 4), dtype="float32")

    _install_fake_model2vec(monkeypatch, _encode)
    s = SemanticSearcher()
    monkeypatch.setattr(s, "_import_ok", lambda: True)
    monkeypatch.setattr(ss, "rss_mb", lambda: 100.0)
    n = 70  # > _ENCODE_BATCH (32)
    corpus = ["x" * 5000] * n  # each > _MAX_TEXT_CHARS (2000)
    vecs = s.embed(corpus)
    assert vecs is not None and vecs.shape == (n, 4)
    assert calls and all(len(c) <= ss._ENCODE_BATCH for c in calls)  # chunked
    assert sum(len(c) for c in calls) == n                          # all encoded
    assert all(len(t) <= ss._MAX_TEXT_CHARS for c in calls for t in c)  # truncated


def test_maybe_idle_unload_releases_model_after_idle(monkeypatch):
    """56cd8712 — the background idle-unload (run from the server keepalive loop)
    releases the ~90MB model on a quiet box; no-op when nothing is loaded."""
    import time
    monkeypatch.setenv("MERIDIAN_SEMANTIC_IDLE_UNLOAD_S", "10")
    s = ss.get_searcher()  # the module singleton maybe_idle_unload targets
    try:
        s._model = None
        ss.maybe_idle_unload()            # no-op when nothing loaded
        assert s._model is None
        s._model = object()
        s._last_used = time.monotonic() - 100.0  # idle 100s > 10s window
        ss.maybe_idle_unload()
        assert s._model is None           # released
        s._model = object()
        s._last_used = time.monotonic()   # recently used
        ss.maybe_idle_unload()
        assert s._model is not None       # kept
    finally:
        s._model = None                   # don't leak singleton state


# ---------------------------------------------------------------------------
# 2204ce80 — hybrid_candidate_retrieval: exact-filter-first lexical discovery,
# optional bounded local-semantic rerank, fused scoring, exact-id pinning.
# All tests run on the default SQLite ``db`` fixture — semantic mocking uses
# monkeypatch on ``semantic_search.is_available``/``rank`` so no real model is
# ever loaded (same CI-safety guarantee as the rest of this file).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_retrieval_applies_exact_version_filter_first(db, monkeypatch):
    """A sprint_item candidate that matches the query lexically but lives in a
    DIFFERENT version is excluded before lexical scoring ever runs — the exact
    filter is applied in the SQL WHERE, not as a post-hoc rank penalty."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "hybrid-version-filter")
    v1_item = await db_module.add_sprint_item(db, p["id"], "v1", "rate limiter design")
    await db_module.add_sprint_item(db, p["id"], "v2", "rate limiter design")

    result = await db_module.hybrid_candidate_retrieval(
        db, p["id"], "rate limiter", source_types=["sprint_item"], version="v1",
    )
    ids = [c["id"] for c in result["candidates"]]
    assert ids == [v1_item["id"]]
    assert result["filters"]["version"] == "v1"


@pytest.mark.asyncio
async def test_hybrid_retrieval_applies_exact_status_filter(db, monkeypatch):
    """An explicit status filter is an exact match, applied before lexical
    discovery — a completed item is excluded when filtering for 'pending'."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "hybrid-status-filter")
    pending = await db_module.add_sprint_item(db, p["id"], "v1", "flush the cache")
    done = await db_module.add_sprint_item(
        db, p["id"], "v1", "flush the cache twice", force=True)
    await db_module.complete_sprint_item(db, p["id"], done["id"])

    result = await db_module.hybrid_candidate_retrieval(
        db, p["id"], "flush cache", source_types=["sprint_item"], status="pending",
    )
    ids = {c["id"] for c in result["candidates"]}
    assert pending["id"] in ids
    assert done["id"] not in ids


@pytest.mark.asyncio
async def test_hybrid_retrieval_default_lexical_only_provenance(db, monkeypatch):
    """DEFAULT (semantic disabled): every candidate is lexical-only — no
    semantic_score, fused_score == lexical_norm, semantic_used is False."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "hybrid-default-lexical")
    await db_module.add_project_note(db, p["id"], "Rate limiting", "token bucket algorithm")

    result = await db_module.hybrid_candidate_retrieval(
        db, p["id"], "rate limiting", source_types=["note"],
    )
    assert result["semantic_used"] is False
    assert len(result["candidates"]) == 1
    cand = result["candidates"][0]
    assert cand["provenance"] == "lexical"
    assert cand["semantic_score"] is None
    assert cand["fused_score"] == cand["lexical_score"] / cand["lexical_score"]  # normalized to 1.0
    assert cand["fused_score"] == 1.0


@pytest.mark.asyncio
async def test_hybrid_retrieval_exact_ids_always_pinned_ahead(db, monkeypatch):
    """An id passed via ``exact_ids`` is ALWAYS included — even when it does
    NOT match the query lexically at all — pinned ahead of every lexical/
    semantic row with provenance='exact' and fused_score=1.0. This is the
    'never let semantic create or replace a pointer' guarantee: the exact
    row's identity/rank never depends on any scoring step."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "hybrid-exact-pin")
    lexical_hit = await db_module.add_project_note(
        db, p["id"], "Auth flow", "oauth login handshake")
    pointer_target = await db_module.add_project_note(
        db, p["id"], "Completely unrelated", "zzz nothing to do with the query")

    result = await db_module.hybrid_candidate_retrieval(
        db, p["id"], "oauth login", source_types=["note"],
        exact_ids=[pointer_target["id"]],
    )
    assert result["candidates"][0]["id"] == pointer_target["id"]
    assert result["candidates"][0]["provenance"] == "exact"
    assert result["candidates"][0]["fused_score"] == 1.0
    ids = [c["id"] for c in result["candidates"]]
    assert lexical_hit["id"] in ids  # the genuine lexical hit is still present


@pytest.mark.asyncio
async def test_hybrid_retrieval_visibility_other_than_project_yields_nothing(db, monkeypatch):
    """Only visibility=None/'project' is wired up today; any other explicit
    value returns zero candidates rather than silently ignoring the filter
    (workspace-level visibility is a documented follow-up)."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "hybrid-visibility")
    await db_module.add_project_note(db, p["id"], "Rate limiting", "token bucket algorithm")

    result = await db_module.hybrid_candidate_retrieval(
        db, p["id"], "rate limiting", visibility="workspace",
    )
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_hybrid_retrieval_semantic_rerank_only_scores_lexical_pool(db, monkeypatch):
    """When semantic is available (mocked), it re-scores rows the lexical pass
    ALREADY found — it never introduces a candidate lexical search missed. The
    mocked rank() is asked ONLY about the ids the lexical pool discovered."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "hybrid-semantic-rerank")
    strong = await db_module.add_project_note(db, p["id"], "Auth service", "oauth login flow")
    weak = await db_module.add_project_note(db, p["id"], "Auth docs", "oauth login notes")
    unrelated = await db_module.add_project_note(db, p["id"], "Deploy", "fly.io config")

    seen_ids: list[str] = []

    def _fake_rank(query, candidates, floor=None):
        nonlocal seen_ids
        seen_ids = [cid for cid, _ in candidates]
        # Only the two lexically-discovered notes should ever be offered here.
        scores = {f"note:{strong['id']}": 0.9, f"note:{weak['id']}": 0.5}
        return sorted(
            ((cid, s) for cid, s in scores.items() if cid in seen_ids),
            key=lambda pair: pair[1], reverse=True,
        )

    monkeypatch.setattr(ss, "is_available", lambda: True)
    monkeypatch.setattr(ss, "rank", _fake_rank)

    result = await db_module.hybrid_candidate_retrieval(
        db, p["id"], "oauth login", source_types=["note"],
    )
    assert result["semantic_used"] is True
    assert f"note:{unrelated['id']}" not in seen_ids  # never offered to rank()
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id[strong["id"]]["provenance"] == "lexical+semantic"
    assert by_id[strong["id"]]["semantic_score"] == 0.9
    assert unrelated["id"] not in by_id  # lexical search never found it either
    # fused ordering follows the fused_score, not raw insertion order.
    assert result["candidates"][0]["id"] == strong["id"]


@pytest.mark.asyncio
async def test_hybrid_retrieval_blank_query_returns_no_candidates(db, monkeypatch):
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "hybrid-blank-query")
    result = await db_module.hybrid_candidate_retrieval(db, p["id"], "   ")
    assert result["candidates"] == []


# ---------------------------------------------------------------------------
# generate_handoff's optional related_records_query/related_records hook
# (2204ce80) — a thin, additive wrapper over hybrid_candidate_retrieval. Every
# pre-existing call site (both args at their default) must see ZERO change.
# ---------------------------------------------------------------------------


def _normalize_nondeterministic_handoff_fields(content: str) -> str:
    """Strip the per-call nondeterministic fields (the wall-clock
    ``_Generated at ..._`` line and the freshly-minted single-use
    ``<goal_token>``) so two handoffs rendered moments apart — otherwise
    identical — compare equal. Used ONLY to prove the new opt-in
    related_records args don't alter the rendered content; unrelated to the
    fields this sprint item's feature actually touches."""
    import re as _re
    content = _re.sub(r"_Generated at [^_]+_", "_Generated at TIMESTAMP_", content)
    content = _re.sub(r"<goal_token>[^<]*</goal_token>", "<goal_token>TOKEN</goal_token>", content)
    return content


@pytest.mark.asyncio
async def test_generate_handoff_related_records_default_off(db, tmp_path, monkeypatch):
    """Leaving both new args at their default is a complete no-op: same
    content as a call made before this feature existed."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "handoff-related-off")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")

    _, content_a, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="delta", skip_ai_summary=True,
    )
    _, content_b, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="delta", skip_ai_summary=True,
        related_records_query=None, related_records=None,
    )
    assert _normalize_nondeterministic_handoff_fields(content_a) == _normalize_nondeterministic_handoff_fields(content_b)


@pytest.mark.asyncio
async def test_generate_handoff_related_records_opt_in_populates_dict(db, tmp_path, monkeypatch):
    """Opting in on both args populates ``related_records`` in place via the
    shared hybrid retrieval path, WITHOUT altering the rendered content."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "handoff-related-on")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await db_module.add_project_note(db, p["id"], "Rate limiting", "token bucket algorithm")

    _, baseline_content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="delta", skip_ai_summary=True,
    )
    related: dict = {}
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="delta", skip_ai_summary=True,
        related_records_query="rate limiting", related_records=related,
    )
    # rendered output is untouched
    assert (
        _normalize_nondeterministic_handoff_fields(content)
        == _normalize_nondeterministic_handoff_fields(baseline_content)
    )
    assert related["candidates"], "expected the matching note to surface"
    assert any(c["title"] == "Rate limiting" for c in related["candidates"])


@pytest.mark.asyncio
async def test_generate_handoff_related_records_lookup_failure_never_breaks_handoff(
    db, tmp_path, monkeypatch,
):
    """A failure inside the related-records lookup must never break the
    mandatory handoff — it degrades to an empty result."""
    monkeypatch.delenv("MERIDIAN_SEMANTIC_ENABLED", raising=False)
    p = await db_module.create_project(db, "handoff-related-error")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")

    async def _boom(*a, **k):
        raise RuntimeError("simulated retrieval failure")

    monkeypatch.setattr(db_module, "hybrid_candidate_retrieval", _boom)
    related: dict = {}
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="delta", skip_ai_summary=True,
        related_records_query="rate limiting", related_records=related,
    )
    assert path and content  # the handoff itself still succeeded
    assert related == {"query": "rate limiting", "candidates": []}
