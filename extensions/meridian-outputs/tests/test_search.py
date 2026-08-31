"""Tests for meridian_outputs.search -- the literal-match-boosted superset
of outputs_local.search_outputs.

Sprint item c6236ef4.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meridian_outputs import annotate as AN
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
        exactly the base BM25 order (a no-op re-rank).

        Code-review note: since a444313d, this specific fixture is NOT a
        genuinely zero-boost case -- both files' CSV columns contain both
        query tokens ("alpha", "beta"), so a444313d's metadata-field boost
        DOES fire here (+16.0 for each, confirmed empirically), it's just
        SYMMETRIC between the two candidates, so relative order is
        unaffected -- a constant offset added to both sides of a comparison
        never changes which side is larger. See
        TestFieldBoostIsGenuinelyZero below for an isolated true-zero-boost
        case."""
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


class TestFieldBoostIsGenuinelyZero:
    @duckdb_required
    def test_zero_boost_when_nothing_matches_any_field(self, tmp_path: Path) -> None:
        """An isolated true-zero-boost case (code review): neither file's
        filename, metadata, script, or provenance connects to the query at
        all -- the query only matches via generic body text -- so ordering
        must be identical to raw BM25 for a genuinely-zero-boost reason,
        not a coincidentally-symmetric one (see the test above)."""
        (tmp_path / "one.csv").write_text(
            "col1,col2\nzephyr quartz\n", encoding="utf-8",
        )
        (tmp_path / "two.csv").write_text(
            "col3,col4\nzephyr quartz zephyr\n", encoding="utf-8",
        )

        for hit_path in (str(tmp_path / "one.csv"), str(tmp_path / "two.csv")):
            boost, _ = S._field_boost(
                S._normalize("zephyr quartz"), S._normalize_tokens("zephyr quartz"),
                {"path": hit_path, "csv_columns": None, "json_keys": None,
                 "generating_script": None},
                str(tmp_path),
            )
            assert boost == 0.0

        base = OL.search_outputs(str(tmp_path), "zephyr quartz", limit=10)
        _clear_index_cache()
        wrapped = S.search_outputs(str(tmp_path), "zephyr quartz", limit=10)
        assert [h["path"] for h in wrapped["hits"]] == [h["path"] for h in base["hits"]]


# ---------------------------------------------------------------------------
# Negative/zero limit (sprint item a444313d, code-review fix)
# ---------------------------------------------------------------------------

class TestLimitEdgeCases:
    @duckdb_required
    def test_negative_limit_matches_underlying_engine_not_a_negative_slice(
        self, tmp_path: Path,
    ) -> None:
        """outputs_local.search's own internal safe_limit = max(1, limit)
        clamp means a negative caller limit still returns up to 1 hit (a
        pre-existing, unrelated-to-this-item quirk of the underlying
        engine) -- both the pre-a444313d search.py and the real engine
        agree on this. a444313d's own final truncation must not introduce
        a NEW behavior here via `hits[:limit]`'s negative-slice semantics
        ("drop the last N") where none existed before."""
        (tmp_path / "widget.csv").write_text("col\nwidget\n", encoding="utf-8")

        base = OL.search_outputs(str(tmp_path), "widget", limit=-5)
        _clear_index_cache()
        wrapped = S.search_outputs(str(tmp_path), "widget", limit=-5)

        assert len(wrapped["hits"]) == len(base["hits"])
        assert len(wrapped["hits"]) >= 1

    @duckdb_required
    def test_zero_limit_matches_underlying_engine(self, tmp_path: Path) -> None:
        """Verified directly against the real engine (not assumed): the
        underlying safe_limit = max(1, int(limit)) clamp means limit=0
        still returns up to 1 hit, same as limit=-5 -- there is no special
        "0 means none" case anywhere in the actual call chain. The fielded
        wrapper must match this exactly, not introduce its own different
        limit=0 semantics."""
        (tmp_path / "widget.csv").write_text("col\nwidget\n", encoding="utf-8")

        base = OL.search_outputs(str(tmp_path), "widget", limit=0)
        _clear_index_cache()
        wrapped = S.search_outputs(str(tmp_path), "widget", limit=0)

        assert len(wrapped["hits"]) == len(base["hits"]) == 1


def _clear_index_cache() -> None:
    with OL._index_cache_lock:
        while OL._index_cache:
            _, idx = OL._index_cache.popitem()
            idx.close()


# ---------------------------------------------------------------------------
# Fielded ranking: exact/stem/phrase tiers (sprint item a444313d)
# ---------------------------------------------------------------------------

class TestFieldBoostTiers:
    @duckdb_required
    def test_exact_stem_outranks_filename_phrase_match(self, tmp_path: Path) -> None:
        exact = tmp_path / "metrics.csv"
        exact.write_text("col\nvalue=1\n", encoding="utf-8")
        phrase = tmp_path / "final_metrics_summary.csv"
        phrase.write_text("col\nvalue=1\n", encoding="utf-8")

        result = S.search_outputs(str(tmp_path), "metrics", limit=10)
        assert result["hits"][0]["path"] == str(exact)

    @duckdb_required
    def test_filename_phrase_outranks_relative_path_only_match(
        self, tmp_path: Path,
    ) -> None:
        # Tantivy indexes CONTENT only (basename + metadata + body), never
        # directory names -- path_only's body must ALSO mention "metrics"
        # or it is never retrieved as a candidate at all, regardless of any
        # relative-path boost (a boost can only rerank an already-fetched
        # hit, never recall one Tantivy never indexed).
        sub = tmp_path / "metrics_run"
        sub.mkdir()
        path_only = sub / "results.csv"
        path_only.write_text("col\nvalue=1\nmetrics reference in body\n", encoding="utf-8")
        filename_match = tmp_path / "metrics_summary.csv"
        filename_match.write_text("col\nvalue=1\n", encoding="utf-8")

        result = S.search_outputs(str(tmp_path), "metrics", limit=10)
        paths = [h["path"] for h in result["hits"]]
        assert str(path_only) in paths  # sanity: retrievable at all
        assert paths.index(str(filename_match)) < paths.index(str(path_only))


# ---------------------------------------------------------------------------
# Metadata (CSV column / JSON key) field signal (sprint item a444313d)
# ---------------------------------------------------------------------------

class TestMetadataFieldSignal:
    @duckdb_required
    def test_matching_csv_column_recovers_buried_hit(self, tmp_path: Path) -> None:
        """The true match's ONLY connection to the query is a CSV column
        name -- unrelated filename, unrelated body -- while many decoys
        repeat the query term heavily in their body to dominate raw BM25.
        Proves the metadata-field boost is a genuine, independent ranking
        signal, not just filename/body matching in disguise."""
        target = tmp_path / "unrelated_name.csv"
        target.write_text("throughput,other\n1,2\n", encoding="utf-8")

        # Fewer decoys than the overfetch window (limit * 3, capped at
        # limit + 30 -- see search.py's two-tier overfetch, kept modest for
        # the common include_archival=True path) so target has room to
        # actually be FETCHED -- a boost can only rerank an already-fetched
        # candidate, never recall one crowded out of the raw overfetch.
        for i in range(6):
            (tmp_path / f"decoy_{i:02d}.csv").write_text(
                "col\n" + "throughput " * 5 + f"\n{i}\n", encoding="utf-8",
            )

        limit = 3
        raw = OL.search_outputs(str(tmp_path), "throughput", limit=limit)
        assert str(target) not in {h["path"] for h in raw["hits"]}, (
            "test setup invalid -- the true match must NOT already be in "
            "the raw top-N for this test to actually exercise the boost"
        )

        result = S.search_outputs(str(tmp_path), "throughput", limit=limit)
        assert str(target) in [h["path"] for h in result["hits"]]
        assert len(result["hits"]) <= limit


# ---------------------------------------------------------------------------
# Provenance field signal (sprint item a444313d) -- annotate.get_provenance,
# deliberately NOT outputs_local's separate directory-note annotations table
# ---------------------------------------------------------------------------

class TestProvenanceFieldSignal:
    @duckdb_required
    def test_matching_provenance_note_promotes_a_weakly_connected_hit(
        self, tmp_path: Path,
    ) -> None:
        """A provenance-only signal cannot RECALL a candidate Tantivy never
        indexed at all -- annotate.py's ledger is a separate store the FTS
        engine never searches, so a hit with ZERO other connection to the
        query can never be fetched in the first place (unlike metadata,
        which is baked directly into the indexed content). What the
        provenance boost CAN do -- and what this proves -- is promote a hit
        that's already a genuine (if weak) Tantivy candidate ahead of
        stronger-raw-BM25 decoys once its provenance note also matches."""
        target = tmp_path / "unrelated_name.csv"
        target.write_text("a,b\ncalibration,2\n", encoding="utf-8")
        AN.record_provenance(
            str(tmp_path), str(target),
            note="rerun after the calibration fix landed",
        )

        for i in range(6):
            (tmp_path / f"decoy_{i:02d}.csv").write_text(
                "col\n" + "calibration " * 5 + f"\n{i}\n", encoding="utf-8",
            )

        limit = 3
        raw = OL.search_outputs(str(tmp_path), "calibration", limit=limit)
        assert str(target) not in {h["path"] for h in raw["hits"]}, (
            "test setup invalid -- the true match must NOT already be in "
            "the raw top-N for this test to actually exercise the boost"
        )

        result = S.search_outputs(str(tmp_path), "calibration", limit=limit)
        assert str(target) in [h["path"] for h in result["hits"]]

    def test_provenance_boost_never_recalls_a_tantivy_invisible_hit(
        self, tmp_path: Path,
    ) -> None:
        """Documents the genuine limitation above directly: a hit with NO
        Tantivy-visible connection to the query at all is never returned,
        no matter how strongly its provenance note matches."""
        target = tmp_path / "unrelated_name.csv"
        target.write_text("a,b\n1,2\n", encoding="utf-8")  # zero query overlap
        AN.record_provenance(
            str(tmp_path), str(target),
            note="rerun after the calibration fix landed",
        )

        result = S.search_outputs(str(tmp_path), "calibration", limit=10)
        assert str(target) not in [h["path"] for h in result["hits"]]

    def test_provenance_lookup_failure_degrades_to_zero_boost(
        self, tmp_path: Path,
    ) -> None:
        """No ledger, no crash -- a file never provenance-tagged just gets
        no provenance-field bonus, not an exception."""
        tokens = S._provenance_note_tokens(str(tmp_path), str(tmp_path / "x.csv"))
        assert tokens == set()


# ---------------------------------------------------------------------------
# Overfetch enables archival backfill (sprint item a444313d)
# ---------------------------------------------------------------------------

class TestArchivalBackfillViaOverfetch:
    @duckdb_required
    def test_include_archival_false_backfills_from_overfetch_pool(
        self, tmp_path: Path,
    ) -> None:
        # 3 canonical files -- exactly `limit` -- plus 10 archival twins
        # (identical content, `_old` suffix -> classify_canonical_archival's
        # own archival-pair heuristic). With overfetch pulling in all 13
        # matching hits, the 3 canonical files must ALWAYS be fully
        # recoverable regardless of incidental raw tie-order between
        # identical-content archival/canonical pairs.
        for i in range(3):
            content = f"col\nvalue={i}\nwidget keyword here\n"
            (tmp_path / f"widget_{i}.csv").write_text(content, encoding="utf-8")
            (tmp_path / f"widget_{i}_old.csv").write_text(content, encoding="utf-8")
        for i in range(3, 10):
            content = f"col\nvalue={i}\nwidget keyword here\n"
            (tmp_path / f"widget_{i}_old.csv").write_text(content, encoding="utf-8")

        limit = 3
        result = S.search_outputs(
            str(tmp_path), "widget", limit=limit, include_archival=False,
        )
        assert len(result["hits"]) == limit
        assert all(not h["is_archival"] for h in result["hits"])


# ---------------------------------------------------------------------------
# Deterministic tie-break (sprint item a444313d)
# ---------------------------------------------------------------------------

class TestDeterministicTieBreak:
    @duckdb_required
    def test_ties_broken_by_relative_path_and_stable_across_calls(
        self, tmp_path: Path,
    ) -> None:
        content = "col\nvalue=1\nunrelated body text here\n"
        (tmp_path / "zzz_file.csv").write_text(content, encoding="utf-8")
        (tmp_path / "aaa_file.csv").write_text(content, encoding="utf-8")

        result1 = S.search_outputs(str(tmp_path), "unrelated", limit=10)
        _clear_index_cache()
        result2 = S.search_outputs(str(tmp_path), "unrelated", limit=10)

        paths1 = [h["path"] for h in result1["hits"]]
        paths2 = [h["path"] for h in result2["hits"]]
        assert paths1 == paths2
        assert paths1[0] == str(tmp_path / "aaa_file.csv")


# ---------------------------------------------------------------------------
# Degraded-state passthrough is unaffected by overfetch (sprint item a444313d)
# ---------------------------------------------------------------------------

class TestDegradedPassthroughWithOverfetch:
    @duckdb_required
    def test_degraded_and_convergence_untouched_by_reranking(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "a.csv").write_text("col\n1\n", encoding="utf-8")
        base = OL.search_outputs(str(tmp_path), "col", limit=3)
        _clear_index_cache()
        wrapped = S.search_outputs(str(tmp_path), "col", limit=3)

        assert wrapped["degraded"] == base["degraded"]
        assert wrapped["convergence"] == base["convergence"]
        assert wrapped["total_indexed"] == base["total_indexed"]
        assert wrapped["total_in_index"] == base["total_in_index"]
