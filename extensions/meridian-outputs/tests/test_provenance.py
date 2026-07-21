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

class TestFindOutputsBySource:
    def test_empty_source_path(self, tmp_path: Path) -> None:
        result = PV.find_outputs_by_source(str(tmp_path), "")
        assert result == {"source_path": "", "outputs": [], "total": 0}

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
        assert result == {"source_path": "completely_different_script.py", "outputs": [], "total": 0}
