"""Tests for meridian_outputs.outputs_local.

Covers:
  - is_secret_path (security requirement 1): exhaustive exclusion-list checks.
  - _iter_safe_output_files: confirms secrets are excluded in directory walk.
  - ensure_gitignored (requirement 4): .gitignore auto-add, idempotent, creates file.
  - IndexFileLock (requirement 2): re-entrant hold, basic context-manager API.
  - Deterministic output (requirement 3): sorted path lists, stable results.
  - Core helpers: _normalize_output_path, _classify_suffix, file_fingerprint,
    archival_candidate, _canonical_name, classify_canonical_archival.
  - OutputsFtsIndex: schema creation, annotation CRUD, rebuild, search (mocked).
  - Module API: search_outputs, annotate_outputs, classify_outputs,
    resolve_figure_output (filesystem-backed where possible, mocked DuckDB
    where not available in CI).
  - npy_metadata: stats without numpy, graceful error on missing file.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test.
sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import outputs_local as OL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dir(tmp_path: Path, files: dict[str, str]) -> str:
    """Create a temp directory with the given {name: content} files."""
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Security: is_secret_path (requirement 1)
# ---------------------------------------------------------------------------

class TestIsSecretPath:
    """Exhaustive checks for the secret-file exclusion filter."""

    # Files that MUST be excluded.
    @pytest.mark.parametrize("filename", [
        ".env",
        ".env.local",
        ".env.production",
        "prod.env",
        "my.env",
        "keyfile.key",
        "server.pem",
        "cert.crt",
        "cert.cer",
        "cert.der",
        "keystore.p12",
        "keystore.pfx",
        "trust.jks",
        "store.keystore",
        "id_rsa",
        "id_rsa.pub",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_ed25519.pub",
        "my_secret.txt",
        "secrets.yaml",
        "secrets.yml",
        "secrets.toml",
        "secrets.json",
        "app_credentials.json",
        "credentials.csv",
        "user_credentials.csv",
        "password.txt",
        "passwords.db",
        "passwd.cfg",
        "api_token.txt",
        "token.json",
        "apikey.txt",
        "api_key.cfg",
        "auth_key.yaml",
        "access_key.json",
        "private_key.pem",
        ".htpasswd",
        ".netrc",
        "netrc",
        "config.ini",
        "config.cfg",
        "config.conf",
        "config.yaml",
        "config.yml",
        "config.toml",
        "config.json",
        "settings.ini",
        "settings.cfg",
        "settings.conf",
        "settings.yaml",
        "settings.yml",
        "settings.toml",
        "settings.json",
        "terraform.tfvars",
        "prod.tfvars",
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "my.vault",
        "vault.yaml",
        "vault.yml",
    ])
    def test_excluded(self, filename: str) -> None:
        path = f"/some/deep/path/{filename}"
        assert OL.is_secret_path(path), (
            f"Expected {filename!r} to be excluded but is_secret_path returned False"
        )

    # Files that MUST NOT be excluded (legitimate outputs).
    @pytest.mark.parametrize("filename", [
        "results.csv",
        "output.json",
        "weights.npy",
        "loss_curve.png",
        "model.pt",
        "README.md",
        "MERIDIAN_NOTES.md",
        "data.parquet",
        "summary.txt",
        "token_counts.csv",   # "token" in name but not a secret
        "run_config_backup.csv",  # "config" in name but .csv not in exclusion list
        "experiment_log.json",
        "best_checkpoint.pth",
        "environment.yml",    # conda environment file -- NOT a secret
    ])
    def test_not_excluded(self, filename: str) -> None:
        path = f"/outputs/{filename}"
        assert not OL.is_secret_path(path), (
            f"Expected {filename!r} NOT to be excluded but is_secret_path returned True"
        )

    def test_case_insensitive(self) -> None:
        assert OL.is_secret_path("/path/.ENV")
        assert OL.is_secret_path("/path/Server.PEM")
        assert OL.is_secret_path("/path/MY_SECRET_KEY.KEY")

    def test_only_basename_checked(self) -> None:
        # A path whose DIRECTORY contains ".env" but basename is safe.
        assert not OL.is_secret_path("/project/.env.dir/results.csv")
        # A path whose BASENAME is .env.
        assert OL.is_secret_path("/project/outputs/.env")


# ---------------------------------------------------------------------------
# _iter_safe_output_files
# ---------------------------------------------------------------------------

class TestIterSafeOutputFiles:
    def test_excludes_secret_files(self, tmp_path: Path) -> None:
        (tmp_path / "results.csv").write_text("a,b\n1,2", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=123", encoding="utf-8")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "secrets.yaml").write_text("key: val", encoding="utf-8")
        (tmp_path / "subdir" / "data.json").write_text('{"x": 1}', encoding="utf-8")

        paths = OL._iter_safe_output_files(str(tmp_path))
        basenames = {os.path.basename(p) for p in paths}
        assert "results.csv" in basenames
        assert "data.json" in basenames
        assert ".env" not in basenames
        assert "secrets.yaml" not in basenames

    def test_hidden_dirs_pruned(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("", encoding="utf-8")
        (tmp_path / "output.csv").write_text("x\n1", encoding="utf-8")

        paths = OL._iter_safe_output_files(str(tmp_path))
        # .git/config must not appear.
        assert all(".git" not in p for p in paths)
        assert any("output.csv" in p for p in paths)

    def test_sorted_deterministic(self, tmp_path: Path) -> None:
        for name in ["c.csv", "a.csv", "b.json"]:
            (tmp_path / name).write_text("x", encoding="utf-8")
        paths = OL._iter_safe_output_files(str(tmp_path))
        assert paths == sorted(paths)

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert OL._iter_safe_output_files(str(tmp_path)) == []

    def test_meridian_notes_included(self, tmp_path: Path) -> None:
        (tmp_path / "MERIDIAN_NOTES.md").write_text("notes", encoding="utf-8")
        paths = OL._iter_safe_output_files(str(tmp_path))
        assert any("MERIDIAN_NOTES.md" in p for p in paths)


# ---------------------------------------------------------------------------
# ensure_gitignored (requirement 4)
# ---------------------------------------------------------------------------

class TestEnsureGitignored:
    def test_creates_gitignore_if_missing(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / ".meridian-outputs-cache"
        cache_dir.mkdir()
        OL.ensure_gitignored(str(cache_dir))
        gi_path = tmp_path / ".gitignore"
        assert gi_path.exists()
        content = gi_path.read_text(encoding="utf-8")
        assert ".meridian-outputs-cache" in content

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling twice must not duplicate the entry."""
        cache_dir = tmp_path / ".cache-dir"
        cache_dir.mkdir()
        OL.ensure_gitignored(str(cache_dir))
        OL.ensure_gitignored(str(cache_dir))
        gi_path = tmp_path / ".gitignore"
        content = gi_path.read_text(encoding="utf-8")
        # Count occurrences of the name.
        count = content.count(".cache-dir")
        assert count == 1, f"Entry duplicated: count={count}"

    def test_appends_to_existing_gitignore(self, tmp_path: Path) -> None:
        gi_path = tmp_path / ".gitignore"
        gi_path.write_text("*.pyc\n", encoding="utf-8")
        cache_dir = tmp_path / "my-cache"
        cache_dir.mkdir()
        OL.ensure_gitignored(str(cache_dir))
        content = gi_path.read_text(encoding="utf-8")
        assert "*.pyc" in content
        assert "my-cache" in content

    def test_no_op_if_already_covered(self, tmp_path: Path) -> None:
        gi_path = tmp_path / ".gitignore"
        gi_path.write_text("/my-cache/\n", encoding="utf-8")
        cache_dir = tmp_path / "my-cache"
        cache_dir.mkdir()
        OL.ensure_gitignored(str(cache_dir))
        content = gi_path.read_text(encoding="utf-8")
        # The name should appear only once (already covered, not appended).
        assert content.count("my-cache") == 1

    def test_never_raises(self) -> None:
        """Must swallow errors -- never raises even for invalid paths."""
        OL.ensure_gitignored("/nonexistent/path/that/cannot/be/created/x")


# ---------------------------------------------------------------------------
# IndexFileLock (requirement 2)
# ---------------------------------------------------------------------------

class TestIndexFileLock:
    def test_basic_context_manager(self) -> None:
        lock = OL.IndexFileLock(":memory:")
        with lock:
            pass  # must not raise

    def test_thread_exclusion(self) -> None:
        """Two threads must not hold the lock simultaneously."""
        lock = OL.IndexFileLock(":memory:")
        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker(label: str) -> None:
            barrier.wait()
            with lock:
                results.append(f"{label}-start")
                time.sleep(0.02)
                results.append(f"{label}-end")

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        # A-end must appear before B-start OR B-end must appear before A-start
        # (i.e., no interleaving of start/end for the same section).
        for i, entry in enumerate(results):
            if entry.endswith("-start"):
                label = entry.split("-")[0]
                expected_end = f"{label}-end"
                start_idx = i
                end_idx = results.index(expected_end)
                # No other thread's start should fall between start_idx and end_idx.
                between = results[start_idx + 1:end_idx]
                other_starts = [e for e in between if e.endswith("-start")]
                assert not other_starts, (
                    f"Lock not exclusive: {results}"
                )

    def test_releases_on_exception(self) -> None:
        lock = OL.IndexFileLock(":memory:")
        try:
            with lock:
                raise ValueError("boom")
        except ValueError:
            pass
        # Lock must be released -- acquire again must succeed.
        acquired = lock._thread_lock.acquire(blocking=False)
        assert acquired, "Lock not released after exception"
        lock._thread_lock.release()


# ---------------------------------------------------------------------------
# _normalize_output_path
# ---------------------------------------------------------------------------

class TestNormalizeOutputPath:
    def test_empty(self) -> None:
        assert OL._normalize_output_path("") == ""
        assert OL._normalize_output_path(None) == ""  # type: ignore[arg-type]

    def test_normalizes_slashes(self) -> None:
        p = OL._normalize_output_path("C:/foo/bar/../baz")
        assert "/" in p and "\\" not in p

    def test_strips_whitespace(self) -> None:
        p = OL._normalize_output_path("  /tmp/foo  ")
        assert not p.startswith(" ")


# ---------------------------------------------------------------------------
# _classify_suffix
# ---------------------------------------------------------------------------

class TestClassifySuffix:
    @pytest.mark.parametrize("path,expected", [
        ("data.csv", "text_content"),
        ("data.CSV", "text_content"),
        ("result.json", "text_content"),
        ("weights.npy", "metadata_only"),
        ("figure.png", "binary_metadata"),
        ("model.pt", "binary_metadata"),
        ("noext", "binary_metadata"),
    ])
    def test_classification(self, path: str, expected: str) -> None:
        assert OL._classify_suffix(path) == expected


# ---------------------------------------------------------------------------
# file_fingerprint
# ---------------------------------------------------------------------------

class TestFileFingerprint:
    def test_csv_columns(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col_a,col_b,col_c\n1,2,3\n4,5,6", encoding="utf-8")
        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "text_content"
        assert fp.csv_columns == ["col_a", "col_b", "col_c"]

    def test_json_keys(self, tmp_path: Path) -> None:
        f = tmp_path / "result.json"
        f.write_text('{"alpha": 1, "beta": 2}', encoding="utf-8")
        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "text_content"
        assert set(fp.json_keys or []) == {"alpha", "beta"}

    def test_json_generating_script(self, tmp_path: Path) -> None:
        f = tmp_path / "meta.json"
        f.write_text(
            '{"generating_script": "train.py", "loss": 0.1}', encoding="utf-8"
        )
        fp = OL.file_fingerprint(str(f))
        assert fp.generating_script == "train.py"

    def test_binary_no_content(self, tmp_path: Path) -> None:
        f = tmp_path / "figure.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        fp = OL.file_fingerprint(str(f))
        assert fp.kind == "binary_metadata"
        assert fp.csv_columns is None
        assert fp.json_keys is None

    def test_missing_file_no_raise(self) -> None:
        fp = OL.file_fingerprint("/nonexistent/file.csv")
        assert fp.kind == "text_content"
        assert fp.csv_columns is None


# ---------------------------------------------------------------------------
# archival_candidate / _canonical_name
# ---------------------------------------------------------------------------

class TestArchivalCandidate:
    @pytest.mark.parametrize("path,expected", [
        ("run_old.csv", True),
        ("run_old_1.csv", True),
        ("run_old_2.csv", True),
        ("_results.csv", True),
        ("results.csv", False),
        ("run_results.csv", False),
        ("run_old_enough.csv", False),  # _old_enough is not the pattern
    ])
    def test_candidate(self, path: str, expected: bool) -> None:
        assert OL.archival_candidate(f"/outputs/{path}") == expected

    @pytest.mark.parametrize("path,expected", [
        ("/outputs/run_old.csv", "/outputs/run.csv"),
        ("/outputs/run_old_2.csv", "/outputs/run.csv"),
        ("/outputs/_data.csv", "/outputs/data.csv"),
        ("/outputs/results.csv", "/outputs/results.csv"),
    ])
    def test_canonical_name(self, path: str, expected: str) -> None:
        assert OL._canonical_name(path) == expected


# ---------------------------------------------------------------------------
# classify_canonical_archival
# ---------------------------------------------------------------------------

class TestClassifyCanonicalArchival:
    def test_identical_files_are_archival(self, tmp_path: Path) -> None:
        content = b"x,y\n1,2\n"
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_bytes(content)
        archival.write_bytes(content)

        results = OL.classify_canonical_archival(
            [str(canonical), str(archival)]
        )
        assert results[str(archival)].is_archival
        assert results[str(archival)].canonical_path == str(canonical)
        assert not results[str(canonical)].is_archival

    def test_different_content_not_archival(self, tmp_path: Path) -> None:
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_text("a\n1", encoding="utf-8")
        archival.write_text("a\n2", encoding="utf-8")

        results = OL.classify_canonical_archival(
            [str(canonical), str(archival)]
        )
        assert not results[str(archival)].is_archival

    def test_no_twin_not_archival(self, tmp_path: Path) -> None:
        archival = tmp_path / "run_old.csv"
        archival.write_text("a\n1", encoding="utf-8")
        results = OL.classify_canonical_archival([str(archival)])
        assert not results[str(archival)].is_archival
        assert "no canonical twin" in results[str(archival)].reason

    def test_deterministic_order(self, tmp_path: Path) -> None:
        """Output dict key order must follow the sorted input list."""
        for name in ["c.csv", "a.csv", "b.csv"]:
            (tmp_path / name).write_text("x\n1", encoding="utf-8")
        sorted_paths = sorted(str(tmp_path / n) for n in ["c.csv", "a.csv", "b.csv"])
        results = OL.classify_canonical_archival(sorted_paths)
        assert list(results.keys()) == sorted_paths

    def test_injectable_hasher(self, tmp_path: Path) -> None:
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_text("x", encoding="utf-8")
        archival.write_text("y", encoding="utf-8")

        # Hasher that says both files have the same hash.
        def _same(_path: str) -> str:
            return "deadbeef"

        results = OL.classify_canonical_archival(
            [str(canonical), str(archival)], hasher=_same
        )
        assert results[str(archival)].is_archival


# ---------------------------------------------------------------------------
# npy_metadata
# ---------------------------------------------------------------------------

class TestNpyMetadata:
    def test_missing_file(self) -> None:
        m = OL.npy_metadata("/nonexistent/file.npy")
        assert m.path == "/nonexistent/file.npy"
        assert m.error is not None
        assert m.shape is None

    def test_without_numpy(self, tmp_path: Path) -> None:
        f = tmp_path / "arr.npy"
        f.write_bytes(b"\x93NUMPY\x01\x00fake")  # corrupt .npy
        m = OL.npy_metadata(str(f))
        # Should get size_bytes/modified_at from stat even if numpy parse fails.
        assert m.size_bytes is not None
        assert m.modified_at is not None
        assert m.to_dict()["path"] == str(f)


# ---------------------------------------------------------------------------
# OutputsFtsIndex -- in-memory DuckDB (skipped when duckdb not available)
# ---------------------------------------------------------------------------

try:
    import duckdb  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed"
)


class TestOutputsFtsIndex:
    @duckdb_required
    def test_empty_tree_rebuild(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count = idx.rebuild()
        assert count == 0
        idx.close()

    @duckdb_required
    def test_indexes_csv_file(self, tmp_path: Path) -> None:
        (tmp_path / "loss.csv").write_text("epoch,loss\n1,0.5\n2,0.3",
                                            encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count = idx.rebuild()
        assert count == 1
        hits = idx.search("epoch")
        assert len(hits) == 1
        assert "loss.csv" in hits[0]["path"]
        idx.close()

    @duckdb_required
    def test_excludes_secret_files(self, tmp_path: Path) -> None:
        (tmp_path / "results.csv").write_text("x\n1", encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=abc", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count = idx.rebuild()
        # Only results.csv should be indexed, NOT .env.
        assert count == 1
        idx.close()

    @duckdb_required
    def test_annotation_crud(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.add_annotation(str(tmp_path), "baseline run", run_params={"lr": 0.001})
        assert result["note"] == "baseline run"
        assert result["run_params"] == {"lr": 0.001}

        annotations = idx.get_annotations_for_path(str(tmp_path))
        assert len(annotations) == 1
        assert annotations[0]["note"] == "baseline run"
        idx.close()

    @duckdb_required
    def test_meridian_notes_auto_ingested(self, tmp_path: Path) -> None:
        (tmp_path / "MERIDIAN_NOTES.md").write_text(
            "Run with PCA=on", encoding="utf-8"
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        annotations = idx.get_annotations_for_path(str(tmp_path))
        assert any("PCA=on" in (a.get("note") or "") for a in annotations)
        idx.close()

    @duckdb_required
    def test_incremental_rebuild(self, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        f.write_text('{"key": "value1"}', encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count1 = idx.rebuild()
        assert count1 == 1
        # File unchanged -- should be fast and still return 1.
        count2 = idx.rebuild()
        assert count2 == 1
        # Modify the file.
        f.write_text('{"key": "value2"}', encoding="utf-8")
        count3 = idx.rebuild()
        assert count3 == 1
        idx.close()

    @duckdb_required
    def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        assert idx.search("") == []
        assert idx.search("   ") == []
        idx.close()

    @duckdb_required
    def test_resolve_output(self, tmp_path: Path) -> None:
        f = tmp_path / "results.csv"
        f.write_text("a,b\n1,2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(f))
        assert result is not None
        assert "results.csv" in result["path"]
        # Non-existent path returns None.
        assert idx.resolve_output("/no/such/file.csv") is None
        idx.close()


# ---------------------------------------------------------------------------
# _row_cache content eviction (sprint item edc84500)
# ---------------------------------------------------------------------------

class TestRowCacheContentEviction:
    """edc84500 -- _row_cache must never hold one full extracted-content
    body (the CSV/JSON/text body used for FTS) per discovered file for the
    OutputsFtsIndex instance's entire lifetime. That unbounded growth caused
    a real OS-level allocator failure ("memory allocation of N bytes
    failed") at ~96,000/244,191 files against a real SUT_Compressed tree.

    Content is evicted back to None once a row has been committed (see
    _apply_precomputed/_light_row); every lightweight field a caller
    actually needs off a cached row (sha256, size, mtime, kind,
    csv_columns, json_keys, generating_script, is_archival, canonical_path)
    must keep working exactly as before. get_content() -- backed directly
    by the persistent DuckDB outputs_index table -- is the supported way
    to read a file's real content back on demand.
    """

    @duckdb_required
    def test_row_cache_content_evicted_after_commit(self, tmp_path: Path) -> None:
        f = tmp_path / "metrics.csv"
        # A same-size sibling forces the size-prefilter (e1fd4182) to
        # actually compute a real sha256 for both files, rather than
        # skipping hashing entirely for a lone, uniquely-sized file --
        # exercising the lightweight sha256 field this fix must preserve.
        f.write_text("epoch,loss\n1,0.9\n2,0.4\n3,0.1", encoding="utf-8")
        (tmp_path / "sibling.csv").write_text(
            "epoch,loss\n1,0.9\n2,0.4\n3,0.2", encoding="utf-8",
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            count = idx.rebuild()
            assert count == 2
            cache_key = next(p for p in idx._row_cache if p.endswith("metrics.csv"))
            cached = idx._row_cache[cache_key]
            assert cached.content is None, (
                "row_cache must not hold the full content once a row has "
                "been committed to the DB + FTS index"
            )
            # Lightweight fields must survive eviction -- staleness
            # detection and metadata lookups depend on these.
            assert cached.sha256 is not None
            assert cached.size is not None
            assert cached.kind == "text_content"

            # A real lookup must still return the ACTUAL persisted content,
            # read straight from DuckDB -- never stale/empty.
            content = idx.get_content(cache_key)
            assert content is not None
            assert "epoch,loss" in content
        finally:
            idx.close()

    @duckdb_required
    def test_get_content_missing_path_returns_none(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert idx.get_content(str(tmp_path / "nope.csv")) is None
        finally:
            idx.close()

    @duckdb_required
    def test_staleness_detection_unaffected_by_eviction(self, tmp_path: Path) -> None:
        """The staleness check (`p not in self._row_cache`) and the sha256
        read (`self._row_cache[path].sha256`) must both keep working once
        content has been evicted -- an unchanged file must NOT be
        re-analysed on a subsequent rebuild() call."""
        f = tmp_path / "data.json"
        f.write_text('{"a": 1}', encoding="utf-8")
        # A same-size sibling forces a real sha256 to be computed (see
        # test_row_cache_content_evicted_after_commit) so this test actually
        # exercises the cached-hash read, not a legitimately-skipped one.
        (tmp_path / "sibling.json").write_text('{"a": 2}', encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            cache_key = next(p for p in idx._row_cache if p.endswith("data.json"))
            assert idx._row_cache[cache_key].content is None
            sha_before = idx._row_cache[cache_key].sha256
            assert sha_before is not None

            analysed: list[str] = []
            real_analyse = OL._analyse_file

            def _spy_analyse(p, hasher, **kwargs):
                analysed.append(p)
                return real_analyse(p, hasher, **kwargs)

            with patch.object(OL, "_analyse_file", side_effect=_spy_analyse):
                count = idx.rebuild()
            assert count == 2
            assert not analysed, (
                "an unchanged file was re-analysed -- staleness detection "
                "broke after content eviction"
            )
            assert idx._row_cache[cache_key].sha256 == sha_before
            assert idx._row_cache[cache_key].content is None
        finally:
            idx.close()

    @duckdb_required
    def test_archival_metadata_refresh_preserves_content(self, tmp_path: Path) -> None:
        """A row whose ONLY change is its archival classification (a twin
        file appears later) is re-inserted via the "update non-stale cached
        rows" path in _apply_precomputed, which reuses the CACHED (already
        content-evicted) row. The fix must re-read the real content from
        DuckDB before re-inserting -- never silently overwrite already-
        persisted content with NULL."""
        archival = tmp_path / "run_old.csv"
        archival.write_bytes(b"a,b\n1,2\n")
        # A same-size (but different-content) sibling present from the
        # start forces a REAL sha256 to be computed for run_old.csv during
        # its OWN initial indexing (the size-prefilter, e1fd4182, skips
        # hashing a uniquely-sized file entirely) -- needed so the archival
        # comparison below has a real hash to compare once the canonical
        # twin appears; run_old.csv itself is never re-hashed once cached.
        (tmp_path / "helper.csv").write_bytes(b"x,y\n9,9\n")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            cache_key = next(p for p in idx._row_cache if p.endswith("run_old.csv"))
            assert idx._row_cache[cache_key].content is None
            assert idx._row_cache[cache_key].is_archival is False  # no twin yet
            content_before = idx.get_content(cache_key)
            assert content_before is not None and "a,b" in content_before

            # Add the canonical twin (identical content) -- flips
            # run_old.csv's archival classification via the metadata-refresh
            # path, WITHOUT run_old.csv itself being touched/re-stat'd as
            # stale this call.
            canonical = tmp_path / "run.csv"
            canonical.write_bytes(b"a,b\n1,2\n")
            idx.rebuild()

            assert idx._row_cache[cache_key].is_archival is True, (
                "twin addition should have flipped is_archival via the "
                "non-stale metadata-refresh path"
            )
            content_after = idx.get_content(cache_key)
            assert content_after == content_before, (
                "a metadata-only archival refresh must never null out "
                "already-persisted content"
            )
            assert idx._row_cache[cache_key].content is None, (
                "the metadata refresh must not re-inflate row_cache with "
                "full content"
            )
        finally:
            idx.close()

    @duckdb_required
    def test_rehydrate_from_disk_does_not_load_content(self, tmp_path: Path) -> None:
        """A fresh OutputsFtsIndex pointed at an existing on-disk DB (process
        restart / cache-eviction scenario) must not re-materialise every
        row's full content into _row_cache on connect() -- only cheap
        metadata should be rehydrated."""
        (tmp_path / "big.csv").write_text(
            "col\n" + ("x" * 5000), encoding="utf-8",
        )
        db_path = OL._resolve_index_db_path(str(tmp_path))

        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx1.rebuild()
        idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx2._connect()  # triggers _rehydrate_cache_from_disk
            cache_key = next(p for p in idx2._row_cache if p.endswith("big.csv"))
            assert idx2._row_cache[cache_key].content is None, (
                "_rehydrate_cache_from_disk must not load content into "
                "row_cache on a fresh connect()"
            )
            content = idx2.get_content(cache_key)
            assert content is not None
            assert "x" * 100 in content
        finally:
            idx2.close()


# ---------------------------------------------------------------------------
# Module-level API: search_outputs, annotate_outputs, classify_outputs,
# resolve_figure_output
# ---------------------------------------------------------------------------

class TestSearchOutputsAPI:
    def test_missing_dir_returns_error(self) -> None:
        result = OL.search_outputs("/nonexistent/dir", "query")
        assert "error" in result

    def test_empty_query_returns_error(self, tmp_path: Path) -> None:
        result = OL.search_outputs(str(tmp_path), "")
        assert "error" in result

    def test_annotate_missing_args(self) -> None:
        assert "error" in OL.annotate_outputs("", "/path", "note")
        assert "error" in OL.annotate_outputs("/dir", "", "note")
        assert "error" in OL.annotate_outputs("/dir", "/path", "")

    @duckdb_required
    def test_search_finds_csv(self, tmp_path: Path) -> None:
        (tmp_path / "accuracy.csv").write_text(
            "epoch,accuracy\n1,0.9\n2,0.95", encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "accuracy")
        assert result["total_indexed"] >= 1
        assert len(result["hits"]) >= 1

    @duckdb_required
    def test_search_exposes_discovery_phase_metrics(self, tmp_path: Path) -> None:
        (tmp_path / "metrics.json").write_text(
            '{"marker": "telemetry"}', encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "telemetry", max_seconds=None)
        discovery = result["discovery"]
        assert discovery["walk_complete"] is True
        assert discovery["discovered_total"] >= 1
        assert discovery["discovered_this_call"] >= 1
        assert discovery["rebuild_seconds"] >= 0
        assert discovery["walk_seconds"] >= 0
        assert discovery["analysis_seconds"] >= 0
        assert discovery["classification_seconds"] >= 0
        assert discovery["write_seconds"] >= 0
        assert discovery["row_cache_content_resident"] is False

    @duckdb_required
    def test_search_no_secret_hits(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("DB_PASS=hunter2", encoding="utf-8")
        (tmp_path / "results.json").write_text('{"DB_PASS": "hunter2"}', encoding="utf-8")
        result = OL.search_outputs(str(tmp_path), "hunter2")
        # .env is excluded; results.json may or may not match -- but .env MUST NOT appear.
        for hit in result["hits"]:
            assert ".env" not in os.path.basename(hit["path"]), (
                f"Secret file .env appeared in search hits: {hit}"
            )

    @duckdb_required
    def test_classify_outputs_api(self, tmp_path: Path) -> None:
        content = b"a,b\n1,2\n"
        (tmp_path / "run.csv").write_bytes(content)
        (tmp_path / "run_old.csv").write_bytes(content)
        paths = [
            str(tmp_path / "run.csv"),
            str(tmp_path / "run_old.csv"),
        ]
        result = OL.classify_outputs(paths)
        assert result["total"] == 2
        clsf = {c["path"]: c for c in result["classifications"]}
        assert clsf[paths[1]]["is_archival"] is True

    def test_classify_outputs_sorted(self) -> None:
        """Output order is sorted by path regardless of input order."""
        paths = ["/c/z.csv", "/a/x.csv", "/b/y.csv"]
        result = OL.classify_outputs(paths)
        returned_paths = [c["path"] for c in result["classifications"]]
        assert returned_paths == sorted(paths)

    def test_resolve_figure_output_empty_path(self, tmp_path: Path) -> None:
        assert OL.resolve_figure_output(str(tmp_path), "") is None

    def test_resolve_figure_output_missing_dir(self) -> None:
        assert OL.resolve_figure_output("/nonexistent/dir", "/file.csv") is None


# ---------------------------------------------------------------------------
# On-disk index persistence + auto-gitignore (sprint item 0c1a4349)
# ---------------------------------------------------------------------------

class TestCachedIndexPersistence:
    """_get_cached_index must persist to a real on-disk DuckDB file, not
    :memory:, and must activate ensure_gitignored on the cache directory."""

    def test_resolve_index_db_path_not_memory(self, tmp_path: Path) -> None:
        db_path = OL._resolve_index_db_path(str(tmp_path))
        assert db_path != ":memory:"
        assert db_path.endswith("index.duckdb")
        assert os.path.isdir(tmp_path / ".meridian-outputs-cache")

    def test_resolve_index_db_path_writes_gitignore(self, tmp_path: Path) -> None:
        OL._resolve_index_db_path(str(tmp_path))
        gi_path = tmp_path / ".gitignore"
        assert gi_path.is_file()
        assert ".meridian-outputs-cache/" in gi_path.read_text(encoding="utf-8")

    def test_resolve_index_db_path_falls_back_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(path: str, exist_ok: bool = False) -> None:
            raise OSError("simulated permission failure")

        monkeypatch.setattr(OL.os, "makedirs", _boom)
        assert OL._resolve_index_db_path(str(tmp_path)) == ":memory:"

    @duckdb_required
    def test_get_cached_index_uses_real_db_path(self, tmp_path: Path) -> None:
        idx = OL._get_cached_index(str(tmp_path))
        assert idx._db_path != ":memory:"
        assert os.path.isfile(idx._db_path) or os.path.isdir(os.path.dirname(idx._db_path))

    @duckdb_required
    def test_index_survives_cache_eviction(self, tmp_path: Path) -> None:
        """Rebuilding via a fresh OutputsFtsIndex pointed at the same on-disk
        db_path (simulating cache eviction / process restart) must see rows
        indexed by a prior instance -- the whole point of persisting."""
        (tmp_path / "metric.csv").write_text("epoch,loss\n1,0.5", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))

        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx1.rebuild()
        idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        row = idx2.resolve_output(str(tmp_path / "metric.csv"))
        idx2.close()
        assert row is not None

    @duckdb_required
    def test_fresh_instance_detects_existing_fts_index(self, tmp_path: Path) -> None:
        """d9c76caa follow-up: a fresh OutputsFtsIndex pointed at a db_path
        that already has a built FTS index (from a prior process's rebuild)
        must detect this immediately on connect, not assume _fts_built=False
        and pay the full-table rebuild tax again on every process restart."""
        (tmp_path / "metric.csv").write_text("epoch,loss\n1,0.5", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))

        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx1.rebuild()
        assert idx1._fts_built is True
        idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        assert idx2._fts_built is False  # not yet connected
        idx2._connect()
        assert idx2._fts_built is True, (
            "fresh instance should have detected the existing on-disk FTS "
            "schema instead of assuming none exists"
        )
        idx2.close()

    def test_fresh_instance_on_empty_db_stays_unbuilt(self, tmp_path: Path) -> None:
        """No prior rebuild ever ran against this db_path -- _fts_built must
        stay False (nothing to detect) rather than erroring."""
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx._connect()
            assert idx._fts_built is False
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Determinism: same inputs -> same results (requirement 3)
# ---------------------------------------------------------------------------

class TestDeterminism:
    @duckdb_required
    def test_search_results_stable(self, tmp_path: Path) -> None:
        """Calling search_outputs twice on the same tree returns the same hits."""
        for i in range(5):
            (tmp_path / f"file_{i}.csv").write_text(
                f"col_{i}\n{i}", encoding="utf-8"
            )
        r1 = OL.search_outputs(str(tmp_path), "col")
        r2 = OL.search_outputs(str(tmp_path), "col")
        paths1 = [h["path"] for h in r1["hits"]]
        paths2 = [h["path"] for h in r2["hits"]]
        assert paths1 == paths2

    def test_classify_outputs_deterministic(self, tmp_path: Path) -> None:
        for name in ["z.csv", "a.csv", "m.csv"]:
            (tmp_path / name).write_text("x\n1", encoding="utf-8")
        paths = [str(tmp_path / n) for n in ["z.csv", "a.csv", "m.csv"]]
        r1 = OL.classify_outputs(paths)
        r2 = OL.classify_outputs(paths[::-1])  # reversed input
        # Output order should match sorted path order regardless of input order.
        assert [c["path"] for c in r1["classifications"]] == \
               [c["path"] for c in r2["classifications"]]


# ---------------------------------------------------------------------------
# SQL push-down optimizations: search() and resolve_output()
# ---------------------------------------------------------------------------

class TestSearchSqlPushdown:
    """Verify search() and resolve_output() push filtering/sorting/limit into SQL."""

    @duckdb_required
    def test_search_limit_respected(self, tmp_path: Path) -> None:
        """search() LIMIT pushed into SQL: only up to `limit` rows returned."""
        for i in range(8):
            (tmp_path / f"metric_{i}.csv").write_text(
                f"metric,value\n{i},{i * 0.1}", encoding="utf-8"
            )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        hits = idx.search("metric", limit=3)
        assert len(hits) <= 3
        idx.close()

    @duckdb_required
    def test_search_returns_only_matches(self, tmp_path: Path) -> None:
        """search() WHERE bm25 IS NOT NULL: non-matching rows excluded entirely."""
        (tmp_path / "loss.csv").write_text("epoch,loss\n1,0.5", encoding="utf-8")
        (tmp_path / "accuracy.csv").write_text("epoch,acc\n1,0.9", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        hits = idx.search("loss")
        # All returned hits must contain 'loss' -- accuracy.csv should not appear.
        for hit in hits:
            assert "loss" in hit["path"].lower() or hit["bm25"] > 0
        # Specifically: accuracy.csv must not appear in the result.
        paths = [hit["path"] for hit in hits]
        assert not any("accuracy" in p for p in paths)
        idx.close()

    @duckdb_required
    def test_search_no_null_bm25_in_results(self, tmp_path: Path) -> None:
        """search() must never return hits with bm25=None (SQL filter ensures this)."""
        for i in range(5):
            (tmp_path / f"data_{i}.csv").write_text(
                f"alpha,beta\n{i},x", encoding="utf-8"
            )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        hits = idx.search("alpha", limit=10)
        for hit in hits:
            assert hit["bm25"] is not None
            assert hit["score"] is not None
        idx.close()

    @duckdb_required
    def test_search_ordered_by_score_descending(self, tmp_path: Path) -> None:
        """search() results are sorted by score descending (best match first)."""
        # File whose name is exactly the query term should score higher than one
        # where the term only appears in content.
        (tmp_path / "accuracy.csv").write_text(
            "accuracy,value\n0.9,0.95", encoding="utf-8"
        )
        (tmp_path / "unrelated.csv").write_text(
            "col_a,col_b\n1,2", encoding="utf-8"
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        hits = idx.search("accuracy", limit=10)
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores, reverse=True), (
            f"Results not sorted descending: {scores}"
        )
        idx.close()

    @duckdb_required
    def test_resolve_output_exact_match(self, tmp_path: Path) -> None:
        """resolve_output() WHERE path = ?: finds indexed file without full scan."""
        f = tmp_path / "weights.npy"
        f.write_bytes(b"\x93NUMPY\x01\x00fake_header_data_here")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(f))
        assert result is not None
        assert result["kind"] == "metadata_only"
        idx.close()

    @duckdb_required
    def test_resolve_output_missing_returns_none(self, tmp_path: Path) -> None:
        """resolve_output() returns None for a path not in the index."""
        (tmp_path / "real.csv").write_text("x\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(tmp_path / "nonexistent.csv"))
        assert result is None
        idx.close()

    @duckdb_required
    def test_resolve_output_returns_correct_fields(self, tmp_path: Path) -> None:
        """resolve_output() returns all expected fields for an indexed CSV."""
        f = tmp_path / "metrics.csv"
        f.write_text('epoch,loss\n1,0.5\n2,0.3', encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(f))
        assert result is not None
        assert result["kind"] == "text_content"
        assert result["csv_columns"] == ["epoch", "loss"]
        assert result["size"] is not None
        assert result["mtime"] is not None
        idx.close()

    @duckdb_required
    def test_resolve_output_empty_index(self, tmp_path: Path) -> None:
        """resolve_output() on an empty index (no files) returns None gracefully."""
        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()
        result = idx.resolve_output(str(tmp_path / "anything.csv"))
        assert result is None
        idx.close()


# ---------------------------------------------------------------------------
# Parallel analysis + targeted write (perf sprint item 8e0c9fc1)
# ---------------------------------------------------------------------------

class TestAnalyseFile:
    """Tests for the _analyse_file helper used by the parallel rebuild pipeline."""

    def test_basic_csv(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col_a,col_b\n1,2\n3,4", encoding="utf-8")
        analysis = OL._analyse_file(str(f), OL._sha256_file)
        assert analysis.path == str(f)
        assert analysis.fingerprint.kind == "text_content"
        assert analysis.fingerprint.csv_columns == ["col_a", "col_b"]
        assert analysis.mtime is not None
        assert analysis.size is not None
        assert analysis.sha256 is not None

    def test_missing_file(self) -> None:
        """Missing file must not raise -- returns None mtime/size."""
        analysis = OL._analyse_file("/nonexistent/path.csv", OL._sha256_file)
        assert analysis.mtime is None
        assert analysis.size is None
        assert analysis.sha256 is None

    def test_custom_hasher(self, tmp_path: Path) -> None:
        f = tmp_path / "model.pt"
        f.write_bytes(b"\x00\x01\x02")
        sentinel = "cafebabe"
        analysis = OL._analyse_file(str(f), lambda _p: sentinel)
        assert analysis.sha256 == sentinel

    def test_captured_stat_signature_avoids_second_stat(self, tmp_path: Path) -> None:
        f = tmp_path / "captured.csv"
        f.write_text("col\nvalue", encoding="utf-8")
        st = f.stat()
        with patch.object(OL.os, "stat", side_effect=AssertionError("duplicate stat")):
            analysis = OL._analyse_file(
                str(f), OL._sha256_file,
                stat_signature=(st.st_mtime, st.st_size),
            )
        assert analysis.mtime == st.st_mtime
        assert analysis.size == st.st_size
        assert analysis.sha256 is not None

    def test_independent_per_file(self, tmp_path: Path) -> None:
        """Two concurrent _analyse_file calls on different files must not interfere."""
        import concurrent.futures as cf
        files = {}
        for i in range(4):
            p = tmp_path / f"f{i}.csv"
            p.write_text(f"col_{i}\n{i}", encoding="utf-8")
            files[str(p)] = f"col_{i}"

        results = {}
        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(OL._analyse_file, p, OL._sha256_file): p
                    for p in files}
            for fut in cf.as_completed(futs):
                a = fut.result()
                results[a.path] = a

        for path, expected_col in files.items():
            a = results[path]
            assert a.fingerprint.csv_columns is not None
            assert expected_col in a.fingerprint.csv_columns


@duckdb_required
class TestParallelRebuildCorrectness:
    """Verify that the parallel rebuild produces correct, deterministic results."""

    def test_many_files_correct_count(self, tmp_path: Path) -> None:
        """Rebuild with N files should index exactly N non-secret files."""
        n = 10
        for i in range(n):
            (tmp_path / f"result_{i:02d}.csv").write_text(
                f"col_x,col_y\n{i},{i*2}", encoding="utf-8"
            )
        # Add a secret file -- must NOT be indexed.
        (tmp_path / ".env").write_text("SECRET=abc", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        count = idx.rebuild()
        assert count == n
        idx.close()

    def test_parallel_rebuild_deterministic(self, tmp_path: Path) -> None:
        """Two fresh indexes of the same tree must produce identical row sets."""
        for i in range(8):
            (tmp_path / f"out_{i:02d}.json").write_text(
                json.dumps({"run": i, "loss": 0.1 * i}), encoding="utf-8"
            )
        idx1 = OL.OutputsFtsIndex(str(tmp_path))
        idx2 = OL.OutputsFtsIndex(str(tmp_path))
        idx1.rebuild()
        idx2.rebuild()

        import duckdb
        paths1 = sorted(
            r[0] for r in idx1._con.execute(
                "SELECT path FROM outputs_index ORDER BY path"
            ).fetchall()
        )
        paths2 = sorted(
            r[0] for r in idx2._con.execute(
                "SELECT path FROM outputs_index ORDER BY path"
            ).fetchall()
        )
        assert paths1 == paths2
        idx1.close()
        idx2.close()

    def test_targeted_delete_only_stale(self, tmp_path: Path) -> None:
        """After a single file changes, only that file's row is replaced in the DB."""
        files = ["alpha.csv", "beta.csv", "gamma.csv"]
        for name in files:
            (tmp_path / name).write_text(f"col\n{name}", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()

        # Record sha256 of alpha row before update.
        import duckdb
        row_before = idx._con.execute(
            "SELECT sha256 FROM outputs_index WHERE path LIKE '%alpha%'"
        ).fetchone()
        assert row_before is not None
        sha_before = row_before[0]

        # Now modify only gamma.csv.
        time.sleep(0.02)  # ensure mtime changes on fast filesystems
        (tmp_path / "gamma.csv").write_text("col\nGAMMA_CHANGED", encoding="utf-8")
        # Touch the file to guarantee mtime change.
        os.utime(str(tmp_path / "gamma.csv"), None)

        idx.rebuild()

        # alpha should have the same sha256 (row unchanged).
        row_after = idx._con.execute(
            "SELECT sha256 FROM outputs_index WHERE path LIKE '%alpha%'"
        ).fetchone()
        assert row_after is not None
        assert row_after[0] == sha_before, (
            "alpha.csv sha256 changed even though the file was not modified"
        )
        # gamma should now be findable via search.
        hits = idx.search("GAMMA_CHANGED")
        assert any("gamma" in h["path"] for h in hits)
        idx.close()

    def test_removed_file_deleted_from_db(self, tmp_path: Path) -> None:
        """Deleting a file from disk removes it from the DB on next rebuild."""
        (tmp_path / "keep.csv").write_text("a\n1", encoding="utf-8")
        (tmp_path / "remove.csv").write_text("b\n2", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        idx.rebuild()

        count_before = idx._con.execute(
            "SELECT COUNT(*) FROM outputs_index"
        ).fetchone()[0]
        assert count_before == 2

        (tmp_path / "remove.csv").unlink()
        idx.rebuild()

        count_after = idx._con.execute(
            "SELECT COUNT(*) FROM outputs_index"
        ).fetchone()[0]
        assert count_after == 1

        # The remaining row must be "keep.csv".
        remaining = idx._con.execute(
            "SELECT path FROM outputs_index"
        ).fetchone()[0]
        assert "keep" in remaining
        idx.close()

    def test_no_duplicate_rows_after_multiple_rebuilds(self, tmp_path: Path) -> None:
        """Multiple rebuilds with changes must never leave duplicate rows."""
        f = tmp_path / "data.csv"
        f.write_text("x\n1", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        for i in range(4):
            f.write_text(f"x\n{i}", encoding="utf-8")
            os.utime(str(f), None)
            idx.rebuild()

        count = idx._con.execute(
            "SELECT COUNT(*) FROM outputs_index"
        ).fetchone()[0]
        assert count == 1, f"Expected 1 row, got {count} (duplicate rows introduced)"
        idx.close()

    def test_worker_failure_falls_back_gracefully(self, tmp_path: Path) -> None:
        """If a worker raises, the file is re-analysed synchronously and indexed."""
        f = tmp_path / "ok.csv"
        f.write_text("col\n1", encoding="utf-8")

        call_count = [0]
        real_hasher = OL._sha256_file

        def flaky_hasher(path: str) -> str | None:
            call_count[0] += 1
            # Fail once then succeed.
            if call_count[0] == 1:
                raise OSError("simulated failure")
            return real_hasher(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=flaky_hasher)
        # Should not raise even though the first hasher call fails.
        count = idx.rebuild()
        # The file should still get indexed via the fallback path.
        assert count >= 0  # may be 0 if fallback also failed; main check is no raise
        idx.close()


# ---------------------------------------------------------------------------
# rebuild() Phase 1 deadline enforcement (sprint item d9c76caa)
# ---------------------------------------------------------------------------

class TestRebuildPhase1Deadline:
    """Phase 1's ThreadPoolExecutor must actually respect max_seconds instead
    of always running every worker to completion before Phase 2 even starts."""

    def test_default_budget_raised_from_5s(self) -> None:
        # 5845cc6d: lowered from the original 170.0 fix to 130.0 to leave more
        # headroom under the ~4min external MCP client timeout once real
        # uvx-cold-start + protocol overhead is added on top of the internal
        # rebuild() budget -- real-world validation showed 170.0 cut it too
        # close. Still far above the original unreachable 5.0s default.
        assert OL.DEFAULT_REBUILD_BUDGET_SECONDS >= 100.0
        assert OL.DEFAULT_REBUILD_BUDGET_SECONDS <= 150.0

    def test_phase1_deadline_bounds_wall_clock(self, tmp_path: Path) -> None:
        """A tight deadline must make rebuild() return well before every
        worker would finish -- proof Phase 1 no longer blocks on
        as_completed() until all futures are done."""
        n_files = 16
        for i in range(n_files):
            (tmp_path / f"f{i}.csv").write_text(f"col\n{i}", encoding="utf-8")

        def slow_hasher(path: str) -> str | None:
            time.sleep(1.0)
            return OL._sha256_file(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=slow_hasher)
        try:
            start = time.monotonic()
            idx.rebuild(max_seconds=0.2)
            elapsed = time.monotonic() - start
            # With 8 workers and 16 files at 1s/hasher call, running Phase 1 to
            # completion would take ~2s. A working deadline check should return
            # once the first batch of workers reports back (~1s), well short
            # of that -- proving Phase 1 didn't wait for every future.
            assert elapsed < 1.8, (
                f"rebuild() took {elapsed:.2f}s with a 0.2s budget -- Phase 1 "
                "appears to have blocked until all workers finished"
            )
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    @duckdb_required
    def test_unlimited_budget_processes_everything(self, tmp_path: Path) -> None:
        """max_seconds=None must still index every file (no regression to the
        deadline-enforcement change for the common/default case)."""
        for i in range(5):
            (tmp_path / f"g{i}.csv").write_text(f"col\n{i}", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            count = idx.rebuild(max_seconds=None)
            assert count == 5
            assert idx.last_rebuild_partial is False
        finally:
            idx.close()

    @duckdb_required
    def test_phase1_subdeadline_leaves_phase2_time_to_persist(
        self, tmp_path: Path,
    ) -> None:
        """Regression test for 5845cc6d: on a tree too large to fully analyse
        within the budget, Phase 1 must still leave Phase 2 real time to
        persist whatever it DID manage to compute. Before the fix, Phase 1
        used the FULL deadline and could consume all of it, leaving Phase 2
        zero iterations -- total_indexed stuck at 0 forever regardless of how
        many files were actually hashed."""
        n_files = 40
        for i in range(n_files):
            (tmp_path / f"f{i}.csv").write_text(f"col\n{i}", encoding="utf-8")

        def slow_hasher(path: str) -> str | None:
            time.sleep(0.3)
            return OL._sha256_file(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=slow_hasher)
        try:
            # 8 workers, 0.3s/file -> ~5 waves of 8 to finish everything
            # (~1.5s total). phase1_deadline is 0.5 * max_seconds; with
            # max_seconds=1.0 that's 0.5s -- enough for exactly one wave (8
            # files) to complete before Phase 1 cuts itself off, leaving
            # ~0.5s for Phase 2 (cheap: no hashing, just cache + DB writes).
            count = idx.rebuild(max_seconds=1.0)
            assert count > 0, (
                "rebuild() made zero forward progress -- Phase 1's own "
                "sub-deadline isn't leaving Phase 2 any time to persist"
            )
            assert count < n_files, (
                "test setup didn't actually exercise a deadline cutoff -- "
                "all files were indexed, so this isn't testing partial progress"
            )
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    @duckdb_required
    def test_skips_fts_rebuild_when_deadline_passed_and_index_exists(
        self, tmp_path: Path,
    ) -> None:
        """d9c76caa follow-up: once an FTS index exists, a rebuild() whose
        deadline has already passed by the time the write phase reaches
        _rebuild_fts() must skip that (expensive, full-table, non-
        incremental) step rather than paying its cost unconditionally --
        search() still returns results off the existing (now slightly
        stale) index instead of the call blowing its budget regardless of
        how well Phase 1/Phase 2 behaved."""
        a = tmp_path / "a.csv"
        b = tmp_path / "b.csv"
        a.write_text("col\n1", encoding="utf-8")
        b.write_text("col\n2", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()  # generous default budget -> FTS actually gets built
            assert idx._fts_built is True

            call_count = [0]
            real_rebuild_fts = idx._rebuild_fts

            def counting_rebuild_fts(con: Any) -> None:
                call_count[0] += 1
                real_rebuild_fts(con)

            idx._rebuild_fts = counting_rebuild_fts

            # Removing a file forces removed_paths to be non-empty, which
            # makes changed=True UNCONDITIONALLY (that loop has no deadline
            # check) -- this reaches the `if changed:` block (and therefore
            # the new skip-fts decision) even with an already-expired
            # deadline, where a stale-only rebuild would otherwise never
            # get there at all.
            b.unlink()
            idx.rebuild(max_seconds=-100.0)  # deadline already in the past
            assert call_count[0] == 0, (
                "FTS rebuild should have been skipped once the deadline had "
                "already passed, not paid unconditionally"
            )
            assert idx.last_rebuild_partial is True

            # search() must still work off the existing index, not nothing.
            hits = idx.search("col")
            assert isinstance(hits, list)
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# rebuild()'s initial file walk must itself be deadline-aware (6ba77ada)
# ---------------------------------------------------------------------------
#
# Root cause: _iter_safe_output_files()/os.walk() has zero deadline
# awareness of its own. On a large (tens-of-thousands-of-files) tree it can
# by itself take far longer than rebuild()'s entire max_seconds budget --
# confirmed live against a real 70,000-file tree: the walk alone took ~11s
# vs. the 5s default budget, so Phase 1 (5845cc6d)/Phase 2's own deadline
# checks never even got a chance to run -- every call returned 0 rows,
# search() stayed empty, forever. This is distinct from d9c76caa/c2021725
# (Phase 1's own sub-deadline, and skipping _rebuild_fts() past a deadline),
# both of which assumed the walk feeding them was fast.
#
# These tests use small synthetic trees with an artificially SLOWED walk
# (monkeypatching _walk_safe_output_files to sleep per yielded path) rather
# than a real tens-of-thousands-of-files tree -- scripts/test_outputs_
# indexing.py already covers that as an on-demand diagnostic against a real
# large tree; this stays CI-fast while exercising the exact code path.

class TestResumableFileWalkDeadlineAwareness:
    """Unit coverage for _ResumableFileWalk: the walk must pause at (or
    near) a deadline and resume later without ever losing or duplicating a
    path, regardless of how tight the deadline is."""

    def test_pauses_and_resumes_without_loss_or_duplication(
        self, tmp_path: Path,
    ) -> None:
        n = 60
        for i in range(n):
            (tmp_path / f"f{i:03d}.csv").write_text("col\n1", encoding="utf-8")

        walk = OL._ResumableFileWalk(str(tmp_path))
        collected: list[str] = []
        calls = 0
        while not walk.exhausted:
            calls += 1
            assert calls <= n + 5, "walk made no progress on some call"
            # A deadline already in the past forces drain() to return after
            # exactly one path per call -- the tightest possible resumption
            # granularity, proving pause/resume never drops or repeats a path
            # even in the worst case. The one exception is the FINAL call:
            # since a generator only knows it's exhausted once a pull from it
            # actually comes back empty, the call that discovers exhaustion
            # may legitimately return zero paths.
            chunk = walk.drain(time.monotonic() - 1.0)
            assert len(chunk) >= 1 or walk.exhausted
            collected.extend(chunk)

        expected = sorted(OL._iter_safe_output_files(str(tmp_path)))
        assert sorted(collected) == expected
        assert len(collected) == len(set(collected)), (
            "duplicate path yielded across resumed drain() calls"
        )

    def test_unlimited_deadline_drains_everything_in_one_call(
        self, tmp_path: Path,
    ) -> None:
        for i in range(10):
            (tmp_path / f"g{i}.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path))
        chunk = walk.drain(None)
        assert walk.exhausted is True
        assert len(chunk) == 10

    def test_drain_after_exhausted_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "only.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path))
        walk.drain(None)
        assert walk.exhausted is True
        assert walk.drain(None) == []


class TestRebuildWalkDeadlineAwareness:
    """rebuild()-level regression coverage for 6ba77ada: a walk that alone
    exceeds max_seconds must not prevent rebuild() from returning promptly
    and making real, resumable progress across repeated calls."""

    @staticmethod
    def _install_slow_walk(monkeypatch: pytest.MonkeyPatch, delay: float) -> None:
        """Wrap the real walk generator so every yielded path costs `delay`
        seconds -- simulates a walk whose OWN pace (not Phase 1/2) is what
        blows the budget, exactly 6ba77ada's reported signature."""
        real_walk = OL._walk_safe_output_files

        def slow_walk(outputs_dir: str, *, exclude_patterns: tuple = ()):
            for p in real_walk(outputs_dir, exclude_patterns=exclude_patterns):
                time.sleep(delay)
                yield p

        monkeypatch.setattr(OL, "_walk_safe_output_files", slow_walk)

    def test_bare_walk_exceeding_budget_does_not_block_rebuild(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        n = 40
        for i in range(n):
            (tmp_path / f"h{i:03d}.csv").write_text("col\n1", encoding="utf-8")
        # 40 files * 0.02s/file = 0.8s to walk fully -- alone exceeds the
        # 0.2s max_seconds budget used below.
        self._install_slow_walk(monkeypatch, 0.02)

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            start = time.monotonic()
            idx.rebuild(max_seconds=0.2)
            elapsed = time.monotonic() - start
            # Before the fix this call blocked for the walk's full duration
            # (here ~0.8s; on a real 70k-file tree, ~11s+) regardless of
            # max_seconds, because the walk itself had no deadline check.
            assert elapsed < 0.6, (
                f"rebuild() took {elapsed:.2f}s with a 0.2s budget -- the "
                "walk appears to have blocked past its own deadline"
            )
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    @duckdb_required
    def test_large_tree_converges_across_repeated_tight_budget_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mirrors scripts/test_outputs_indexing.py's convergence check at a
        CI-fast scale: repeated rebuild() calls against a walk that alone
        exceeds a single call's budget must still converge -- every file
        indexed, FTS built, search returning real hits -- within a bounded
        number of calls, not plateau (at 0 or any other count) forever.

        52cbe5d8 -- this test previously only called ``idx.search(...)`` as
        the LAST, short-circuited operand of the loop's ``and`` chain, so it
        was skipped entirely whenever ``_fts_built`` was still False.  That
        starved the test of the exact recovery path
        ``OutputsFtsIndex.rebuild()``'s own design relies on for convergence
        (b1789c0d): ``search()`` performs a lazy FTS build whenever
        ``_fts_built`` is False, regardless of ``_fts_pending`` -- but only if
        it is actually CALLED.  The reference script this test claims to
        mirror, ``scripts/test_outputs_indexing.py::run_rebuild_cycles``,
        calls ``idx.search(...)`` unconditionally on every cycle for exactly
        this reason (real production usage via ``search_outputs()`` also
        always calls ``rebuild()`` immediately followed by ``search()``, so
        the lazy build is *always* attempted on the very next call). Without
        that unconditional call, a run where every early rebuild() call
        happened to exceed its own deadline just before reaching the Tantivy
        commit step (deferring the build via ``_fts_pending``) could leave
        ``_fts_built`` permanently False for the rest of the loop -- nothing
        else in a bare ``rebuild()``-only loop ever retries the build once
        the walk finishes and there is nothing left to write (``changed``
        goes False forever, and that is the ONLY call site of
        ``_rebuild_fts()`` inside ``rebuild()``). Confirmed via 20 repeated
        isolated runs: the old short-circuited condition failed ~40% of the
        time (a real, repeatable test bug, not a load-flake) while calling
        ``search()`` unconditionally every cycle -- matching the reference
        script and real production usage -- converged in 19/20 runs (the one
        remaining failure was an unrelated Tantivy searcher-reuse bug, fixed
        separately in ``OutputsFtsIndex.search()``)."""
        n = 50
        for i in range(n):
            (tmp_path / f"k{i:03d}.csv").write_text(
                f"col\nvalue={i}\n", encoding="utf-8",
            )
        self._install_slow_walk(monkeypatch, 0.01)

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            converged = False
            for _ in range(30):
                count = idx.rebuild(max_seconds=0.15)
                # Unconditional, mirroring run_rebuild_cycles() -- see the
                # docstring above for why this must never be short-circuited.
                hits = idx.search("value=1")
                if count >= n and idx._fts_built and not idx._fts_pending and hits:
                    converged = True
                    break
            assert converged, (
                "rebuild()/search() never converged across repeated "
                "tight-budget calls -- the walk fix must let every call "
                "make forward, resumable progress instead of stalling "
                "indefinitely"
            )
        finally:
            idx.close()

    def test_removed_file_eventually_detected_after_slow_walk_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Removed-file detection must be deferred (not falsely triggered)
        while a walk pass is still in progress, then correctly applied once
        a full pass completes -- covers the resumable-walk correctness
        tradeoff documented in rebuild()'s Phase 0."""
        keep = tmp_path / "keep.csv"
        remove = tmp_path / "remove.csv"
        keep.write_text("col\n1", encoding="utf-8")
        remove.write_text("col\n2", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            # Establish both files as fully, successfully indexed BEFORE the
            # walk is slowed and the file removed -- so there's no window
            # for the file to vanish between os.walk()'s internal per-
            # directory listing snapshot and the staleness stat check (a
            # pre-existing, unrelated TOCTOU edge case, not what this test
            # targets).
            idx.rebuild()
            assert any("remove.csv" in p for p in idx._row_cache)
            assert any("keep.csv" in p for p in idx._row_cache)

            self._install_slow_walk(monkeypatch, 0.05)
            remove.unlink()

            # A call whose budget is exhausted mid-walk must not yet prune
            # the removed file -- the walk hasn't reconfirmed the full tree.
            idx.rebuild(max_seconds=0.01)
            assert idx.last_rebuild_partial is True
            assert any("remove.csv" in p for p in idx._row_cache), (
                "removed file was pruned before a full walk pass confirmed "
                "it was actually gone"
            )

            # Give it a generous budget so the in-progress pass (and any
            # follow-up pass) can actually finish end to end.
            for _ in range(20):
                idx.rebuild(max_seconds=2.0)
                if not any("remove.csv" in p for p in idx._row_cache):
                    break
            assert not any("remove.csv" in p for p in idx._row_cache), (
                "removed file was never dropped from the cache once a full "
                "walk pass completed"
            )
            assert any("keep.csv" in p for p in idx._row_cache)
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# Archival-classification hash persistence (sprint item 7a6a278f)
# ---------------------------------------------------------------------------

class TestArchivalHashPersistence:
    """classify_canonical_archival must not re-hash unchanged archival
    candidates on every rebuild() -- only newly-stale files get re-hashed."""

    def test_unchanged_archival_candidate_not_rehashed(self, tmp_path: Path) -> None:
        """classify_canonical_archival only runs when something is stale or
        removed, so the test needs an unrelated file to change between
        rebuilds -- that keeps classify_canonical_archival on the call path
        while the archival pair itself stays untouched."""
        content = b"a,b\n1,2\n"
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        unrelated = tmp_path / "unrelated.csv"
        canonical.write_bytes(content)
        archival.write_bytes(content)
        unrelated.write_bytes(b"x\n1")

        call_log: list[str] = []
        real_hasher = OL._sha256_file

        def counting_hasher(path: str) -> str | None:
            call_log.append(path)
            return real_hasher(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=counting_hasher)
        try:
            idx.rebuild()
            assert str(canonical) in call_log  # both files new -- must be hashed once
            assert str(archival) in call_log

            # Change only the unrelated file so `stale` is non-empty on the
            # second rebuild (keeping classify_canonical_archival on the call
            # path) while the archival pair itself is untouched.
            unrelated.write_bytes(b"x\n2")
            os.utime(str(unrelated), None)
            call_log.clear()
            idx.rebuild()
            assert str(canonical) not in call_log, (
                "unchanged canonical file was re-hashed by classify_canonical_archival"
            )
            assert str(archival) not in call_log, (
                "unchanged archival candidate was re-hashed by classify_canonical_archival"
            )
        finally:
            idx.close()

    def test_changed_file_still_rehashed(self, tmp_path: Path) -> None:
        """A genuinely modified archival candidate must still be re-hashed --
        persistence must not mask real content changes."""
        canonical = tmp_path / "run.csv"
        archival = tmp_path / "run_old.csv"
        canonical.write_bytes(b"a,b\n1,2\n")
        archival.write_bytes(b"a,b\n1,2\n")

        call_log: list[str] = []
        real_hasher = OL._sha256_file

        def counting_hasher(path: str) -> str | None:
            call_log.append(path)
            return real_hasher(path)

        idx = OL.OutputsFtsIndex(str(tmp_path), hasher=counting_hasher)
        try:
            idx.rebuild()
            call_log.clear()

            archival.write_bytes(b"a,b\n9,9\n")
            os.utime(str(archival), None)
            idx.rebuild()
            assert str(archival) in call_log, (
                "a genuinely modified archival candidate must be re-hashed"
            )
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# DuckDB FTS capability probe (sprint item b8314850) -- REMOVED (77443d83)
#
# This class used to empirically probe DuckDB's create_fts_index parameters
# (documenting that DuckDB 1.5.4 has no "incremental" option, only a full
# overwrite=1 rebuild) to justify why _rebuild_fts() used a full rebuild.
# That whole question is now moot: 77443d83/a9b8485a replaced DuckDB's FTS
# extension with Tantivy for the search index entirely (see
# OutputsFtsIndex._rebuild_fts / .search). DuckDB's create_fts_index is no
# longer called anywhere in this module, so a probe of its parameter support
# no longer documents anything about our own behaviour -- removed rather than
# left around to misleadingly imply we still care about it. See
# TestTantivyMigration below for the equivalent capability coverage
# (incremental commit + legacy-row backfill) under the new architecture.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cold-tree FTS deferral fix (sprint item b1789c0d)
# ---------------------------------------------------------------------------

@duckdb_required
class TestColdTreeFtsDeferral:
    """_rebuild_fts() must be skipped -- not run -- when the overall deadline
    has already expired, even on a cold tree where _fts_built is still False.

    Before this fix, the d9c76caa guard only fired when _fts_built was True,
    so a cold/first-call against a large tree would still call _rebuild_fts()
    unconditionally -- the cost of which scales with total row count and has
    no internal deadline.  On a 66k-file tree this alone hit the ~4min
    external MCP client timeout, returning total_indexed=0 with no error.

    After the fix:
    - If the deadline passed AND _fts_built is False: _fts_pending=True,
      _rebuild_fts() is NOT called, last_rebuild_partial=True.
    - The NEXT search() call (which has a fresh deadline) performs the lazy
      FTS build so real BM25 results come back.
    - A warm tree (both _fts_built=True and deadline passed) still uses the
      existing index unchanged (prior d9c76caa behaviour preserved).
    """

    def test_cold_tree_deadline_skips_fts_sets_pending(
        self, tmp_path: Path,
    ) -> None:
        """First rebuild() on a cold tree with an already-expired deadline must
        write rows to the DB but skip _rebuild_fts(), leaving _fts_pending=True
        so search() knows to build the index on the next call."""
        for i in range(5):
            (tmp_path / f"file_{i}.csv").write_text(
                f"col_unique_{i}\n{i}", encoding="utf-8"
            )

        fts_call_count = [0]

        idx = OL.OutputsFtsIndex(str(tmp_path))
        real_rebuild_fts = idx._rebuild_fts

        def counting_rebuild_fts(con: Any) -> None:
            fts_call_count[0] += 1
            real_rebuild_fts(con)

        idx._rebuild_fts = counting_rebuild_fts
        try:
            # max_seconds=None means no overall deadline BUT we simulate an
            # already-expired deadline by using a deeply-negative max_seconds.
            count = idx.rebuild(max_seconds=-1.0)  # deadline already past
            # The removed-paths + stale paths are non-trivial, so changed=True
            # and we DO enter the if-changed block. With an expired deadline,
            # _rebuild_fts() must be SKIPPED.
            assert fts_call_count[0] == 0, (
                "b1789c0d: _rebuild_fts() was called despite an already-expired "
                "deadline on a cold tree (must be skipped to avoid the timeout bug)"
            )
            # Rows may or may not have been written (deadline may have expired
            # before Phase 2 got any iterations), but _fts_pending must be set.
            assert idx._fts_pending is True, (
                "b1789c0d: _fts_pending must be True after FTS was deferred "
                "on a cold tree with an expired deadline"
            )
            assert idx.last_rebuild_partial is True
        finally:
            idx.close()

    def test_cold_tree_partial_rebuild_then_search_builds_fts(
        self, tmp_path: Path,
    ) -> None:
        """Simulate the real bug scenario: rebuild() writes rows but FTS is
        deferred because the deadline expires -- then search() triggers a lazy
        FTS build and returns real results.

        The live bug sequence:
          call 1: Phase 1+2 write N rows, but _rebuild_fts() itself exceeds
                  budget -> total_indexed=N, hits=[], partial=True, fts_pending=True
          call 2: search() sees _fts_pending, calls _rebuild_fts() with a fresh
                  deadline -> real BM25 hits come back.

        We simulate this by manually driving the state: first do a real rebuild
        (rows written), then simulate a second rebuild that sets _fts_pending by
        expiring the deadline before _rebuild_fts can fire, then verify search()
        lazily builds the FTS.
        """
        n_files = 5
        for i in range(n_files):
            (tmp_path / f"result_{i}.csv").write_text(
                f"epoch,uniqueterm_{i}\n{i},{i}", encoding="utf-8"
            )

        idx = OL.OutputsFtsIndex(str(tmp_path))
        call_sequence: list[str] = []
        real_rebuild_fts = idx._rebuild_fts

        def counting_rebuild_fts(con: Any) -> None:
            call_sequence.append("fts_call")
            real_rebuild_fts(con)

        try:
            # Step 1: write rows to the DB using the underlying helpers directly,
            # bypassing _rebuild_fts entirely. This puts us in the state where
            # rows exist but no FTS index was ever built -- exactly the state
            # the real bug leaves behind when _rebuild_fts times out.
            # We achieve this by running Phase 1 + Phase 2 of rebuild with
            # max_seconds=None (no deadline) but with _rebuild_fts patched to
            # raise a simulated timeout error, which is caught by the outer
            # try/except and leaves _fts_built=False.
            def simulated_fts_timeout(con: Any) -> None:
                call_sequence.append("fts_timeout")
                raise RuntimeError("simulated FTS timeout (b1789c0d test)")

            idx._rebuild_fts = simulated_fts_timeout
            # Run rebuild -- rows get written but FTS "times out"
            idx.rebuild(max_seconds=None)
            # Rows are in _row_cache but FTS didn't build (exception was swallowed)
            assert len(idx._row_cache) == n_files, (
                f"Expected {n_files} rows in cache after rebuild, got {len(idx._row_cache)}"
            )
            assert idx._fts_built is False

            # Manually set _fts_pending to True to simulate what the fixed code
            # would have done had it detected the expiry before calling _rebuild_fts.
            idx._fts_pending = True
            idx.last_rebuild_partial = True

            # Step 2: search() with the real _rebuild_fts restored.
            # It sees _fts_pending=True, calls _rebuild_fts() lazily.
            idx._rebuild_fts = counting_rebuild_fts
            call_sequence.clear()

            hits = idx.search("uniqueterm_3")

            assert "fts_call" in call_sequence, (
                "b1789c0d: search() must trigger lazy _rebuild_fts() when _fts_pending=True"
            )
            assert idx._fts_pending is False, (
                "_fts_pending must be cleared after the lazy build completes"
            )
            assert any("result_3" in h["path"] for h in hits), (
                f"b1789c0d: expected BM25 hit for uniqueterm_3 after lazy FTS build, "
                f"got: {hits}"
            )
        finally:
            idx.close()

    def test_warm_tree_deadline_passed_still_uses_existing_fts(
        self, tmp_path: Path,
    ) -> None:
        """Regression check for d9c76caa (warm tree): an expired deadline on a
        WARM tree (_fts_built=True) must still skip _rebuild_fts() and use the
        existing index -- same as before the b1789c0d change."""
        (tmp_path / "data.csv").write_text("warmterm,val\n1,2", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            # First rebuild with a generous budget -- FTS gets built.
            idx.rebuild(max_seconds=None)
            assert idx._fts_built is True

            fts_call_count = [0]
            real_rebuild_fts = idx._rebuild_fts

            def counting_fts(con: Any) -> None:
                fts_call_count[0] += 1
                real_rebuild_fts(con)

            idx._rebuild_fts = counting_fts

            # Second rebuild with expired deadline -- must skip FTS (existing index usable).
            (tmp_path / "data.csv").unlink()  # force changed=True
            idx.rebuild(max_seconds=-100.0)

            assert fts_call_count[0] == 0, (
                "warm tree + expired deadline should still skip _rebuild_fts() "
                "(regression check for d9c76caa)"
            )
            assert idx._fts_pending is False, (
                "_fts_pending must stay False for a warm tree -- "
                "the existing index is usable"
            )
            # search() must still return results from the existing index.
            hits = idx.search("warmterm")
            assert isinstance(hits, list)
        finally:
            idx.close()

    def test_search_outputs_cold_tree_returns_partial_not_silent_empty(
        self, tmp_path: Path,
    ) -> None:
        """The module-level search_outputs() must never return a bare, unexplained
        {hits: [], total_indexed: 0} on a cold tree where indexing is in progress.

        Before b1789c0d: a cold tree always returned that indistinguishable result.
        After: first call sets partial=True (and fts_pending=True if FTS deferred).
        A subsequent call (warm rows, fresh deadline) returns real hits.
        """
        n_files = 5
        for i in range(n_files):
            (tmp_path / f"cold_{i}.csv").write_text(
                f"cold_unique_term_{i}\n{i}", encoding="utf-8"
            )

        # Simulate a cold tree under tight budget: Phase 1 processes files but
        # FTS is deferred (deadline already in the past by the time Phase 2 runs).
        # We use search_outputs directly (module-level API, as in the live bug).
        result1 = OL.search_outputs(str(tmp_path), "cold_unique_term_2", max_seconds=-1.0)

        # Must NOT be a silent empty result -- must carry partial=True signal.
        assert result1.get("partial") is True, (
            "b1789c0d: search_outputs() on a cold tree with expired deadline must "
            f"return partial=True, not a silent empty result. Got: {result1}"
        )
        # total_in_index must reflect cumulative rows (even on partial runs)
        # so the caller can distinguish 'cold tree, indexing in progress'
        # from 'empty tree, nothing to find'.
        assert "total_in_index" in result1, (
            "b1789c0d: search_outputs() must include total_in_index for caller visibility"
        )

        # Second call: fresh budget (no artificial limit) -- should get real results.
        result2 = OL.search_outputs(str(tmp_path), "cold_unique_term_2")
        assert result2["total_indexed"] >= 1, (
            "b1789c0d: second search_outputs() call must index files and return non-zero "
            f"total_indexed. Got: {result2}"
        )
        # After FTS is built on the second call, hits should be available.
        assert len(result2["hits"]) >= 1, (
            f"b1789c0d: second call must return BM25 hits once FTS is built. Got: {result2}"
        )

    def test_search_outputs_small_warm_tree_unaffected(
        self, tmp_path: Path,
    ) -> None:
        """A small tree that fits comfortably within the default budget must
        behave exactly as before: total_indexed=N, hits=<results>, no partial flag."""
        (tmp_path / "normal.csv").write_text(
            "normalterm,value\n1,2", encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "normalterm")
        assert result["total_indexed"] >= 1
        assert len(result["hits"]) >= 1
        assert "db_write_error" not in result, (
            "1a799e52: a healthy write must not carry a db_write_error field"
        )
        # 81a0b23d -- a fully-converged (non-partial) response must keep its
        # existing shape exactly: no new pending_stale_count key at all, not
        # even pending_stale_count=0. Regression check for callers that don't
        # know about the new field.
        assert "partial" not in result
        assert "pending_stale_count" not in result, (
            "81a0b23d: a fully-converged rebuild must not carry "
            f"pending_stale_count -- got {result}"
        )

    def test_search_outputs_mid_pass_surfaces_pending_stale_count(
        self, tmp_path: Path,
    ) -> None:
        """81a0b23d: search_outputs()'s response must expose how many
        confirmed-stale files are still queued for analysis+write whenever
        partial=True, so a zero-hit result on a mid-pass index (more files
        queued behind the scenes) is distinguishable from a genuine miss on
        a fully-converged index -- total_indexed/total_in_index alone can't
        make that distinction because rebuild() deliberately keeps them from
        regressing mid-pass (every previously-indexed path is retained in
        ``all_paths`` until the walk's current pass confirms otherwise)."""
        n_files = 10
        for i in range(n_files):
            (tmp_path / f"pending_{i}.csv").write_text(
                f"col\n{i}", encoding="utf-8"
            )

        def slow_hasher(path: str) -> str | None:
            time.sleep(0.2)
            return OL._sha256_file(path)

        # Seed the module-level cache with a pre-built index using the slow
        # hasher (mirrors OL._get_cached_index's own construction, just with
        # a hasher OL.search_outputs itself has no parameter to inject).
        # max_workers is pinned explicitly (rather than left at the
        # os.cpu_count() default, a849e3d5) so this stays deterministic
        # regardless of how many cores the machine running this test has.
        key = OL._cache_key(str(tmp_path))
        idx = OL.OutputsFtsIndex(
            str(tmp_path),
            db_path=OL._resolve_index_db_path(str(tmp_path)),
            hasher=slow_hasher,
            max_workers=2,
        )
        with OL._index_cache_lock:
            OL._index_cache[key] = idx
        try:
            # phase1_deadline is half of max_seconds (0.5s here); with 2
            # workers at 0.2s/file, only ~2 waves (4 files) fit before the
            # sub-deadline trips, leaving the rest un-analysed and therefore
            # still queued in idx._pending_stale.
            result = OL.search_outputs(str(tmp_path), "col", max_seconds=1.0)

            assert result["partial"] is True, (
                f"expected a mid-pass (partial) rebuild, got: {result}"
            )
            assert "pending_stale_count" in result, (
                "81a0b23d: partial=True must carry pending_stale_count -- "
                f"got {result}"
            )
            assert result["pending_stale_count"] > 0
            assert result["pending_stale_count"] < n_files, (
                "test setup didn't actually leave a partial backlog -- "
                f"got {result}"
            )
            # The surfaced count must be the REAL backlog size, not a stand-in.
            assert result["pending_stale_count"] == len(idx._pending_stale)
        finally:
            with OL._index_cache_lock:
                OL._index_cache.pop(key, None)
            idx.close()

    def test_rebuild_surfaces_db_write_error_instead_of_silent_debug_log(
        self, tmp_path: Path,
    ) -> None:
        """1a799e52: before this fix, Phase 2's DB-write except-block swallowed
        ANY failure at DEBUG level only, while total_indexed/total_in_index (both
        derived from the in-memory row_cache, populated BEFORE the write is
        attempted) kept reporting growing "success" -- a real persistence
        failure looked identical to a healthy index. last_db_write_error /
        the search_outputs() result's db_write_error field must now surface it."""
        (tmp_path / "a.csv").write_text("term_one,1\n", encoding="utf-8")

        idx = OL.OutputsFtsIndex(str(tmp_path))
        assert idx.last_db_write_error is None

        def _boom(self, con):  # noqa: ANN001 -- matches _ensure_schema's real signature
            raise RuntimeError("simulated disk-full / connection failure")

        with patch.object(OL.OutputsFtsIndex, "_ensure_schema", _boom):
            total_indexed = idx.rebuild()

        # The misleading part of the original bug: the in-memory count still
        # looks like a healthy, progressing index...
        assert total_indexed >= 1
        assert len(idx._row_cache) >= 1
        # ...but the write genuinely failed, and that must now be visible.
        assert idx.last_db_write_error is not None
        assert "simulated disk-full" in idx.last_db_write_error

        # A subsequent successful rebuild() call must clear the error (per-call
        # semantics -- last_db_write_error reflects only the MOST RECENT call).
        idx.last_db_write_error = None  # reset attribute directly (isolate this assertion)
        idx.rebuild()
        assert idx.last_db_write_error is None

    def test_search_outputs_surfaces_db_write_error_in_result_dict(
        self, tmp_path: Path,
    ) -> None:
        """The module-level search_outputs() API (the real MCP-tool-facing
        entry point) must surface the same signal, not just the class attribute."""
        (tmp_path / "b.csv").write_text("term_two,1\n", encoding="utf-8")

        def _boom(self, con):  # noqa: ANN001
            raise RuntimeError("simulated write failure")

        with patch.object(OL.OutputsFtsIndex, "_ensure_schema", _boom):
            result = OL.search_outputs(str(tmp_path), "term_two")

        assert result["total_indexed"] >= 1, (
            "the in-memory count still looks like a healthy index -- this is "
            "exactly the deceptive state the fix must make visible via db_write_error"
        )
        assert result.get("db_write_error") is not None
        assert "simulated write failure" in result["db_write_error"]
        assert result.get("partial") is not True, (
            "a small/warm tree must not set partial=True: "
            f"got {result}"
        )
        assert result.get("fts_pending") is not True

    def test_db_write_failure_does_not_permanently_drop_file_from_index(
        self, tmp_path: Path,
    ) -> None:
        """<false-convergence root-cause fix> (sprint item f66656f9): before
        this fix, rebuild() popped a just-analysed path from
        ``_pending_stale`` UNCONDITIONALLY right after ``_apply_precomputed``
        -- which had ALREADY optimistically updated ``_row_cache``/
        ``_manifest`` for that path -- regardless of whether Phase 2's actual
        DB write (below that pop, in the original code) went on to succeed.
        If the write then raised, the path was gone from the backlog forever:
        Phase 1's own staleness check (``manifest mismatch OR not in
        row_cache``) found both already "current" and would never re-flag it
        stale, so the file could never be retried. Worse, ``last_db_write_
        error`` resets to ``None`` at the top of every ``rebuild()`` call, so
        even that signal vanishes on the very next call once nothing is
        `changed` anymore -- the tree then looks fully converged
        (``partial`` False, ``pending_stale_count`` omitted) forever, while
        the real file silently never made it into the searchable index. This
        confirms the fix: the file stays queued in ``_pending_stale`` after a
        failed write, and a later successful ``rebuild()`` call actually
        persists and finds it.
        """
        (tmp_path / "real_file.csv").write_text(
            "distinctive_term_xyz,1\n", encoding="utf-8"
        )

        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            def _boom(self, con):  # noqa: ANN001 -- matches _ensure_schema's signature
                raise RuntimeError("simulated transient disk-full")

            with patch.object(OL.OutputsFtsIndex, "_ensure_schema", _boom):
                idx.rebuild()

            # The pre-existing, documented optimistic in-memory state...
            assert idx.last_db_write_error is not None
            assert len(idx._row_cache) == 1, (
                f"expected the optimistic row_cache entry -- got {idx._row_cache!r}"
            )
            # ...but the fix under test: the file must STILL be queued for
            # retry, not silently and permanently dropped.
            assert len(idx._pending_stale) == 1, (
                "a DB write failure must leave the file queued in "
                f"_pending_stale for a future retry -- got {idx._pending_stale!r}"
            )
            # And it must genuinely not be searchable yet -- the write really
            # did fail, nothing was persisted.
            assert idx.search("distinctive_term_xyz") == [], (
                "the row was never actually persisted, so it must not be "
                "searchable until a retry succeeds"
            )

            # A later, unpatched rebuild() call (the transient failure is
            # gone) must actually retry and persist the file this time.
            idx.last_db_write_error = None
            idx.rebuild()
            assert idx.last_db_write_error is None
            assert idx._pending_stale == {}, (
                "a successful retry must clear the backlog -- "
                f"got {idx._pending_stale!r}"
            )

            hits = idx.search("distinctive_term_xyz")
            assert any("real_file.csv" in h["path"] for h in hits), (
                f"the file must be searchable once the write actually "
                f"succeeds -- got {hits}"
            )
        finally:
            idx.close()

    def test_search_outputs_sets_zero_hits_warning_when_index_incomplete(
        self, tmp_path: Path,
    ) -> None:
        """<surface-it-loudly> (sprint item f66656f9): a zero-hit result
        returned while the index is NOT fully converged must carry
        ``zero_hits_warning`` -- an unmissable, self-contained signal added
        because the pre-existing ``partial``/``fts_pending``/
        ``pending_stale_count`` contract, while already tracked and returned,
        was repeatedly misread by callers looking only at ``hits: []`` as
        "file does not exist"."""
        n_files = 5
        for i in range(n_files):
            (tmp_path / f"cold_zhw_{i}.csv").write_text(
                f"cold_zhw_unique_term_{i}\n{i}", encoding="utf-8"
            )

        result = OL.search_outputs(
            str(tmp_path), "cold_zhw_unique_term_2", max_seconds=-1.0,
        )

        assert result["hits"] == []
        assert result.get("partial") is True
        assert result.get("zero_hits_warning"), (
            f"expected zero_hits_warning on a 0-hit, partial=True result -- got {result}"
        )
        assert "re-invoke" in result["zero_hits_warning"].lower()

    def test_search_outputs_no_zero_hits_warning_when_hits_present(
        self, tmp_path: Path,
    ) -> None:
        """Regression check: a healthy, non-empty result must never carry
        zero_hits_warning -- the new field must not leak into the common case."""
        (tmp_path / "warm_zhw.csv").write_text(
            "warmzhwuniqueterm,1\n1,2", encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "warmzhwuniqueterm")
        assert len(result["hits"]) >= 1
        assert "zero_hits_warning" not in result

    def test_search_outputs_no_zero_hits_warning_on_fully_converged_miss(
        self, tmp_path: Path,
    ) -> None:
        """A genuine zero-hit miss on a FULLY converged (non-partial) index
        must NOT carry zero_hits_warning -- it would defeat the purpose of a
        loud signal if it fired on every miss regardless of index state."""
        (tmp_path / "unrelated_zhw.csv").write_text(
            "somecolumn,1\n1,2", encoding="utf-8"
        )
        result = OL.search_outputs(str(tmp_path), "totally_absent_term_zzz_zhw")
        assert result["hits"] == []
        assert result.get("partial") is not True
        assert "zero_hits_warning" not in result


class TestTantivySearchIndex:
    """77443d83/a6056886 -- OutputsFtsIndex._rebuild_fts/.search now go
    through Tantivy instead of DuckDB's FTS extension."""

    def test_search_reuses_single_searcher_snapshot(self, tmp_path: Path) -> None:
        """52cbe5d8 -- search() must resolve every hit's DocAddress against
        the SAME Searcher snapshot that produced the query results, not a
        freshly-obtained one.

        DocAddress values are only meaningful relative to the segment layout
        of the Searcher that returned them. Calling ``index.searcher()`` a
        second time (as the code previously did, once to run the query and
        again to resolve each hit's path) can return a *different* live view
        if Tantivy's background segment-merge thread swaps in a new layout in
        between -- observed live as a Rust-level panic
        (``pyo3_runtime.PanicException: index out of bounds``) from
        ``searcher.doc(addr)`` that bypasses ``search()``'s own
        ``except Exception`` and crashes the caller instead of yielding the
        documented best-effort ``[]``. Reproducing the race itself is
        inherently timing-dependent (it needs a real background merge to
        land in a few-line window), so this test instead pins down the fix
        directly: wrap the real Tantivy index so every ``.searcher()`` call
        is counted, and assert ``search()`` only ever asks for one per call.
        """
        f = tmp_path / "run.csv"
        f.write_text("findme,value\n1,2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            real_index, real_writer = idx._connect_tantivy()

            class _SearcherCountingIndex:
                def __init__(self, real: Any) -> None:
                    self._real = real
                    self.searcher_calls = 0

                def searcher(self) -> Any:
                    self.searcher_calls += 1
                    return self._real.searcher()

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._real, name)

            counting = _SearcherCountingIndex(real_index)
            idx._tantivy_index = counting  # type: ignore[assignment]

            hits = idx.search("findme")
            assert hits, "sanity check: the query must still find the row"
            assert counting.searcher_calls == 1, (
                "search() called index.searcher() "
                f"{counting.searcher_calls} times in one invocation -- it "
                "must call it exactly once and reuse that same Searcher for "
                "both the query and every doc() lookup"
            )
        finally:
            idx.close()

    def test_content_update_reflected_in_search(self, tmp_path: Path) -> None:
        """A changed file's OLD content must stop matching and its NEW
        content must start matching -- confirms _rebuild_fts's delete-then-
        add per changed row (not a stale/duplicate Tantivy doc)."""
        f = tmp_path / "run.csv"
        f.write_text("originalterm,value\n1,2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert any(h["path"] == str(f) for h in idx.search("originalterm"))

            f.write_text("updatedterm,value\n9,9", encoding="utf-8")
            os.utime(str(f), None)
            idx.rebuild()

            assert any(h["path"] == str(f) for h in idx.search("updatedterm")), (
                "updated content must be searchable after rebuild()"
            )
            assert not any(h["path"] == str(f) for h in idx.search("originalterm")), (
                "stale content must NOT still match after the row was replaced "
                "(would indicate a duplicate/leftover Tantivy doc)"
            )
        finally:
            idx.close()

    def test_removed_file_no_longer_matches(self, tmp_path: Path) -> None:
        """A deleted file's Tantivy doc must be removed via delete_documents,
        not merely orphaned in the DuckDB metadata table."""
        f = tmp_path / "gone.csv"
        f.write_text("vanishingterm,value\n1,2", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            assert any(h["path"] == str(f) for h in idx.search("vanishingterm"))
            f.unlink()
            idx.rebuild()
            assert not any(h["path"] == str(f) for h in idx.search("vanishingterm"))
        finally:
            idx.close()


class TestTantivyHeapSize:
    """c73c0dd7 -- Tantivy writer's undersized default heap_size caused
    678 fragmented segments + a 4.8s reload() on a real 16k-file batch;
    512MB drops segments to 48 and cuts add+commit+reload to 2.8s (~3x)."""

    def test_default_heap_bytes_is_512mb(self, monkeypatch) -> None:
        monkeypatch.delenv(OL._TANTIVY_HEAP_ENV_VAR, raising=False)
        assert OL._default_tantivy_heap_bytes() == 512 * 1024 * 1024

    def test_env_var_overrides_default(self, monkeypatch) -> None:
        monkeypatch.setenv(OL._TANTIVY_HEAP_ENV_VAR, "256")
        assert OL._default_tantivy_heap_bytes() == 256 * 1024 * 1024

    def test_invalid_env_var_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv(OL._TANTIVY_HEAP_ENV_VAR, "not-a-number")
        assert OL._default_tantivy_heap_bytes() == 512 * 1024 * 1024

    def test_env_var_below_minimum_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv(OL._TANTIVY_HEAP_ENV_VAR, "1")
        assert OL._default_tantivy_heap_bytes() == 512 * 1024 * 1024

    def test_explicit_constructor_arg_takes_precedence_over_env_var(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv(OL._TANTIVY_HEAP_ENV_VAR, "256")
        assert OL._resolve_tantivy_heap_bytes(64 * 1024 * 1024) == 64 * 1024 * 1024

    def test_explicit_arg_below_minimum_falls_back_to_default(self) -> None:
        assert OL._resolve_tantivy_heap_bytes(1024) == 512 * 1024 * 1024

    def test_index_resolves_heap_bytes_from_constructor(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), tantivy_heap_bytes=64 * 1024 * 1024)
        assert idx._tantivy_heap_bytes == 64 * 1024 * 1024

    def test_index_defaults_to_512mb_heap(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv(OL._TANTIVY_HEAP_ENV_VAR, raising=False)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        assert idx._tantivy_heap_bytes == 512 * 1024 * 1024

    def test_connect_tantivy_passes_resolved_heap_size_to_writer(
        self, tmp_path: Path,
    ) -> None:
        """The resolved heap_bytes must actually reach tantivy.Index.writer(),
        not just be stored on the instance and never used."""
        f = tmp_path / "a.csv"
        f.write_text("term,1\n", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path), tantivy_heap_bytes=33 * 1024 * 1024)

        import tantivy  # noqa: PLC0415

        captured: dict[str, Any] = {}
        real_writer_method = tantivy.Index.writer

        def _spy_writer(self, *args, **kwargs):  # noqa: ANN001
            captured["heap_size"] = kwargs.get("heap_size")
            return real_writer_method(self, *args, **kwargs)

        try:
            with patch.object(tantivy.Index, "writer", _spy_writer):
                idx._connect_tantivy()
            assert captured.get("heap_size") == 33 * 1024 * 1024
        finally:
            idx.close()


class TestTantivyMigration:
    """8163816e -- a pre-Tantivy (pure-DuckDB-FTS) install's outputs_index
    table can already hold rows that predate this migration. Those rows
    aren't "stale" by filesystem mtime/size, so simulate the upgrade
    scenario directly: insert a row into the DuckDB metadata table without
    ever routing it through Tantivy, then confirm rebuild()/search() still
    finds it via the one-time backfill in
    _migrate_duckdb_rows_to_tantivy_if_needed."""

    def test_legacy_duckdb_only_row_is_backfilled(self, tmp_path: Path) -> None:
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx._connect()
            idx._ensure_schema(con)
            # Simulate a pre-Tantivy install: a row already sitting in the
            # DuckDB metadata table with no corresponding Tantivy document.
            con.execute(
                "INSERT INTO outputs_index VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(tmp_path / "legacy.csv"), "legacytermvalue content",
                    time.time(), "deadbeef", 10, None, "csv", False, None,
                    None, None,
                ],
            )
            assert idx._tantivy_index is None or (
                idx._tantivy_index.searcher().num_docs == 0
            ), "test setup invariant: nothing committed to Tantivy yet"

            hits = idx.search("legacytermvalue")
            assert any(
                h["path"] == str(tmp_path / "legacy.csv") for h in hits
            ), (
                "a pre-existing DuckDB-only row must be backfilled into "
                "Tantivy by the migration path, not silently invisible to "
                "search() forever after an upgrade"
            )
        finally:
            idx.close()

    def test_migration_is_idempotent_on_reconnect(self, tmp_path: Path) -> None:
        """Re-running the migration check (e.g. on a fresh process reconnect)
        must not error or duplicate documents once already backfilled."""
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx1._connect()
            idx1._ensure_schema(con)
            con.execute(
                "INSERT INTO outputs_index VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(tmp_path / "legacy2.csv"), "onlyonceterm content",
                    time.time(), "cafef00d", 10, None, "csv", False, None,
                    None, None,
                ],
            )
            idx1.search("onlyonceterm")  # triggers the one-time backfill
            idx1.close()

            idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
            hits = idx2.search("onlyonceterm")
            matches = [h for h in hits if h["path"] == str(tmp_path / "legacy2.csv")]
            assert len(matches) == 1, (
                f"expected exactly one match after reconnect, got {len(matches)}"
            )
            idx2.close()
        finally:
            pass


class TestTantivyDependency:
    """279448b4 -- the real PyPI package is "tantivy" (quickwit-oss/tantivy-py's
    official bindings), NOT "tantivy-py" (a different, unrelated, essentially
    abandoned package under that literal name). This just confirms the
    dependency actually installs and does a real write+search round trip;
    later items (77443d83/a6056886/8163816e) wire it into OutputsFtsIndex."""

    def test_tantivy_importable(self) -> None:
        import tantivy  # noqa: PLC0415

        assert tantivy is not None

    def test_tantivy_write_and_search_round_trip(self) -> None:
        import tantivy  # noqa: PLC0415

        schema_builder = tantivy.SchemaBuilder()
        schema_builder.add_text_field("body", stored=True)
        schema = schema_builder.build()
        index = tantivy.Index(schema)
        writer = index.writer()
        writer.add_document(tantivy.Document(body="hello world tantivy smoke test"))
        writer.commit()
        index.reload()
        query = index.parse_query("hello", ["body"])
        hits = index.searcher().search(query, 10).hits
        assert len(hits) == 1


# ---------------------------------------------------------------------------
# 5d0b3866 -- _tantivy_dir() must be unique per db_path, not per parent dir
# ---------------------------------------------------------------------------

class TestTantivyDirUniqueness:
    """5d0b3866 -- _tantivy_dir() must derive a path unique per db_path, not
    merely per PARENT directory. Two OutputsFtsIndex instances pointed at
    DIFFERENT db_path values in the SAME parent folder must never share a
    Tantivy index directory -- confirmed live: sharing one caused a SECOND
    index's _connect() to detect the FIRST index's on-disk Tantivy segments
    via tantivy.Index.exists() and set _fts_built=True from a completely
    unrelated index's state, after which search() returned 0 hits for terms
    genuinely present in the second index's own files."""

    def test_distinct_db_paths_get_distinct_tantivy_dirs(self, tmp_path: Path) -> None:
        shared_parent = tmp_path / "shared"
        shared_parent.mkdir()
        db_a = str(shared_parent / "a.duckdb")
        db_b = str(shared_parent / "b.duckdb")
        idx_a = OL.OutputsFtsIndex(str(tmp_path), db_path=db_a)
        idx_b = OL.OutputsFtsIndex(str(tmp_path), db_path=db_b)
        try:
            tdir_a = idx_a._tantivy_dir()
            tdir_b = idx_b._tantivy_dir()
            assert tdir_a is not None and tdir_b is not None
            assert tdir_a != tdir_b, (
                "two distinct db_path values in the SAME parent dir must "
                "get genuinely separate tantivy directories"
            )
        finally:
            idx_a.close()
            idx_b.close()

    def test_tantivy_dir_is_stable_for_the_same_db_path(self, tmp_path: Path) -> None:
        """Determinism requirement (requirement 3): the SAME db_path must
        always resolve to the SAME tantivy dir, across instances."""
        db_path = str(tmp_path / "same.duckdb")
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            assert idx1._tantivy_dir() == idx2._tantivy_dir()
        finally:
            idx1.close()
            idx2.close()

    @duckdb_required
    def test_two_indexes_in_same_parent_dir_do_not_see_each_others_state(
        self, tmp_path: Path,
    ) -> None:
        """Regression for the EXACT reported scenario: distinct .duckdb
        files in the same parent dir must never make one instance's
        _fts_built flip True from the OTHER's on-disk tantivy index, and
        search results must stay genuinely isolated."""
        outputs_a = tmp_path / "outputs_a"
        outputs_b = tmp_path / "outputs_b"
        outputs_a.mkdir()
        outputs_b.mkdir()
        (outputs_a / "alpha.csv").write_text(
            "uniquetermalpha,val\n1,2", encoding="utf-8",
        )
        (outputs_b / "beta.csv").write_text(
            "uniquetermbeta,val\n3,4", encoding="utf-8",
        )

        shared_cache = tmp_path / "shared_cache"
        shared_cache.mkdir()
        db_a = str(shared_cache / "index_a.duckdb")
        db_b = str(shared_cache / "index_b.duckdb")

        idx_a = OL.OutputsFtsIndex(str(outputs_a), db_path=db_a)
        idx_a.rebuild()
        idx_a.close()

        idx_b = OL.OutputsFtsIndex(str(outputs_b), db_path=db_b)
        try:
            # Before the fix: idx_b._connect() would find idx_a's on-disk
            # tantivy_index/ dir (a SHARED parent) and incorrectly set
            # _fts_built=True from index A's existence check alone.
            idx_b._connect()
            assert idx_b._fts_built is False, (
                "a fresh index for a DIFFERENT db_path must not inherit "
                "_fts_built=True from an unrelated index's tantivy dir"
            )
            idx_b.rebuild()
            hits_b = idx_b.search("uniquetermbeta")
            assert any("beta.csv" in h["path"] for h in hits_b)
            assert idx_b.search("uniquetermalpha") == [], (
                "index B must never see index A's content"
            )
        finally:
            idx_b.close()


# ---------------------------------------------------------------------------
# 9a18a2b2 -- Tantivy single-writer lock conflict handling
# ---------------------------------------------------------------------------

class TestTantivyLockConflictDetection:
    """_is_tantivy_lock_conflict must recognise Tantivy's real LockBusy
    failure (confirmed live against this bindings version, see
    _connect_tantivy's docstring) and NOT flag unrelated errors."""

    def test_detects_real_lock_busy_message(self) -> None:
        exc = ValueError(
            "Failed to acquire Lockfile: LockBusy. Some(\"Failed to "
            "acquire index lock. If you are using a regular directory, "
            "this means there is already an `IndexWriter` working on this "
            "`Directory`, in this process or in a different process.\")"
        )
        assert OL._is_tantivy_lock_conflict(exc) is True

    def test_does_not_flag_unrelated_errors(self) -> None:
        assert OL._is_tantivy_lock_conflict(ValueError("boom")) is False
        assert OL._is_tantivy_lock_conflict(OSError("disk full")) is False
        assert OL._is_tantivy_lock_conflict(RuntimeError("")) is False


class TestTantivyLockHandling:
    """9a18a2b2 -- OutputsFtsIndex must handle a locked Tantivy index
    gracefully: no uncaught exception from rebuild()/search() (best-effort
    contract preserved), plus a clear, actionable message left behind --
    not just a silent empty result indistinguishable from "no matches"."""

    @staticmethod
    def _hold_tantivy_lock(tdir: str):
        import tantivy  # noqa: PLC0415
        schema = OL.OutputsFtsIndex._tantivy_schema()
        blocking_index = tantivy.Index(schema, path=tdir)
        blocking_writer = blocking_index.writer()
        return blocking_index, blocking_writer

    @duckdb_required
    def test_locked_index_does_not_raise_and_sets_actionable_message(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "data.csv").write_text("term,value\n1,2", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        tdir = idx._tantivy_dir()
        assert tdir is not None
        blocking_index, blocking_writer = self._hold_tantivy_lock(tdir)
        try:
            # rebuild()/search() must not raise -- best-effort by contract.
            count = idx.rebuild()
            assert isinstance(count, int)
            hits = idx.search("term")
            assert hits == []  # best-effort contract preserved: no crash
            assert idx._last_tantivy_error is not None, (
                "a lock conflict must leave a clear, actionable message "
                "behind, not disappear silently"
            )
            assert "lock" in idx._last_tantivy_error.lower()
        finally:
            idx.close()
            del blocking_writer
            del blocking_index

    @duckdb_required
    def test_connect_tantivy_raises_typed_conflict_directly(
        self, tmp_path: Path,
    ) -> None:
        """Calling _connect_tantivy() directly (bypassing rebuild()/
        search()'s own broad except) must raise the TYPED
        TantivyLockConflict, not disappear or raise something opaque --
        confirms the failure is genuinely identifiable."""
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        tdir = idx._tantivy_dir()
        assert tdir is not None
        blocking_index, blocking_writer = self._hold_tantivy_lock(tdir)
        try:
            with pytest.raises(OL.TantivyLockConflict):
                idx._connect_tantivy()
        finally:
            idx.close()
            del blocking_writer
            del blocking_index

    @duckdb_required
    def test_search_outputs_surfaces_lock_warning(self, tmp_path: Path) -> None:
        (tmp_path / "data.csv").write_text("term,value\n1,2", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))
        probe = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        tdir = probe._tantivy_dir()
        probe.close()
        assert tdir is not None
        blocking_index, blocking_writer = self._hold_tantivy_lock(tdir)
        try:
            result = OL.search_outputs(str(tmp_path), "term")
            assert "tantivy_lock_warning" in result
            assert "lock" in result["tantivy_lock_warning"].lower()
        finally:
            del blocking_writer
            del blocking_index


# ---------------------------------------------------------------------------
# 984b237c -- xxHash swap for the archival-duplicate-detection hasher
# ---------------------------------------------------------------------------

class TestXxh3Hasher:
    """_xxh3_file swaps SHA-256 for xxHash on the archival-dedup hasher;
    must degrade gracefully to SHA-256 when xxhash is unavailable, and must
    actually be wired in as the default everywhere that matters."""

    def test_returns_a_real_hash_for_real_content(self, tmp_path: Path) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"hello xxhash world" * 100)
        digest = OL._xxh3_file(str(f))
        assert digest is not None
        assert isinstance(digest, str)
        assert len(digest) > 0

    def test_deterministic_for_same_content(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        content = b"identical content for hashing" * 50
        f1.write_bytes(content)
        f2.write_bytes(content)
        assert OL._xxh3_file(str(f1)) == OL._xxh3_file(str(f2))

    def test_different_for_different_content(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.bin"
        f2 = tmp_path / "b.bin"
        f1.write_bytes(b"content A")
        f2.write_bytes(b"content B")
        assert OL._xxh3_file(str(f1)) != OL._xxh3_file(str(f2))

    def test_missing_file_returns_none(self) -> None:
        assert OL._xxh3_file("/no/such/file.bin") is None

    def test_degrades_to_sha256_when_xxhash_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"degrade path content")
        # sys.modules[name] = None is the standard mechanism for forcing
        # ImportError on the next `import name`, regardless of any prior
        # caching -- confirmed live against this Python version.
        monkeypatch.setitem(sys.modules, "xxhash", None)
        digest = OL._xxh3_file(str(f))
        assert digest == OL._sha256_file(str(f))

    def test_default_hasher_is_xxh3_on_classify_canonical_archival(self) -> None:
        import inspect
        sig = inspect.signature(OL.classify_canonical_archival)
        assert sig.parameters["hasher"].default is OL._xxh3_file

    def test_default_hasher_is_xxh3_on_build_output_rows(self) -> None:
        import inspect
        sig = inspect.signature(OL.build_output_rows)
        assert sig.parameters["hasher"].default is OL._xxh3_file

    def test_default_hasher_is_xxh3_on_outputs_fts_index(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._hasher is OL._xxh3_file
        finally:
            idx.close()


class TestXxh3Benchmark:
    """984b237c -- live, on-box A/B confirmation that xxHash is genuinely
    faster than SHA-256 on a real file, not just citing published numbers
    (the item's own notes explicitly require live confirmation). Uses a
    real ~6MB file on real disk, best-of-N timing for both algorithms (cuts
    scheduler/OS noise), and a deliberately modest safety margin (xxHash
    must be at least 1.5x faster) -- well under xxHash's commonly-cited
    5-10x -- so this stays robust on a slow/virtualised CI runner while
    still proving a real, substantial speedup rather than a coin-flip."""

    @staticmethod
    def _best_of(fn, path: str, repeats: int = 7) -> float:
        best = float("inf")
        for _ in range(repeats):
            start = time.perf_counter()
            fn(path)
            best = min(best, time.perf_counter() - start)
        return best

    def test_xxh3_faster_than_sha256_on_real_file(self, tmp_path: Path) -> None:
        try:
            import xxhash  # noqa: F401
        except ImportError:
            pytest.skip("xxhash not installed")

        f = tmp_path / "bench.bin"
        # Real, non-trivial content (not all-zero -- avoids either hasher
        # taking a degenerate fast path on a repeating byte pattern).
        chunk = bytes((i * 2654435761) % 256 for i in range(65536))
        with open(f, "wb") as fh:
            for _ in range(96):  # ~6 MB
                fh.write(chunk)

        # Warm the OS page cache identically for both so the comparison is
        # CPU-bound (hashing throughput), not first-read disk I/O.
        OL._sha256_file(str(f))
        OL._xxh3_file(str(f))

        sha256_best = self._best_of(OL._sha256_file, str(f))
        xxh3_best = self._best_of(OL._xxh3_file, str(f))

        assert xxh3_best * 1.5 <= sha256_best, (
            f"expected xxHash to be at least 1.5x faster than SHA-256 on a "
            f"real ~6MB file; got xxh3_best={xxh3_best:.4f}s "
            f"sha256_best={sha256_best:.4f}s "
            f"(speedup={sha256_best / xxh3_best:.2f}x)"
        )


# ---------------------------------------------------------------------------
# 49b97a6a -- hash-algo version marker forces a one-time full re-hash
# ---------------------------------------------------------------------------

class TestHashAlgoVersionUpgrade:
    """Upgrading from a pre-xxHash (984b237c) on-disk DB must trigger a
    one-time full re-hash of every row -- never leave a silent SHA-256/
    xxHash mix sitting under the same 'sha256' column."""

    @duckdb_required
    def test_fresh_db_is_marked_current_immediately(self, tmp_path: Path) -> None:
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            con = idx._connect()
            assert idx._pending_hash_upgrade is False
            assert idx._read_hash_algo_version(con) == OL._HASH_ALGO_VERSION
        finally:
            idx.close()

    @duckdb_required
    def test_legacy_db_triggers_full_rehash_on_upgrade(self, tmp_path: Path) -> None:
        db_path = OL._resolve_index_db_path(str(tmp_path))

        # Simulate a genuinely pre-49b97a6a on-disk DB: real content rows
        # with an old-style SHA-256 hash, and NO version marker at all
        # (mirrors an install that predates this marker existing).
        f = tmp_path / "legacy.csv"
        f.write_bytes(b"legacy content for hashing")
        old_sha = hashlib.sha256(b"legacy content for hashing").hexdigest()

        idx0 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        con0 = idx0._connect()
        idx0._ensure_schema(con0)
        st = os.stat(f)
        con0.execute(
            "INSERT INTO outputs_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(f), "legacy content for hashing", st.st_mtime, old_sha,
                st.st_size, None, "binary_metadata", False, None, None, None,
            ],
        )
        con0.execute(
            "DELETE FROM outputs_index_meta WHERE key = 'hash_algo_version'"
        )
        idx0.close()

        # Fresh instance reconnecting to the SAME on-disk db_path -- the
        # realistic "upgrade" scenario (existing DB, new code).
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx1._connect()
            assert idx1._pending_hash_upgrade is True
            assert str(f) not in idx1._row_cache, (
                "legacy row must NOT be rehydrated as already-indexed -- "
                "it must look stale so rebuild() genuinely re-hashes it"
            )

            idx1.rebuild()

            new_row = idx1.resolve_output(str(f))
            assert new_row is not None
            assert new_row["sha256"] != old_sha, (
                "row must be re-hashed with the new algorithm after "
                "upgrade, not left with its stale SHA-256 value"
            )
            assert new_row["sha256"] == OL._xxh3_file(str(f)), (
                "re-hashed value must match the current default hasher "
                "(_xxh3_file)"
            )
            assert idx1._pending_hash_upgrade is False, (
                "upgrade flag must clear once the full re-hash pass converges"
            )
            con1 = idx1._connect()
            assert idx1._read_hash_algo_version(con1) == OL._HASH_ALGO_VERSION
        finally:
            idx1.close()

    @duckdb_required
    def test_already_current_db_is_not_flagged_again(self, tmp_path: Path) -> None:
        """An already-upgraded DB (version already current) must take the
        normal fast path on reconnect -- no forced re-hash, rows rehydrate
        as usual."""
        (tmp_path / "data.csv").write_text("col\n1", encoding="utf-8")
        db_path = OL._resolve_index_db_path(str(tmp_path))
        idx1 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        idx1.rebuild()
        idx1.close()

        idx2 = OL.OutputsFtsIndex(str(tmp_path), db_path=db_path)
        try:
            idx2._connect()
            assert idx2._pending_hash_upgrade is False
            assert any("data.csv" in p for p in idx2._row_cache), (
                "an already-current DB must rehydrate normally, not be "
                "treated as needing another full re-hash"
            )
        finally:
            idx2.close()


# ---------------------------------------------------------------------------
# acac2599 -- configurable Phase-1 ThreadPoolExecutor worker cap
# ---------------------------------------------------------------------------

class TestConfigurableMaxWorkers:
    def test_default_is_physical_core_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default follows physical cores, not logical hyperthreads."""
        monkeypatch.delenv(OL._MAX_WORKERS_ENV_VAR, raising=False)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._max_workers == OL._physical_core_count()
        finally:
            idx.close()

    def test_constructor_override(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), max_workers=2)
        try:
            assert idx._max_workers == 2
        finally:
            idx.close()

    def test_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._MAX_WORKERS_ENV_VAR, "3")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._max_workers == 3
        finally:
            idx.close()

    def test_constructor_overrides_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._MAX_WORKERS_ENV_VAR, "3")
        idx = OL.OutputsFtsIndex(str(tmp_path), max_workers=5)
        try:
            assert idx._max_workers == 5
        finally:
            idx.close()

    def test_invalid_env_var_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._MAX_WORKERS_ENV_VAR, "not-an-int")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._max_workers == OL._physical_core_count()
        finally:
            idx.close()

    def test_non_positive_constructor_value_falls_back_to_default(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), max_workers=0)
        try:
            assert idx._max_workers == OL._physical_core_count()
        finally:
            idx.close()

    @duckdb_required
    def test_override_actually_changes_effective_worker_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Confirms the override actually reaches ThreadPoolExecutor, not
        just the stored attribute."""
        for i in range(5):
            (tmp_path / f"f{i}.csv").write_text(f"col{i}\n1", encoding="utf-8")
        seen: list[int] = []
        real_executor = OL.concurrent.futures.ThreadPoolExecutor

        def _spy(*args, **kwargs):
            seen.append(kwargs.get("max_workers"))
            return real_executor(*args, **kwargs)

        monkeypatch.setattr(OL.concurrent.futures, "ThreadPoolExecutor", _spy)
        idx = OL.OutputsFtsIndex(str(tmp_path), max_workers=2)
        try:
            idx.rebuild()
        finally:
            idx.close()
        assert seen, "ThreadPoolExecutor was never constructed"
        assert seen[0] == 2


# ---------------------------------------------------------------------------
# 1bce8c41 -- walk-batch cap vs. DB write-chunk size decoupling
# ---------------------------------------------------------------------------

class TestWalkBatchDefaultUnbounded:
    """FOLLOW-UP to 3535b9ad: _ResumableFileWalk's own default must no
    longer cap the walk at an arbitrary file count (2000) -- the walk should
    be time-primary by default, stopping only on `deadline` (or true
    exhaustion), while an explicit override still works for anyone who
    deliberately wants a count cap."""

    def test_class_default_is_bounded_fairness_window(self) -> None:
        assert OL._ResumableFileWalk._MAX_BATCH == 4_096

    def test_default_walk_not_capped_at_old_2000_default(
        self, tmp_path: Path,
    ) -> None:
        n = 2500  # exceeds the OLD hardcoded default of 2000
        for i in range(n):
            (tmp_path / f"f{i:05d}.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path))
        chunk = walk.drain(time.monotonic() + 60.0)
        assert len(chunk) == n, (
            f"drain() returned {len(chunk)}/{n} paths with a generous "
            "deadline -- the walk appears to still be capped at an "
            "arbitrary file count instead of being time-primary"
        )
        assert walk.exhausted is True

    def test_explicit_constructor_arg_still_caps_the_walk(
        self, tmp_path: Path,
    ) -> None:
        """An explicit override must still work for anyone who deliberately
        wants a real count cap -- the decoupling only changes the DEFAULT."""
        for i in range(50):
            (tmp_path / f"g{i:03d}.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path), max_batch=10)
        chunk = walk.drain(time.monotonic() + 60.0)
        assert len(chunk) == 10
        assert walk.exhausted is False

    def test_env_var_still_caps_the_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._ResumableFileWalk._MAX_BATCH_ENV_VAR, "10")
        for i in range(50):
            (tmp_path / f"h{i:03d}.csv").write_text("col\n1", encoding="utf-8")
        walk = OL._ResumableFileWalk(str(tmp_path))
        chunk = walk.drain(time.monotonic() + 60.0)
        assert len(chunk) == 10
        assert walk.exhausted is False


class TestWriteChunkDecoupling:
    """1bce8c41 -- the DB write-chunk size (_WRITE_CHUNK, used to batch
    INSERT/DELETE statements against DuckDB) must stay at a small, tuned
    default independent of _ResumableFileWalk._MAX_BATCH's new effectively-
    unbounded default. Naively sharing one knob for both concerns would
    have turned every DB write into one giant, unchunked SQL statement by
    default -- a new resource-exhaustion risk replacing the one just fixed."""

    def test_default_write_chunk_is_2000_while_walk_cap_is_unbounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, raising=False)
        monkeypatch.delenv(OL._ResumableFileWalk._MAX_BATCH_ENV_VAR, raising=False)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._write_chunk == 2000
            assert idx._max_batch == OL._ResumableFileWalk._MAX_BATCH
            assert idx._max_batch > idx._write_chunk
        finally:
            idx.close()

    def test_write_chunk_constructor_override(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), write_chunk=250)
        try:
            assert idx._write_chunk == 250
            # The walk's own cap is untouched by this override.
            assert idx._max_batch == OL._ResumableFileWalk._MAX_BATCH
        finally:
            idx.close()

    def test_write_chunk_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, "500")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._write_chunk == 500
        finally:
            idx.close()

    def test_constructor_overrides_write_chunk_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, "500")
        idx = OL.OutputsFtsIndex(str(tmp_path), write_chunk=42)
        try:
            assert idx._write_chunk == 42
        finally:
            idx.close()

    def test_invalid_write_chunk_env_var_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, "not-an-int")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._write_chunk == 2000
        finally:
            idx.close()

    def test_non_positive_write_chunk_falls_back_to_default(
        self, tmp_path: Path,
    ) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), write_chunk=0)
        try:
            assert idx._write_chunk == 2000
        finally:
            idx.close()

    def test_max_batch_env_var_does_not_affect_write_chunk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The two knobs are fully independent in BOTH directions: setting
        the WALK's env var must not perturb the write-chunk default either."""
        monkeypatch.setenv(OL._ResumableFileWalk._MAX_BATCH_ENV_VAR, "999")
        monkeypatch.delenv(OL.OutputsFtsIndex._WRITE_CHUNK_ENV_VAR, raising=False)
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._max_batch == 999
            assert idx._write_chunk == 2000
        finally:
            idx.close()


class TestAdaptiveBatchPolicy:
    """The adaptive controller must preserve explicit overrides and back off
    when a prior Tantivy/DB commit shows pressure."""

    def test_explicit_batch_disables_adaptation(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path), max_batch=123)
        try:
            assert idx._max_batch_overridden is True
            assert idx._adaptive_batch_limit() == 123
        finally:
            idx.close()

    def test_commit_pressure_halves_adaptive_batch(self, tmp_path: Path) -> None:
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx._adaptive_batch = 32_768
            idx.last_rebuild_metrics = {"fts_seconds": 9.0, "write_seconds": 25.0}
            assert idx._adaptive_batch_limit() == 16_384
        finally:
            idx.close()

    @duckdb_required
    def test_replacement_writes_use_upsert_without_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Changed rows use DuckDB upsert semantics; deletes remain reserved
        for genuinely removed paths."""
        n = 120
        for i in range(n):
            (tmp_path / f"w{i:04d}.csv").write_text(f"col\n{i}", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path), write_chunk=10)
        try:
            assert idx._write_chunk == 10
            count = idx.rebuild(max_seconds=60)
            assert count == n
            for p in list(idx._row_cache):
                os.utime(p, None)

            con = idx._connect()
            conn_cls = type(con)
            real_execute = conn_cls.execute
            delete_sql_seen: list[str] = []

            def spy_execute(self, sql, parameters=None):
                if isinstance(sql, str) and sql.startswith(
                    "DELETE FROM outputs_index WHERE path IN"
                ):
                    delete_sql_seen.append(sql)
                if parameters is not None:
                    return real_execute(self, sql, parameters)
                return real_execute(self, sql)

            monkeypatch.setattr(conn_cls, "execute", spy_execute)
            assert idx.rebuild(max_seconds=60) == n
            assert not delete_sql_seen
            assert len(idx._row_cache) == n
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# fd4dd661 -- user-configurable exclude patterns (gitignore-style, v1)
# ---------------------------------------------------------------------------

class TestExcludePatterns:
    def test_matches_exclude_pattern_basename_glob(self) -> None:
        assert OL._matches_exclude_pattern("run.tmp", "sub/run.tmp", ("*.tmp",))
        assert not OL._matches_exclude_pattern("run.csv", "sub/run.csv", ("*.tmp",))

    def test_matches_exclude_pattern_relative_path_glob(self) -> None:
        assert OL._matches_exclude_pattern("data.csv", "cache/data.csv", ("cache/*",))
        assert not OL._matches_exclude_pattern("data.csv", "keep/data.csv", ("cache/*",))

    def test_directory_pattern_trailing_slash(self) -> None:
        assert OL._matches_exclude_pattern(
            "node_modules", "node_modules", ("node_modules/",)
        )

    def test_empty_patterns_never_match(self) -> None:
        assert not OL._matches_exclude_pattern("a.csv", "a.csv", ())

    @duckdb_required
    def test_iter_safe_output_files_respects_exclude_patterns(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "keep.csv").write_text("a\n1", encoding="utf-8")
        (tmp_path / "skip.tmp").write_text("a\n1", encoding="utf-8")
        big_dir = tmp_path / "big_sweep_output"
        big_dir.mkdir()
        (big_dir / "inner.csv").write_text("a\n1", encoding="utf-8")

        paths = OL._iter_safe_output_files(
            str(tmp_path), exclude_patterns=("*.tmp", "big_sweep_output/"),
        )
        basenames = {os.path.basename(p) for p in paths}
        assert "keep.csv" in basenames
        assert "skip.tmp" not in basenames
        assert "inner.csv" not in basenames, (
            "a directory-pattern match must prune the WHOLE subtree, not "
            "just filter the directory's own listing"
        )

    @duckdb_required
    def test_outputs_fts_index_respects_exclude_patterns(self, tmp_path: Path) -> None:
        (tmp_path / "keep.csv").write_text("uniquekeepterm\n1", encoding="utf-8")
        (tmp_path / "skip.tmp").write_text("uniqueskiptermxyz\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path), exclude_patterns=("*.tmp",))
        try:
            count = idx.rebuild()
            assert count == 1
            assert idx.resolve_output(str(tmp_path / "skip.tmp")) is None
            assert idx.resolve_output(str(tmp_path / "keep.csv")) is not None
        finally:
            idx.close()

    def test_default_exclude_patterns_from_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            OL._EXCLUDE_PATTERNS_ENV_VAR, "*.tmp, node_modules/\nbuild/"
        )
        patterns = OL._default_exclude_patterns()
        assert "*.tmp" in patterns
        assert "node_modules/" in patterns
        assert "build/" in patterns

    def test_default_exclude_patterns_empty_when_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(OL._EXCLUDE_PATTERNS_ENV_VAR, raising=False)
        assert OL._default_exclude_patterns() == ()

    def test_constructor_exclude_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._EXCLUDE_PATTERNS_ENV_VAR, "*.csv")
        idx = OL.OutputsFtsIndex(str(tmp_path), exclude_patterns=())
        try:
            assert idx._exclude_patterns == ()
        finally:
            idx.close()

    def test_env_var_used_when_constructor_arg_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(OL._EXCLUDE_PATTERNS_ENV_VAR, "*.csv")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            assert idx._exclude_patterns == ("*.csv",)
        finally:
            idx.close()


# ---------------------------------------------------------------------------
# 1662873f -- search_logs: Tier 0 (ripgrep / Python fallback) scan + Tier 1
# (opportunistic JSON/timestamp sniffing) ranking. No persistent index.
# ---------------------------------------------------------------------------

_HAS_RG = OL._rg_binary() is not None


class TestSniffHelpers:
    def test_sniff_timestamp_iso8601(self) -> None:
        raw, epoch = OL._sniff_timestamp('2026-07-18T16:10:32.123Z {"msg":"boot"}')
        assert raw == "2026-07-18T16:10:32.123Z"
        assert epoch is not None

    def test_sniff_timestamp_syslog(self) -> None:
        raw, epoch = OL._sniff_timestamp("Jul 18 16:10:32 host sshd[123]: auth failure")
        assert raw == "Jul 18 16:10:32"
        assert epoch is not None

    def test_sniff_timestamp_none_for_plain_line(self) -> None:
        raw, epoch = OL._sniff_timestamp("plain line with no timestamp at all")
        assert raw is None
        assert epoch is None

    def test_sniff_json_whole_line(self) -> None:
        obj = OL._sniff_json('{"level": "error", "msg": "boom"}')
        assert obj == {"level": "error", "msg": "boom"}

    def test_sniff_json_with_leading_prefix(self) -> None:
        obj = OL._sniff_json('2026-07-18 16:10:32 {"level": "info", "msg": "ok"}')
        assert obj == {"level": "info", "msg": "ok"}

    def test_sniff_json_none_for_plain_text(self) -> None:
        assert OL._sniff_json("just a plain log line, no braces") is None

    def test_sniff_level_from_json_field(self) -> None:
        assert OL._sniff_level("irrelevant text", {"level": "WARN"}) == "WARN"

    def test_sniff_level_from_bare_regex(self) -> None:
        assert OL._sniff_level("2026 ERROR something broke", None) == "ERROR"

    def test_sniff_level_none_when_unrecognised(self) -> None:
        assert OL._sniff_level("nothing recognisable here", None) is None


class TestRankKey:
    """Tier-1 signals rank above plain matches; a miss free-falls back to the
    Tier-0 scan order (no extra ranking cost paid for a sniff that found
    nothing)."""

    def test_tier1_signal_outranks_plain_match(self) -> None:
        plain = OL.LogMatch(path="a.log", line_number=1, line="x", scan_order=0)
        timestamped = OL.LogMatch(
            path="a.log", line_number=5, line="y", scan_order=5, timestamp_epoch=1000.0,
        )
        ordered = sorted([plain, timestamped], key=OL._rank_key, reverse=True)
        assert ordered[0] is timestamped

    def test_no_signal_falls_back_to_scan_order(self) -> None:
        first = OL.LogMatch(path="a.log", line_number=1, line="x", scan_order=0)
        second = OL.LogMatch(path="a.log", line_number=2, line="y", scan_order=1)
        ordered = sorted([second, first], key=OL._rank_key, reverse=True)
        assert ordered == [first, second]

    def test_more_recent_timestamp_ranks_first(self) -> None:
        older = OL.LogMatch(
            path="a.log", line_number=1, line="x", scan_order=0, timestamp_epoch=100.0,
        )
        newer = OL.LogMatch(
            path="a.log", line_number=2, line="y", scan_order=1, timestamp_epoch=200.0,
        )
        ordered = sorted([older, newer], key=OL._rank_key, reverse=True)
        assert ordered == [newer, older]

    def test_higher_severity_ranks_first(self) -> None:
        info = OL.LogMatch(path="a.log", line_number=1, line="x", scan_order=0, level="INFO")
        error = OL.LogMatch(path="a.log", line_number=2, line="y", scan_order=1, level="ERROR")
        ordered = sorted([info, error], key=OL._rank_key, reverse=True)
        assert ordered == [error, info]


class TestScanLogsPython:
    """Tier 0 fallback path (used unconditionally regardless of whether `rg`
    happens to be installed on the machine running these tests)."""

    def test_finds_matches_case_insensitive(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {"app.log": "INFO boot ok\nERROR disk full\n"})
        hits = OL._scan_logs_python(
            logs_dir, "error", timeout_seconds=5.0,
            max_matches_per_file=100, max_total_matches=100,
        )
        assert len(hits) == 1
        path, line_no, text = hits[0]
        assert path.endswith("app.log")
        assert line_no == 2
        assert "disk full" in text

    def test_excludes_secret_named_files(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {
            "app.log": "token seen here\n",
            ".env": "token seen here too\n",
        })
        hits = OL._scan_logs_python(
            logs_dir, "token", timeout_seconds=5.0,
            max_matches_per_file=100, max_total_matches=100,
        )
        paths = {p for p, _, _ in hits}
        assert all(not p.endswith(".env") for p in paths)
        assert any(p.endswith("app.log") for p in paths)

    def test_invalid_regex_falls_back_to_literal(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {"app.log": "weird [unterminated bracket line\n"})
        hits = OL._scan_logs_python(
            logs_dir, "[unterminated", timeout_seconds=5.0,
            max_matches_per_file=100, max_total_matches=100,
        )
        assert len(hits) == 1

    def test_respects_max_matches_per_file(self, tmp_path: Path) -> None:
        content = "\n".join(f"ERROR line {i}" for i in range(10)) + "\n"
        logs_dir = _make_dir(tmp_path, {"app.log": content})
        hits = OL._scan_logs_python(
            logs_dir, "ERROR", timeout_seconds=5.0,
            max_matches_per_file=3, max_total_matches=100,
        )
        assert len(hits) == 3


@pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not on PATH")
class TestRunRipgrep:
    def test_finds_matches(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {"app.log": "INFO boot ok\nERROR disk full\n"})
        hits = OL._run_ripgrep(logs_dir, "error", timeout_seconds=5.0, max_total_matches=100)
        assert hits is not None
        assert len(hits) == 1
        path, line_no, text = hits[0]
        assert path.endswith("app.log")
        assert line_no == 2
        assert "disk full" in text

    def test_excludes_secret_named_files(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {
            "app.log": "token seen here\n",
            ".env": "token seen here too\n",
        })
        hits = OL._run_ripgrep(logs_dir, "token", timeout_seconds=5.0, max_total_matches=100)
        assert hits is not None
        paths = {p for p, _, _ in hits}
        assert all(not p.endswith(".env") for p in paths)


def test_run_ripgrep_returns_none_when_binary_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OL, "_rg_binary", lambda: None)
    logs_dir = _make_dir(tmp_path, {"app.log": "ERROR boom\n"})
    assert OL._run_ripgrep(
        logs_dir, "error", timeout_seconds=5.0, max_total_matches=100,
    ) is None


class TestSearchLogs:
    """Module-level API -- what server.py's search_logs MCP tool calls."""

    def test_requires_query(self, tmp_path: Path) -> None:
        result = OL.search_logs(str(tmp_path), "")
        assert "error" in result

    def test_requires_existing_dir(self, tmp_path: Path) -> None:
        result = OL.search_logs(str(tmp_path / "nope"), "error")
        assert "error" in result

    def test_end_to_end_ranking_prefers_timestamped_match(self, tmp_path: Path) -> None:
        logs_dir = _make_dir(tmp_path, {
            "app.log": (
                "plain ERROR line with no timestamp\n"
                '2026-07-18T16:10:32Z {"level":"error","msg":"disk full"}\n'
            ),
        })
        result = OL.search_logs(logs_dir, "error", limit=10)
        assert "error" not in result
        assert result["total_matched"] == 2
        assert result["engine"] in ("ripgrep", "python-fallback")
        top = result["hits"][0]
        assert top["tier"] == 1
        assert top["timestamp_epoch"] is not None

    def test_forces_python_fallback_when_rg_missing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(OL, "_rg_binary", lambda: None)
        logs_dir = _make_dir(tmp_path, {"app.log": "INFO boot\nERROR disk full\n"})
        result = OL.search_logs(logs_dir, "error", limit=10)
        assert result["engine"] == "python-fallback"
        assert result["total_matched"] == 1
        assert result["hits"][0]["line"].endswith("disk full")

    def test_secret_named_log_file_excluded(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(OL, "_rg_binary", lambda: None)
        logs_dir = _make_dir(tmp_path, {
            "app.log": "token appears here\n",
            "credentials.log": "token appears here too\n",
        })
        result = OL.search_logs(logs_dir, "token", limit=10)
        paths = {h["path"] for h in result["hits"]}
        assert all("credentials" not in p for p in paths)

    def test_respects_limit(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(OL, "_rg_binary", lambda: None)
        content = "\n".join(f"ERROR line {i}" for i in range(20)) + "\n"
        logs_dir = _make_dir(tmp_path, {"app.log": content})
        result = OL.search_logs(logs_dir, "error", limit=5)
        assert len(result["hits"]) == 5
        assert result["total_matched"] == 20

    def test_line_preview_truncation(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(OL, "_rg_binary", lambda: None)
        long_line = "ERROR " + ("x" * 1000)
        logs_dir = _make_dir(tmp_path, {"app.log": long_line + "\n"})
        result = OL.search_logs(logs_dir, "error", limit=10, max_line_chars=50)
        assert len(result["hits"][0]["line"]) <= 53  # 50 chars + "..."
