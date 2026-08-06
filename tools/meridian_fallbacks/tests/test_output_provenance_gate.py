"""Tests for tools/meridian_fallbacks/output_provenance_gate.py (sprint item
d3374b0e, proposal 1abedabe-2f82-40e5-a320-3b32d550cc40).

Imports the module standalone (no ``tools``/``tools.meridian_fallbacks``
package, no ``__init__.py``) -- the sibling package-skeleton item (4ff6ff22)
lands those in a parallel worktree; per this item's instructions this file
must work today, before that skeleton exists, and continue working unchanged
once it is cherry-picked in ahead of this commit.

Every fixture below is a disposable, hand-built JSON ledger written directly
via ``json.dump`` into a ``tmp_path`` -- deliberately NOT produced by
importing ``meridian_outputs.annotate``/``fingerprint`` (this module has no
dependency on that package; the fixtures prove the format-compatible parity
claim in the module's own docstring by construction, not by delegation).

Covers:
  - Argument validation.
  - Exact hit (a hand-built provenance_ledger.json record).
  - Unregistered vs. unknown, and the inconclusive/converged distinction
    (forced truncation of the local scan).
  - Directory-note (MERIDIAN_NOTES.md) fallback.
  - stale_by_script: a fingerprint_ledger.json entry whose tagged script
    hash no longer matches the script's current content -- promotes EXACT
    to STALE_BY_SCRIPT. Also the negative case (script unchanged -> stays
    EXACT) and the orthogonal case (OUTPUT content changed, not the script
    -> stays EXACT but ``staleness.stale`` is True).
  - Canonical/archival identity (byte-identical twin vs. content-mismatched
    twin vs. no twin present).
  - Generator-script path/SHA-256 resolution.
  - Exact output SHA-256 always freshly computed, independent of the ledger.
  - Index generation increments across repeated calls.
  - CLI entry point (main()).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import output_provenance_gate as OPG  # noqa: E402


def _write_provenance_ledger(outputs_dir: Path, entries: dict[str, dict]) -> Path:
    cache_dir = outputs_dir / ".meridian-outputs-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = cache_dir / "provenance_ledger.json"
    ledger_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return ledger_path


def _write_fingerprint_ledger(outputs_dir: Path, entries: dict[str, dict]) -> Path:
    cache_dir = outputs_dir / ".meridian-outputs-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = cache_dir / "fingerprint_ledger.json"
    ledger_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return ledger_path


def _provenance_record(
    path: str, *, generating_script: str | None = None,
    params: dict | None = None, content_hash: str | None = None,
) -> dict:
    """Hand-built record matching annotate.ProvenanceRecord's exact shape."""
    return {
        "path": path,
        "generating_script": generating_script,
        "params": params or {},
        "sprint_item_id": None,
        "decision_id": None,
        "note": None,
        "recorded_at": time.time(),
        "recorded_at_iso": "2026-08-05T00:00:00+00:00",
        "content_hash": content_hash,
    }


def _fingerprint_record(
    output_path: str, *, script_path: str | None, script_hash: str | None,
) -> dict:
    """Hand-built record matching fingerprint.ScriptTaggedFingerprint's shape."""
    return {
        "path": output_path,
        "kind": "text_content",
        "csv_columns": None,
        "json_keys": None,
        "generating_script": script_path,
        "script_path": script_path,
        "script_hash": script_hash,
        "tagged_at": "2026-08-05T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

class TestArgumentValidation:
    def test_missing_outputs_dir(self) -> None:
        result = OPG.check_output_provenance("", "/some/file.csv")
        assert "error" in result

    def test_nonexistent_outputs_dir(self, tmp_path: Path) -> None:
        result = OPG.check_output_provenance(str(tmp_path / "does_not_exist"), "/some/file.csv")
        assert "error" in result

    def test_missing_path(self, tmp_path: Path) -> None:
        result = OPG.check_output_provenance(str(tmp_path), "")
        assert "error" in result


# ---------------------------------------------------------------------------
# Exact hit
# ---------------------------------------------------------------------------

class TestExactHit:
    def test_exact_record_reports_authoritative_status(self, tmp_path: Path) -> None:
        f = tmp_path / "results.csv"
        f.write_text("epoch,accuracy\n1,0.9\n", encoding="utf-8")
        # Hash the bytes actually on disk (write_text applies platform
        # newline translation on Windows -- hashing the source literal
        # directly would not match).
        content_hash = OPG._sha256_file(str(f))
        _write_provenance_ledger(tmp_path, {
            OPG._normalize_path(str(f)): _provenance_record(
                str(f), generating_script="train.py",
                params={"lr": 0.01, "epochs": 10}, content_hash=content_hash,
            ),
        })

        status = OPG.check_output_provenance(str(tmp_path), str(f))
        assert status["provenance_type"] == OPG.EXACT
        assert status["record"]["generating_script"] == "train.py"
        assert status["record"]["params"] == {"lr": 0.01, "epochs": 10}
        assert status["directory_note"] is None
        assert status["staleness"]["exists_on_disk"] is True
        assert status["staleness"]["stale"] is False
        assert status["script_staleness"] is None  # never fingerprint-tagged
        assert status["output_sha256"] == content_hash
        assert status["inconclusive"] is False

    def test_output_sha256_always_freshly_computed(self, tmp_path: Path) -> None:
        """output_sha256 reflects CURRENT disk content, independent of
        whatever content_hash happens to be recorded in the ledger."""
        f = tmp_path / "stamped.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")
        _write_provenance_ledger(tmp_path, {
            OPG._normalize_path(str(f)): _provenance_record(
                str(f), content_hash="deliberately-wrong-hash",
            ),
        })

        status = OPG.check_output_provenance(str(tmp_path), str(f))
        assert status["output_sha256"] == OPG._sha256_file(str(f))
        assert status["output_sha256"] != "deliberately-wrong-hash"
        # The mismatch IS surfaced -- just via staleness, not output_sha256.
        assert status["staleness"]["stale"] is True


# ---------------------------------------------------------------------------
# Unregistered / unknown, and the inconclusive/converged distinction
# ---------------------------------------------------------------------------

class TestUnregisteredUnknown:
    def test_discovered_path_without_record_is_unregistered(self, tmp_path: Path) -> None:
        f = tmp_path / "orphaned_output.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(f))
        assert status["provenance_type"] == OPG.UNREGISTERED
        assert status["record"] is None
        assert status["directory_note"] is None
        assert status["inconclusive"] is False
        assert status["convergence"]["converged"] is True

    def test_nonexistent_path_with_converged_scan_is_confidently_unknown(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "unrelated.csv").write_text("x\n", encoding="utf-8")
        never = tmp_path / "never_created.csv"  # never written to disk

        status = OPG.check_output_provenance(str(tmp_path), str(never))
        assert status["provenance_type"] == OPG.UNKNOWN
        assert status["convergence"]["converged"] is True
        assert status["inconclusive"] is False

    def test_truncated_scan_before_reaching_target_is_inconclusive(
        self, tmp_path: Path,
    ) -> None:
        """The target file DOES exist, but a deliberately tiny max_scan_files
        cuts the (sorted, deterministic) walk off before it reaches it --
        this must report inconclusive=True, never a confident UNKNOWN."""
        (tmp_path / "aaa_first.csv").write_text("1\n", encoding="utf-8")
        (tmp_path / "bbb_second.csv").write_text("2\n", encoding="utf-8")
        target = tmp_path / "zzz_target.csv"  # sorts last
        target.write_text("3\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(target), max_scan_files=2)
        assert status["convergence"]["truncated"] is True
        assert status["convergence"]["converged"] is False
        assert status["provenance_type"] == OPG.UNKNOWN
        assert status["inconclusive"] is True

    def test_unregistered_and_unknown_share_shape_differ_only_by_type(
        self, tmp_path: Path,
    ) -> None:
        indexed = tmp_path / "indexed.csv"
        indexed.write_text("a,b\n1,2\n", encoding="utf-8")
        never = tmp_path / "never.csv"

        s_indexed = OPG.check_output_provenance(str(tmp_path), str(indexed))
        s_never = OPG.check_output_provenance(str(tmp_path), str(never))
        assert s_indexed["provenance_type"] != s_never["provenance_type"]
        for key in ("record", "directory_note", "staleness", "script_staleness"):
            assert s_indexed[key] is None
            assert s_never[key] is None


# ---------------------------------------------------------------------------
# Directory-note fallback
# ---------------------------------------------------------------------------

class TestDirectoryFallback:
    def test_meridian_notes_covered_path_surfaces_as_directory_fallback(
        self, tmp_path: Path,
    ) -> None:
        sub = tmp_path / "run_17"
        sub.mkdir()
        (sub / "MERIDIAN_NOTES.md").write_text(
            "This run: PCA on, BFS off, overwritten 5x.", encoding="utf-8",
        )
        target = sub / "width_hist.csv"
        target.write_text("bin,count\n1,5\n2,9\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(target))
        assert status["provenance_type"] == OPG.DIRECTORY_FALLBACK
        assert status["record"] is None
        assert status["directory_note"] is not None
        assert status["directory_note"]["source"] == "MERIDIAN_NOTES.md"
        assert "PCA on" in status["directory_note"]["note"]
        assert status["staleness"] is None
        assert status["inconclusive"] is False

    def test_exact_record_takes_priority_over_directory_note(
        self, tmp_path: Path,
    ) -> None:
        sub = tmp_path / "run_18"
        sub.mkdir()
        (sub / "MERIDIAN_NOTES.md").write_text("dir note", encoding="utf-8")
        f = sub / "has_exact_record.csv"
        f.write_text("a,b\n3,4\n", encoding="utf-8")
        _write_provenance_ledger(tmp_path, {
            OPG._normalize_path(str(f)): _provenance_record(str(f)),
        })

        status = OPG.check_output_provenance(str(tmp_path), str(f))
        assert status["provenance_type"] == OPG.EXACT
        assert status["directory_note"] is None


# ---------------------------------------------------------------------------
# stale_by_script (the new, hash- and script-aware status)
# ---------------------------------------------------------------------------

class TestStaleByScript:
    def test_script_changed_since_tagging_promotes_to_stale_by_script(
        self, tmp_path: Path,
    ) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('v1 -- has a bug')\n", encoding="utf-8")
        original_script_hash = OPG._sha256_file(str(script))

        output = tmp_path / "predictions.csv"
        output.write_text("id,pred\n1,0.5\n", encoding="utf-8")
        output_hash = OPG._sha256_file(str(output))

        _write_provenance_ledger(tmp_path, {
            OPG._normalize_path(str(output)): _provenance_record(
                str(output), generating_script=str(script), content_hash=output_hash,
            ),
        })
        _write_fingerprint_ledger(tmp_path, {
            str(output): _fingerprint_record(
                str(output), script_path=str(script), script_hash=original_script_hash,
            ),
        })

        # The bug is fixed -- script content changes, output is untouched.
        script.write_text("print('v2 -- bug fixed')\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(output))
        assert status["provenance_type"] == OPG.STALE_BY_SCRIPT
        assert status["script_staleness"]["is_stale"] is True
        assert "script content changed" in status["script_staleness"]["reason"]
        # The output file itself is unchanged -- generic staleness stays False.
        assert status["staleness"]["stale"] is False
        assert status["generator_script"]["resolved_path"] == str(script)
        assert status["generator_script"]["sha256"] == OPG._sha256_file(str(script))

    def test_unchanged_script_stays_exact(self, tmp_path: Path) -> None:
        script = tmp_path / "stable.py"
        script.write_text("print('unchanged')\n", encoding="utf-8")
        script_hash = OPG._sha256_file(str(script))

        output = tmp_path / "stable_output.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")

        _write_provenance_ledger(tmp_path, {
            OPG._normalize_path(str(output)): _provenance_record(
                str(output), generating_script=str(script),
            ),
        })
        _write_fingerprint_ledger(tmp_path, {
            str(output): _fingerprint_record(
                str(output), script_path=str(script), script_hash=script_hash,
            ),
        })

        status = OPG.check_output_provenance(str(tmp_path), str(output))
        assert status["provenance_type"] == OPG.EXACT
        assert status["script_staleness"]["is_stale"] is False

    def test_output_content_mismatch_alone_does_not_trigger_stale_by_script(
        self, tmp_path: Path,
    ) -> None:
        """A generic output-content mismatch (the file itself changed) is a
        DIFFERENT signal (staleness.stale) from stale_by_script (the
        generating SCRIPT changed) -- they must not be conflated."""
        output = tmp_path / "overwritten.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")
        original_hash = OPG._sha256_file(str(output))
        _write_provenance_ledger(tmp_path, {
            OPG._normalize_path(str(output)): _provenance_record(
                str(output), content_hash=original_hash,
            ),
        })

        output.write_text("a,b\n999,999\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(output))
        assert status["provenance_type"] == OPG.EXACT  # not stale_by_script
        assert status["staleness"]["stale"] is True
        assert status["script_staleness"] is None  # never fingerprint-tagged


# ---------------------------------------------------------------------------
# Canonical/archival identity
# ---------------------------------------------------------------------------

class TestArchivalIdentity:
    def test_byte_identical_twin_is_archival(self, tmp_path: Path) -> None:
        canonical = tmp_path / "results.csv"
        canonical.write_text("a,b\n1,2\n", encoding="utf-8")
        archival = tmp_path / "results_old.csv"
        archival.write_text("a,b\n1,2\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(archival))
        assert status["archival"]["is_archival"] is True
        assert status["archival"]["canonical_path"] == str(canonical)

    def test_content_mismatched_twin_is_not_archival(self, tmp_path: Path) -> None:
        canonical = tmp_path / "sweep.csv"
        canonical.write_text("a,b\n1,2\n", encoding="utf-8")
        candidate = tmp_path / "sweep_old.csv"
        candidate.write_text("a,b\n999,999\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(candidate))
        assert status["archival"]["is_archival"] is False
        assert status["archival"]["canonical_path"] == str(canonical)
        assert "differs" in status["archival"]["reason"]

    def test_no_twin_present_is_not_archival(self, tmp_path: Path) -> None:
        lonely = tmp_path / "_orphan_old.csv"
        lonely.write_text("a,b\n1,2\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(lonely))
        assert status["archival"]["is_archival"] is False
        assert "no canonical twin" in status["archival"]["reason"]

    def test_ordinary_name_is_not_a_candidate(self, tmp_path: Path) -> None:
        f = tmp_path / "ordinary_output.csv"
        f.write_text("a,b\n1,2\n", encoding="utf-8")

        status = OPG.check_output_provenance(str(tmp_path), str(f))
        assert status["archival"]["is_archival"] is False
        assert status["archival"]["canonical_path"] is None
        assert "not a name-pattern candidate" in status["archival"]["reason"]

    def test_nonexistent_path_has_no_archival_classification(
        self, tmp_path: Path,
    ) -> None:
        never = tmp_path / "never_old.csv"
        status = OPG.check_output_provenance(str(tmp_path), str(never))
        assert status["archival"] is None


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------

class TestIndexGeneration:
    def test_generation_increments_across_calls(self, tmp_path: Path) -> None:
        f = tmp_path / "a.csv"
        f.write_text("1\n", encoding="utf-8")

        first = OPG.check_output_provenance(str(tmp_path), str(f))
        second = OPG.check_output_provenance(str(tmp_path), str(f))
        assert second["convergence"]["generation"] == first["convergence"]["generation"] + 1

    def test_missing_index_ledger_resets_generation_gracefully(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "a.csv"
        f.write_text("1\n", encoding="utf-8")
        first = OPG.check_output_provenance(str(tmp_path), str(f))
        assert first["convergence"]["generation"] == 1

        # Disposable ledger -- deleting it must never break correctness.
        ledger = Path(OPG._index_ledger_path(str(tmp_path)))
        assert ledger.is_file()
        ledger.unlink()

        second = OPG.check_output_provenance(str(tmp_path), str(f))
        assert second["convergence"]["generation"] == 1


# ---------------------------------------------------------------------------
# Robustness: no ledgers at all
# ---------------------------------------------------------------------------

class TestNoLedgers:
    def test_no_cache_dir_at_all_does_not_crash(self, tmp_path: Path) -> None:
        f = tmp_path / "bare.csv"
        f.write_text("a\n", encoding="utf-8")
        status = OPG.check_output_provenance(str(tmp_path), str(f))
        assert status["provenance_type"] == OPG.UNREGISTERED
        assert status["record"] is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def test_main_exits_zero_on_success(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        f = tmp_path / "cli_target.csv"
        f.write_text("a\n", encoding="utf-8")
        exit_code = OPG.main([str(tmp_path), str(f)])
        assert exit_code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["provenance_type"] == OPG.UNREGISTERED

    def test_main_exits_nonzero_on_error(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        exit_code = OPG.main([str(tmp_path / "missing"), str(tmp_path / "f.csv")])
        assert exit_code != 0
