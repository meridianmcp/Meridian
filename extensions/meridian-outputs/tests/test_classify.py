"""Tests for meridian_outputs.classify.classify_outputs.

Focus: the broader archival-naming heuristic must not report a duplicate on
a name guess plus equal size alone. is_archival is set only when the content
hashes match, as the module docstring has always promised.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import classify as CL


def _record(result: dict, name: str) -> dict:
    return next(c for c in result["classifications"] if c["path"].endswith(name))


class TestBroaderHeuristicNeedsHashConfirmation:
    def test_same_size_different_content_is_not_archival(self, tmp_path: Path) -> None:
        canonical = tmp_path / "sweep.csv"
        backup = tmp_path / "sweep.csv.bak_41img_mislabeled"
        canonical.write_bytes(b"radius,mae\n0.1,1.2\n")
        backup.write_bytes(b"radius,mae\n0.1,9.9\n")
        assert canonical.stat().st_size == backup.stat().st_size  # premise

        result = CL.classify_outputs([str(canonical), str(backup)])
        rec = _record(result, ".bak_41img_mislabeled")
        assert rec["is_archival"] is False
        assert rec["canonical_path"] == str(canonical)
        assert "content hash differs" in rec["reason"]

    def test_same_size_identical_content_is_confirmed_archival(
        self, tmp_path: Path,
    ) -> None:
        body = b"radius,mae\n0.1,1.2\n"
        canonical = tmp_path / "sweep.csv"
        backup = tmp_path / "sweep_backup.csv"
        canonical.write_bytes(body)
        backup.write_bytes(body)

        result = CL.classify_outputs([str(canonical), str(backup)])
        rec = _record(result, "sweep_backup.csv")
        assert rec["is_archival"] is True
        assert rec["canonical_path"] == str(canonical)
        assert "content hash identical" in rec["reason"]
        assert "confirmed duplicate" in rec["reason"]

    def test_hash_unavailable_stays_unconfirmed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = b"a,b\n1,2\n"
        canonical = tmp_path / "run.csv"
        backup = tmp_path / "run.csv~"
        canonical.write_bytes(body)
        backup.write_bytes(body)
        monkeypatch.setattr(CL, "files_content_identical", lambda _a, _b: None)

        result = CL.classify_outputs([str(canonical), str(backup)])
        rec = _record(result, "run.csv~")
        assert rec["is_archival"] is False
        assert "unconfirmed" in rec["reason"]
        assert "not marked archival" in rec["reason"]

    def test_different_sizes_never_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        canonical = tmp_path / "run.csv"
        backup = tmp_path / "run_deprecated.csv"
        canonical.write_bytes(b"a,b\n1,2\n")
        backup.write_bytes(b"a,b\n1,2\n3,4\n")

        def _must_not_hash(_a: str, _b: str) -> bool:
            raise AssertionError("hash comparison ran on a size mismatch")

        monkeypatch.setattr(CL, "files_content_identical", _must_not_hash)
        result = CL.classify_outputs([str(canonical), str(backup)])
        rec = _record(result, "run_deprecated.csv")
        assert rec["is_archival"] is False
        assert "sizes differ" in rec["reason"]

    def test_mtime_relation_reported(self, tmp_path: Path) -> None:
        import os
        body = b"x\n1\n"
        canonical = tmp_path / "a.csv"
        backup = tmp_path / "a_copy.csv"
        canonical.write_bytes(body)
        backup.write_bytes(body)
        os.utime(canonical, (1_000_000, 1_000_000))
        os.utime(backup, (2_000_000, 2_000_000))

        rec = _record(CL.classify_outputs([str(canonical), str(backup)]), "a_copy.csv")
        assert rec["is_archival"] is True
        assert "newer than that twin" in rec["reason"]


class TestDelegatedConvention:
    def test_old_suffix_still_uses_outputs_local_verdict(self, tmp_path: Path) -> None:
        body = b"x,y\n1,2\n"
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_bytes(body)
        archival.write_bytes(body)

        result = CL.classify_outputs([str(archival), str(canonical)])
        assert result["total"] == 2
        assert [c["path"] for c in result["classifications"]] == sorted(
            [str(archival), str(canonical)]
        )
        rec = _record(result, "run_old.csv")
        assert rec["is_archival"] is True
        assert "SHA-256 identical" in rec["reason"]
        assert rec["size"] == len(body)

    def test_guess_not_in_batch_is_reported(self, tmp_path: Path) -> None:
        backup = tmp_path / "solo.csv.bak"
        backup.write_bytes(b"x")
        rec = _record(CL.classify_outputs([str(backup)]), "solo.csv.bak")
        assert rec["is_archival"] is False
        assert "not present in this batch" in rec["reason"]
