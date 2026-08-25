"""MDE-3 -- orphaned staged-DOCX file detection (extensions/meridian-docs/
meridian_docs/docs_intel.py:find_orphaned_docx_staged_files).

Closes the confirmed "CRASH RECOVERY" gap this repo's own C84-W1 gap-matrix
note documents for _atomic_write_docx_bytes: a crash between STAGE and
PROMOTE leaves an orphaned ``.meridian-docx-stage-*.tmp`` file in dest's
directory forever, and nothing previously scanned for it.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "extensions" / "meridian-docs"))

from meridian_docs import docs_intel  # noqa: E402


def _touch_stage_file(directory: Path, suffix: str = "", age_seconds: float = 0.0) -> Path:
    name = f".meridian-docx-stage-{suffix or 'abc123'}.tmp"
    p = directory / name
    p.write_bytes(b"partial docx bytes")
    if age_seconds:
        old_time = time.time() - age_seconds
        import os
        os.utime(p, (old_time, old_time))
    return p


class TestFindOrphanedDocxStagedFiles:
    def test_missing_directory_returns_empty(self):
        assert docs_intel.find_orphaned_docx_staged_files("/definitely/does/not/exist") == []

    def test_empty_directory_returns_empty(self, tmp_path):
        assert docs_intel.find_orphaned_docx_staged_files(str(tmp_path)) == []

    def test_finds_a_staged_file(self, tmp_path):
        _touch_stage_file(tmp_path)
        results = docs_intel.find_orphaned_docx_staged_files(str(tmp_path))
        assert len(results) == 1
        assert results[0]["path"].endswith(".tmp")
        assert results[0]["size_bytes"] > 0

    def test_ignores_unrelated_files(self, tmp_path):
        (tmp_path / "report.docx").write_bytes(b"real docx")
        (tmp_path / "notes.txt").write_text("hello")
        (tmp_path / "other.tmp").write_bytes(b"unrelated tmp file")
        assert docs_intel.find_orphaned_docx_staged_files(str(tmp_path)) == []

    def test_exact_prefix_and_suffix_match_required(self, tmp_path):
        (tmp_path / "meridian-docx-stage-x.tmp").write_bytes(b"missing leading dot")
        (tmp_path / ".meridian-docx-stage-x.txt").write_bytes(b"wrong suffix")
        assert docs_intel.find_orphaned_docx_staged_files(str(tmp_path)) == []

    def test_fresh_staged_file_is_not_flagged_as_likely_orphan(self, tmp_path):
        _touch_stage_file(tmp_path, age_seconds=0.0)
        results = docs_intel.find_orphaned_docx_staged_files(str(tmp_path), max_age_seconds=3600)
        assert len(results) == 1
        assert results[0]["likely_orphan"] is False

    def test_old_staged_file_is_flagged_as_likely_orphan(self, tmp_path):
        _touch_stage_file(tmp_path, age_seconds=7200)
        results = docs_intel.find_orphaned_docx_staged_files(str(tmp_path), max_age_seconds=3600)
        assert len(results) == 1
        assert results[0]["likely_orphan"] is True
        assert results[0]["age_seconds"] >= 7200 - 5  # small clock-tolerance

    def test_multiple_files_sorted_oldest_first(self, tmp_path):
        _touch_stage_file(tmp_path, suffix="new", age_seconds=10)
        _touch_stage_file(tmp_path, suffix="old", age_seconds=10000)
        results = docs_intel.find_orphaned_docx_staged_files(str(tmp_path))
        assert len(results) == 2
        assert "old" in results[0]["path"]
        assert "new" in results[1]["path"]

    def test_never_deletes_or_modifies_anything(self, tmp_path):
        p = _touch_stage_file(tmp_path)
        original_bytes = p.read_bytes()
        docs_intel.find_orphaned_docx_staged_files(str(tmp_path))
        assert p.exists()
        assert p.read_bytes() == original_bytes

    def test_matches_the_literal_prefix_atomic_write_docx_bytes_uses(self, tmp_path):
        """Guard against the two constants drifting apart silently: the
        detection prefix/suffix must match exactly what
        _atomic_write_docx_bytes's own tempfile.NamedTemporaryFile call
        uses, or a real orphaned file would never be detected."""
        assert docs_intel._DOCX_STAGE_PREFIX == ".meridian-docx-stage-"
        assert docs_intel._DOCX_STAGE_SUFFIX == ".tmp"
