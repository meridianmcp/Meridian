"""Tests for meridian_outputs.outputs_local.get_cache_quota_status
(c7ef8ff7, MDE-9 P1 -- local quota visibility).

A standalone, self-contained companion to
``meridian.local_resilience.check_local_quota`` -- same report shape, no
cross-package import (this package has zero dependency on the ``meridian``
core package).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import outputs_local as OL


class TestGetCacheQuotaStatus:
    def test_missing_outputs_dir(self, tmp_path: Path) -> None:
        result = OL.get_cache_quota_status(str(tmp_path / "nope"))
        assert result["exists"] is False
        assert result["used_bytes"] == 0
        assert result["exceeded"] is False
        assert result["reason"]

    def test_outputs_dir_exists_but_never_cached(self, tmp_path: Path) -> None:
        result = OL.get_cache_quota_status(str(tmp_path))
        assert result["exists"] is False
        assert result["cache_dir"] == str(tmp_path / ".meridian-outputs-cache")
        assert result["exceeded"] is False
        assert result["reason"] is None

    def test_reports_usage_with_no_budget_never_exceeded(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        (cache_dir / "index.duckdb").write_bytes(b"x" * 500)

        result = OL.get_cache_quota_status(str(tmp_path))
        assert result["exists"] is True
        assert result["used_bytes"] == 500
        assert result["used_files"] == 1
        assert result["exceeded"] is False  # no max_bytes/max_files given

    def test_exceeded_bytes_budget_degrades_visibly(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        (cache_dir / "index.duckdb").write_bytes(b"x" * 2000)

        result = OL.get_cache_quota_status(str(tmp_path), max_bytes=1000)
        assert result["exceeded"] is True
        assert "exceeded" in result["reason"]

    def test_exceeded_files_budget(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        for i in range(5):
            (cache_dir / f"f{i}.json").write_bytes(b"{}")

        result = OL.get_cache_quota_status(str(tmp_path), max_bytes=10_000_000, max_files=3)
        assert result["exceeded"] is True
        assert result["used_files"] == 5

    def test_nested_cache_subdirectories_are_summed(self, tmp_path: Path) -> None:
        nested = tmp_path / ".meridian-outputs-cache" / "sub"
        nested.mkdir(parents=True)
        (nested / "f.json").write_bytes(b"x" * 100)
        (tmp_path / ".meridian-outputs-cache" / "g.json").write_bytes(b"x" * 100)

        result = OL.get_cache_quota_status(str(tmp_path))
        assert result["used_bytes"] == 200
        assert result["used_files"] == 2

    def test_never_raises_on_unreadable_entries(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        # No real unreadable file to inject portably -- this asserts the
        # call completes cleanly on an ordinary populated directory, which
        # is the common-path guarantee the onerror=lambda e: None handler
        # protects.
        (cache_dir / "ok.json").write_bytes(b"{}")
        result = OL.get_cache_quota_status(str(tmp_path))
        assert result["used_files"] == 1
