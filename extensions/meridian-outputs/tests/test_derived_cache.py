"""Tests for meridian_outputs.derived_cache (sprint item 19917525,
v0.2.6-75D -- "first-class derived-artifact cache and fast variant
rendering").

Covers:
  - compute_cache_key: deterministic, order-independent params, sensitive to
    source_path/variant/params changes.
  - put/get round trip: a fresh cache entry is served back byte-identical.
  - Fast-path invalidation: an unchanged source is a "fresh" hit with no
    hash computed; a touched-but-unchanged source falls back to a verified
    "fresh_by_hash" hit; genuinely changed/missing source content is a real
    miss ("source_changed"/"source_missing"); a manually-removed artifact
    file is a miss ("artifact_missing") that also self-heals the manifest.
  - invalidate_variant: single-variant vs. whole-source purge, on-disk
    artifact file removal, no-op on nothing-to-remove.
  - invalidate_stale_sources: composition with fingerprint's EXISTING
    script-staleness ledger -- a script content change invalidates the
    derived cache for outputs it tagged, without this module re-deriving
    staleness logic itself.
  - evict_to_budget: real LRU eviction by last_accessed_at, byte and
    file-count budgets, on-disk file removal, no-op when within budget.
  - get_or_render_variant: render_fn called exactly once per distinct key
    (the actual "fast" in fast variant rendering), force=True bypass,
    TypeError on a non-bytes render_fn return.
  - get_cache_stats / get_convergence_state: aggregate reporting, and a
    real (not asserted) convergence signal that catches a corrupt manifest
    or a manifest row pointing at a missing artifact file.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import derived_cache as DC
from meridian_outputs import fingerprint as FP


# ---------------------------------------------------------------------------
# compute_cache_key
# ---------------------------------------------------------------------------

class TestComputeCacheKey:
    def test_deterministic(self, tmp_path: Path) -> None:
        src = str(tmp_path / "a.csv")
        k1 = DC.compute_cache_key(src, "thumbnail", {"width": 100})
        k2 = DC.compute_cache_key(src, "thumbnail", {"width": 100})
        assert k1 == k2

    def test_params_order_independent(self, tmp_path: Path) -> None:
        src = str(tmp_path / "a.csv")
        k1 = DC.compute_cache_key(src, "v", {"a": 1, "b": 2})
        k2 = DC.compute_cache_key(src, "v", {"b": 2, "a": 1})
        assert k1 == k2

    def test_different_variant_differs(self, tmp_path: Path) -> None:
        src = str(tmp_path / "a.csv")
        assert DC.compute_cache_key(src, "v1") != DC.compute_cache_key(src, "v2")

    def test_different_params_differs(self, tmp_path: Path) -> None:
        src = str(tmp_path / "a.csv")
        k1 = DC.compute_cache_key(src, "v", {"width": 100})
        k2 = DC.compute_cache_key(src, "v", {"width": 200})
        assert k1 != k2

    def test_different_source_differs(self, tmp_path: Path) -> None:
        k1 = DC.compute_cache_key(str(tmp_path / "a.csv"), "v")
        k2 = DC.compute_cache_key(str(tmp_path / "b.csv"), "v")
        assert k1 != k2

    def test_no_params_equivalent_to_empty_dict(self, tmp_path: Path) -> None:
        src = str(tmp_path / "a.csv")
        assert DC.compute_cache_key(src, "v") == DC.compute_cache_key(src, "v", {})

    def test_path_case_and_slash_insensitive_on_equality_semantics(self, tmp_path: Path) -> None:
        """Reuses outputs_local._normalize_output_path -- same
        case/slash-insensitive equality the FTS index already uses."""
        src = tmp_path / "Sub" / "A.csv"
        src.parent.mkdir()
        src.write_text("x", encoding="utf-8")
        k1 = DC.compute_cache_key(str(src), "v")
        k2 = DC.compute_cache_key(str(src).replace("\\", "/"), "v")
        assert k1 == k2


# ---------------------------------------------------------------------------
# put_cached_variant / get_cached_variant round trip + invalidation
# ---------------------------------------------------------------------------

class TestPutAndGetCachedVariant:
    def _make_source(self, tmp_path: Path, content: bytes = b"a,b\n1,2\n") -> Path:
        src = tmp_path / "source.csv"
        src.write_bytes(content)
        return src

    def test_miss_on_empty_cache(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        result = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        assert result["hit"] is False
        assert result["reason"] == "no_entry"
        assert result["data"] is None
        assert result["entry"] is None

    def test_put_then_get_is_a_fresh_hit(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        entry = DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"THUMBBYTES")
        assert entry.size_bytes == len(b"THUMBBYTES")
        assert entry.hit_count == 0

        result = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        assert result["hit"] is True
        assert result["reason"] == "fresh"
        assert result["data"] == b"THUMBBYTES"
        assert result["entry"]["hit_count"] == 1  # bookkeeping reflects this lookup

    def test_artifact_persisted_under_meridian_outputs_cache(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        entry = DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"X")
        full = tmp_path / entry.artifact_path
        assert full.is_file()
        assert ".meridian-outputs-cache" in entry.artifact_path.replace("\\", "/")
        assert full.read_bytes() == b"X"

    def test_gitignore_created_for_cache_dir(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"X")
        gi = tmp_path / ".gitignore"
        assert gi.is_file()
        assert ".meridian-outputs-cache" in gi.read_text(encoding="utf-8")

    def test_distinct_variants_of_same_source_coexist(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"THUMB")
        DC.put_cached_variant(str(tmp_path), str(src), "preview", b"PREVIEW")

        thumb = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        preview = DC.get_cached_variant(str(tmp_path), str(src), "preview")
        assert thumb["data"] == b"THUMB"
        assert preview["data"] == b"PREVIEW"

    def test_different_params_are_different_cache_entries(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        DC.put_cached_variant(str(tmp_path), str(src), "resize", b"SMALL", params={"width": 100})
        DC.put_cached_variant(str(tmp_path), str(src), "resize", b"BIG", params={"width": 800})

        small = DC.get_cached_variant(str(tmp_path), str(src), "resize", {"width": 100})
        big = DC.get_cached_variant(str(tmp_path), str(src), "resize", {"width": 800})
        assert small["data"] == b"SMALL"
        assert big["data"] == b"BIG"

    def test_source_content_changed_is_a_real_miss(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path, b"original content")
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"CACHED")

        time.sleep(0.02)
        src.write_bytes(b"totally different content, different size too")

        result = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        assert result["hit"] is False
        assert result["reason"] == "source_changed"
        assert result["data"] is None

    def test_stale_entry_is_dropped_from_manifest(self, tmp_path: Path) -> None:
        """A confirmed source_changed miss proactively drops the now-known-
        bad row so a later stats/convergence read doesn't count it."""
        src = self._make_source(tmp_path, b"original")
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"CACHED")
        src.write_bytes(b"changed, definitely different length now")
        DC.get_cached_variant(str(tmp_path), str(src), "thumb")

        stats = DC.get_cache_stats(str(tmp_path))
        assert stats["entry_count"] == 0

    def test_source_missing_is_a_miss(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"CACHED")
        os.remove(src)

        result = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        assert result["hit"] is False
        assert result["reason"] == "source_missing"

    def test_mtime_touched_but_content_unchanged_is_fresh_by_hash(self, tmp_path: Path) -> None:
        """Same bytes rewritten (e.g. a copy/restore) changes mtime/size
        signature but not content -- must still be served from cache via
        the content-hash fallback, not needlessly invalidated."""
        content = b"stable content, exact same bytes"
        src = self._make_source(tmp_path, content)
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"CACHED")

        time.sleep(0.02)
        src.write_bytes(content)  # same bytes, new mtime

        result = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        assert result["hit"] is True
        assert result["reason"] == "fresh_by_hash"
        assert result["data"] == b"CACHED"

    def test_artifact_file_missing_is_a_miss_and_self_heals(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        entry = DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"CACHED")
        os.remove(tmp_path / entry.artifact_path)

        result = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        assert result["hit"] is False
        assert result["reason"] == "artifact_missing"

        stats = DC.get_cache_stats(str(tmp_path))
        assert stats["entry_count"] == 0  # stale manifest row was dropped

    def test_repeated_hits_increment_hit_count(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"X")
        DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        result = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        assert result["entry"]["hit_count"] == 3

    def test_put_rejects_non_bytes(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        with pytest.raises(TypeError):
            DC.put_cached_variant(str(tmp_path), str(src), "thumb", "not bytes")  # type: ignore[arg-type]

    def test_re_put_overwrites_previous_entry(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"OLD")
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"NEW")
        result = DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        assert result["data"] == b"NEW"

    def test_owner_recorded_for_audit(self, tmp_path: Path) -> None:
        src = self._make_source(tmp_path)
        entry = DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"X", owner="benchmark-run-1")
        assert entry.owner == "benchmark-run-1"


# ---------------------------------------------------------------------------
# DerivedArtifactEntry.from_dict -- schema-evolution tolerance
# ---------------------------------------------------------------------------

class TestDerivedArtifactEntryFromDict:
    def test_tolerant_of_missing_newer_fields(self) -> None:
        """A manifest row written by an earlier schema version lacking
        hit_count/owner must still load, mirroring fingerprint.py's own
        old/new ledger-row compatibility convention."""
        legacy_row = {
            "key": "abc123",
            "source_path": "/x/y.csv",
            "variant": "thumb",
            "params": {},
            "content_type": None,
            "artifact_path": ".meridian-outputs-cache/derived/artifacts/abc123.bin",
            "size_bytes": 10,
            "source_size": 10,
            "source_mtime_ns": 12345,
            "source_content_hash": "deadbeef",
            "schema_version": 1,
            "created_at": 1.0,
            "created_at_iso": "2026-01-01T00:00:00+00:00",
            "last_accessed_at": 1.0,
            "last_accessed_at_iso": "2026-01-01T00:00:00+00:00",
            # hit_count/owner deliberately absent
        }
        entry = DC.DerivedArtifactEntry.from_dict(legacy_row)
        assert entry.hit_count == 0
        assert entry.owner is None
        assert entry.key == "abc123"


# ---------------------------------------------------------------------------
# invalidate_variant
# ---------------------------------------------------------------------------

class TestInvalidateVariant:
    def test_invalidate_specific_variant(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"T")
        DC.put_cached_variant(str(tmp_path), str(src), "preview", b"P")

        removed = DC.invalidate_variant(str(tmp_path), str(src), "thumb")
        assert len(removed) == 1

        assert DC.get_cached_variant(str(tmp_path), str(src), "thumb")["hit"] is False
        assert DC.get_cached_variant(str(tmp_path), str(src), "preview")["hit"] is True

    def test_invalidate_all_variants_for_source(self, tmp_path: Path) -> None:
        src_a = tmp_path / "a.csv"
        src_a.write_bytes(b"a")
        src_b = tmp_path / "b.csv"
        src_b.write_bytes(b"b")
        DC.put_cached_variant(str(tmp_path), str(src_a), "thumb", b"TA")
        DC.put_cached_variant(str(tmp_path), str(src_a), "preview", b"PA")
        DC.put_cached_variant(str(tmp_path), str(src_b), "thumb", b"TB")

        removed = DC.invalidate_variant(str(tmp_path), str(src_a))
        assert len(removed) == 2

        assert DC.get_cached_variant(str(tmp_path), str(src_a), "thumb")["hit"] is False
        assert DC.get_cached_variant(str(tmp_path), str(src_a), "preview")["hit"] is False
        assert DC.get_cached_variant(str(tmp_path), str(src_b), "thumb")["hit"] is True

    def test_invalidate_removes_on_disk_artifact(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        entry = DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"T")
        full = tmp_path / entry.artifact_path
        assert full.is_file()

        DC.invalidate_variant(str(tmp_path), str(src), "thumb")
        assert not full.is_file()

    def test_invalidate_nothing_to_remove_is_a_noop(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        removed = DC.invalidate_variant(str(tmp_path), str(src), "thumb")
        assert removed == []


# ---------------------------------------------------------------------------
# invalidate_stale_sources -- composition with fingerprint's staleness ledger
# ---------------------------------------------------------------------------

class TestInvalidateStaleSources:
    def test_script_change_invalidates_derived_cache_for_its_outputs(self, tmp_path: Path) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        output = tmp_path / "result.csv"
        output.write_bytes(b"a,b\n1,2\n")

        # Tag the output against this script version (fingerprint's own ledger).
        FP.tag_output(str(output), str(tmp_path), script_path=str(script))
        # Cache a derived variant of that output.
        DC.put_cached_variant(str(tmp_path), str(output), "thumb", b"CACHED_THUMB")
        assert DC.get_cached_variant(str(tmp_path), str(output), "thumb")["hit"] is True

        # The script changes (e.g. a bug fix) -- the OUTPUT's own bytes never
        # change, so the derived cache's own signature/hash check alone would
        # never catch this.
        script.write_text("print('v2 -- bug fixed')\n", encoding="utf-8")

        result = DC.invalidate_stale_sources(str(tmp_path))
        assert result["stale_sources_checked"] == 1
        assert len(result["invalidated_keys"]) == 1

        assert DC.get_cached_variant(str(tmp_path), str(output), "thumb")["hit"] is False

    def test_no_stale_sources_is_a_noop(self, tmp_path: Path) -> None:
        result = DC.invalidate_stale_sources(str(tmp_path))
        assert result == {"stale_sources_checked": 0, "invalidated_keys": []}

    def test_unrelated_cache_entries_survive(self, tmp_path: Path) -> None:
        """Only the derived cache for the actually-stale source is purged --
        a cached variant for an untagged, unrelated file is untouched."""
        script = tmp_path / "train.py"
        script.write_text("v1", encoding="utf-8")
        tagged_output = tmp_path / "tagged.csv"
        tagged_output.write_bytes(b"x")
        FP.tag_output(str(tagged_output), str(tmp_path), script_path=str(script))
        DC.put_cached_variant(str(tmp_path), str(tagged_output), "thumb", b"T")

        other_output = tmp_path / "other.csv"
        other_output.write_bytes(b"y")
        DC.put_cached_variant(str(tmp_path), str(other_output), "thumb", b"O")

        script.write_text("v2", encoding="utf-8")
        DC.invalidate_stale_sources(str(tmp_path))

        assert DC.get_cached_variant(str(tmp_path), str(tagged_output), "thumb")["hit"] is False
        assert DC.get_cached_variant(str(tmp_path), str(other_output), "thumb")["hit"] is True


# ---------------------------------------------------------------------------
# evict_to_budget -- real LRU eviction
# ---------------------------------------------------------------------------

class TestEvictToBudget:
    def _put_n(self, tmp_path: Path, n: int, size: int = 100) -> list[str]:
        keys = []
        for i in range(n):
            src = tmp_path / f"s{i}.csv"
            src.write_bytes(f"content-{i}".encode())
            entry = DC.put_cached_variant(str(tmp_path), str(src), "v", bytes(size))
            keys.append(entry.key)
            time.sleep(0.005)  # ensure distinct last_accessed_at ordering
        return keys

    def test_noop_when_no_budget_given(self, tmp_path: Path) -> None:
        self._put_n(tmp_path, 3)
        result = DC.evict_to_budget(str(tmp_path))
        assert result["evicted_count"] == 0

    def test_noop_when_within_budget(self, tmp_path: Path) -> None:
        self._put_n(tmp_path, 3, size=10)
        result = DC.evict_to_budget(str(tmp_path), max_bytes=10_000)
        assert result["evicted_count"] == 0

    def test_evicts_oldest_first_by_max_files(self, tmp_path: Path) -> None:
        keys = self._put_n(tmp_path, 5)
        result = DC.evict_to_budget(str(tmp_path), max_files=2)
        assert result["evicted_count"] == 3
        # the 3 OLDEST (first-inserted, first-accessed) keys were evicted
        assert set(result["evicted_keys"]) == set(keys[:3])
        assert result["remaining_count"] == 2

    def test_evicts_by_max_bytes(self, tmp_path: Path) -> None:
        self._put_n(tmp_path, 4, size=100)
        result = DC.evict_to_budget(str(tmp_path), max_bytes=250)
        assert result["remaining_bytes"] <= 250
        assert result["evicted_count"] >= 2

    def test_eviction_removes_on_disk_files(self, tmp_path: Path) -> None:
        self._put_n(tmp_path, 3)
        stats_before = DC.get_cache_stats(str(tmp_path))
        assert stats_before["entry_count"] == 3

        artifacts_dir = tmp_path / ".meridian-outputs-cache" / "derived" / "artifacts"
        assert len(list(artifacts_dir.iterdir())) == 3

        DC.evict_to_budget(str(tmp_path), max_files=1)
        assert len(list(artifacts_dir.iterdir())) == 1

    def test_recently_hit_entry_is_not_evicted_before_older_untouched_one(
        self, tmp_path: Path,
    ) -> None:
        """A HIT refreshes last_accessed_at -- real LRU, not FIFO."""
        keys = self._put_n(tmp_path, 3)
        # Touch the OLDEST entry (keys[0]) via a real lookup so its
        # last_accessed_at becomes the newest.
        src0 = tmp_path / "s0.csv"
        time.sleep(0.01)
        DC.get_cached_variant(str(tmp_path), str(src0), "v")

        result = DC.evict_to_budget(str(tmp_path), max_files=1)
        assert keys[0] not in result["evicted_keys"]


# ---------------------------------------------------------------------------
# get_or_render_variant -- fast variant rendering
# ---------------------------------------------------------------------------

class TestGetOrRenderVariant:
    def test_render_fn_called_once_then_served_from_cache(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"data")
        calls = []

        def render(path: str, **params: object) -> bytes:
            calls.append((path, params))
            return b"RENDERED"

        r1 = DC.get_or_render_variant(str(tmp_path), str(src), "thumb", render)
        assert r1["cache_hit"] is False
        assert r1["data"] == b"RENDERED"
        assert len(calls) == 1

        r2 = DC.get_or_render_variant(str(tmp_path), str(src), "thumb", render)
        assert r2["cache_hit"] is True
        assert r2["data"] == b"RENDERED"
        assert len(calls) == 1  # render_fn NOT called again -- the actual "fast" path

    def test_params_forwarded_to_render_fn(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"data")
        seen = {}

        def render(path: str, **params: object) -> bytes:
            seen.update(params)
            return b"OK"

        DC.get_or_render_variant(str(tmp_path), str(src), "resize", render, params={"width": 128})
        assert seen == {"width": 128}

    def test_force_bypasses_cache_and_re_renders(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"data")
        calls = []

        def render(path: str, **params: object) -> bytes:
            calls.append(1)
            return f"RENDER_{len(calls)}".encode()

        DC.get_or_render_variant(str(tmp_path), str(src), "thumb", render)
        r2 = DC.get_or_render_variant(str(tmp_path), str(src), "thumb", render, force=True)
        assert len(calls) == 2
        assert r2["cache_hit"] is False
        assert r2["data"] == b"RENDER_2"

    def test_source_change_forces_re_render(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"v1")
        calls = []

        def render(path: str, **params: object) -> bytes:
            calls.append(1)
            return Path(path).read_bytes() + b"-rendered"

        r1 = DC.get_or_render_variant(str(tmp_path), str(src), "thumb", render)
        assert r1["data"] == b"v1-rendered"

        src.write_bytes(b"v2-different-length")
        r2 = DC.get_or_render_variant(str(tmp_path), str(src), "thumb", render)
        assert r2["cache_hit"] is False
        assert r2["data"] == b"v2-different-length-rendered"
        assert len(calls) == 2

    def test_non_bytes_return_raises_type_error(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"data")

        def bad_render(path: str, **params: object) -> str:
            return "not bytes"  # type: ignore[return-value]

        with pytest.raises(TypeError):
            DC.get_or_render_variant(str(tmp_path), str(src), "thumb", bad_render)  # type: ignore[arg-type]

    def test_entry_persisted_after_render(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"data")
        DC.get_or_render_variant(str(tmp_path), str(src), "thumb", lambda p, **kw: b"X")
        stats = DC.get_cache_stats(str(tmp_path))
        assert stats["entry_count"] == 1


# ---------------------------------------------------------------------------
# get_cache_stats
# ---------------------------------------------------------------------------

class TestGetCacheStats:
    def test_empty_cache(self, tmp_path: Path) -> None:
        stats = DC.get_cache_stats(str(tmp_path))
        assert stats["entry_count"] == 0
        assert stats["total_bytes"] == 0
        assert stats["total_hits"] == 0
        assert stats["distinct_variants"] == []
        assert stats["oldest_created_at"] is None

    def test_aggregate_counts(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"12345")
        DC.put_cached_variant(str(tmp_path), str(src), "preview", b"1234567890")
        DC.get_cached_variant(str(tmp_path), str(src), "thumb")
        DC.get_cached_variant(str(tmp_path), str(src), "thumb")

        stats = DC.get_cache_stats(str(tmp_path))
        assert stats["entry_count"] == 2
        assert stats["total_bytes"] == 15
        assert stats["total_hits"] == 2
        assert stats["distinct_variants"] == ["preview", "thumb"]


# ---------------------------------------------------------------------------
# get_convergence_state -- real, checkable self-consistency
# ---------------------------------------------------------------------------

class TestGetConvergenceState:
    def test_never_used_cache_is_converged_empty(self, tmp_path: Path) -> None:
        state = DC.get_convergence_state(str(tmp_path))
        assert state.converged is True
        assert state.manifest_readable is True
        assert state.entry_count == 0
        assert state.missing_artifact_keys == []

    def test_valid_cache_is_converged(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"X")

        state = DC.get_convergence_state(str(tmp_path))
        assert state.converged is True
        assert state.entry_count == 1
        assert state.reason is None

    def test_corrupt_manifest_is_not_converged(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"X")

        manifest_path = Path(DC._manifest_path(str(tmp_path)))
        manifest_path.write_text("{not valid json", encoding="utf-8")

        state = DC.get_convergence_state(str(tmp_path))
        assert state.converged is False
        assert state.manifest_readable is False
        assert state.reason is not None

    def test_missing_artifact_file_is_not_converged(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        entry = DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"X")
        os.remove(tmp_path / entry.artifact_path)

        state = DC.get_convergence_state(str(tmp_path))
        assert state.converged is False
        assert entry.key in state.missing_artifact_keys
        assert state.reason is not None

    def test_convergence_state_embeds_quota_report(self, tmp_path: Path) -> None:
        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        DC.put_cached_variant(str(tmp_path), str(src), "thumb", b"X")

        state = DC.get_convergence_state(str(tmp_path), max_bytes=1)
        assert state.quota["exists"] is True
        assert "used_bytes" in state.quota

    def test_to_dict_shape(self, tmp_path: Path) -> None:
        state = DC.get_convergence_state(str(tmp_path))
        d = state.to_dict()
        for field in (
            "outputs_dir", "converged", "manifest_readable", "entry_count",
            "total_bytes", "missing_artifact_keys", "quota", "last_write_at",
            "reason",
        ):
            assert field in d


# ---------------------------------------------------------------------------
# server.py MCP tool wrappers -- registration + the base64 boundary logic
# that lives IN server.py (not derived_cache.py), so needs its own coverage.
# ---------------------------------------------------------------------------

class TestServerMcpWrappers:
    _TOOL_NAMES = (
        "get_cached_derived_variant",
        "put_cached_derived_variant",
        "invalidate_derived_variant",
        "invalidate_stale_derived_variants",
        "evict_derived_cache",
        "get_derived_cache_stats",
        "get_derived_cache_convergence_state",
    )

    def test_all_tools_registered(self) -> None:
        from meridian_outputs import server as srv

        registered = {t.name for t in srv.mcp._tool_manager.list_tools()}
        for name in self._TOOL_NAMES:
            assert name in registered, f"{name} not registered as an MCP tool"

    def test_put_then_get_round_trips_through_base64(self, tmp_path: Path) -> None:
        from meridian_outputs import server as srv
        import base64

        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        raw = b"\x00\x01\xffbinary-ish bytes"

        put_result = srv.put_cached_derived_variant(
            str(tmp_path), str(src), "thumb", base64.b64encode(raw).decode("ascii"),
        )
        assert put_result["key"]
        assert "data" not in put_result  # metadata only, never raw bytes

        get_result = srv.get_cached_derived_variant(str(tmp_path), str(src), "thumb")
        assert get_result["hit"] is True
        assert base64.b64decode(get_result["data_base64"]) == raw

    def test_get_miss_reports_none_data_base64(self, tmp_path: Path) -> None:
        from meridian_outputs import server as srv

        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        result = srv.get_cached_derived_variant(str(tmp_path), str(src), "thumb")
        assert result["hit"] is False
        assert result["data_base64"] is None

    def test_invalidate_wrapper_shape(self, tmp_path: Path) -> None:
        from meridian_outputs import server as srv
        import base64

        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        srv.put_cached_derived_variant(str(tmp_path), str(src), "thumb", base64.b64encode(b"X").decode("ascii"))

        result = srv.invalidate_derived_variant(str(tmp_path), str(src), "thumb")
        assert result["removed_count"] == 1
        assert len(result["removed_keys"]) == 1

    def test_stats_and_convergence_wrappers(self, tmp_path: Path) -> None:
        from meridian_outputs import server as srv

        stats = srv.get_derived_cache_stats(str(tmp_path))
        assert stats["entry_count"] == 0

        state = srv.get_derived_cache_convergence_state(str(tmp_path))
        assert state["converged"] is True

    def test_evict_wrapper(self, tmp_path: Path) -> None:
        from meridian_outputs import server as srv
        import base64

        src = tmp_path / "s.csv"
        src.write_bytes(b"x")
        srv.put_cached_derived_variant(str(tmp_path), str(src), "thumb", base64.b64encode(b"X" * 100).decode("ascii"))

        result = srv.evict_derived_cache(str(tmp_path), max_bytes=1)
        assert result["evicted_count"] == 1

    def test_invalidate_stale_wrapper(self, tmp_path: Path) -> None:
        from meridian_outputs import server as srv

        result = srv.invalidate_stale_derived_variants(str(tmp_path))
        assert result == {"stale_sources_checked": 0, "invalidated_keys": []}


# ---------------------------------------------------------------------------
# Package-level re-exports (mirrors this package's fingerprint precedent)
# ---------------------------------------------------------------------------

class TestPackageLevelReExports:
    def test_public_api_importable_from_package_root(self) -> None:
        import meridian_outputs as pkg

        for name in (
            "DerivedArtifactEntry", "DerivedCacheConvergenceState",
            "compute_cache_key", "evict_to_budget", "get_cache_stats",
            "get_cached_variant", "get_derived_cache_convergence_state",
            "get_or_render_variant", "invalidate_stale_sources",
            "invalidate_variant", "put_cached_variant",
        ):
            assert hasattr(pkg, name), f"{name} not re-exported at package level"
            assert name in pkg.__all__
