"""Coverage for the backend-neutral vector-index contract (e1475682).

Exercises :mod:`meridian_codeindex.vector_index`:

* :class:`DuckDBVSSBackend` end-to-end (real DuckDB + VSS extension — the
  same dependency :mod:`meridian_codeindex.code_index` already requires for
  its own optional vector leg) and its degrade-to-``VectorBackendUnavailable``
  path when the native extension can't load.
* :class:`LexicalBM25Backend` end-to-end (real DuckDB FTS).
* :class:`PgVectorBackend`'s validation (dimension/table-name) and its SQL/
  control-flow against an injected fake psycopg-shaped connection — no live
  Postgres is available in this environment, so the genuinely-Postgres-only
  behavior (``CREATE EXTENSION vector`` succeeding, real ``<=>`` ANN search)
  is intentionally NOT exercised here; that gap is inherent to a sandbox with
  no Postgres server, not a gap in this module's own logic.
* :func:`run_benchmark` / :func:`run_lexical_benchmark` — recall/latency/
  memory measurement, and graceful reporting of an unavailable backend.
* :func:`should_enable_pgvector` — the evidence gate, every branch.
* :func:`compare_candidates` — the full lexical/local-vector/pgvector
  orchestration the item's notes require.

Standalone package's own test suite — no Meridian import anywhere (see
``meridian_codeindex`` package docstring). Meridian-side persistence of this
contract's metadata (``meridian.db.vector_index_state``) is covered in the
parent repo's ``tests/test_semantic_search.py`` instead.
"""
from __future__ import annotations

import pytest

from meridian_codeindex.vector_index import (
    BenchmarkResult,
    DuckDBVSSBackend,
    IndexMetadata,
    LexicalBM25Backend,
    PgVectorBackend,
    VectorBackendUnavailable,
    VectorMatch,
    VectorRecord,
    compare_candidates,
    content_fingerprint,
    run_benchmark,
    run_lexical_benchmark,
    should_enable_pgvector,
)


def _vss_available() -> bool:
    try:
        import duckdb

        con = duckdb.connect()
        con.execute("INSTALL vss")
        con.execute("LOAD vss")
        con.close()
        return True
    except Exception:  # noqa: BLE001
        return False


_VSS_OK = _vss_available()


def _one_hot_vec(i: int, n: int = 5) -> tuple[float, ...]:
    """A near-one-hot unit vector pointing mostly along dimension ``i`` of an
    ``n``-dimensional space — distinct COSINE DIRECTIONS (not just distinct
    magnitudes) so a cosine-similarity backend can actually discriminate
    between records. Colinear vectors like ``(1,0,0)``/``(2,0,0)`` are
    cosine-IDENTICAL (cosine similarity is scale-invariant) and must never be
    used as a "distinct records" fixture for a cosine backend."""
    return tuple(1.0 if j == i else 0.01 for j in range(n))


# ===========================================================================
# content_fingerprint
# ===========================================================================


def test_content_fingerprint_order_independent():
    a = [VectorRecord(id="1", text="hello"), VectorRecord(id="2", text="world")]
    b = [VectorRecord(id="2", text="world"), VectorRecord(id="1", text="hello")]
    assert content_fingerprint(a) == content_fingerprint(b)


def test_content_fingerprint_changes_with_content():
    a = [VectorRecord(id="1", text="hello")]
    b = [VectorRecord(id="1", text="goodbye")]
    assert content_fingerprint(a) != content_fingerprint(b)


def test_content_fingerprint_empty_corpus_is_stable():
    assert content_fingerprint([]) == content_fingerprint([])


# ===========================================================================
# DuckDBVSSBackend
# ===========================================================================


@pytest.mark.skipif(not _VSS_OK, reason="DuckDB VSS extension not available in this env")
def test_duckdb_vss_backend_upsert_and_query_end_to_end():
    backend = DuckDBVSSBackend(embedding_model="test-model", project_id="p1", scope="s1")
    records = [
        VectorRecord(id="a", text="alpha", vector=(1.0, 0.0, 0.0, 0.0)),
        VectorRecord(id="b", text="beta", vector=(0.0, 1.0, 0.0, 0.0)),
        VectorRecord(id="c", text="gamma", vector=(0.9, 0.1, 0.0, 0.0)),
    ]
    backend.open()
    try:
        backend.upsert(records)
        matches = backend.query((1.0, 0.0, 0.0, 0.0), top_k=2)
        assert matches
        assert matches[0].id == "a"  # nearest to the exact query vector
        meta = backend.describe()
        assert meta.backend == "duckdb_vss"
        assert meta.dimension == 4
        assert meta.record_count == 3
        assert meta.embedding_model == "test-model"
        assert meta.project_id == "p1"
        assert meta.source_fingerprint == content_fingerprint(records)
    finally:
        backend.close()


@pytest.mark.skipif(not _VSS_OK, reason="DuckDB VSS extension not available in this env")
def test_duckdb_vss_backend_upsert_skips_records_without_vectors():
    backend = DuckDBVSSBackend()
    backend.open()
    try:
        backend.upsert([
            VectorRecord(id="a", text="alpha", vector=(1.0, 0.0)),
            VectorRecord(id="no-vec", text="nothing", vector=None),
        ])
        assert backend.describe().record_count == 1
    finally:
        backend.close()


@pytest.mark.skipif(not _VSS_OK, reason="DuckDB VSS extension not available in this env")
def test_duckdb_vss_backend_rejects_dimension_change_on_same_instance():
    backend = DuckDBVSSBackend()
    backend.open()
    try:
        backend.upsert([VectorRecord(id="a", text="a", vector=(1.0, 0.0))])
        with pytest.raises(ValueError):
            backend.upsert([VectorRecord(id="b", text="b", vector=(1.0, 0.0, 0.0))])
    finally:
        backend.close()


def test_duckdb_vss_backend_rejects_mixed_dimensions_in_one_call():
    backend = DuckDBVSSBackend(connection=object())  # never reaches DuckDB
    with pytest.raises(ValueError):
        backend.upsert([
            VectorRecord(id="a", text="a", vector=(1.0, 0.0)),
            VectorRecord(id="b", text="b", vector=(1.0, 0.0, 0.0)),
        ])


def test_duckdb_vss_backend_query_before_upsert_returns_empty():
    backend = DuckDBVSSBackend(connection=object())
    assert backend.query((1.0, 0.0), top_k=5) == []


class _BoomOnInstallConnection:
    """A DuckDB-connection stand-in whose extension install always fails —
    simulates the VSS extension being unavailable (offline sandbox, etc.)."""

    def execute(self, sql, params=None):  # noqa: ANN001
        if "INSTALL" in sql.upper():
            raise RuntimeError("simulated: network unreachable, extension unavailable")
        raise AssertionError(f"unexpected sql before INSTALL failure: {sql}")

    def close(self):
        pass


def test_duckdb_vss_backend_unavailable_when_extension_cannot_load():
    backend = DuckDBVSSBackend(connection=_BoomOnInstallConnection())
    with pytest.raises(VectorBackendUnavailable):
        backend.open()


def test_duckdb_vss_backend_unavailable_when_duckdb_not_importable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _boom_import(name, *a, **k):
        if name == "duckdb":
            raise ImportError("simulated: duckdb not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom_import)
    backend = DuckDBVSSBackend()
    with pytest.raises(VectorBackendUnavailable):
        backend.open()


# ===========================================================================
# LexicalBM25Backend
# ===========================================================================


def test_lexical_bm25_backend_end_to_end():
    backend = LexicalBM25Backend()
    backend.open()
    try:
        backend.upsert([
            VectorRecord(id="1", text="parse the authentication token"),
            VectorRecord(id="2", text="rotate the api key nightly"),
            VectorRecord(id="3", text="compress the output archive"),
        ])
        hits = backend.query_text("authentication token parsing", top_k=3)
        assert hits
        assert hits[0].id == "1"
        meta = backend.describe()
        assert meta.backend == "bm25_lexical"
        assert meta.record_count == 3
    finally:
        backend.close()


def test_lexical_bm25_backend_empty_query_returns_empty():
    backend = LexicalBM25Backend()
    backend.open()
    try:
        backend.upsert([VectorRecord(id="1", text="something")])
        assert backend.query_text("", top_k=5) == []
        assert backend.query_text("   ", top_k=5) == []
    finally:
        backend.close()


def test_lexical_bm25_backend_unavailable_when_extension_cannot_load():
    backend = LexicalBM25Backend(connection=_BoomOnInstallConnection())
    with pytest.raises(VectorBackendUnavailable):
        backend.open()


# ===========================================================================
# PgVectorBackend — validation + injected-fake-connection SQL/control-flow
# ===========================================================================


def test_pgvector_backend_rejects_non_positive_dimension():
    with pytest.raises(ValueError):
        PgVectorBackend(connection=object(), dimension=0)
    with pytest.raises(ValueError):
        PgVectorBackend(connection=object(), dimension=-1)


def test_pgvector_backend_rejects_unsafe_table_name():
    with pytest.raises(ValueError):
        PgVectorBackend(connection=object(), dimension=3, table="bad; DROP TABLE x;--")
    with pytest.raises(ValueError):
        PgVectorBackend(connection=object(), dimension=3, table="1_starts_with_digit")


def test_pgvector_backend_requires_dsn_or_connection():
    """Never auto-connects: with neither dsn nor connection, open() raises
    before touching the network or importing psycopg."""
    backend = PgVectorBackend(dimension=3)
    with pytest.raises(VectorBackendUnavailable):
        backend.open()


class _FakePgCursor:
    """Minimal psycopg3-shaped sync cursor over an in-memory dict store —
    enough to exercise PgVectorBackend's SQL construction and control flow
    without a live Postgres/pgvector server."""

    def __init__(self, store: dict) -> None:
        self._store = store
        self._result: list | None = None

    def __enter__(self) -> "_FakePgCursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
        params = params or ()
        s = sql.upper()
        if "CREATE EXTENSION" in s or "CREATE TABLE" in s:
            self._result = None
        elif s.strip().startswith("INSERT INTO"):
            rid, text, vec_literal = params
            self._store[rid] = (text, vec_literal)
            self._result = None
        elif "SELECT COUNT(*)" in s:
            self._result = [(len(self._store),)]
        elif "ORDER BY" in s:
            qlit, _qlit2, top_k = params
            rows = [
                (rid, 1.0 if vec_literal == qlit else 0.0)
                for rid, (_text, vec_literal) in self._store.items()
            ]
            rows.sort(key=lambda r: r[1], reverse=True)
            self._result = rows[: int(top_k)]
        else:  # pragma: no cover - defensive
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result or []


class _FakePgConnection:
    def __init__(self) -> None:
        self._store: dict = {}
        self.closed = False

    def cursor(self) -> _FakePgCursor:
        return _FakePgCursor(self._store)

    def close(self) -> None:
        self.closed = True


def test_pgvector_backend_open_upsert_query_with_injected_connection():
    con = _FakePgConnection()
    backend = PgVectorBackend(connection=con, dimension=3, table="test_vecs")
    backend.open()
    records = [
        VectorRecord(id="a", text="alpha", vector=(1.0, 0.0, 0.0)),
        VectorRecord(id="b", text="beta", vector=(0.0, 1.0, 0.0)),
    ]
    backend.upsert(records)
    matches = backend.query((1.0, 0.0, 0.0), top_k=5)
    assert matches
    assert matches[0].id == "a"
    meta = backend.describe()
    assert meta.backend == "pgvector"
    assert meta.record_count == 2
    assert meta.dimension == 3
    backend.close()
    assert con.closed is False  # caller-supplied connection is never owned/closed by the backend


def test_pgvector_backend_owns_and_closes_connection_it_opened():
    con = _FakePgConnection()

    class _Psycopg:
        @staticmethod
        def connect(dsn, autocommit=True):  # noqa: ANN001
            assert autocommit is True
            return con

    import sys

    sys_modules_backup = sys.modules.get("psycopg")
    sys.modules["psycopg"] = _Psycopg  # type: ignore[assignment]
    try:
        backend = PgVectorBackend(dsn="postgresql://fake/db", dimension=3)
        backend.open()
        backend.upsert([VectorRecord(id="a", text="a", vector=(1.0, 0.0, 0.0))])
        backend.close()
        assert con.closed is True  # owned connection IS closed
    finally:
        if sys_modules_backup is not None:
            sys.modules["psycopg"] = sys_modules_backup
        else:
            sys.modules.pop("psycopg", None)


def test_pgvector_backend_dimension_mismatch_raises():
    con = _FakePgConnection()
    backend = PgVectorBackend(connection=con, dimension=3)
    backend.open()
    with pytest.raises(ValueError):
        backend.upsert([VectorRecord(id="x", text="x", vector=(1.0, 2.0))])


def test_pgvector_backend_query_before_open_returns_empty():
    backend = PgVectorBackend(connection=_FakePgConnection(), dimension=3)
    assert backend.query((1.0, 0.0, 0.0)) == []


def test_pgvector_backend_open_failure_wraps_as_unavailable():
    class _BoomConnection:
        def cursor(self):
            raise RuntimeError("simulated: extension not installed on this server")

    backend = PgVectorBackend(connection=_BoomConnection(), dimension=3)
    with pytest.raises(VectorBackendUnavailable):
        backend.open()


# ===========================================================================
# run_benchmark / run_lexical_benchmark
# ===========================================================================


class _AlwaysUnavailableBackend:
    name = "always_unavailable"

    def open(self) -> None:
        raise VectorBackendUnavailable("simulated: never available")

    def upsert(self, records) -> None:  # pragma: no cover - never reached
        raise AssertionError("upsert must not be called when open() failed")

    def query(self, vector, top_k=10):  # pragma: no cover - never reached
        raise AssertionError("query must not be called when open() failed")

    def describe(self) -> IndexMetadata:  # pragma: no cover
        return IndexMetadata(backend=self.name)

    def close(self) -> None:
        pass


def test_run_benchmark_reports_unavailable_backend_gracefully():
    result = run_benchmark(
        _AlwaysUnavailableBackend(),
        [VectorRecord(id="1", text="x", vector=(1.0,))],
        [((1.0,), "1")],
    )
    assert result.available is False
    assert result.backend == "always_unavailable"
    assert "never available" in (result.error or "")


@pytest.mark.skipif(not _VSS_OK, reason="DuckDB VSS extension not available in this env")
def test_run_benchmark_measures_recall_and_latency():
    records = [
        VectorRecord(id=str(i), text=f"item {i}", vector=_one_hot_vec(i))
        for i in range(5)
    ]
    queries = [(_one_hot_vec(i), str(i)) for i in range(5)]
    result = run_benchmark(DuckDBVSSBackend(), records, queries, top_k=1)
    assert result.available is True
    assert result.recall_at_k == 1.0
    assert result.record_count == 5
    assert result.query_count == 5
    assert result.build_ms is not None and result.build_ms >= 0.0
    assert result.latency_ms_p50 is not None and result.latency_ms_p50 >= 0.0
    assert result.latency_ms_p95 is not None and result.latency_ms_p95 >= 0.0


def test_run_lexical_benchmark_measures_recall():
    backend = LexicalBM25Backend()
    records = [
        VectorRecord(id="1", text="parse the authentication token"),
        VectorRecord(id="2", text="rotate the api key nightly"),
        VectorRecord(id="3", text="compress the output archive"),
    ]
    queries = [("authentication token parsing", "1"), ("rotate api key", "2")]
    result = run_lexical_benchmark(backend, records, queries, top_k=3)
    assert result.available is True
    assert result.recall_at_k == 1.0
    assert result.backend == "bm25_lexical"


def test_benchmark_result_to_dict_is_json_ready():
    r = BenchmarkResult(backend="x", available=True, recall_at_k=0.5)
    d = r.to_dict()
    assert d["backend"] == "x"
    assert d["recall_at_k"] == 0.5


# ===========================================================================
# should_enable_pgvector — the evidence gate
# ===========================================================================


def test_should_enable_pgvector_true_when_recall_matches_or_beats_baseline():
    duckdb_r = BenchmarkResult(backend="duckdb_vss", available=True, recall_at_k=0.8)
    pgvector_r = BenchmarkResult(backend="pgvector", available=True, recall_at_k=0.82)
    enabled, reason = should_enable_pgvector(duckdb_r, pgvector_r)
    assert enabled is True
    assert "evidence supports" in reason


def test_should_enable_pgvector_true_within_tolerance():
    duckdb_r = BenchmarkResult(backend="duckdb_vss", available=True, recall_at_k=0.90)
    pgvector_r = BenchmarkResult(backend="pgvector", available=True, recall_at_k=0.89)
    enabled, _reason = should_enable_pgvector(duckdb_r, pgvector_r, recall_tolerance=0.02)
    assert enabled is True


def test_should_enable_pgvector_false_on_regression():
    duckdb_r = BenchmarkResult(backend="duckdb_vss", available=True, recall_at_k=0.9)
    pgvector_r = BenchmarkResult(backend="pgvector", available=True, recall_at_k=0.5)
    enabled, reason = should_enable_pgvector(duckdb_r, pgvector_r)
    assert enabled is False
    assert "regresses" in reason


def test_should_enable_pgvector_false_when_pgvector_unavailable():
    duckdb_r = BenchmarkResult(backend="duckdb_vss", available=True, recall_at_k=0.9)
    pgvector_r = BenchmarkResult(backend="pgvector", available=False, error="no dsn configured")
    enabled, reason = should_enable_pgvector(duckdb_r, pgvector_r)
    assert enabled is False
    assert "unavailable" in reason


def test_should_enable_pgvector_false_when_no_duckdb_baseline():
    duckdb_r = BenchmarkResult(backend="duckdb_vss", available=False, error="boom")
    pgvector_r = BenchmarkResult(backend="pgvector", available=True, recall_at_k=0.9)
    enabled, reason = should_enable_pgvector(duckdb_r, pgvector_r)
    assert enabled is False
    assert "baseline" in reason


def test_should_enable_pgvector_false_when_recall_not_measured():
    duckdb_r = BenchmarkResult(backend="duckdb_vss", available=True, recall_at_k=None)
    pgvector_r = BenchmarkResult(backend="pgvector", available=True, recall_at_k=0.9)
    enabled, reason = should_enable_pgvector(duckdb_r, pgvector_r)
    assert enabled is False
    assert "not measured" in reason


# ===========================================================================
# compare_candidates — the full orchestration
# ===========================================================================


@pytest.mark.skipif(not _VSS_OK, reason="DuckDB VSS extension not available in this env")
def test_compare_candidates_without_pgvector_backend_defaults_closed():
    records = [
        VectorRecord(id=str(i), text=f"item {i}", vector=_one_hot_vec(i))
        for i in range(5)
    ]
    queries = [(_one_hot_vec(i), str(i)) for i in range(5)]
    result = compare_candidates(records=records, vector_queries=queries)
    assert result["decision"]["pgvector_enabled"] is False
    assert "not supplied" in result["decision"]["reason"]
    assert "duckdb_vss" in result["results"]
    assert "pgvector" not in result["results"]


@pytest.mark.skipif(not _VSS_OK, reason="DuckDB VSS extension not available in this env")
def test_compare_candidates_includes_lexical_when_supplied():
    records = [VectorRecord(id="1", text="alpha token", vector=(1.0, 0.0))]
    vector_queries = [((1.0, 0.0), "1")]
    lexical_queries = [("alpha token", "1")]
    result = compare_candidates(
        records=records, vector_queries=vector_queries,
        lexical_queries=lexical_queries, lexical_backend=LexicalBM25Backend(),
    )
    assert "bm25_lexical" in result["results"]
    assert result["results"]["bm25_lexical"]["recall_at_k"] == 1.0


class _FakeGoodPgVectorBackend:
    """Duck-typed VectorIndexBackend with perfect recall — used to exercise
    compare_candidates'/should_enable_pgvector's ENABLED path without a real
    Postgres server."""

    name = "pgvector"

    def __init__(self) -> None:
        self._store: dict = {}

    def open(self) -> None:
        pass

    def upsert(self, records) -> None:  # noqa: ANN001
        for r in records:
            if r.vector:
                self._store[r.id] = r.vector

    def query(self, vector, top_k=10):  # noqa: ANN001
        def _dist(vec):
            return sum((a - b) ** 2 for a, b in zip(vec, vector))

        ranked = sorted(self._store.items(), key=lambda kv: _dist(kv[1]))
        return [VectorMatch(id=rid, score=-_dist(vec)) for rid, vec in ranked[:top_k]]

    def describe(self) -> IndexMetadata:
        return IndexMetadata(backend=self.name, record_count=len(self._store))

    def close(self) -> None:
        pass


@pytest.mark.skipif(not _VSS_OK, reason="DuckDB VSS extension not available in this env")
def test_compare_candidates_enables_pgvector_when_evidence_supports():
    records = [
        VectorRecord(id=str(i), text=f"item {i}", vector=_one_hot_vec(i))
        for i in range(5)
    ]
    queries = [(_one_hot_vec(i), str(i)) for i in range(5)]
    result = compare_candidates(
        records=records, vector_queries=queries,
        pgvector_backend=_FakeGoodPgVectorBackend(),
    )
    assert result["decision"]["pgvector_enabled"] is True
    assert result["results"]["pgvector"]["recall_at_k"] == 1.0
    assert result["results"]["duckdb_vss"]["recall_at_k"] == 1.0
