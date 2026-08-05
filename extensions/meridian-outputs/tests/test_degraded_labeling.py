"""Explicit `degraded` labeling for a partial/not-yet-converged outputs
index -- item e631d54f (follow-up to 6af1518d's ConvergenceState).

Prior state: `search_outputs()` already tracked `convergence`
(`OutputsFtsIndex.get_convergence_state()`) on every response, and already
surfaced a loud `zero_hits_warning` when `hits` was EMPTY and the index
hadn't converged. There was no equivalent signal for a NON-empty `hits`
response from a not-yet-converged index -- a caller doing an
existence/dedup/provenance check against real, already-indexed hits had no
way to tell "these are ALL the matches" from "these are matches found so
far; more indexing is still pending" without separately reading
`convergence["converged"]` itself. `search_outputs()`'s top-level
`degraded` field (and `get_indexed_output_status()`'s `degraded` field)
close that gap: a caller MUST treat `degraded=True` output as candidates
only, never as an authoritative "nothing else matches" / "confirmed
absent" answer.

Fully local, no hosted call, no network -- consistent with the rest of the
meridian_outputs package.
"""
from __future__ import annotations

from pathlib import Path

from meridian_outputs import outputs_local as OL
from meridian_outputs import search as OS


def test_search_outputs_not_degraded_once_fully_converged(tmp_path: Path) -> None:
    (tmp_path / "keep.csv").write_text("keyword_alpha\n", encoding="utf-8")
    result = OL.search_outputs(str(tmp_path), "keyword_alpha")
    assert result["hits"]
    assert result["convergence"]["converged"] is True
    assert result["degraded"] is False


def test_search_outputs_degraded_true_with_nonempty_hits_when_partial(
    tmp_path: Path, monkeypatch,
) -> None:
    """A NON-empty hits list from a not-yet-converged index must still be
    flagged degraded -- the exact gap `zero_hits_warning` alone doesn't
    cover, since that field only ever fires on a ZERO-hit response.

    Deterministically simulates "the ambient walk still has a pending
    backlog entry" by injecting directly into `_pending_stale` (the same
    state `get_convergence_state()` itself reads) rather than racing a
    tiny time budget against a slow hasher -- `rebuild()` is frozen to a
    no-op so the injected state survives the `search_outputs()` call under
    test instead of being (correctly) cleared by a real rebuild pass.
    """
    (tmp_path / "keep.csv").write_text("keyword_alpha\n", encoding="utf-8")
    outputs_dir = str(tmp_path)

    # First pass: fully converges with one real, findable row.
    first = OL.search_outputs(outputs_dir, "keyword_alpha")
    assert first["hits"]
    assert first["degraded"] is False

    idx = OL._get_cached_index(outputs_dir)
    monkeypatch.setattr(idx, "rebuild", lambda max_seconds=None: len(idx._row_cache))
    idx._pending_stale["/still/pending/fake.csv"] = (None, None)
    idx.last_rebuild_partial = True

    second = OL.search_outputs(outputs_dir, "keyword_alpha")
    # The already-persisted row is still searchable...
    assert second["hits"], "previously-indexed content must remain searchable"
    # ...but the index has NOT fully converged (a pending entry remains), so
    # this must be labeled degraded even with real hits present.
    assert second["convergence"]["converged"] is False
    assert second["degraded"] is True


def test_get_indexed_output_status_confirmed_present_when_converged(
    tmp_path: Path,
) -> None:
    target = tmp_path / "found.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    outputs_dir = str(tmp_path)
    # rebuild via search_outputs first so the index has actually converged.
    OL.search_outputs(outputs_dir, "a")

    status = OL.get_indexed_output_status(outputs_dir, str(target))
    assert status["row"] is not None
    assert status["degraded"] is False
    assert status["convergence"]["converged"] is True


def test_get_indexed_output_status_degraded_true_before_walk_reaches_path(
    tmp_path: Path,
) -> None:
    """A path the ambient walk hasn't gotten to yet must come back
    `degraded=True` -- a `row=None` here is NOT proof of absence.

    Deterministically simulates "the walk still has a pending backlog
    entry" via direct state injection (see the analogous search_outputs
    test above) rather than a timing race.
    """
    (tmp_path / "slow.csv").write_text("x\n", encoding="utf-8")
    outputs_dir = str(tmp_path)

    idx = OL._get_cached_index(outputs_dir)
    idx._pending_stale["/still/pending/other.csv"] = (None, None)
    assert idx.get_convergence_state().converged is False

    status = OL.get_indexed_output_status(outputs_dir, str(tmp_path / "slow.csv"))
    assert status["degraded"] is True, (
        "an index that hasn't converged must never report a confident "
        "membership answer, even when row happens to be None"
    )
    assert status["row"] is None


def test_get_indexed_output_status_missing_args_are_degraded_not_absent() -> None:
    status = OL.get_indexed_output_status("", "whatever.csv")
    assert status["row"] is None
    assert status["degraded"] is True

    status2 = OL.get_indexed_output_status("/some/dir/that/does/not/exist", "")
    assert status2["row"] is None
    assert status2["degraded"] is True


def test_get_indexed_output_status_shape_matches_get_indexed_output(
    tmp_path: Path,
) -> None:
    """The `row` field is identical to what get_indexed_output already
    returns -- this is purely additive, never a behavior change for the
    existing function."""
    target = tmp_path / "same.csv"
    target.write_text("z\n", encoding="utf-8")
    outputs_dir = str(tmp_path)
    OL.search_outputs(outputs_dir, "z")

    plain = OL.get_indexed_output(outputs_dir, str(target))
    status = OL.get_indexed_output_status(outputs_dir, str(target))
    assert status["row"] == plain


def test_search_module_literal_boost_passes_degraded_through_unchanged(
    tmp_path: Path,
) -> None:
    """meridian_outputs.search's literal-match re-ranking wrapper must pass
    the `degraded` field through unchanged -- reordering hits never changes
    whether the underlying index has converged."""
    (tmp_path / "keep.csv").write_text("keyword_alpha\n", encoding="utf-8")
    outputs_dir = str(tmp_path)
    direct = OL.search_outputs(outputs_dir, "keyword_alpha")
    wrapped = OS.search_outputs(outputs_dir, "keyword_alpha")
    assert "degraded" in wrapped
    assert wrapped["degraded"] == direct["degraded"] == False  # noqa: E712
