"""MDE-6 -- BM25/Tantivy cold-start, dependency verification, and
deterministic fallback hardening.

Scope (see docs/meridian-docs-research-grade-investigation-2026-08-23.md,
MDE-6): make BM25 installation and warm-start behavior explicit, verify
Tantivy/xxhash at startup with a clear diagnostic (never a swallowed
exception), guarantee a deterministic fallback search when Tantivy is
unavailable/corrupted, and make sure a cold/corrupted index can never
produce a false zero-hit result or crash the server.

Fully local, no hosted call, no network -- consistent with the rest of the
meridian_outputs package.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from meridian_outputs import outputs_local as OL

try:
    import duckdb  # noqa: F401
    _DUCKDB_AVAILABLE = True
except ImportError:
    _DUCKDB_AVAILABLE = False

duckdb_required = pytest.mark.skipif(
    not _DUCKDB_AVAILABLE, reason="duckdb not installed",
)

try:
    import tantivy  # noqa: F401
    _TANTIVY_AVAILABLE = True
except ImportError:
    _TANTIVY_AVAILABLE = False

tantivy_required = pytest.mark.skipif(
    not _TANTIVY_AVAILABLE, reason="tantivy not installed",
)


@pytest.fixture(autouse=True)
def _reset_dependency_cache(monkeypatch: pytest.MonkeyPatch):
    """verify_search_dependencies() caches module-level, process-wide --
    isolate each test from any override a previous test made, and force a
    fresh (real) probe on the next call in THIS test unless a test
    explicitly overrides it itself."""
    monkeypatch.setattr(OL, "_SEARCH_DEPENDENCY_CACHE", None)
    yield
    monkeypatch.setattr(OL, "_SEARCH_DEPENDENCY_CACHE", None)


# ---------------------------------------------------------------------------
# 1. Dependency verification
# ---------------------------------------------------------------------------

class TestVerifySearchDependencies:
    def test_probe_missing_module_reports_unavailable_with_diagnostic(self) -> None:
        info = OL._probe_optional_dependency(
            "this_module_definitely_does_not_exist_mde6", "install it somehow",
        )
        assert info["available"] is False
        assert info["version"] is None
        assert info["error"]  # non-empty diagnostic, not a swallowed exception
        assert "ModuleNotFoundError" in info["error"] or "ImportError" in info["error"]
        assert info["install_hint"] == "install it somehow"

    def test_probe_real_module_reports_available(self) -> None:
        info = OL._probe_optional_dependency("json", "n/a")
        assert info["available"] is True
        assert info["error"] is None
        assert info["install_hint"] is None

    @tantivy_required
    def test_real_environment_reports_tantivy_and_xxhash_available(self) -> None:
        """This repo declares both as real dependencies (pixi.toml,
        pyproject.toml, item 52cbe5d8) -- in a correctly provisioned
        pixi environment both must verify as importable.

        2026-09-24 -- the registry also carries "detect_secrets" now (a
        genuine optional extra backing has_secret_content(), same class as
        blake3: never installed in the standard pixi env, so its own
        "available" is NOT asserted here, only that the key exists in the
        registry's shape)."""
        result = OL.verify_search_dependencies(force=True)
        assert set(result) == {"tantivy", "xxhash", "detect_secrets"}
        assert result["tantivy"]["available"] is True
        assert result["tantivy"]["version"] is not None

    def test_result_is_cached_across_calls(self) -> None:
        first = OL.verify_search_dependencies()
        second = OL.verify_search_dependencies()
        assert first is second  # exact same cached object, no re-probe

    def test_force_true_reprobes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = OL.verify_search_dependencies()
        forced = OL.verify_search_dependencies(force=True)
        assert forced is not first  # a fresh dict, even though contents match

    def test_missing_dependency_logs_a_clear_warning_not_silently(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The core MDE-6 requirement: a missing dependency must be
        diagnosed loudly (WARNING), never just a swallowed exception deep
        inside some other call."""
        real_probe = OL._probe_optional_dependency

        def _fake_probe(name: str, hint: str):
            if name == "tantivy":
                return {
                    "available": False, "version": None,
                    "error": "ModuleNotFoundError: simulated for test",
                    "install_hint": hint,
                }
            return real_probe(name, hint)

        monkeypatch.setattr(OL, "_probe_optional_dependency", _fake_probe)
        with caplog.at_level("WARNING", logger="meridian_outputs.outputs_local"):
            result = OL.verify_search_dependencies(force=True)
        assert result["tantivy"]["available"] is False
        assert any(
            "tantivy" in rec.message and "simulated for test" in rec.message
            for rec in caplog.records
        ), f"expected a WARNING naming the missing dependency -- got {caplog.records!r}"

    def test_xxh3_file_degrades_via_verified_cache_when_xxhash_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_xxh3_file's degrade-to-sha256 path must consult the SAME
        verified/cached dependency state, not a separate silent
        try/except with no diagnostic (the pre-MDE-6 behavior)."""
        target = tmp_path / "f.bin"
        target.write_bytes(b"hello world")
        monkeypatch.setattr(OL, "_SEARCH_DEPENDENCY_CACHE", {
            "tantivy": {"available": True, "version": "x", "error": None, "install_hint": None},
            "xxhash": {"available": False, "version": None,
                       "error": "simulated", "install_hint": "install xxhash"},
        })
        digest = OL._xxh3_file(str(target))
        # SHA-256 hex digests are 64 chars; xxh3_128 hex digests are 32.
        assert digest is not None
        assert len(digest) == 64, "must have fallen back to _sha256_file"


# ---------------------------------------------------------------------------
# 2. Cold-start / corrupted-index hardening
# ---------------------------------------------------------------------------

@tantivy_required
class TestTantivyColdStartHardening:
    @staticmethod
    def _corrupt_meta_json(tdir: str) -> None:
        os.makedirs(tdir, exist_ok=True)
        with open(os.path.join(tdir, "meta.json"), "w", encoding="utf-8") as fh:
            fh.write("{this is not valid tantivy meta.json!!!")

    def test_open_cold_start_safe_recovers_from_corrupted_directory(
        self, tmp_path: Path,
    ) -> None:
        tdir = str(tmp_path / "tantivy_index__test__deadbeef")
        self._corrupt_meta_json(tdir)
        schema = OL.OutputsFtsIndex._tantivy_schema()

        index, warning = OL._open_tantivy_index_cold_start_safe(schema, tdir)

        assert index is not None
        assert warning is not None
        assert "corrupt" in warning.lower()
        # A real, usable index at the ORIGINAL path -- a writer can open.
        writer = index.writer()
        writer.add_document(tantivy.Document(path="p", content="hello"))
        writer.commit()

        # The corrupted directory was quarantined (renamed aside), not
        # deleted -- forensics-preserving, and the original path now holds
        # a fresh, working index instead of the corrupted one.
        siblings = os.listdir(tmp_path)
        quarantine_dirs = [
            s for s in siblings
            if s.startswith(os.path.basename(tdir) + ".corrupt-")
        ]
        assert len(quarantine_dirs) == 1, f"expected one quarantine dir, got {siblings!r}"
        with open(
            os.path.join(str(tmp_path), quarantine_dirs[0], "meta.json"),
            encoding="utf-8",
        ) as fh:
            assert "not valid" in fh.read(), (
                "the quarantined copy must retain the original corrupted "
                "content for forensics, not be wiped"
            )

    def test_open_cold_start_safe_never_touches_a_healthy_index(
        self, tmp_path: Path,
    ) -> None:
        """A fresh empty directory, or a genuinely healthy existing index,
        must open cleanly with NO warning -- cold-start recovery is only
        for the corrupted case."""
        tdir = str(tmp_path / "tantivy_index__healthy__cafef00d")
        os.makedirs(tdir, exist_ok=True)
        schema = OL.OutputsFtsIndex._tantivy_schema()
        index, warning = OL._open_tantivy_index_cold_start_safe(schema, tdir)
        assert warning is None
        assert index is not None
        assert os.listdir(tmp_path) == [os.path.basename(tdir)], (
            "no quarantine directory should be created for a healthy open"
        )

    def test_open_cold_start_safe_raises_typed_error_when_unrecoverable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When even the post-quarantine fresh open fails, this must raise
        the typed, documented TantivyIndexUnavailable -- not crash with an
        opaque error, and not silently pretend to succeed."""
        tdir = str(tmp_path / "tantivy_index__unrecoverable__0badc0de")
        self._corrupt_meta_json(tdir)
        schema = OL.OutputsFtsIndex._tantivy_schema()

        real_index_cls = tantivy.Index

        def _always_raise(*args, **kwargs):
            raise ValueError("simulated: still broken after quarantine")

        monkeypatch.setattr(tantivy, "Index", _always_raise)
        try:
            with pytest.raises(OL.TantivyIndexUnavailable):
                OL._open_tantivy_index_cold_start_safe(schema, tdir)
        finally:
            monkeypatch.setattr(tantivy, "Index", real_index_cls)

    def test_connect_tantivy_cold_start_recovers_and_classifies_error_kind(
        self, tmp_path: Path,
    ) -> None:
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        db_path = OL._resolve_index_db_path(str(outputs_dir))
        idx = OL.OutputsFtsIndex(str(outputs_dir), db_path=db_path)
        try:
            tdir = idx._tantivy_dir()
            assert tdir is not None
            self._corrupt_meta_json(tdir)

            index, writer = idx._connect_tantivy()
            assert index is not None and writer is not None
            assert idx._last_tantivy_error is not None
            assert idx._last_tantivy_error_kind == "cold_start_rebuild"
            assert idx._last_tantivy_error_kind != "lock_conflict"
        finally:
            idx.close()

    def test_dependency_unavailable_raises_typed_error_with_diagnostic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        idx = OL.OutputsFtsIndex(str(outputs_dir))
        monkeypatch.setattr(OL, "_SEARCH_DEPENDENCY_CACHE", {
            "tantivy": {"available": False, "version": None,
                        "error": "ImportError: simulated",
                        "install_hint": "pip install tantivy>=0.22"},
            "xxhash": {"available": True, "version": "x", "error": None,
                       "install_hint": None},
        })
        try:
            with pytest.raises(OL.TantivyIndexUnavailable) as excinfo:
                idx._connect_tantivy()
            assert "not available" in str(excinfo.value).lower()
            assert idx._last_tantivy_error_kind == "dependency_unavailable"
        finally:
            idx.close()

    @duckdb_required
    def test_end_to_end_no_false_zero_hit_after_cold_start_corruption(
        self, tmp_path: Path,
    ) -> None:
        """MDE-6 acceptance: 'a cold-start/restart/lock-contention test
        proves no false zero-hit result'. Simulates a process restart onto
        a Tantivy index directory corrupted by a prior crash -- the
        documented real entry point (search_outputs(), which always calls
        rebuild() before search()) must still find the already-known file,
        not silently report zero hits, and must not crash.
        """
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        (outputs_dir / "keep.csv").write_text(
            "cold_start_unique_term_9f3a\n1", encoding="utf-8",
        )

        # First pass: build a real, healthy index.
        first = OL.search_outputs(str(outputs_dir), "cold_start_unique_term_9f3a")
        assert first["hits"]
        assert first["backend"] == "tantivy"

        idx = OL._get_cached_index(str(outputs_dir))
        tdir = idx._tantivy_dir()
        assert tdir is not None
        idx.close()
        # Drop every cached instance so the next search_outputs() call
        # opens a genuinely FRESH OutputsFtsIndex -- simulating a real
        # process restart onto the on-disk state.
        with OL._index_cache_lock:
            while OL._index_cache:
                _, cached = OL._index_cache.popitem()
                if cached is not idx:
                    cached.close()

        # Simulate the crash: corrupt the persisted Tantivy index.
        self._corrupt_meta_json(tdir)

        second = OL.search_outputs(str(outputs_dir), "cold_start_unique_term_9f3a")
        assert second["hits"], (
            "a corrupted-on-restart Tantivy index must not produce a false "
            f"zero-hit result for already-known content -- got {second!r}"
        )
        assert any("keep.csv" in h["path"] for h in second["hits"])
        # The recovery is visible, not silent -- either the cold-start
        # rebuild message (tantivy backend, recovered) or a deterministic
        # fallback reason is present; either way, never a bare, unexplained
        # empty/degraded response.
        assert second.get("tantivy_index_warning") or second.get("backend_reason")


# ---------------------------------------------------------------------------
# 3. Deterministic fallback search
# ---------------------------------------------------------------------------

@duckdb_required
class TestDeterministicFallbackSearch:
    @staticmethod
    def _break_tantivy(monkeypatch: pytest.MonkeyPatch, idx: "OL.OutputsFtsIndex") -> None:
        def _boom(self):
            raise RuntimeError("simulated: tantivy backend broken this call")
        monkeypatch.setattr(OL.OutputsFtsIndex, "_connect_tantivy", _boom)

    def test_search_falls_back_when_tantivy_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "a.csv").write_text("findable_fallback_term_42\n1", encoding="utf-8")
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()  # real content committed to DuckDB + a healthy tantivy index
            self._break_tantivy(monkeypatch, idx)

            hits = idx.search("findable_fallback_term_42")
            assert hits, "deterministic fallback must still find persisted content"
            assert idx._last_search_backend == "deterministic_fallback"
            assert idx._last_search_backend_reason is not None
            assert "RuntimeError" in idx._last_search_backend_reason
            hit = hits[0]
            assert hit["bm25"] is None, "a fallback hit must never fabricate a BM25 score"
            assert hit["score"] == 1.0
        finally:
            idx.close()

    def test_search_outputs_surfaces_backend_and_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "a.csv").write_text("findable_fallback_term_77\n1", encoding="utf-8")
        # Prime a healthy index first (search_outputs owns index caching).
        OL.search_outputs(str(tmp_path), "findable_fallback_term_77")
        idx = OL._get_cached_index(str(tmp_path))
        self._break_tantivy(monkeypatch, idx)

        result = OL.search_outputs(str(tmp_path), "findable_fallback_term_77")
        assert result["backend"] == "deterministic_fallback"
        assert result.get("backend_reason")
        assert result.get("tantivy_index_warning") is None, (
            "an uncaught runtime error mid-search is not classified as a "
            "cold-start/dependency Tantivy error -- only _connect_tantivy's "
            "own two hardened failure modes set that field"
        )
        assert result["hits"], "must not be a false zero-hit result"

    def test_healthy_tantivy_path_reports_backend_tantivy(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "a.csv").write_text("healthytantivyterm\n1", encoding="utf-8")
        result = OL.search_outputs(str(tmp_path), "healthytantivyterm")
        assert result["backend"] == "tantivy"
        assert "backend_reason" not in result

    def test_fallback_is_deterministic_across_repeated_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for i in range(5):
            (tmp_path / f"f{i}.csv").write_text(
                f"shared_determinism_term value={i}\n", encoding="utf-8",
            )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            self._break_tantivy(monkeypatch, idx)
            first = [h["path"] for h in idx.search("shared_determinism_term", limit=10)]
            second = [h["path"] for h in idx.search("shared_determinism_term", limit=10)]
            assert first == second
            assert first == sorted(first), "fallback order must be deterministic (path ASC)"
            assert len(first) == 5
        finally:
            idx.close()

    def test_fallback_escapes_like_wildcards_literally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "percent.csv").write_text("50%off_special\n1", encoding="utf-8")
        (tmp_path / "unrelated.csv").write_text(
            "nothing interesting here at all\n2", encoding="utf-8",
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            self._break_tantivy(monkeypatch, idx)
            hits = idx.search("50%off")
            paths = [h["path"] for h in hits]
            assert any("percent.csv" in p for p in paths)
            assert not any("unrelated.csv" in p for p in paths), (
                "an unescaped '%' would match every row's content as a SQL "
                "wildcard -- the literal query must only match the literal "
                "substring"
            )
        finally:
            idx.close()

    def test_fallback_respects_include_archival_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "keep_arch_term.csv").write_text(
            "archival_fallback_term\n1", encoding="utf-8",
        )
        (tmp_path / "keep_arch_term_old.csv").write_text(
            "archival_fallback_term\n1", encoding="utf-8",
        )
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            idx.rebuild()
            self._break_tantivy(monkeypatch, idx)
            hits = idx.search("archival_fallback_term", include_archival=False)
            assert hits
            assert all(not h["is_archival"] for h in hits)
        finally:
            idx.close()

    def test_fallback_no_usable_connection_degrades_to_empty_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even the fallback's OWN failure path must never raise -- the
        best-effort contract has no further fallback below this one."""
        idx = OL.OutputsFtsIndex(str(tmp_path))
        try:
            def _boom_connect(self):
                raise RuntimeError("simulated: no usable DuckDB connection")
            monkeypatch.setattr(OL.OutputsFtsIndex, "_read_connect", _boom_connect)
            monkeypatch.setattr(OL.OutputsFtsIndex, "_connect", _boom_connect)
            result = idx._deterministic_fallback_search(
                "whatever", limit=10, include_archival=True, reason="test",
            )
            assert result == []
        finally:
            idx.close()
