"""Tests for meridian_outputs.provenance (sprint item e422de44).

Covers:
  - resolve_figure_output: exact-path tier (unchanged legacy contract) and
    the new basename-fallback tier (relocated/renamed figure files).
  - find_outputs_by_source: the new reverse direction -- script/data path ->
    the outputs it generated.
  - Edge cases mirrored from test_outputs_local.py's resolve_figure_output
    coverage (empty path, missing dir) so the drop-in contract holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import outputs_local as OL
from meridian_outputs import provenance as PV

try:
    import duckdb as _duckdb_probe  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


# ---------------------------------------------------------------------------
# Edge cases (mirror legacy resolve_figure_output contract)
# ---------------------------------------------------------------------------

class TestResolveFigureOutputEdgeCases:
    def test_empty_path(self, tmp_path: Path) -> None:
        assert PV.resolve_figure_output(str(tmp_path), "") is None

    def test_missing_dir(self) -> None:
        assert PV.resolve_figure_output("/nonexistent/dir", "/file.csv") is None

    def test_no_match_anywhere(self, tmp_path: Path) -> None:
        (tmp_path / "unrelated.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        assert PV.resolve_figure_output(str(tmp_path), "/no/such/figure.png") is None


# ---------------------------------------------------------------------------
# Forward: exact-path tier
# ---------------------------------------------------------------------------

class TestResolveFigureOutputExactTier:
    @duckdb_required
    def test_exact_match_reports_match_type(self, tmp_path: Path) -> None:
        f = tmp_path / "results.csv"
        f.write_text("epoch,accuracy\n1,0.9\n", encoding="utf-8")
        result = PV.resolve_figure_output(str(tmp_path), str(f))
        assert result is not None
        assert result["match_type"] == "exact"
        assert result["queried_path"] == str(f)
        assert "results.csv" in result["path"]


# ---------------------------------------------------------------------------
# Forward: basename-fallback tier (the actual fix)
# ---------------------------------------------------------------------------

class TestResolveFigureOutputBasenameTier:
    @duckdb_required
    def test_relocated_figure_resolves_via_basename(self, tmp_path: Path) -> None:
        """A figure copied to a different folder than where it was indexed
        must still resolve -- this is exactly the "stale relocation note"
        failure mode the exact-only lookup could not catch."""
        real_dir = tmp_path / "outputs_run_17"
        real_dir.mkdir()
        real_file = real_dir / "width_hist.csv"
        real_file.write_text("bin,count\n1,5\n2,9\n", encoding="utf-8")

        # The docx figure references a copy that lives somewhere else
        # entirely (e.g. a docs/media staging folder) -- NOT indexed itself.
        relocated_path = str(tmp_path / "docs_media" / "width_hist.csv")

        result = PV.resolve_figure_output(str(tmp_path), relocated_path)
        assert result is not None
        assert result["match_type"] == "basename"
        assert result["queried_path"] == relocated_path
        assert result["path"].replace("\\", "/").endswith("width_hist.csv")
        assert result["candidate_count"] >= 1

    @duckdb_required
    def test_legacy_function_would_have_missed_it(self, tmp_path: Path) -> None:
        """Confirms the prior-state bug: outputs_local's exact-only function
        returns None for the exact same relocated-figure scenario."""
        real_dir = tmp_path / "outputs_run_17"
        real_dir.mkdir()
        (real_dir / "width_hist.csv").write_text("bin,count\n1,5\n", encoding="utf-8")
        relocated_path = str(tmp_path / "docs_media" / "width_hist.csv")

        assert OL.resolve_figure_output(str(tmp_path), relocated_path) is None
        assert PV.resolve_figure_output(str(tmp_path), relocated_path) is not None


# ---------------------------------------------------------------------------
# Reverse: source -> outputs (the direction that did not exist at all)
# ---------------------------------------------------------------------------

_NO_TRUNCATION = {"scan_saturated": False, "index_partial": False, "outputs_capped": False}


def _one_hop_core(result: dict) -> dict:
    """The pre-gap-set-C response keys only. find_outputs_by_source now also
    returns ``truncated`` and ``limits`` on every answer, including the empty
    one, so exact-equality checks compare these three keys and assert the
    new ones separately."""
    return {k: result[k] for k in ("source_path", "outputs", "total")}


class TestFindOutputsBySource:
    def test_empty_source_path(self, tmp_path: Path) -> None:
        result = PV.find_outputs_by_source(str(tmp_path), "")
        assert _one_hop_core(result) == {"source_path": "", "outputs": [], "total": 0}
        assert result["truncated"] == _NO_TRUNCATION

    def test_missing_dir(self) -> None:
        result = PV.find_outputs_by_source("/nonexistent/dir", "gen.py")
        assert result["outputs"] == []
        assert result["total"] == 0

    @duckdb_required
    def test_finds_outputs_generated_by_script(self, tmp_path: Path) -> None:
        (tmp_path / "run_a.json").write_text(
            '{"generating_script": "width_baseline_generator/generate_baselines.py", '
            '"width_px": 1.2}',
            encoding="utf-8",
        )
        (tmp_path / "run_b.json").write_text(
            '{"generating_script": "width_baseline_generator/generate_baselines.py", '
            '"width_px": 1.3}',
            encoding="utf-8",
        )
        (tmp_path / "unrelated.json").write_text(
            '{"generating_script": "other_script.py", "value": 1}',
            encoding="utf-8",
        )

        result = PV.find_outputs_by_source(
            str(tmp_path),
            "C:/Users/dev/project/width_baseline_generator/generate_baselines.py",
        )
        assert result["total"] == 2
        paths = {os.path.basename(o["path"]) for o in result["outputs"]}
        assert paths == {"run_a.json", "run_b.json"}

    @duckdb_required
    def test_no_outputs_for_unrelated_source(self, tmp_path: Path) -> None:
        (tmp_path / "run_a.json").write_text(
            '{"generating_script": "generate_baselines.py", "width_px": 1.2}',
            encoding="utf-8",
        )
        result = PV.find_outputs_by_source(str(tmp_path), "completely_different_script.py")
        assert _one_hop_core(result) == {
            "source_path": "completely_different_script.py", "outputs": [], "total": 0,
        }
        assert result["truncated"]["outputs_capped"] is False


# ---------------------------------------------------------------------------
# Gap set C, "forward-trace-cap": clamped one-hop limits, truncation flags,
# and the bounded transitive trace_forward_outputs. These tests replace
# outputs_local.search_outputs with an in-memory fake so they do not depend
# on duckdb; one duckdb-gated test at the end runs a real chain end to end.
# ---------------------------------------------------------------------------


class _FakeSearch:
    """Stand-in for outputs_local.search_outputs over a fixed row list.

    Returns every row (a superset, like a broad BM25 hit list) truncated to
    ``limit``; find_outputs_by_source's own generating_script filter does
    the matching. Records every call so tests can assert the effective
    (clamped) ``limit`` the index was asked for.
    """

    def __init__(self, rows: list[dict], *, partial: bool = False,
                 fts_pending: bool = False) -> None:
        self.rows = rows
        self.partial = partial
        self.fts_pending = fts_pending
        self.calls: list[dict] = []

    def __call__(self, outputs_dir, query, *, limit=10, include_archival=True, **_kw):
        self.calls.append({"query": query, "limit": limit})
        hits = [dict(r, score=1.0, bm25=1.0) for r in self.rows][:limit]
        result = {"outputs_dir": outputs_dir, "query": query, "hits": hits}
        if self.partial:
            result["partial"] = True
        if self.fts_pending:
            result["fts_pending"] = True
            result["partial"] = True
        return result


def _row(root: Path, rel: str, generating_script: str, mtime: float) -> dict:
    return {
        "path": str(root / rel),
        "generating_script": generating_script,
        "kind": "json_content",
        "mtime": mtime,
        "is_archival": False,
        "canonical_path": None,
    }


@pytest.fixture
def fake_search(monkeypatch):
    def _install(rows, **kwargs):
        fake = _FakeSearch(rows, **kwargs)
        monkeypatch.setattr(OL, "search_outputs", fake)
        return fake
    return _install


def _chain_rows(root: Path) -> list[dict]:
    """gen.py -> a.json -> b.json -> c.json (each cites the previous)."""
    return [
        _row(root, "a.json", "gen.py", 30.0),
        _row(root, "b.json", "a.json", 20.0),
        _row(root, "c.json", "b.json", 10.0),
    ]


def _strip_trace_fields(node: dict) -> dict:
    return {k: v for k, v in node.items() if k not in ("depth", "parent")}


class TestFindOutputsBySourceBounds:
    def test_scan_saturated_when_window_is_full(self, tmp_path, fake_search) -> None:
        fake_search([
            _row(tmp_path, "a.json", "gen.py", 2.0),
            _row(tmp_path, "b.json", "gen.py", 1.0),
        ])
        result = PV.find_outputs_by_source(str(tmp_path), "gen.py", search_limit=1)
        assert result["total"] == 1
        assert result["truncated"]["scan_saturated"] is True
        assert result["truncated"]["outputs_capped"] is False

    def test_window_not_saturated_when_hits_fit(self, tmp_path, fake_search) -> None:
        fake_search([
            _row(tmp_path, "a.json", "gen.py", 2.0),
            _row(tmp_path, "b.json", "gen.py", 1.0),
        ])
        result = PV.find_outputs_by_source(str(tmp_path), "gen.py", search_limit=3)
        assert result["total"] == 2
        assert result["truncated"] == _NO_TRUNCATION

    def test_index_partial_is_propagated(self, tmp_path, fake_search) -> None:
        fake_search([_row(tmp_path, "a.json", "gen.py", 1.0)], partial=True)
        result = PV.find_outputs_by_source(str(tmp_path), "gen.py")
        assert result["truncated"]["index_partial"] is True
        assert result["total"] == 1

    def test_fts_pending_counts_as_index_partial(self, tmp_path, fake_search) -> None:
        fake = fake_search([], fts_pending=True)
        fake.partial = False
        result = PV.find_outputs_by_source(str(tmp_path), "gen.py")
        assert result["outputs"] == []
        assert result["truncated"]["index_partial"] is True

    def test_outputs_capped_when_total_exceeds_limit(self, tmp_path, fake_search) -> None:
        fake_search([
            _row(tmp_path, "a.json", "gen.py", 2.0),
            _row(tmp_path, "b.json", "gen.py", 1.0),
        ])
        result = PV.find_outputs_by_source(str(tmp_path), "gen.py", limit=1)
        assert result["total"] == 2
        assert [os.path.basename(o["path"]) for o in result["outputs"]] == ["a.json"]
        assert result["truncated"]["outputs_capped"] is True

    def test_limits_are_clamped_to_ceilings_and_flagged(self, tmp_path, fake_search) -> None:
        fake = fake_search([_row(tmp_path, "a.json", "gen.py", 1.0)])
        result = PV.find_outputs_by_source(
            str(tmp_path), "gen.py", limit=10**6, search_limit=10**7,
        )
        assert fake.calls[-1]["limit"] == PV.FORWARD_SEARCH_LIMIT_CEILING == 5000
        assert result["limits"] == {
            "limit": PV.FORWARD_LIMIT_CEILING,
            "search_limit": PV.FORWARD_SEARCH_LIMIT_CEILING,
            "limit_clamped": True,
            "search_limit_clamped": True,
        }
        assert PV.FORWARD_LIMIT_CEILING == 1000

    def test_limits_at_or_below_ceiling_are_not_flagged(self, tmp_path, fake_search) -> None:
        fake = fake_search([])
        result = PV.find_outputs_by_source(
            str(tmp_path), "gen.py", limit=1000, search_limit=0,
        )
        # search_limit is floored at 1 (historical behaviour), not "clamped".
        assert fake.calls[-1]["limit"] == 1
        assert result["limits"] == {
            "limit": 1000, "search_limit": 1,
            "limit_clamped": False, "search_limit_clamped": False,
        }

    def test_missing_dir_still_reports_limits_and_no_truncation(self) -> None:
        result = PV.find_outputs_by_source("/nonexistent/dir", "gen.py", limit=5000)
        assert result["truncated"] == _NO_TRUNCATION
        assert result["limits"]["limit"] == 1000
        assert result["limits"]["limit_clamped"] is True


class TestTraceForwardOutputs:
    def test_full_chain_within_caps(self, tmp_path, fake_search) -> None:
        fake_search(_chain_rows(tmp_path))
        result = PV.trace_forward_outputs(str(tmp_path), "gen.py", max_depth=3)
        names = [(os.path.basename(n["path"]), n["depth"]) for n in result["nodes"]]
        assert names == [("a.json", 1), ("b.json", 2), ("c.json", 3)]
        assert result["nodes"][0]["parent"] == "gen.py"
        assert result["nodes"][1]["parent"] == str(tmp_path / "a.json")
        assert result["nodes"][2]["parent"] == str(tmp_path / "b.json")
        assert result["total"] == 3
        assert result["truncated"] is False
        assert result["truncation"]["reason"] is None
        assert result["truncation"]["reasons"] == []
        assert result["truncation"]["depth_reached"] == 3
        assert result["truncation"]["nodes_visited"] == 3

    def test_depth_cap_truncates_with_reason_max_depth(self, tmp_path, fake_search) -> None:
        fake_search(_chain_rows(tmp_path))
        result = PV.trace_forward_outputs(str(tmp_path), "gen.py", max_depth=1)
        assert [os.path.basename(n["path"]) for n in result["nodes"]] == ["a.json"]
        assert result["truncated"] is True
        assert result["truncation"]["reason"] == "max_depth"
        assert result["truncation"]["depth_reached"] == 1

    def test_depth_cap_without_anything_beyond_is_not_truncated(
        self, tmp_path, fake_search,
    ) -> None:
        fake_search(_chain_rows(tmp_path))
        result = PV.trace_forward_outputs(str(tmp_path), "gen.py", max_depth=3)
        assert result["truncated"] is False  # c.json has no dependents

    def test_depth_cap_probe_that_cannot_rule_out_more_is_flagged(
        self, tmp_path, fake_search,
    ) -> None:
        # Three rows, search_limit=3: every search window comes back full.
        # Only a.json cites gen.py, and nothing cites a.json, but the
        # depth-cap probe from a.json cannot rule a dependent out because
        # its own window saturated, so max_depth is reported (fail closed).
        rows = [
            _row(tmp_path, "a.json", "gen.py", 3.0),
            _row(tmp_path, "x.json", "unrelated.py", 2.0),
            _row(tmp_path, "y.json", "unrelated.py", 1.0),
        ]
        fake_search(rows)
        result = PV.trace_forward_outputs(
            str(tmp_path), "gen.py", max_depth=1, search_limit=3,
        )
        # Hop 1 window holds all 3 rows -> saturated (reported), and the
        # depth-cap probe's window is equally full -> max_depth reported too.
        assert result["truncation"]["scan_saturated"] is True
        assert result["truncation"]["reasons"] == ["max_depth", "scan_saturated"]
        assert result["truncation"]["reason"] == "max_depth"

    def test_node_cap_truncates_with_reason_max_nodes(self, tmp_path, fake_search) -> None:
        fake_search([
            _row(tmp_path, "a.json", "gen.py", 3.0),
            _row(tmp_path, "b.json", "gen.py", 2.0),
            _row(tmp_path, "c.json", "gen.py", 1.0),
        ])
        result = PV.trace_forward_outputs(str(tmp_path), "gen.py", max_nodes=2)
        assert [os.path.basename(n["path"]) for n in result["nodes"]] == ["a.json", "b.json"]
        assert result["truncated"] is True
        assert result["truncation"]["reason"] == "max_nodes"
        assert result["truncation"]["nodes_visited"] == 2

    def test_node_cap_exactly_met_is_not_truncated(self, tmp_path, fake_search) -> None:
        fake_search([
            _row(tmp_path, "a.json", "gen.py", 2.0),
            _row(tmp_path, "b.json", "gen.py", 1.0),
        ])
        result = PV.trace_forward_outputs(str(tmp_path), "gen.py", max_nodes=2)
        assert result["total"] == 2
        assert result["truncated"] is False

    def test_two_cycle_terminates(self, tmp_path, fake_search) -> None:
        fake = fake_search([
            _row(tmp_path, "a.json", "b.json", 2.0),
            _row(tmp_path, "b.json", "a.json", 1.0),
        ])
        result = PV.trace_forward_outputs(
            str(tmp_path), str(tmp_path / "a.json"), max_depth=25, max_nodes=5000,
        )
        assert [os.path.basename(n["path"]) for n in result["nodes"]] == ["b.json"]
        assert result["truncated"] is False
        assert len(fake.calls) <= 3

    def test_three_cycle_terminates_and_visits_each_once(self, tmp_path, fake_search) -> None:
        fake_search([
            _row(tmp_path, "a.json", "c.json", 3.0),
            _row(tmp_path, "b.json", "a.json", 2.0),
            _row(tmp_path, "c.json", "b.json", 1.0),
        ])
        result = PV.trace_forward_outputs(str(tmp_path), str(tmp_path / "a.json"), max_depth=25)
        names = [os.path.basename(n["path"]) for n in result["nodes"]]
        assert names == ["b.json", "c.json"]
        assert len(set(names)) == len(names)
        assert result["truncated"] is False

    def test_max_depth_one_reproduces_one_hop_output(self, tmp_path, fake_search) -> None:
        rows = [
            _row(tmp_path, "a.json", "gen.py", 3.0),
            _row(tmp_path, "sub/b.json", "scripts/gen.py", 2.0),  # basename match
            _row(tmp_path, "c.json", "a.json", 1.0),  # hop 2 only
            _row(tmp_path, "d.json", "other.py", 0.5),
        ]
        fake_search(rows)
        one_hop = PV.find_outputs_by_source(str(tmp_path), "gen.py")
        trace = PV.trace_forward_outputs(str(tmp_path), "gen.py", max_depth=1)
        assert [_strip_trace_fields(n) for n in trace["nodes"]] == one_hop["outputs"]
        assert trace["total"] == one_hop["total"] == 2
        assert all(n["depth"] == 1 and n["parent"] == "gen.py" for n in trace["nodes"])
        for flag, value in one_hop["truncated"].items():
            assert trace["truncation"][flag] == value

    def test_hops_beyond_first_require_exact_path_not_basename(
        self, tmp_path, fake_search,
    ) -> None:
        fake_search([
            _row(tmp_path, "a.json", "gen.py", 3.0),
            # Shares a.json's basename but names a DIFFERENT file:
            _row(tmp_path, "x.json", "elsewhere/a.json", 2.0),
            # Names a.json exactly by its outputs_dir-relative spelling:
            _row(tmp_path, "y.json", "a.json", 1.0),
        ])
        one_hop_from_a = PV.find_outputs_by_source(str(tmp_path), str(tmp_path / "a.json"))
        assert {os.path.basename(o["path"]) for o in one_hop_from_a["outputs"]} == {
            "x.json", "y.json",
        }
        trace = PV.trace_forward_outputs(str(tmp_path), "gen.py", max_depth=2)
        assert [os.path.basename(n["path"]) for n in trace["nodes"]] == ["a.json", "y.json"]

    def test_absolute_generating_script_matches_at_later_hops(
        self, tmp_path, fake_search,
    ) -> None:
        fake_search([
            _row(tmp_path, "a.json", "gen.py", 2.0),
            _row(tmp_path, "b.json", str(tmp_path / "a.json"), 1.0),
        ])
        trace = PV.trace_forward_outputs(str(tmp_path), "gen.py")
        assert [os.path.basename(n["path"]) for n in trace["nodes"]] == ["a.json", "b.json"]

    def test_hop_flags_are_ored_into_truncation(self, tmp_path, fake_search) -> None:
        fake_search(
            [_row(tmp_path, "a.json", "gen.py", 2.0), _row(tmp_path, "b.json", "gen.py", 1.0)],
            partial=True,
        )
        result = PV.trace_forward_outputs(str(tmp_path), "gen.py", limit=1, max_depth=1)
        assert result["truncated"] is True
        assert result["truncation"]["outputs_capped"] is True
        assert result["truncation"]["index_partial"] is True
        assert "outputs_capped" in result["truncation"]["reasons"]
        assert "index_partial" in result["truncation"]["reasons"]

    def test_caps_are_clamped_and_flagged(self, tmp_path, fake_search) -> None:
        fake_search([])
        result = PV.trace_forward_outputs(
            str(tmp_path), "gen.py", max_depth=10**3, max_nodes=10**6,
            limit=10**5, search_limit=10**6,
        )
        assert result["limits"] == {
            "max_depth": PV.TRACE_MAX_DEPTH_CEILING,
            "max_nodes": PV.TRACE_MAX_NODES_CEILING,
            "limit": PV.FORWARD_LIMIT_CEILING,
            "search_limit": PV.FORWARD_SEARCH_LIMIT_CEILING,
            "max_depth_clamped": True,
            "max_nodes_clamped": True,
            "limit_clamped": True,
            "search_limit_clamped": True,
        }
        assert result["nodes"] == []
        assert result["truncated"] is False

    def test_empty_source_and_missing_dir_return_empty_untruncated(self, tmp_path) -> None:
        for outputs_dir, source in ((str(tmp_path), ""), ("/nonexistent/dir", "gen.py")):
            result = PV.trace_forward_outputs(outputs_dir, source)
            assert result["nodes"] == []
            assert result["total"] == 0
            assert result["truncated"] is False
            assert result["truncation"]["depth_reached"] == 0

    def test_package_level_export(self) -> None:
        import meridian_outputs

        assert meridian_outputs.trace_forward_outputs is PV.trace_forward_outputs
        assert "trace_forward_outputs" in meridian_outputs.__all__

    def test_mcp_tool_is_registered_and_forwards_args(self, tmp_path, monkeypatch) -> None:
        from meridian_outputs import server as srv

        registered = {t.name for t in srv.mcp._tool_manager.list_tools()}
        assert "trace_forward_outputs" in registered
        seen: dict = {}

        def _spy(outputs_dir, source_path, **kwargs):
            seen.update(outputs_dir=outputs_dir, source_path=source_path, **kwargs)
            return {"ok": True}

        monkeypatch.setattr(PV, "trace_forward_outputs", _spy)
        assert srv.trace_forward_outputs(
            str(tmp_path), "gen.py", max_depth=2, max_nodes=7, limit=3, search_limit=9,
        ) == {"ok": True}
        assert seen == {
            "outputs_dir": str(tmp_path), "source_path": "gen.py",
            "max_depth": 2, "max_nodes": 7, "limit": 3, "search_limit": 9,
        }

    def test_readme_documents_the_new_tool(self) -> None:
        readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
        assert "`trace_forward_outputs`" in readme

    @duckdb_required
    def test_real_index_chain_end_to_end(self, tmp_path: Path) -> None:
        (tmp_path / "stage1.json").write_text(
            '{"generating_script": "pipeline/make_stage1.py", "v": 1}', encoding="utf-8",
        )
        (tmp_path / "stage2.json").write_text(
            '{"generating_script": "stage1.json", "v": 2}', encoding="utf-8",
        )
        result = PV.trace_forward_outputs(str(tmp_path), "pipeline/make_stage1.py")
        names = [(os.path.basename(n["path"]), n["depth"]) for n in result["nodes"]]
        assert ("stage1.json", 1) in names
        assert ("stage2.json", 2) in names


# ---------------------------------------------------------------------------
# Read paths never create the cache directory or edit .gitignore
# ---------------------------------------------------------------------------

class TestReadsLeaveAFreshOutputsDirUntouched:
    def test_reading_ledgers_and_registry_creates_no_cache_dir(self, tmp_path: Path) -> None:
        """A dry-run bundle import reads every ledger through these readers;
        a read must not create .meridian-outputs-cache or write a
        .gitignore beside it. Only a write may do that."""
        from meridian_outputs import annotate, artifact_registry, bundle, fingerprint, run_manifest

        outputs = tmp_path / "outputs"
        outputs.mkdir()
        fig = outputs / "fig.csv"
        fig.write_text("a\n1\n", encoding="utf-8")
        d = str(outputs)

        assert annotate.get_provenance(d, str(fig)) is None
        assert annotate.get_provenance_history(d, str(fig)) is None
        assert annotate.list_provenance(d) == []
        assert fingerprint.check_staleness(d) == []
        assert fingerprint.find_stale_by_script(d, str(outputs / "gen.py")) == []
        assert artifact_registry.get_artifact(d, "no-such-id") is None
        assert artifact_registry.list_artifacts(d) == []
        assert artifact_registry.resolve_artifact(d, canonical_path=str(fig))["status"] == (
            artifact_registry.UNRESOLVED
        )
        assert artifact_registry.get_artifact_sources(d, "no-such-id") == []
        assert run_manifest.get_run_manifest(d, "r1") is None
        assert run_manifest.list_run_manifests(d) == []
        for module, reader, _filename in bundle._LEDGER_MEMBERS.values():
            getattr(module, reader)(d)

        assert sorted(p.name for p in outputs.iterdir()) == ["fig.csv"]
        assert not (tmp_path / ".gitignore").exists()

        # A write still creates and gitignores the cache directory.
        annotate.record_provenance(d, str(fig))
        assert (outputs / ".meridian-outputs-cache").is_dir()
        assert (outputs / ".gitignore").is_file()
