"""Targeted coverage of ``OutputsFtsIndex.get_convergence_state()``
(58e64c86) under partial/inconclusive/degraded conditions, added as part of
ba0af0a4 (DOCS-R2-B) to back the new
``meridian.fallbacks.check_docx_promotion_evidence`` gate's ``convergence``
input with real, dedicated coverage of the state it consumes -- existing
coverage of this class is scattered across test_outputs_indexer.py /
test_degraded_labeling.py / test_provenance_status.py, none of which
exercises ``get_convergence_state()`` as its own subject end-to-end across
every one of its derived flags.

``get_convergence_state()`` is a pure, lock-guarded READ of five instance
attributes (``index_revision``, ``last_rebuilt_at``, ``last_rebuild_partial``,
``last_pending_count``, ``last_db_write_error``, ``last_walk_errors``) into
one explicit dict -- most tests here set those attributes directly
(white-box) to exercise every derived-flag combination precisely and
quickly, without needing to coerce a real filesystem/DuckDB failure for
each one. Two tests additionally exercise the REAL end-to-end path (a
genuine ``rebuild()`` over real files, and a genuine walk error via a
monkeypatched ``os.walk``) so the derived-state tests are anchored to
actual index behavior, not just to the dict-shape contract in isolation.
"""
from __future__ import annotations

import os

import pytest

from meridian import fallbacks
from meridian.outputs_indexer import OutputsFtsIndex


def _fresh_index(tmp_path) -> OutputsFtsIndex:
    outputs_dir = str(tmp_path / "outputs")
    os.makedirs(outputs_dir, exist_ok=True)
    return OutputsFtsIndex(outputs_dir, db_path=":memory:")


# ---------------------------------------------------------------------------
# Never rebuilt / freshly constructed
# ---------------------------------------------------------------------------


def test_never_rebuilt_before_any_rebuild_call(tmp_path):
    idx = _fresh_index(tmp_path)
    state = idx.get_convergence_state()
    assert state["never_rebuilt"] is True
    assert state["last_rebuilt_at"] is None
    assert state["index_revision"] == 0
    assert state["inconclusive"] is False
    assert state["partial_index"] is False
    # Never having rebuilt is distinct from "degraded" -- degraded is about
    # a rebuild that ran and came back untrustworthy, not "hasn't run yet".
    assert state["degraded"] is False


# ---------------------------------------------------------------------------
# Real end-to-end happy path
# ---------------------------------------------------------------------------


def test_real_rebuild_over_clean_tree_converges(tmp_path):
    idx = _fresh_index(tmp_path)
    with open(os.path.join(idx.outputs_dir, "result.csv"), "w", encoding="utf-8") as fh:
        fh.write("a,b\n1,2\n")
    count = idx.rebuild()
    assert count == 1
    state = idx.get_convergence_state()
    assert state["never_rebuilt"] is False
    assert state["last_rebuilt_at"] is not None
    assert state["index_revision"] == 1
    assert state["inconclusive"] is False
    assert state["partial_index"] is False
    assert state["degraded"] is False
    assert state["walk_errors"] == []
    assert state["db_write_error"] is None
    assert state["total_indexed"] == 1
    assert state["outputs_dir"] == idx.outputs_dir

    # A second, no-op rebuild over an unchanged tree must NOT bump
    # index_revision again (58e64c86's "confirmed same content" contract).
    idx.rebuild()
    state2 = idx.get_convergence_state()
    assert state2["index_revision"] == 1
    assert state2["degraded"] is False


# ---------------------------------------------------------------------------
# inconclusive -- a real walk error, exercised through the actual rebuild()
# code path (monkeypatched os.walk), not just attribute injection.
# ---------------------------------------------------------------------------


def test_real_walk_error_during_rebuild_makes_convergence_inconclusive(tmp_path, monkeypatch):
    import meridian.outputs_indexer as indexer_mod

    idx = _fresh_index(tmp_path)
    with open(os.path.join(idx.outputs_dir, "result.csv"), "w", encoding="utf-8") as fh:
        fh.write("a,b\n1,2\n")

    real_walk = os.walk

    def _walk_with_injected_error(top, onerror=None, **kwargs):
        if onerror is not None:
            onerror(OSError(13, "Permission denied", os.path.join(top, "locked_subdir")))
        yield from real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(indexer_mod.os, "walk", _walk_with_injected_error)

    idx.rebuild()
    state = idx.get_convergence_state()
    assert state["walk_errors"] != []
    assert state["inconclusive"] is True
    # inconclusive is one of the two OR-branches feeding `degraded` (see
    # the method's own docstring): a caller must not treat this rebuild's
    # results as fully authoritative.
    assert state["degraded"] is True
    # A resolved-error re-run (no injected failure) clears it -- confirms
    # last_walk_errors is reset PER CALL, not accumulated forever.
    monkeypatch.setattr(indexer_mod.os, "walk", real_walk)
    idx.rebuild()
    state3 = idx.get_convergence_state()
    assert state3["walk_errors"] == []
    assert state3["inconclusive"] is False


# ---------------------------------------------------------------------------
# White-box: every derived-flag combination via direct attribute injection.
# ---------------------------------------------------------------------------


def test_partial_index_alone_is_degraded_but_not_inconclusive(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.last_rebuilt_at = 123.0
    idx.index_revision = 1
    idx.last_rebuild_partial = True
    state = idx.get_convergence_state()
    assert state["partial_index"] is True
    assert state["inconclusive"] is False
    assert state["degraded"] is True


def test_db_write_error_alone_makes_convergence_inconclusive_and_degraded(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.last_rebuilt_at = 123.0
    idx.last_db_write_error = "duckdb.IOException: disk full"
    state = idx.get_convergence_state()
    assert state["db_write_error"] == "duckdb.IOException: disk full"
    assert state["inconclusive"] is True
    assert state["degraded"] is True


def test_pending_count_alone_is_degraded_but_not_inconclusive(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.last_rebuilt_at = 123.0
    idx.last_pending_count = 5
    state = idx.get_convergence_state()
    assert state["pending_count"] == 5
    assert state["inconclusive"] is False
    assert state["partial_index"] is False
    assert state["degraded"] is True


def test_walk_errors_deduplicated_and_sorted(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.last_rebuilt_at = 123.0
    idx.last_walk_errors = ["zzz error", "aaa error", "zzz error"]
    state = idx.get_convergence_state()
    assert state["walk_errors"] == ["aaa error", "zzz error"]
    assert state["inconclusive"] is True


def test_get_convergence_state_never_raises_and_is_side_effect_free(tmp_path):
    idx = _fresh_index(tmp_path)
    before = dict(vars(idx))
    for _ in range(3):
        idx.get_convergence_state()
    after = dict(vars(idx))
    # Reading convergence state repeatedly must never itself mutate any of
    # the underlying signals it reports on.
    for key in ("index_revision", "last_rebuilt_at", "last_rebuild_partial",
                "last_pending_count", "last_db_write_error", "last_walk_errors"):
        assert before[key] == after[key]


# ---------------------------------------------------------------------------
# Feeding into the new gate: OutputsFtsIndex.get_convergence_state()'s shape
# is exactly what meridian.fallbacks.check_docx_promotion_evidence's
# ``convergence`` parameter expects (ba0af0a4) -- a real cross-module
# integration check, not just a shape assumption.
# ---------------------------------------------------------------------------


def test_convergence_state_feeds_directly_into_promotion_evidence_gate(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.last_rebuilt_at = 123.0
    idx.last_db_write_error = "boom"

    convergence = idx.get_convergence_state()
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/some.docx", "stage-1", "canon-1", "canon-1",
        convergence=convergence,
    )
    assert result["verdict"] == fallbacks.PROMOTION_DEGRADED
    assert result["convergence"]["db_write_error"] == "boom"


def test_clean_convergence_state_feeds_into_verified_gate_result(tmp_path):
    idx = _fresh_index(tmp_path)
    with open(os.path.join(idx.outputs_dir, "result.csv"), "w", encoding="utf-8") as fh:
        fh.write("a,b\n1,2\n")
    idx.rebuild()

    convergence = idx.get_convergence_state()
    result = fallbacks.check_docx_promotion_evidence(
        "/tmp/some.docx", "stage-1", "canon-1", "canon-1",
        convergence=convergence,
    )
    assert result["verdict"] == fallbacks.PROMOTION_VERIFIED
