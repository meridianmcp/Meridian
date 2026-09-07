"""6507e83a (C84-W3, category 8 residual) -- orphaned staged-DOCX file
detection for meridian/doc_store.py's OWN write path
(``meridian.doc_store.find_orphaned_docx_staged_files``).

Mirrors ``tests/test_mde3_docx_orphan_staged_files.py`` (which covers
extensions/meridian-docs/meridian_docs/docs_intel.py's identically-named
function) test-for-test: doc_store.py's ``_write_docx_transaction`` stages to
the exact same ``.meridian-docx-stage-*.tmp`` naming convention
(``tempfile.mkstemp(prefix=".meridian-docx-stage-", suffix=".tmp", ...)``)
but, before this item, had no detection function of its own -- a crash
during a doc_store.py-driven write (update_paragraph, link_figure_caption,
...) was invisible to every existing tool, including docs_intel.py's
detector (which only looks for its OWN write pipeline's leftovers and has no
reason to be pointed at a doc_store-managed directory).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from meridian import doc_store


def _touch_stage_file(directory: Path, suffix: str = "", age_seconds: float = 0.0) -> Path:
    name = f".meridian-docx-stage-{suffix or 'abc123'}.tmp"
    p = directory / name
    p.write_bytes(b"partial docx bytes")
    if age_seconds:
        old_time = time.time() - age_seconds
        os.utime(p, (old_time, old_time))
    return p


class TestDocStoreFindOrphanedDocxStagedFiles:
    def test_missing_directory_returns_empty(self):
        assert doc_store.find_orphaned_docx_staged_files("/definitely/does/not/exist") == []

    def test_empty_directory_returns_empty(self, tmp_path):
        assert doc_store.find_orphaned_docx_staged_files(str(tmp_path)) == []

    def test_finds_a_staged_file(self, tmp_path):
        _touch_stage_file(tmp_path)
        results = doc_store.find_orphaned_docx_staged_files(str(tmp_path))
        assert len(results) == 1
        assert results[0]["path"].endswith(".tmp")
        assert results[0]["size_bytes"] > 0

    def test_ignores_unrelated_files(self, tmp_path):
        (tmp_path / "report.docx").write_bytes(b"real docx")
        (tmp_path / "notes.txt").write_text("hello")
        (tmp_path / "other.tmp").write_bytes(b"unrelated tmp file")
        assert doc_store.find_orphaned_docx_staged_files(str(tmp_path)) == []

    def test_exact_prefix_and_suffix_match_required(self, tmp_path):
        (tmp_path / "meridian-docx-stage-x.tmp").write_bytes(b"missing leading dot")
        (tmp_path / ".meridian-docx-stage-x.txt").write_bytes(b"wrong suffix")
        assert doc_store.find_orphaned_docx_staged_files(str(tmp_path)) == []

    def test_fresh_staged_file_is_not_flagged_as_likely_orphan(self, tmp_path):
        _touch_stage_file(tmp_path, age_seconds=0.0)
        results = doc_store.find_orphaned_docx_staged_files(str(tmp_path), max_age_seconds=3600)
        assert len(results) == 1
        assert results[0]["likely_orphan"] is False

    def test_old_staged_file_is_flagged_as_likely_orphan(self, tmp_path):
        _touch_stage_file(tmp_path, age_seconds=7200)
        results = doc_store.find_orphaned_docx_staged_files(str(tmp_path), max_age_seconds=3600)
        assert len(results) == 1
        assert results[0]["likely_orphan"] is True
        assert results[0]["age_seconds"] >= 7200 - 5  # small clock-tolerance

    def test_multiple_files_sorted_oldest_first(self, tmp_path):
        _touch_stage_file(tmp_path, suffix="new", age_seconds=10)
        _touch_stage_file(tmp_path, suffix="old", age_seconds=10000)
        results = doc_store.find_orphaned_docx_staged_files(str(tmp_path))
        assert len(results) == 2
        assert "old" in results[0]["path"]
        assert "new" in results[1]["path"]

    def test_never_deletes_or_modifies_anything(self, tmp_path):
        p = _touch_stage_file(tmp_path)
        original_bytes = p.read_bytes()
        doc_store.find_orphaned_docx_staged_files(str(tmp_path))
        assert p.exists()
        assert p.read_bytes() == original_bytes

    def test_matches_the_literal_prefix_write_docx_transaction_uses(self, tmp_path):
        """Guard against the two constants drifting apart silently: the
        detection prefix/suffix must match exactly what
        _write_docx_transaction's own tempfile.mkstemp call uses, or a real
        orphaned file would never be detected."""
        assert doc_store._DOCX_STAGE_PREFIX == ".meridian-docx-stage-"
        assert doc_store._DOCX_STAGE_SUFFIX == ".tmp"

    def test_matches_docs_intel_extensions_naming_convention(self, tmp_path):
        """Both write pipelines (doc_store.py and the meridian-docs extension's
        docs_intel.py) must agree on the SAME staging naming convention --
        that shared convention is exactly what lets a single directory scan
        catch orphans from either writer."""
        import sys

        sys.path.insert(
            0, str(Path(__file__).parent.parent / "extensions" / "meridian-docs")
        )
        from meridian_docs import docs_intel  # noqa: E402

        assert doc_store._DOCX_STAGE_PREFIX == docs_intel._DOCX_STAGE_PREFIX
        assert doc_store._DOCX_STAGE_SUFFIX == docs_intel._DOCX_STAGE_SUFFIX
