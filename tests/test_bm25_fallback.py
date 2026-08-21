"""Coverage for the hardened local BM25 secondary path (sprint item 58e64c86).

Exercises both halves of the "harden local research indexing" contract:

* **Code side** (:mod:`meridian_codeindex.bm25_index`, new) — worktree-aware
  canonical root resolution, the explicit
  index_revision/freshness/partial_index/inconclusive/degraded vocabulary,
  exact path/hash lookup that bypasses BM25 ranking, and a bounded
  refresh-selected-subtree command.
* **Outputs side** (:mod:`meridian.outputs_indexer`, extended) — the same
  vocabulary added to ``OutputsFtsIndex`` (``index_revision`` /
  ``get_convergence_state`` / ``find_by_hash`` / ``refresh_subtree``) plus
  their stateless module-level counterparts.

The unifying acceptance criterion across both: a failed or partial directory
walk must NEVER be silently reported as an authoritative empty result — every
test that simulates a walk failure asserts ``inconclusive``/``degraded`` is
set, not just that ``hits``/``matches`` happens to be empty.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from meridian import outputs_indexer as oi
from meridian_codeindex import bm25_index as bi


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write(path, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _sha256_of(path) -> str:
    """Hash the file's ACTUAL on-disk bytes (not the original text literal) —
    text-mode writes translate ``\\n`` -> ``\\r\\n`` on Windows, so hashing the
    source string directly would not match what the indexer's byte-level
    hasher sees.
    """
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ===========================================================================
# CODE SIDE — meridian_codeindex.bm25_index
# ===========================================================================

def test_bm25_fallback_search_finds_real_symbol_and_reports_fresh_state(tmp_path):
    _write(
        tmp_path / "mod.py",
        "def distinctive_marker_zzqq():\n    return 42\n",
    )
    result = bi.bm25_fallback_search(str(tmp_path), "distinctive_marker_zzqq")
    assert not result.get("error")
    assert result["hits"], "expected a real hit for a symbol that exists"
    assert any("distinctive_marker_zzqq" in (h.get("name") or "") for h in result["hits"])
    assert result["index_revision"] >= 1
    assert result["last_checkpoint_at"] is not None
    assert result["inconclusive"] is False
    assert result["degraded"] is False
    assert result["canonical_root"] == os.path.abspath(str(tmp_path))


def test_bm25_fallback_search_missing_root_is_inconclusive_not_a_bare_miss():
    result = bi.bm25_fallback_search(
        str(pytest.importorskip("pathlib").Path("no/such/root/zzqq")), "anything",
    )
    assert result["hits"] == []
    assert result["inconclusive"] is True
    assert result["degraded"] is True
    assert result["error"]


def test_bm25_fallback_search_blank_query_is_inconclusive(tmp_path):
    result = bi.bm25_fallback_search(str(tmp_path), "   ")
    assert result["inconclusive"] is True
    assert result["degraded"] is True


def test_safe_walk_errors_captures_onerror_callback(tmp_path, monkeypatch):
    """Directly exercises the walk-health primitive: a directory-walk
    failure must be captured, not silently dropped the way a bare
    ``os.walk`` (no ``onerror``) would.
    """
    def fake_walk(root, onerror=None, **kwargs):
        if onerror is not None:
            onerror(OSError(13, "Permission denied", str(tmp_path / "secret")))
        return iter([])

    monkeypatch.setattr(bi.os, "walk", fake_walk)
    errors = bi._safe_walk_errors(str(tmp_path))
    assert errors
    assert "Permission denied" in errors[0]


def test_bm25_fallback_search_marks_inconclusive_on_walk_error(tmp_path, monkeypatch):
    """A directory-walk failure must flip inconclusive/degraded even when
    the underlying CodeIndex still returns hits from whatever it DID see —
    a partial walk must never be silently reported as fully authoritative.
    """
    _write(tmp_path / "mod.py", "def foo_zzqq():\n    return 1\n")
    monkeypatch.setattr(bi, "_safe_walk_errors", lambda root: ["boom: permission denied"])
    result = bi.bm25_fallback_search(str(tmp_path), "foo_zzqq")
    assert result["inconclusive"] is True
    assert result["degraded"] is True
    assert result["error"]
    assert result["walk_errors"] == ["boom: permission denied"]


def test_lookup_exact_by_path_and_by_hash(tmp_path):
    _write(tmp_path / "mod.py", "def exact_lookup_zzqq():\n    return 1\n")
    search = bi.bm25_fallback_search(str(tmp_path), "exact_lookup_zzqq")
    assert search["hits"]
    hit = search["hits"][0]

    by_path = bi.lookup_exact(str(tmp_path), path=hit["path"])
    assert by_path["found"] is True
    assert by_path["inconclusive"] is False
    assert by_path["matches"][0]["content_hash"] == hit["content_hash"]

    by_hash = bi.lookup_exact(str(tmp_path), content_hash=hit["content_hash"])
    assert by_hash["found"] is True
    assert by_hash["matches"][0]["path"] == hit["path"]

    # relative path also resolves (relative to root_dir)
    by_rel_path = bi.lookup_exact(str(tmp_path), path="mod.py")
    assert by_rel_path["found"] is True


def test_lookup_exact_not_found_is_distinct_from_inconclusive(tmp_path):
    _write(tmp_path / "mod.py", "def something_zzqq():\n    return 1\n")
    bi.bm25_fallback_search(str(tmp_path), "something_zzqq")  # populate the index

    missing = bi.lookup_exact(str(tmp_path), path="does/not/exist.py")
    assert missing["found"] is False
    assert missing["inconclusive"] is False  # clean walk, genuinely absent

    missing_hash = bi.lookup_exact(str(tmp_path), content_hash="0" * 64)
    assert missing_hash["found"] is False
    assert missing_hash["inconclusive"] is False


def test_lookup_exact_walk_error_makes_absence_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(bi, "_safe_walk_errors", lambda root: ["boom"])
    result = bi.lookup_exact(str(tmp_path), path="whatever.py")
    assert result["found"] is False
    assert result["inconclusive"] is True


def test_lookup_exact_requires_a_selector(tmp_path):
    result = bi.lookup_exact(str(tmp_path))
    assert result["inconclusive"] is True
    assert "error" in result


def test_lookup_exact_missing_root_is_inconclusive():
    result = bi.lookup_exact("no/such/root/zzqq", path="x.py")
    assert result["inconclusive"] is True
    assert "error" in result


def test_refresh_subtree_bounds_cost_to_selected_directory(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub / "inner.py", "def qqzzalpha():\n    return 1\n")
    _write(tmp_path / "outside.py", "def wwyybeta():\n    return 2\n")

    from meridian_codeindex import code_index as ci
    ci._INDEX_CACHE.clear()

    outcome = bi.refresh_subtree(str(tmp_path), "sub")
    assert outcome["indexed"] == 1
    assert outcome["skipped"] == 0
    assert not outcome.get("error")

    # The subtree-only refresh must NOT have touched outside.py -- no
    # shared substring/token with the "sub"-only symbol above.
    after = bi.bm25_fallback_search(str(tmp_path), "wwyybeta", reindex=False)
    assert after["hits"] == []

    inside = bi.bm25_fallback_search(str(tmp_path), "qqzzalpha", reindex=False)
    assert inside["hits"]

    # A subsequent FULL reindex picks up the previously-unindexed file.
    full = bi.bm25_fallback_search(str(tmp_path), "wwyybeta", reindex=True)
    assert full["hits"]


def test_refresh_subtree_rejects_path_outside_root(tmp_path):
    other = tmp_path.parent / f"outside_root_{tmp_path.name}"
    other.mkdir(exist_ok=True)
    try:
        outcome = bi.refresh_subtree(str(tmp_path), str(other))
        assert outcome["indexed"] == 0
        assert "error" in outcome
    finally:
        try:
            other.rmdir()
        except OSError:
            pass


def test_refresh_subtree_missing_subtree_reports_error(tmp_path):
    outcome = bi.refresh_subtree(str(tmp_path), "does-not-exist")
    assert outcome["indexed"] == 0
    assert "error" in outcome


def test_resolve_canonical_root_detects_linked_git_worktree(tmp_path):
    main_repo = tmp_path / "main"
    (main_repo / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    _write(wt / ".git", f"gitdir: {main_repo / '.git' / 'worktrees' / 'wt1'}\n")

    main_result = bi.resolve_canonical_root(str(main_repo))
    assert main_result["is_git_worktree"] is False
    assert os.path.normpath(main_result["git_common_dir"]) == os.path.normpath(
        str(main_repo / ".git")
    )

    wt_result = bi.resolve_canonical_root(str(wt))
    assert wt_result["is_git_worktree"] is True
    assert os.path.normpath(wt_result["git_common_dir"]) == os.path.normpath(
        str(main_repo / ".git")
    )
    # Two worktrees of the same repo resolve to DIFFERENT canonical roots.
    assert wt_result["canonical_root"] != main_result["canonical_root"]


def test_resolve_canonical_root_plain_directory_is_not_a_worktree(tmp_path):
    result = bi.resolve_canonical_root(str(tmp_path))
    assert result["is_git_worktree"] is False
    assert result["git_common_dir"] is None
    assert result["canonical_root"] == os.path.abspath(str(tmp_path))


def test_resolve_canonical_root_missing_dir_is_safe():
    result = bi.resolve_canonical_root("no/such/dir/zzqq")
    assert result["is_git_worktree"] is False


# ===========================================================================
# OUTPUTS SIDE — meridian.outputs_indexer (extended)
# ===========================================================================

def test_outputs_index_revision_and_freshness_advance_on_rebuild(tmp_path):
    _write(tmp_path / "a.csv", "col1,col2\n1,2\n")
    index = oi.OutputsFtsIndex(str(tmp_path))
    assert index.index_revision == 0
    assert index.last_rebuilt_at is None

    index.rebuild()
    assert index.index_revision == 1
    first_checkpoint = index.last_rebuilt_at
    assert first_checkpoint is not None

    # A second rebuild with nothing changed must NOT bump index_revision,
    # but freshness (last_rebuilt_at) still advances -- this call happened.
    index.rebuild()
    assert index.index_revision == 1
    assert index.last_rebuilt_at >= first_checkpoint

    _write(tmp_path / "b.csv", "x,y\n3,4\n")
    index.rebuild()
    assert index.index_revision == 2


def test_outputs_convergence_state_reports_clean_and_degraded_states(tmp_path):
    _write(tmp_path / "a.csv", "col1,col2\n1,2\n")
    index = oi.OutputsFtsIndex(str(tmp_path))
    index.rebuild()
    state = index.get_convergence_state()
    assert state["index_revision"] == 1
    assert state["inconclusive"] is False
    assert state["partial_index"] is False
    assert state["degraded"] is False
    assert state["never_rebuilt"] is False
    assert state["total_indexed"] == 1

    # Simulate a real, unresolved DB-write failure on the most recent pass.
    index.last_db_write_error = "disk full"
    state2 = index.get_convergence_state()
    assert state2["inconclusive"] is True
    assert state2["degraded"] is True


def test_outputs_walk_errors_are_captured_not_silently_dropped(tmp_path, monkeypatch):
    def fake_walk(root, onerror=None, **kwargs):
        if onerror is not None:
            onerror(OSError(13, "Permission denied", str(tmp_path / "locked")))
        return iter([])

    monkeypatch.setattr(oi.os, "walk", fake_walk)
    errors_out: list[str] = []
    files = oi._iter_output_files(str(tmp_path), errors_out=errors_out)
    assert files == []
    assert errors_out
    assert "Permission denied" in errors_out[0]


def test_outputs_rebuild_marks_inconclusive_on_walk_error(tmp_path, monkeypatch):
    _write(tmp_path / "a.csv", "col1,col2\n1,2\n")
    index = oi.OutputsFtsIndex(str(tmp_path))

    real_walk = oi.os.walk

    def flaky_walk(root, onerror=None, **kwargs):
        if onerror is not None:
            onerror(OSError(13, "Permission denied", str(tmp_path / "locked")))
        return real_walk(root, **kwargs)

    monkeypatch.setattr(oi.os, "walk", flaky_walk)
    index.rebuild()
    state = index.get_convergence_state()
    assert state["walk_errors"]
    assert state["inconclusive"] is True
    assert state["degraded"] is True


def test_find_by_hash_locates_exact_content(tmp_path):
    _write(tmp_path / "a.csv", "col1,col2\n1,2\n")
    digest = _sha256_of(tmp_path / "a.csv")

    index = oi.OutputsFtsIndex(str(tmp_path))
    index.rebuild()

    matches = index.find_by_hash(digest)
    assert len(matches) == 1
    assert matches[0]["path"].endswith("a.csv")

    assert index.find_by_hash("f" * 64) == []
    assert index.find_by_hash("") == []


def test_get_indexed_output_by_hash_module_wrapper(tmp_path):
    _write(tmp_path / "a.csv", "col1,col2\n5,6\n")
    digest = _sha256_of(tmp_path / "a.csv")

    result = oi.get_indexed_output_by_hash(str(tmp_path), digest)
    assert result["total"] == 1
    assert result["degraded"] is False
    assert result["convergence"]["index_revision"] >= 1

    missing_dir = oi.get_indexed_output_by_hash(str(tmp_path / "nope"), digest)
    assert missing_dir["degraded"] is True
    assert missing_dir["convergence"]["inconclusive"] is True
    assert missing_dir["matches"] == []


def test_refresh_subtree_bounds_outputs_to_selected_directory(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub / "inner.csv", "a,b\n1,2\n")
    _write(tmp_path / "outer.csv", "c,d\n3,4\n")

    index = oi.OutputsFtsIndex(str(tmp_path))
    outcome = index.refresh_subtree("sub")
    assert outcome["indexed"] == 1
    assert not outcome.get("error")

    state = index.get_convergence_state()
    assert state["total_indexed"] == 1  # outer.csv was never touched

    # A subsequent full rebuild picks up outer.csv too, without losing sub's row.
    index.rebuild()
    state2 = index.get_convergence_state()
    assert state2["total_indexed"] == 2


def test_refresh_subtree_detects_deletion_within_subtree(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    victim = sub / "victim.csv"
    _write(victim, "a,b\n1,2\n")

    index = oi.OutputsFtsIndex(str(tmp_path))
    index.refresh_subtree("sub")
    assert index.get_convergence_state()["total_indexed"] == 1

    os.remove(victim)
    index.refresh_subtree("sub")
    assert index.get_convergence_state()["total_indexed"] == 0


def test_refresh_subtree_rejects_directory_outside_outputs_dir(tmp_path):
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    index = oi.OutputsFtsIndex(str(outputs_dir))
    outcome = index.refresh_subtree(str(outside))
    assert outcome["indexed"] == 0
    assert "error" in outcome


def test_refresh_outputs_subtree_module_wrapper(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub / "a.csv", "x,y\n1,2\n")

    result = oi.refresh_outputs_subtree(str(tmp_path), "sub")
    assert result["indexed"] == 1

    missing = oi.refresh_outputs_subtree(str(tmp_path / "nope"), "sub")
    assert "error" in missing
    assert missing["indexed"] == 0


def test_get_outputs_convergence_state_module_wrapper(tmp_path):
    _write(tmp_path / "a.csv", "x,y\n1,2\n")
    oi.search_outputs(str(tmp_path), "x")  # populates the module-level cache

    state = oi.get_outputs_convergence_state(str(tmp_path))
    assert state["index_revision"] >= 1
    assert state["degraded"] is False

    missing = oi.get_outputs_convergence_state(str(tmp_path / "nope"))
    assert missing["inconclusive"] is True
    assert missing["degraded"] is True


def test_search_outputs_result_carries_convergence_and_degraded(tmp_path):
    _write(tmp_path / "a.csv", "temperature,pressure\n300,101\n")
    result = oi.search_outputs(str(tmp_path), "temperature")
    assert result["hits"]
    assert "convergence" in result
    assert result["degraded"] is False
    assert result["convergence"]["index_revision"] >= 1
