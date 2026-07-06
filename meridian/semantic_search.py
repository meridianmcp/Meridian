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

# 56cd8712 — trip threshold. Lowered from 420 to leave ~130MB headroom on the
# 512MB Fly box so a load can't complete right at the OOM edge.
_DEFAULT_RSS_LIMIT_MB = 380.0
_DEFAULT_COSINE_FLOOR = 0.37
_DEFAULT_IDLE_UNLOAD_S = 600.0
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
        import numpy as np

        if not texts:
            return np.empty((0, 0), dtype="float32")
        model = self._ensure_model()
        if model is None:
            return None
        if not self._rss_ok():  # breaker before the embed spend
            return None
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
        import numpy as np

        if floor is None:
            floor = cosine_floor()
        if not candidates:
            return []
        texts = [t for _, t in candidates]
        q_emb = self.embed([query])
        if q_emb is None or q_emb.shape[0] == 0:
            return []
        c_emb = self.embed(texts)
        if c_emb is None or c_emb.shape[0] == 0:
            return []
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


def maybe_idle_unload() -> None:
    """56cd8712 — release the model if idle past the unload window.

    Safe to call from a background loop (e.g. the server keepalive tick): a no-op
    when no model is loaded, so on a quiet box the ~90MB is actually released
    instead of staying pinned until the next escalation (the lazy-on-next-use
    unload alone would never fire if no further semantic search happens)."""
    _SEARCHER._maybe_idle_unload()


def embed(texts: "list[str]"):
    return _SEARCHER.embed(texts)
