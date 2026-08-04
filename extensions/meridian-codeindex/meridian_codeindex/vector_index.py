"""Backend-neutral vector-index contract (e1475682).

One contract, two vector backends, and an evidence gate between them:

* :class:`DuckDBVSSBackend` — local-first, the *default* candidate. Wraps
  DuckDB's VSS extension (HNSW cosine), the same native extension
  :mod:`meridian_codeindex.code_index` already uses for its own optional
  vector leg — this module extracts that pattern into a reusable,
  corpus-agnostic backend so callers other than :class:`CodeIndex` (e.g. a
  host application's notes/handoffs/sprint-item search) can use it too.
* :class:`PgVectorBackend` — an *optional* shared-Postgres (Neon) candidate.
  **Never auto-constructed and never auto-enabled.** It requires an explicit
  DSN or live connection from the caller, and even then nothing routes real
  traffic to it just because it opened successfully — see
  :func:`should_enable_pgvector`.
* :class:`LexicalBM25Backend` — the "lexical-only" baseline (DuckDB FTS /
  Okapi BM25) every comparison is measured against. Not a
  :class:`VectorIndexBackend` (it queries by text, not by vector) — kept
  distinct rather than forcing a text query through a vector-shaped API.

The item's directive is explicit: *"Do not introduce pgvector merely because
it exists; require measured recall, latency, memory, and cost evidence."*
:func:`run_benchmark` / :func:`run_lexical_benchmark` measure exactly those
axes (recall@k via labelled self-retrieval queries, p50/p95 query latency,
process RSS delta, and index build time as a cost proxy) for whichever
backends the caller supplies, and :func:`should_enable_pgvector` is the one
place that turns those numbers into a yes/no decision — closed by default,
same as a `required` capability with no evidence in AGENTS.md's capability
manifest contract (fail closed, never guess).

Every backend degrades the same way: a missing native extension / driver /
DSN raises :class:`VectorBackendUnavailable` from :meth:`open`, never from
``upsert``/``query`` — callers catch it in exactly one place and fall back to
:class:`LexicalBM25Backend` (or their own existing keyword search — see
``meridian.semantic_search`` for the notes/handoff equivalent of this same
opt-in, circuit-broken posture). Nothing in this module imports Meridian —
it is a standalone contract usable by any host, the same zero-host-dependency
posture as :mod:`meridian_codeindex.code_index`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

_log = logging.getLogger(__name__)

__all__ = [
    "VectorBackendUnavailable",
    "VectorRecord",
    "VectorMatch",
    "IndexMetadata",
    "BenchmarkResult",
    "VectorIndexBackend",
    "DuckDBVSSBackend",
    "PgVectorBackend",
    "LexicalBM25Backend",
    "content_fingerprint",
    "run_benchmark",
    "run_lexical_benchmark",
    "compare_candidates",
    "should_enable_pgvector",
    "DEFAULT_RECALL_REGRESSION_TOLERANCE",
]


class VectorBackendUnavailable(RuntimeError):
    """Raised by :meth:`VectorIndexBackend.open` when a backend's native
    dependency (extension / driver / DSN / model) cannot be loaded here.

    Always a well-formed availability signal, never a programming error —
    callers are expected to catch this in one place and degrade to a lexical
    baseline, exactly like ``meridian.semantic_search``'s "unavailable ->
    keyword-only" posture.
    """


@dataclass(frozen=True)
class VectorRecord:
    """One record to index: a stable ``id``, its source ``text`` (kept for
    the lexical baseline and for debugging hits), and a pre-computed
    embedding ``vector``. This module never computes embeddings itself —
    that stays the host's responsibility (``meridian.semantic_search.embed``,
    :class:`meridian_codeindex.code_index._Embedder`, or any other embedder)
    so this contract has no ML-library dependency of its own."""

    id: str
    text: str
    vector: tuple[float, ...] | None = None


@dataclass(frozen=True)
class VectorMatch:
    """One search hit. ``score`` is always "higher is better" (a similarity,
    never a raw distance) so callers never have to remember which backend
    inverts its native metric."""

    id: str
    score: float


@dataclass
class IndexMetadata:
    """What the item's notes require an index to remember about itself:
    embedding model/version, dimension, a source fingerprint, tenant/project
    scope, and a revision — see ``meridian.db.vector_index_state`` for the
    durable persistence of this shape."""

    backend: str
    embedding_model: str | None = None
    embedding_version: str | None = None
    dimension: int | None = None
    source_fingerprint: str | None = None
    project_id: str | None = None
    scope: str = "default"
    revision: int = 1
    record_count: int = 0


def content_fingerprint(records: Iterable[VectorRecord]) -> str:
    """Deterministic, order-independent sha256 fingerprint of a corpus's
    ``(id, text)`` pairs.

    Sorted by id before hashing, so re-supplying the same corpus in a
    different order yields the same fingerprint. A stored fingerprint that no
    longer matches the live corpus's fingerprint is the staleness signal
    ``meridian.db.vector_index_state`` exists to make checkable.
    """
    h = hashlib.sha256()
    for rid, text in sorted((r.id, r.text or "") for r in records):
        h.update(rid.encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update((text or "").encode("utf-8", "replace"))
        h.update(b"\x01")
    return h.hexdigest()


def _rss_mb() -> float | None:
    """Best-effort current-process RSS in MB, or ``None`` if unmeasurable.

    Self-contained duplicate of ``meridian.semantic_search.rss_mb``'s
    strategy (psutil, then Linux ``/proc/self/statm``) — this module has zero
    Meridian import (see module docstring), so it cannot reuse that function
    directly.
    """
    try:
        import psutil  # type: ignore  # noqa: PLC0415

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - psutil missing or platform quirk
        pass
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            fields = fh.read().split()
        resident_pages = int(fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return resident_pages * page_size / (1024 * 1024)
    except Exception:  # noqa: BLE001 - not Linux / no /proc
        return None


# ===========================================================================
# The contract
# ===========================================================================


class VectorIndexBackend(ABC):
    """One backend implementation of the vector-index contract.

    Lifecycle: :meth:`open` once (raises :class:`VectorBackendUnavailable` if
    this backend cannot run here — the ONLY method allowed to raise that),
    then any number of :meth:`upsert` / :meth:`query` / :meth:`describe`
    calls, then :meth:`close`. Usable as a context manager.

    ``upsert`` is a full-corpus rebuild pattern (mirrors
    ``CodeIndex._rebuild_vss``, which re-embeds "every chunk" on each
    rebuild) — records already present but *absent* from a given ``upsert``
    call are NOT deleted; comparisons in this module always call ``upsert``
    exactly once per freshly-opened backend, so that distinction never
    matters here, but a caller reusing a backend instance across multiple
    ``upsert`` calls should account for it.
    """

    name: str

    @abstractmethod
    def open(self) -> None:
        """Acquire native resources. Raises :class:`VectorBackendUnavailable`
        if this backend cannot be used in this environment right now."""

    @abstractmethod
    def upsert(self, records: Sequence[VectorRecord]) -> None:
        """Insert/update ``records`` that carry a ``vector``. Records with no
        vector are silently skipped (nothing to index)."""

    @abstractmethod
    def query(self, vector: Sequence[float], top_k: int = 10) -> list[VectorMatch]:
        """Nearest-neighbour search. Returns ``[]`` (never raises) for an
        empty/not-yet-built index or a malformed query vector."""

    @abstractmethod
    def describe(self) -> IndexMetadata:
        """Current metadata snapshot — see :class:`IndexMetadata`."""

    def close(self) -> None:
        """Release native resources. No-op by default."""

    def __enter__(self) -> "VectorIndexBackend":
        self.open()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# ===========================================================================
# DuckDB VSS — local-first, default candidate
# ===========================================================================


class DuckDBVSSBackend(VectorIndexBackend):
    """Local DuckDB VSS (HNSW cosine) backend — no network, no shared state.

    Mirrors :meth:`meridian_codeindex.code_index.CodeIndex._rebuild_vss` /
    ``_vss_search``'s SQL shape exactly (same ``INSTALL vss`` /
    ``hnsw_enable_experimental_persistence`` / ``FLOAT[dim]`` column /
    ``array_cosine_distance`` pattern), generalized to an arbitrary
    ``(id, text, vector)`` corpus instead of code chunks specifically.
    """

    name = "duckdb_vss"

    def __init__(
        self,
        *,
        db_path: str = ":memory:",
        connection: Any = None,
        embedding_model: str | None = None,
        embedding_version: str | None = None,
        project_id: str | None = None,
        scope: str = "default",
    ) -> None:
        self._db_path = db_path
        self._con = connection
        self._owns_con = connection is None
        self.embedding_model = embedding_model
        self.embedding_version = embedding_version
        self.project_id = project_id
        self.scope = scope
        self._dim: int | None = None
        self._ready = False
        self._fingerprint: str | None = None
        self._count = 0

    def open(self) -> None:
        try:
            if self._con is None:
                import duckdb  # noqa: PLC0415

                self._con = duckdb.connect(self._db_path)
            self._con.execute("INSTALL vss")
            self._con.execute("LOAD vss")
            self._con.execute("SET hnsw_enable_experimental_persistence = true")
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS vector_records "
                "(id VARCHAR PRIMARY KEY, text VARCHAR)"
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorBackendUnavailable(f"duckdb vss unavailable: {exc}") from exc

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if self._con is None:
            raise VectorBackendUnavailable("DuckDBVSSBackend.open() was not called")
        recs = [r for r in records if r.vector]
        if not recs:
            return
        dim = len(recs[0].vector)  # type: ignore[arg-type]
        if any(len(r.vector) != dim for r in recs):  # type: ignore[arg-type]
            raise ValueError(
                "all records in one upsert() call must share the same "
                "embedding dimension"
            )
        if self._dim is None:
            self._con.execute(f"ALTER TABLE vector_records ADD COLUMN embedding FLOAT[{dim}]")
            self._dim = dim
        elif dim != self._dim:
            raise ValueError(
                f"embedding dimension changed from {self._dim} to {dim} — "
                "open a fresh DuckDBVSSBackend instead of upserting a "
                "different dimension into an existing index"
            )
        for r in recs:
            self._con.execute(
                "INSERT INTO vector_records (id, text, embedding) VALUES (?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET text = excluded.text, "
                "embedding = excluded.embedding",
                [r.id, r.text, list(r.vector)],  # type: ignore[arg-type]
            )
        self._con.execute("DROP INDEX IF EXISTS vector_records_vec_idx")
        self._con.execute(
            "CREATE INDEX vector_records_vec_idx ON vector_records "
            "USING HNSW (embedding) WITH (metric = 'cosine')"
        )
        self._ready = True
        self._count = int(
            self._con.execute("SELECT COUNT(*) FROM vector_records").fetchone()[0]
        )
        self._fingerprint = content_fingerprint(recs)

    def query(self, vector: Sequence[float], top_k: int = 10) -> list[VectorMatch]:
        if not self._ready or self._con is None:
            return []
        dim = self._dim or len(vector)
        try:
            rel = self._con.execute(
                f"SELECT id, array_cosine_distance(embedding, ?::FLOAT[{dim}]) AS dist "
                "FROM vector_records WHERE embedding IS NOT NULL "
                "ORDER BY dist LIMIT ?",
                [list(vector), int(top_k)],
            )
            rows = rel.fetchall()
        except Exception:  # noqa: BLE001
            return []
        # Distance -> similarity: "higher is better" per the contract.
        return [VectorMatch(id=row[0], score=-float(row[1])) for row in rows if row[1] is not None]

    def describe(self) -> IndexMetadata:
        return IndexMetadata(
            backend=self.name,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
            dimension=self._dim,
            source_fingerprint=self._fingerprint,
            project_id=self.project_id,
            scope=self.scope,
            record_count=self._count,
        )

    def close(self) -> None:
        if self._owns_con and self._con is not None:
            try:
                self._con.close()
            except Exception:  # noqa: BLE001
                _log.debug("DuckDBVSSBackend.close failed", exc_info=True)
        self._con = None
        self._ready = False


# ===========================================================================
# pgvector — optional shared-Postgres candidate, never auto-enabled
# ===========================================================================

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _pg_vector_literal(vec: Sequence[float]) -> str:
    """Serialize a vector as pgvector's ``'[1,2,3]'`` text literal.

    Used with an explicit ``%s::vector`` cast rather than relying on the
    optional ``pgvector`` Python package's psycopg adapter being registered
    — this module only depends on ``psycopg`` itself (already a Meridian
    dependency), never the extra ``pgvector`` package.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorBackend(VectorIndexBackend):
    """Optional shared-Postgres (Neon) pgvector backend.

    **Never auto-constructed, never auto-enabled.** Requires an explicit
    ``dsn`` or live ``connection`` — with neither, :meth:`open` raises
    :class:`VectorBackendUnavailable` immediately, before touching the
    network. Even a successful :meth:`open` doesn't mean queries should be
    routed here in production — that decision belongs to
    :func:`should_enable_pgvector`, evaluated against measured benchmark
    evidence, never to this class opening cleanly.

    Uses psycopg3's *synchronous* API directly (this package has no asyncio
    dependency of its own) with ``%s`` placeholders — never ``?`` — per
    Meridian's psycopg3 convention (this is genuine direct psycopg3 code, not
    routed through the ``?``-using aiosqlite-compatible ``meridian.db``
    layer, so the ``%s``-only rule applies here as written).
    """

    name = "pgvector"

    def __init__(
        self,
        *,
        dsn: str | None = None,
        connection: Any = None,
        table: str = "meridian_vector_records",
        dimension: int,
        embedding_model: str | None = None,
        embedding_version: str | None = None,
        project_id: str | None = None,
        scope: str = "default",
    ) -> None:
        if dimension <= 0:
            raise ValueError("PgVectorBackend requires a positive fixed dimension")
        if not _SAFE_IDENTIFIER.match(table):
            raise ValueError(f"unsafe table name: {table!r}")
        self._dsn = dsn
        self._con = connection
        self._owns_con = connection is None
        self._table = table
        self.dimension = dimension
        self.embedding_model = embedding_model
        self.embedding_version = embedding_version
        self.project_id = project_id
        self.scope = scope
        self._ready = False
        self._fingerprint: str | None = None
        self._count = 0

    def open(self) -> None:
        if self._con is None and not self._dsn:
            raise VectorBackendUnavailable(
                "PgVectorBackend requires an explicit dsn= or connection= — "
                "it never auto-connects to any default database"
            )
        try:
            if self._con is None:
                import psycopg  # noqa: PLC0415

                self._con = psycopg.connect(self._dsn, autocommit=True)
            with self._con.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} "
                    f"(id TEXT PRIMARY KEY, text_content TEXT, "
                    f"embedding vector({int(self.dimension)}))"
                )
        except Exception as exc:  # noqa: BLE001
            raise VectorBackendUnavailable(f"pgvector unavailable: {exc}") from exc

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if self._con is None:
            raise VectorBackendUnavailable("PgVectorBackend.open() was not called")
        recs = [r for r in records if r.vector]
        if not recs:
            return
        for r in recs:
            if len(r.vector) != self.dimension:  # type: ignore[arg-type]
                raise ValueError(
                    f"record {r.id!r} vector dim {len(r.vector)} != "  # type: ignore[arg-type]
                    f"backend dimension {self.dimension}"
                )
        with self._con.cursor() as cur:
            for r in recs:
                cur.execute(
                    f"INSERT INTO {self._table} (id, text_content, embedding) "
                    f"VALUES (%s, %s, %s::vector) ON CONFLICT (id) DO UPDATE SET "
                    f"text_content = EXCLUDED.text_content, "
                    f"embedding = EXCLUDED.embedding",
                    (r.id, r.text, _pg_vector_literal(r.vector)),  # type: ignore[arg-type]
                )
            cur.execute(f"SELECT COUNT(*) FROM {self._table}")
            row = cur.fetchone()
            self._count = int(row[0]) if row else 0
        self._ready = True
        self._fingerprint = content_fingerprint(recs)

    def query(self, vector: Sequence[float], top_k: int = 10) -> list[VectorMatch]:
        if not self._ready or self._con is None:
            return []
        if len(vector) != self.dimension:
            return []
        qlit = _pg_vector_literal(vector)
        try:
            with self._con.cursor() as cur:
                cur.execute(
                    f"SELECT id, 1 - (embedding <=> %s::vector) AS score "
                    f"FROM {self._table} ORDER BY embedding <=> %s::vector LIMIT %s",
                    (qlit, qlit, int(top_k)),
                )
                rows = cur.fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [VectorMatch(id=row[0], score=float(row[1])) for row in rows if row[1] is not None]

    def describe(self) -> IndexMetadata:
        return IndexMetadata(
            backend=self.name,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
            dimension=self.dimension,
            source_fingerprint=self._fingerprint,
            project_id=self.project_id,
            scope=self.scope,
            record_count=self._count,
        )

    def close(self) -> None:
        if self._owns_con and self._con is not None:
            try:
                self._con.close()
            except Exception:  # noqa: BLE001
                _log.debug("PgVectorBackend.close failed", exc_info=True)
        self._con = None
        self._ready = False


# ===========================================================================
# Lexical (BM25) baseline — the "lexical-only" benchmark candidate
# ===========================================================================


class LexicalBM25Backend:
    """DuckDB FTS (Okapi BM25) baseline — the "lexical-only" candidate the
    item's notes require benchmarking against.

    Deliberately NOT a :class:`VectorIndexBackend`: BM25 queries by text, not
    by embedding vector, so forcing it through ``query(vector)`` would
    misrepresent what it does. Mirrors
    :meth:`meridian_codeindex.code_index.CodeIndex._rebuild_fts` /
    ``_bm25_search``'s SQL shape.
    """

    name = "bm25_lexical"

    def __init__(self, *, db_path: str = ":memory:", connection: Any = None) -> None:
        self._db_path = db_path
        self._con = connection
        self._owns_con = connection is None
        self._ready = False
        self._fingerprint: str | None = None
        self._count = 0

    def open(self) -> None:
        try:
            if self._con is None:
                import duckdb  # noqa: PLC0415

                self._con = duckdb.connect(self._db_path)
            self._con.execute("INSTALL fts")
            self._con.execute("LOAD fts")
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS lexical_records "
                "(id VARCHAR PRIMARY KEY, text VARCHAR)"
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorBackendUnavailable(f"duckdb fts unavailable: {exc}") from exc

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if self._con is None:
            raise VectorBackendUnavailable("LexicalBM25Backend.open() was not called")
        recs = list(records)
        if not recs:
            return
        for r in recs:
            self._con.execute(
                "INSERT INTO lexical_records (id, text) VALUES (?, ?) "
                "ON CONFLICT (id) DO UPDATE SET text = excluded.text",
                [r.id, r.text],
            )
        self._con.execute(
            "PRAGMA create_fts_index("
            "'lexical_records', 'id', 'text', "
            "stemmer = 'porter', stopwords = 'none', overwrite = 1)"
        )
        self._ready = True
        self._count = int(
            self._con.execute("SELECT COUNT(*) FROM lexical_records").fetchone()[0]
        )
        self._fingerprint = content_fingerprint(recs)

    def query_text(self, query: str, top_k: int = 10) -> list[VectorMatch]:
        if not self._ready or self._con is None or not (query or "").strip():
            return []
        try:
            rel = self._con.execute(
                "SELECT id, fts_main_lexical_records.match_bm25(id, ?) AS bm25 "
                "FROM lexical_records",
                [query],
            )
            rows = rel.fetchall()
        except Exception:  # noqa: BLE001
            return []
        hits = [
            VectorMatch(id=row[0], score=float(row[1]))
            for row in rows if row[1] is not None
        ]
        hits.sort(key=lambda m: m.score, reverse=True)
        return hits[: max(1, int(top_k))]

    def describe(self) -> IndexMetadata:
        return IndexMetadata(
            backend=self.name,
            source_fingerprint=self._fingerprint,
            record_count=self._count,
        )

    def close(self) -> None:
        if self._owns_con and self._con is not None:
            try:
                self._con.close()
            except Exception:  # noqa: BLE001
                _log.debug("LexicalBM25Backend.close failed", exc_info=True)
        self._con = None
        self._ready = False


# ===========================================================================
# Benchmark harness
# ===========================================================================


@dataclass
class BenchmarkResult:
    """Measured evidence for one backend candidate — recall, latency, memory,
    and build time (a cost proxy) — the four axes the item's notes require."""

    backend: str
    available: bool
    record_count: int = 0
    query_count: int = 0
    recall_at_k: float | None = None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    build_ms: float | None = None
    rss_mb_delta: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def run_benchmark(
    backend: VectorIndexBackend,
    records: Sequence[VectorRecord],
    queries: Sequence[tuple[Sequence[float], str]],
    *,
    top_k: int = 5,
) -> BenchmarkResult:
    """Benchmark one :class:`VectorIndexBackend` candidate.

    ``queries`` is ``[(query_vector, expected_record_id), ...]`` — the
    standard self-retrieval recall protocol: each query vector should
    retrieve its associated record within the top ``top_k`` results (e.g. a
    record's own embedding, or a paraphrase's embedding, paired with that
    record's id). A hand-labelled eval set is the fuller version of this —
    see sibling item 116c75c9's retrieval evaluation suite — but this
    protocol is a real, non-trivial recall signal on its own.

    Never raises: a backend that fails to open/build/query is reported with
    ``available=False`` and ``error`` set, so callers can still compare
    whichever candidates DID work in this environment.
    """
    name = getattr(backend, "name", backend.__class__.__name__)
    try:
        backend.open()
    except VectorBackendUnavailable as exc:
        return BenchmarkResult(backend=name, available=False, error=str(exc))
    try:
        rss_before = _rss_mb()
        t0 = time.perf_counter()
        backend.upsert(records)
        build_ms = (time.perf_counter() - t0) * 1000.0
        rss_after = _rss_mb()
        rss_delta = (
            (rss_after - rss_before)
            if rss_before is not None and rss_after is not None
            else None
        )

        hits = 0
        latencies: list[float] = []
        for qvec, expected_id in queries:
            t1 = time.perf_counter()
            matches = backend.query(qvec, top_k=top_k)
            latencies.append((time.perf_counter() - t1) * 1000.0)
            if any(m.id == expected_id for m in matches):
                hits += 1
        recall = (hits / len(queries)) if queries else 0.0
        meta = backend.describe()
        return BenchmarkResult(
            backend=name,
            available=True,
            record_count=meta.record_count,
            query_count=len(queries),
            recall_at_k=recall,
            latency_ms_p50=_percentile(latencies, 0.5) if latencies else None,
            latency_ms_p95=_percentile(latencies, 0.95) if latencies else None,
            build_ms=build_ms,
            rss_mb_delta=rss_delta,
        )
    except Exception as exc:  # noqa: BLE001 - a benchmark must never crash its caller
        _log.warning("run_benchmark: %s backend failed", name, exc_info=True)
        return BenchmarkResult(backend=name, available=False, error=str(exc))
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass


def run_lexical_benchmark(
    backend: LexicalBM25Backend,
    records: Sequence[VectorRecord],
    queries: Sequence[tuple[str, str]],
    *,
    top_k: int = 5,
) -> BenchmarkResult:
    """Benchmark the lexical (BM25) baseline. ``queries`` is
    ``[(query_text, expected_record_id), ...]`` — text, not vectors, mirroring
    :func:`run_benchmark`'s protocol for the vector candidates so recall@k is
    directly comparable across all three."""
    name = getattr(backend, "name", backend.__class__.__name__)
    try:
        backend.open()
    except VectorBackendUnavailable as exc:
        return BenchmarkResult(backend=name, available=False, error=str(exc))
    try:
        rss_before = _rss_mb()
        t0 = time.perf_counter()
        backend.upsert(records)
        build_ms = (time.perf_counter() - t0) * 1000.0
        rss_after = _rss_mb()
        rss_delta = (
            (rss_after - rss_before)
            if rss_before is not None and rss_after is not None
            else None
        )

        hits = 0
        latencies: list[float] = []
        for qtext, expected_id in queries:
            t1 = time.perf_counter()
            matches = backend.query_text(qtext, top_k=top_k)
            latencies.append((time.perf_counter() - t1) * 1000.0)
            if any(m.id == expected_id for m in matches):
                hits += 1
        recall = (hits / len(queries)) if queries else 0.0
        meta = backend.describe()
        return BenchmarkResult(
            backend=name,
            available=True,
            record_count=meta.record_count,
            query_count=len(queries),
            recall_at_k=recall,
            latency_ms_p50=_percentile(latencies, 0.5) if latencies else None,
            latency_ms_p95=_percentile(latencies, 0.95) if latencies else None,
            build_ms=build_ms,
            rss_mb_delta=rss_delta,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("run_lexical_benchmark: %s backend failed", name, exc_info=True)
        return BenchmarkResult(backend=name, available=False, error=str(exc))
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# The evidence gate
# ===========================================================================

#: pgvector must not regress recall versus the local DuckDB VSS candidate by
#: more than this. Small numerical slack — exact float equality is too
#: strict across two different HNSW implementations over the same corpus.
DEFAULT_RECALL_REGRESSION_TOLERANCE = 0.02


def should_enable_pgvector(
    duckdb_result: BenchmarkResult,
    pgvector_result: BenchmarkResult,
    *,
    recall_tolerance: float = DEFAULT_RECALL_REGRESSION_TOLERANCE,
) -> tuple[bool, str]:
    """The evidence gate the item's notes require: never enable pgvector
    "because it exists" — only on measured recall evidence that it is not a
    regression versus the local DuckDB VSS baseline.

    Returns ``(enabled, reason)``. ``enabled`` is only ever ``True`` when
    pgvector is available AND its measured recall is within
    ``recall_tolerance`` of (or better than) DuckDB VSS's. Fails closed —
    pgvector unavailable, no DuckDB baseline to compare against, missing
    recall measurements, or an actual regression all keep the gate shut
    (``False``) — exactly the "required capability with no evidence ==
    non-executable, never a guess" posture AGENTS.md's capability-manifest
    contract already establishes for tool availability.
    """
    if not pgvector_result.available:
        return False, f"pgvector unavailable: {pgvector_result.error or 'no evidence'}"
    if not duckdb_result.available:
        return False, "no local DuckDB VSS baseline to compare against"
    if duckdb_result.recall_at_k is None or pgvector_result.recall_at_k is None:
        return False, "recall was not measured for one or both candidates"
    if pgvector_result.recall_at_k + recall_tolerance < duckdb_result.recall_at_k:
        return False, (
            f"pgvector recall {pgvector_result.recall_at_k:.3f} regresses vs "
            f"duckdb_vss recall {duckdb_result.recall_at_k:.3f} "
            f"(tolerance {recall_tolerance:.3f})"
        )
    return True, (
        f"pgvector recall {pgvector_result.recall_at_k:.3f} >= "
        f"duckdb_vss recall {duckdb_result.recall_at_k:.3f} - tolerance "
        f"({recall_tolerance:.3f}); evidence supports enabling"
    )


def compare_candidates(
    *,
    records: Sequence[VectorRecord],
    vector_queries: Sequence[tuple[Sequence[float], str]],
    lexical_queries: Sequence[tuple[str, str]] | None = None,
    duckdb_backend: VectorIndexBackend | None = None,
    pgvector_backend: VectorIndexBackend | None = None,
    lexical_backend: LexicalBM25Backend | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Run the full lexical / local-vector / shared-pgvector comparison the
    item's notes require, on caller-supplied real records, and apply the
    evidence gate.

    ``pgvector_backend`` / ``lexical_backend`` are optional — omit either to
    compare only the candidates you have available in this environment (e.g.
    no ``dsn`` on hand yet). Returns a JSON-ready dict suitable for
    ``meridian.db.vector_index_state.record_vector_backend_benchmark``'s
    ``evidence`` argument:
    ``{"results": {backend_name: BenchmarkResult-as-dict, ...},
    "decision": {"pgvector_enabled": bool, "reason": str}}``.
    """
    results: dict[str, Any] = {}
    duckdb_backend = duckdb_backend or DuckDBVSSBackend()
    duckdb_result = run_benchmark(duckdb_backend, records, vector_queries, top_k=top_k)
    results["duckdb_vss"] = duckdb_result.to_dict()

    if lexical_backend is not None and lexical_queries:
        lexical_result = run_lexical_benchmark(
            lexical_backend, records, lexical_queries, top_k=top_k
        )
        results["bm25_lexical"] = lexical_result.to_dict()

    decision: dict[str, Any] = {
        "pgvector_enabled": False,
        "reason": "pgvector candidate not supplied",
    }
    if pgvector_backend is not None:
        pgvector_result = run_benchmark(pgvector_backend, records, vector_queries, top_k=top_k)
        results["pgvector"] = pgvector_result.to_dict()
        enabled, reason = should_enable_pgvector(duckdb_result, pgvector_result)
        decision = {"pgvector_enabled": enabled, "reason": reason}

    return {"results": results, "decision": decision}
