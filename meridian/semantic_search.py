"""Model2Vec semantic-search escalation — SAFE-BY-DEFAULT, OPT-IN (56cd8712).

This module is a *lazy, guarded, circuit-broken* semantic searcher layered on top
of the existing keyword / tsvector search in :mod:`meridian.db`. It is designed to
be a **NO-OP on production** until explicitly enabled, because prod runs on a 512MB
Fly box and the static embedding model costs ~90MB WARM RSS.

Design guarantees (see sprint item 56cd8712 + eval finding c1008ef9):

* **OFF by default.** ``is_available()`` returns ``False`` unless ALL of:
  ``MERIDIAN_SEMANTIC_ENABLED`` is truthy, ``model2vec`` is importable, and the
  runtime RSS circuit breaker is not tripped. With the env unset this whole feature
  is inert — callers fall back to keyword-only.
* **Lazy import.** ``model2vec`` is imported only on first *actual use* (first
  escalation / first ``embed``), never at module import or process startup. Merely
  importing this module must not import model2vec or load the model.
* **Lazy load + idle unload.** ``StaticModel.from_pretrained(...)`` runs on first
  use, is cached, and is auto-unloaded after an idle window
  (``MERIDIAN_SEMANTIC_IDLE_UNLOAD_S``, default 600s) to release the ~90MB.
* **Runtime RSS circuit breaker.** Before/after load and before each embed we read
  the process's *current* RSS. If it exceeds ``MERIDIAN_SEMANTIC_RSS_LIMIT_MB``
  (default 420) the breaker TRIPS: the model is unloaded and semantic search returns
  keyword-only until a cooldown elapses. This is a real runtime guard, not a static
  flag.
* **Cosine floor.** ``rank`` drops any candidate whose cosine similarity to the
  query is below ``MERIDIAN_SEMANTIC_COSINE_FLOOR`` (default 0.37) to suppress the
  adversarial false positives the eval measured.

Nothing here raises on import failure — a missing ``model2vec`` simply disables the
searcher. Callers must always treat semantic search as best-effort.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    import numpy as np

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config knobs (all env-tunable; read fresh so tests can monkeypatch os.environ).
# ---------------------------------------------------------------------------

_MODEL_NAME = "minishlab/potion-base-8M"

_ENV_ENABLED = "MERIDIAN_SEMANTIC_ENABLED"
_ENV_RSS_LIMIT_MB = "MERIDIAN_SEMANTIC_RSS_LIMIT_MB"
_ENV_COSINE_FLOOR = "MERIDIAN_SEMANTIC_COSINE_FLOOR"
_ENV_IDLE_UNLOAD_S = "MERIDIAN_SEMANTIC_IDLE_UNLOAD_S"
_ENV_MIN_MARGIN = "MERIDIAN_SEMANTIC_MIN_MARGIN"

# 56cd8712 — trip threshold. Lowered from 420 to leave ~130MB headroom on the
# 512MB Fly box so a load can't complete right at the OOM edge.
_DEFAULT_RSS_LIMIT_MB = 380.0
_DEFAULT_COSINE_FLOOR = 0.37
_DEFAULT_IDLE_UNLOAD_S = 600.0
# 3d3ccf2d — minimum required lead a top candidate must hold over the next-best
# DIFFERENT candidate in the SAME ranked set before it is trusted enough to
# auto-bind. Two near-tied candidates (e.g. a stale record and its fresh
# replacement, both plausible) fail this even when both clear the confidence
# floor — see score_confidence()'s "ambiguous_runner_up" verdict.
_DEFAULT_MIN_MARGIN = 0.05
# How long the breaker stays tripped before we allow a retry.
_BREAKER_COOLDOWN_S = 300.0
# Conservative estimate of the RSS a model LOAD adds (~90MB model + numpy/encode
# overhead + margin). Used for a PRE-load headroom check so we never START a load
# whose projected post-load RSS would breach the limit (the OOM-during-load
# window: RSS is only sampled before/after from_pretrained, never during it).
_MODEL_EST_MB = 120.0
# Per-candidate text is truncated to this before embedding so a few huge notes
# can't spike RSS during encode; the corpus row count is capped separately.
_MAX_TEXT_CHARS = 2000
# Encode in sub-batches with an RSS check between them, bounding the encode-time
# peak (numpy allocates the full batch's matrices at once).
_ENCODE_BATCH = 32

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _rss_limit_mb() -> float:
    return _env_float(_ENV_RSS_LIMIT_MB, _DEFAULT_RSS_LIMIT_MB)


def cosine_floor() -> float:
    """Effective cosine floor (env-tunable, default 0.37)."""
    return _env_float(_ENV_COSINE_FLOOR, _DEFAULT_COSINE_FLOOR)


def _idle_unload_s() -> float:
    return _env_float(_ENV_IDLE_UNLOAD_S, _DEFAULT_IDLE_UNLOAD_S)


def min_margin_floor() -> float:
    """Effective minimum runner-up margin (env-tunable, default 0.05).

    3d3ccf2d — the second half of the deterministic-abstention gate in
    :func:`score_confidence`: a candidate must lead the next-best DIFFERENT
    candidate in its ranked set by at least this much (on top of clearing the
    absolute :func:`cosine_floor`) before it is trusted for automatic binding.
    """
    return _env_float(_ENV_MIN_MARGIN, _DEFAULT_MIN_MARGIN)


# 3d3ccf2d — fusion weights mirroring db.hybrid_candidate_retrieval's own
# split (2204ce80: ``fused = 0.6 * lexical_norm + 0.4 * semantic``) so a
# candidate carrying both signals is weighted identically regardless of which
# call site computed it. Kept as a local, independent constant (not imported
# from meridian.db) because meridian.db already imports this module — a
# reverse import would be circular.
_FUSE_LEXICAL_WEIGHT = 0.6
_FUSE_SEMANTIC_WEIGHT = 0.4


# ---------------------------------------------------------------------------
# Escalation gate (pure, DB-free — unit-testable in isolation) — 56cd8712.
# ---------------------------------------------------------------------------

# Trigram top-score below this means keyword genuinely found nothing good.
_TRIGRAM_NO_GOOD_HIT = 0.1


def should_escalate(pure_fts_count: int, trigram_top: float) -> bool:
    """Decide whether to escalate to semantic search — the CORRECTED gate.

    The item's *written* gate ("escalate if tsvector returns <3 results") never
    fires in practice, because the real keyword search is a permissive 3-way OR
    (pg_trgm similarity OR FTS OR ILIKE-any-word) that almost always returns >=3
    rows. So a "<N results" count is not a signal that keyword search failed.

    The correct signal is that keyword search genuinely found *nothing good*:

    * ``pure_fts_count`` — the count from PURE ``websearch_to_tsquery`` FTS over the
      project's searchable text — is LITERALLY ZERO, AND
    * ``trigram_top`` — the best pg_trgm ``similarity`` over the corpus — is below
      ~0.1 (no fuzzy keyword hit either).

    Only then is semantic search worth the RSS. Returns ``False`` for any
    permissive-OR result (e.g. 3-5 rows), which is exactly the old broken trigger.
    """
    if pure_fts_count is None:
        pure_fts_count = 0
    if trigram_top is None:
        trigram_top = 0.0
    return pure_fts_count == 0 and trigram_top < _TRIGRAM_NO_GOOD_HIT


# ---------------------------------------------------------------------------
# Embedding freshness / degraded-candidate labeling (e631d54f, follow-up to
# 56cd8712). This searcher never persists an index — every escalation embeds
# a freshly-fetched corpus live, so there's no "stale on-disk vector index"
# risk the way meridian_codeindex.CodeIndex or a hypothetical cached
# corpus would have. The one real freshness gap here is different: the
# corpus a caller fetches for ranking is DELIBERATELY bounded (a cap on how
# many rows get embedded per escalation, e.g. db._SEMANTIC_CORPUS_CAP), so a
# "no result above the cosine floor" answer is only ever a statement about
# the rows INSIDE that window — never proof that no better match exists
# project-wide. These two pure, DB-free helpers make that distinction
# checkable by callers (mirroring should_escalate's pure-function style)
# instead of leaving it as prose only.
# ---------------------------------------------------------------------------


def model_name() -> str:
    """The embedding model identifier this module's searches would use.

    A stable "embedding model/version" marker a caller can stamp onto
    semantically-ranked rows (see ``meridian.db._maybe_semantic_escalate``)
    so a later reader can tell which model produced a given match — useful
    once a model upgrade is possible, since this module has no persisted
    index to version-check against (embeddings are always computed live).
    """
    return _MODEL_NAME


def is_corpus_capped(corpus_size: int, cap: int) -> bool:
    """True when ``corpus_size`` hit (or exceeded) the per-escalation cap.

    A capped corpus means semantic ranking only ever considered a bounded
    WINDOW of the project's candidates — real matches, but never a
    project-wide exhaustive answer. Callers must label any row surfaced
    from a capped ranking pass as degraded (candidates only) and must never
    treat "nothing above the cosine floor in this window" as proof nothing
    better exists elsewhere — i.e. a capped semantic escalation must never
    by itself satisfy an authoritative pointer/provenance gate.
    """
    if cap <= 0:
        return corpus_size > 0
    return corpus_size >= cap


# ---------------------------------------------------------------------------
# RSS reader — portable, best-effort. Returns None if it can't measure.
# ---------------------------------------------------------------------------


def rss_mb() -> float | None:
    """Current process resident-set-size in MB, or ``None`` if unmeasurable.

    Tries ``psutil`` first, then Linux ``/proc/self/statm``. If neither works the
    circuit breaker is effectively disabled (returns ``None`` → breaker can't trip),
    which is the intended graceful degradation on platforms we can't measure.
    """
    try:
        import psutil  # type: ignore

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001 - psutil missing or platform quirk; fall through
        pass
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            fields = fh.read().split()
        # statm[1] = resident pages.
        resident_pages = int(fields[1])
        page_size = os.sysconf("SC_PAGE_SIZE")  # bytes per page
        return resident_pages * page_size / (1024 * 1024)
    except Exception:  # noqa: BLE001 - not Linux / no /proc; give up gracefully
        return None


# ---------------------------------------------------------------------------
# Typed, confidence-scored match result with deterministic abstention
# (3d3ccf2d — follow-up on 2204ce80's hybrid_candidate_retrieval). Pure data +
# a pure, DB-free scoring function — no embedding, no I/O, fully unit-testable
# without numpy or a real model.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticMatch:
    """One candidate's confidence-scored verdict.

    ``lexical_score`` is ``None`` when the caller has no corroborating
    lexical/keyword signal for this id (the common case for the pure
    keyword-miss escalation path in ``db._maybe_semantic_escalate``, where
    lexical search already found nothing at all). ``semantic_score`` is the
    raw cosine similarity. ``fused_score`` blends the two
    (:data:`_FUSE_LEXICAL_WEIGHT` / :data:`_FUSE_SEMANTIC_WEIGHT`, mirroring
    ``db.hybrid_candidate_retrieval``'s own split) when a lexical score
    exists, else equals ``semantic_score``. ``threshold`` is the absolute
    confidence floor applied. ``margin`` is this candidate's ``fused_score``
    gap to its NEAREST neighbor (whichever adjacent entry, above or below, is
    closest) in the same ranked set — or its own ``fused_score`` when it has
    no neighbor at all. ``confident`` is ``True`` only when
    BOTH the absolute floor and the margin requirement are met — see
    :func:`score_confidence`. ``reason`` is one of ``"confident_match"``,
    ``"below_confidence_threshold"``, or ``"ambiguous_runner_up"``.

    NEVER carries or implies project/version/status/pointer identity — those
    are hard exact-match gates the caller applies before a candidate ever
    reaches scoring (see ``db._semantic_candidate_corpus``'s project-scoped
    query, ``db.hybrid_candidate_retrieval``'s exact-filter-first pipeline).
    """

    id: str
    lexical_score: "float | None"
    semantic_score: float
    fused_score: float
    threshold: float
    margin: float
    confident: bool
    reason: str


def score_confidence(
    ranked: "list[tuple[str, float]]",
    *,
    floor: float | None = None,
    min_margin: float | None = None,
    lexical_scores: "dict[str, float] | None" = None,
) -> "list[SemanticMatch]":
    """Attach a deterministic confidence + abstention verdict to ``ranked``.

    ``ranked`` is a ``[(id, semantic_score)]`` list — typically
    :meth:`SemanticSearcher.rank`'s own output (real or, in tests, mocked),
    but this function makes NO assumption that it is already floor-filtered
    or sorted; every guarantee below is re-derived here, defensively, so a
    change to the caller's pre-filtering can never silently weaken this gate.

    Two INDEPENDENT requirements must both hold for a candidate to be
    ``confident=True`` (eligible for automatic binding — e.g. being merged
    into search results as an authoritative hit):

    1. **Absolute confidence.** ``semantic_score >= floor`` (default
       :func:`cosine_floor`). Gated on the RAW semantic score, never the
       fused one — a strong lexical corroboration can raise a candidate's
       rank and reported ``fused_score``, but it can never manufacture
       confidence for a candidate the embedding model itself found
       semantically irrelevant. This is what keeps a "lexical false
       positive" (shares vocabulary with the query, e.g. matching rare
       tokens, but is a different concept) from being waved through on
       lexical strength alone.
    2. **Runner-up margin.** This candidate's ``fused_score`` must clear the
       score of its NEAREST neighbor in the ranked set — whichever adjacent
       entry (immediately above OR below it) is closest — by at least
       ``min_margin`` (default :func:`min_margin_floor`). Checking BOTH
       directions matters: two near-tied candidates — e.g. a stale record and
       the fresh one that superseded it, both plausible paraphrases of the
       query — must BOTH abstain, not just the lower-ranked one relative to
       the higher (a naive "lead over the next-lowest" check would let the
       lower-ranked one see no competitor below it and falsely call itself
       confident). Both are returned as ``confident=False`` /
       ``reason="ambiguous_runner_up"`` rather than one being picked
       arbitrarily. This is the deterministic-abstention contract: ambiguous
       candidates are surfaced, never silently resolved. A candidate with no
       neighbor at all (the sole entry) has nothing to be ambiguous against,
       so its margin is its own ``fused_score``.

    A candidate that fails requirement 1 is reported
    ``reason="below_confidence_threshold"``; requirement 2,
    ``reason="ambiguous_runner_up"``; both satisfied,
    ``reason="confident_match"``.

    ``lexical_scores`` (optional ``{id: score}``) fuses a per-candidate
    lexical/keyword signal into ``fused_score`` — a candidate with no entry
    fuses to its raw ``semantic_score`` (no signal, no fusion), matching
    ``db.hybrid_candidate_retrieval``'s own "lexical-only when no semantic
    score" rule in reverse. Results are re-sorted by ``fused_score``
    descending (stable for ties) before margins are computed, so a lexical
    signal can reorder the set exactly as it would in the hybrid path.

    Pure and DB-free: no embedding, no I/O, no project/version/pointer
    awareness — those exact-match gates are the CALLER's responsibility
    (already applied before any row reaches ``ranked``) and are entirely
    unaffected by anything in this function.
    """
    if not ranked:
        return []
    if floor is None:
        floor = cosine_floor()
    if min_margin is None:
        min_margin = min_margin_floor()
    lexical_scores = lexical_scores or {}

    fused_rows: "list[tuple[str, float | None, float, float]]" = []
    for cid, sem_score in ranked:
        sem = float(sem_score)
        lex = lexical_scores.get(cid)
        if lex is not None:
            lex = float(lex)
            fused = _FUSE_LEXICAL_WEIGHT * lex + _FUSE_SEMANTIC_WEIGHT * sem
        else:
            fused = sem
        fused_rows.append((cid, lex, sem, fused))
    fused_rows.sort(key=lambda row: row[3], reverse=True)

    n = len(fused_rows)
    matches: "list[SemanticMatch]" = []
    for i, (cid, lex, sem, fused) in enumerate(fused_rows):
        # Gap to the NEAREST neighbor in either direction — sorted descending,
        # so the entry above (i-1) is >= fused and the entry below (i+1) is
        # <= fused; both gaps are therefore always >= 0.
        gaps = []
        if i > 0:
            gaps.append(fused_rows[i - 1][3] - fused)
        if i + 1 < n:
            gaps.append(fused - fused_rows[i + 1][3])
        margin = min(gaps) if gaps else fused
        if sem < floor:
            confident, reason = False, "below_confidence_threshold"
        elif margin < min_margin:
            confident, reason = False, "ambiguous_runner_up"
        else:
            confident, reason = True, "confident_match"
        matches.append(SemanticMatch(
            id=cid,
            lexical_score=(round(lex, 4) if lex is not None else None),
            semantic_score=round(sem, 4),
            fused_score=round(fused, 4),
            threshold=floor,
            margin=round(margin, 4),
            confident=confident,
            reason=reason,
        ))
    return matches


# ---------------------------------------------------------------------------
# The searcher — a singleton with cached lazy model + circuit breaker state.
# ---------------------------------------------------------------------------


class SemanticSearcher:
    """Lazy, guarded, circuit-broken Model2Vec searcher.

    A single module-level instance (:data:`_SEARCHER`) is used in production; tests
    construct their own or monkeypatch the singleton's readers. Not thread-safe by
    design — the server runs a single asyncio loop and embedding is synchronous.
    """

    def __init__(self) -> None:
        self._model = None  # loaded StaticModel, or None
        self._model2vec_importable: bool | None = None  # tri-state cache
        self._last_used: float = 0.0
        self._tripped: bool = False
        self._tripped_at: float = 0.0

    # -- import / availability ------------------------------------------------

    def _import_ok(self) -> bool:
        """Return whether ``model2vec`` can be imported (cached after first check).

        NEVER raises. Importing this module does not call this; only availability
        checks / first use do, keeping module import free of model2vec.
        """
        if self._model2vec_importable is None:
            try:
                import model2vec  # noqa: F401

                self._model2vec_importable = True
            except Exception:  # noqa: BLE001 - any import error → disabled
                self._model2vec_importable = False
        return self._model2vec_importable

    def is_available(self) -> bool:
        """True only when enabled AND importable AND not tripped.

        Default (env unset) → ``False`` → callers use keyword-only. This gates the
        entire feature: with it False, ``search_all`` never escalates.
        """
        if not _env_truthy(_ENV_ENABLED):
            return False
        if self.is_tripped():
            return False
        return self._import_ok()

    # -- circuit breaker ------------------------------------------------------

    def is_tripped(self) -> bool:
        """Whether the RSS breaker is currently tripped (auto-resets after cooldown)."""
        if self._tripped and (time.monotonic() - self._tripped_at) >= _BREAKER_COOLDOWN_S:
            # Cooldown elapsed — allow a retry (model already unloaded on trip).
            self._tripped = False
        return self._tripped

    def _trip(self, current_rss: float | None) -> None:
        self._tripped = True
        self._tripped_at = time.monotonic()
        self.unload()
        _log.warning(
            "semantic_search: RSS circuit breaker TRIPPED (rss=%.1fMB limit=%.1fMB) "
            "— unloading model, keyword-only until cooldown",
            current_rss if current_rss is not None else -1.0,
            _rss_limit_mb(),
        )

    def _rss_ok(self) -> bool:
        """Read current RSS; trip + return False if over limit. None RSS → OK (disabled)."""
        current = rss_mb()
        if current is None:
            return True  # unmeasurable → breaker disabled, allow
        if current > _rss_limit_mb():
            self._trip(current)
            return False
        return True

    # -- model lifecycle ------------------------------------------------------

    def unload(self) -> None:
        """Drop the cached model to release its ~90MB RSS. Idempotent."""
        self._model = None

    def _maybe_idle_unload(self) -> None:
        if self._model is not None and self._last_used:
            if (time.monotonic() - self._last_used) >= _idle_unload_s():
                _log.info("semantic_search: idle-unloading model after %.0fs", _idle_unload_s())
                self.unload()

    def _ensure_model(self):
        """Load (once) and return the model, or ``None`` if unavailable/tripped.

        Guarded by the RSS breaker before AND after the load: a load that pushes us
        over the limit trips the breaker and returns ``None`` (keyword-only).
        """
        if not self.is_available():
            return None
        self._maybe_idle_unload()
        if self._model is not None:
            return self._model
        # Breaker check before spending memory on the load.
        if not self._rss_ok():
            return None
        # 56cd8712 — PRE-load headroom guard: RSS is only sampled before/after the
        # load, never during it, so on a 512MB box a ~90MB load starting near the
        # limit can OOM mid-load before the post-check runs. Refuse to START a load
        # whose projected post-load RSS (current + _MODEL_EST_MB) would breach the
        # limit — stay keyword-only rather than risk the OOM killer. (Not a trip:
        # nothing is over-limit yet, we're just declining a doomed allocation.)
        _cur = rss_mb()
        if _cur is not None and _cur + _MODEL_EST_MB > _rss_limit_mb():
            _log.info(
                "semantic_search: insufficient headroom to load (rss=%.1fMB + "
                "~%.0fMB > limit %.1fMB) — keyword-only",
                _cur, _MODEL_EST_MB, _rss_limit_mb(),
            )
            return None
        try:
            from model2vec import StaticModel  # lazy — first real use only

            self._model = StaticModel.from_pretrained(_MODEL_NAME)
        except Exception:  # noqa: BLE001 - load failure disables semantic, never raises
            _log.warning("semantic_search: model load failed — keyword-only", exc_info=True)
            self._model = None
            return None
        # Post-load breaker check: the load itself may have pushed RSS over.
        if not self._rss_ok():
            return None
        self._last_used = time.monotonic()
        return self._model

    # -- embedding / ranking --------------------------------------------------

    def embed(self, texts: "list[str]") -> "np.ndarray | None":
        """Embed ``texts`` → ndarray (n, dim), or ``None`` if unavailable/tripped.

        Runs the RSS breaker check before embedding. Never raises.
        """
        # b3537a8d — import numpy ONLY after the empty/availability/breaker
        # early-returns, so calling embed() when semantic is unavailable (no
        # model2vec/numpy installed) or the breaker is tripped returns None WITHOUT
        # crashing on the import — honouring the module's safe-by-default promise.
        if not texts:
            return None
        model = self._ensure_model()
        if model is None:
            return None
        if not self._rss_ok():  # breaker before the embed spend
            return None
        import numpy as np  # reached only when the model loaded → numpy is present
        # 56cd8712 — cap each text + encode in RSS-checked sub-batches so a few large
        # candidates can't spike RSS past the model estimate during one big numpy
        # allocation; a breaker trip mid-corpus aborts to keyword-only.
        capped = [(t or "")[:_MAX_TEXT_CHARS] for t in texts]
        try:
            chunks: "list[np.ndarray]" = []
            for i in range(0, len(capped), _ENCODE_BATCH):
                if i > 0 and not self._rss_ok():  # re-check between sub-batches
                    return None
                batch = capped[i:i + _ENCODE_BATCH]
                chunks.append(np.asarray(model.encode(batch), dtype="float32"))
            vecs = np.vstack(chunks) if chunks else np.empty((0, 0), dtype="float32")
        except Exception:  # noqa: BLE001 - encode failure → keyword-only
            _log.warning("semantic_search: embed failed — keyword-only", exc_info=True)
            return None
        self._last_used = time.monotonic()
        return np.asarray(vecs, dtype="float32")

    def rank(
        self,
        query: str,
        candidates: "list[tuple[str, str]]",
        *,
        floor: float | None = None,
    ) -> "list[tuple[str, float]]":
        """Rank ``candidates`` ([(id, text)]) by cosine to ``query``.

        Returns ``[(id, cosine)]`` sorted descending, dropping anything strictly
        below the cosine ``floor`` (default :func:`cosine_floor`). Returns ``[]`` if
        semantic is unavailable/tripped or embedding fails — callers fall back to
        keyword-only.
        """
        # b3537a8d — empty/unavailable early-returns BEFORE importing numpy, so rank()
        # never crashes without numpy for the degenerate/unavailable cases. numpy is
        # imported only once embed() has returned real vectors (so numpy is present).
        if not candidates:
            return []
        if floor is None:
            floor = cosine_floor()
        texts = [t for _, t in candidates]
        q_emb = self.embed([query])
        if q_emb is None or q_emb.shape[0] == 0:
            return []
        c_emb = self.embed(texts)
        if c_emb is None or c_emb.shape[0] == 0:
            return []
        import numpy as np  # reached only when embed returned real vectors → numpy present
        q = _l2_normalize(q_emb[0])
        c = _l2_normalize_rows(c_emb)
        sims = c @ q  # cosine after normalization
        ranked: list[tuple[str, float]] = []
        for (cid, _text), score in zip(candidates, sims):
            s = float(score)
            if s >= floor:
                ranked.append((cid, s))
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return ranked

    def rank_confident(
        self,
        query: str,
        candidates: "list[tuple[str, str]]",
        *,
        floor: float | None = None,
        min_margin: float | None = None,
        lexical_scores: "dict[str, float] | None" = None,
    ) -> "list[SemanticMatch]":
        """:meth:`rank` + :func:`score_confidence` — the confidence-scored,
        deterministic-abstention counterpart to plain ``rank`` (3d3ccf2d).

        Embeds/ranks exactly like :meth:`rank` (same floor semantics, same
        safety guarantees — never raises, empty/unavailable inputs degrade to
        ``[]``), then layers the confidence + runner-up-margin verdict on top
        via :func:`score_confidence`. Returns EVERY candidate that survived
        :meth:`rank`'s own floor filter, each annotated ``confident``/
        ``reason`` — callers MUST check ``confident`` before treating an
        entry as a decided match; a ``confident=False`` entry is an ambiguous
        candidate to surface, never to auto-bind.
        """
        ranked = self.rank(query, candidates, floor=floor)
        if not ranked:
            return []
        return score_confidence(
            ranked, floor=floor, min_margin=min_margin, lexical_scores=lexical_scores,
        )


def _l2_normalize(vec: "np.ndarray") -> "np.ndarray":
    import numpy as np

    norm = np.linalg.norm(vec)
    if norm == 0:
        return vec
    return vec / norm


def _l2_normalize_rows(mat: "np.ndarray") -> "np.ndarray":
    import numpy as np

    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


# Module-level singleton used by meridian.db.search_all. Constructing it does NOT
# import model2vec or load the model — both happen lazily on first escalation.
_SEARCHER = SemanticSearcher()


def get_searcher() -> SemanticSearcher:
    """Return the process-wide semantic searcher singleton."""
    return _SEARCHER


# Thin module-level convenience wrappers (the public surface callers use).


def is_available() -> bool:
    return _SEARCHER.is_available()


def is_tripped() -> bool:
    return _SEARCHER.is_tripped()


def rank(query: str, candidates: "list[tuple[str, str]]", *, floor: float | None = None):
    return _SEARCHER.rank(query, candidates, floor=floor)


def rank_confident(
    query: str,
    candidates: "list[tuple[str, str]]",
    *,
    floor: float | None = None,
    min_margin: float | None = None,
    lexical_scores: "dict[str, float] | None" = None,
):
    return _SEARCHER.rank_confident(
        query, candidates, floor=floor, min_margin=min_margin, lexical_scores=lexical_scores,
    )


def maybe_idle_unload() -> None:
    """56cd8712 — release the model if idle past the unload window.

    Safe to call from a background loop (e.g. the server keepalive tick): a no-op
    when no model is loaded, so on a quiet box the ~90MB is actually released
    instead of staying pinned until the next escalation (the lazy-on-next-use
    unload alone would never fire if no further semantic search happens)."""
    _SEARCHER._maybe_idle_unload()


def embed(texts: "list[str]"):
    return _SEARCHER.embed(texts)
