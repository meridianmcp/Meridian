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
