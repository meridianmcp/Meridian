"""Tests for meridian_outputs.search -- the literal-match-boosted superset
of outputs_local.search_outputs.

Sprint item c6236ef4.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meridian_outputs import outputs_local as OL
from meridian_outputs import search as S

try:
    import duckdb  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


class TestSearchOutputsPassthrough:
    """Non-happy-path behaviour must be byte-identical to outputs_local's."""

    def test_missing_dir_returns_error_unchanged(self) -> None:
        base = OL.search_outputs("/nonexistent/dir", "query")
        wrapped = S.search_outputs("/nonexistent/dir", "query")
        assert wrapped == base

    def test_empty_query_returns_error_unchanged(self, tmp_path: Path) -> None:
        base = OL.search_outputs(str(tmp_path), "")
        wrapped = S.search_outputs(str(tmp_path), "")
        assert wrapped == base

    @duckdb_required
    def test_empty_tree_no_hits(self, tmp_path: Path) -> None:
        result = S.search_outputs(str(tmp_path), "anything")
        assert result["hits"] == []
        assert "error" not in result


class TestLiteralMatchBoost:
    @duckdb_required
    def test_literal_match_field_present_on_every_hit(self, tmp_path: Path) -> None:
        (tmp_path / "accuracy.csv").write_text(
            "epoch,accuracy\n1,0.9\n2,0.95", encoding="utf-8"
        )
        result = S.search_outputs(str(tmp_path), "accuracy")
        assert len(result["hits"]) >= 1
        for hit in result["hits"]:
            assert "literal_match" in hit

    @duckdb_required
    def test_additive_only_same_hits_as_base(self, tmp_path: Path) -> None:
        """The wrapper must never drop, add, or otherwise change a hit --
        only reorder and annotate. Clear the shared index cache between the
        base and wrapped calls so both start from the same cold state."""
        (tmp_path / "a_results.csv").write_text("a,b\n1,2", encoding="utf-8")
        (tmp_path / "b_results.csv").write_text("c,d\n3,4", encoding="utf-8")

        base = OL.search_outputs(str(tmp_path), "results", limit=10)
        with OL._index_cache_lock:
            while OL._index_cache:
                _, idx = OL._index_cache.popitem()
                idx.close()
        wrapped = S.search_outputs(str(tmp_path), "results", limit=10)

        base_paths = {h["path"] for h in base["hits"]}
        wrapped_paths = {h["path"] for h in wrapped["hits"]}
        assert wrapped_paths == base_paths
        assert len(wrapped["hits"]) == len(base["hits"])
        assert wrapped["total_indexed"] == base["total_indexed"]

    @duckdb_required
    def test_exact_filename_query_outranks_longer_decoy(self, tmp_path: Path) -> None:
        """Reproduces the real-world gap found during investigation: a
        longer, differently-suffixed decoy file must not outrank an exact
        basename match for a query that IS essentially that filename."""
        canonical = tmp_path / "parabolic_radius_sweep_130_results.csv"
        canonical.write_text("radius,mae\n0.1,1.2\n0.2,1.1", encoding="utf-8")
        # A longer sibling name that shares most BM25 tokens with the
        # canonical file plus extra ones -- mirrors the real
        # "..._FULL130.csv.bak_41img_mislabeled" decoy found in the live
        # investigation (extra tokens inflate raw term-frequency overlap
        # while the canonical file is the true literal-substring match).
        decoy = tmp_path / "parabolic_radius_sweep_130_results_FULL130_extra_mislabeled.csv"
        decoy.write_text("radius,mae,full130,extra,mislabeled\n0.1,1.2,1,1,1", encoding="utf-8")

        query = "parabolic_radius_sweep_130_results"
        result = S.search_outputs(str(tmp_path), query, limit=10)
        assert len(result["hits"]) >= 2

        top_hit = result["hits"][0]
        assert top_hit["path"] == str(canonical), (
            "literal-match boost must rank the exact-basename-match canonical "
            f"file first; got {top_hit['path']!r} instead. Full hits: "
            f"{result['hits']}"
        )
        assert top_hit["literal_match"] is True

    @duckdb_required
    def test_no_literal_match_falls_back_to_pure_bm25_order(
        self, tmp_path: Path,
    ) -> None:
        """When nothing literally contains the query, ordering must be
        exactly the base BM25 order (a no-op re-rank)."""
        (tmp_path / "one.csv").write_text("alpha,beta\n1,2", encoding="utf-8")
        (tmp_path / "two.csv").write_text("alpha,beta,gamma\n1,2,3", encoding="utf-8")

        base = OL.search_outputs(str(tmp_path), "alpha beta", limit=10)
        with OL._index_cache_lock:
            while OL._index_cache:
                _, idx = OL._index_cache.popitem()
                idx.close()
        wrapped = S.search_outputs(str(tmp_path), "alpha beta", limit=10)

        assert [h["path"] for h in wrapped["hits"]] == [h["path"] for h in base["hits"]]
        assert all(h["literal_match"] is False for h in wrapped["hits"])
