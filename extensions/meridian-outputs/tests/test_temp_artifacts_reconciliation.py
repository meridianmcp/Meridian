"""Reconciliation tests: meridian.temp_artifacts (NEW, core `meridian`
package) composes with -- never duplicates or conflicts with -- this
package's EXISTING fingerprint.py/annotate.py ledgers (sprint item
c4c74141, source proposal a08defbc-481f-4354-915d-462a254e5e75).

Boundary this file respects
----------------------------
meridian_outputs has a documented ZERO-dependency-on-meridian-core
contract (see meridian/local_resilience.py's own module docstring, and
this package's own "no hosted call is made -- fully local" module
docstrings). That contract is exercised for real by
``.github/workflows/meridian-outputs-cross-env.yml``, which installs
ONLY this extension (``pip install -e extensions/meridian-outputs``) with
no meridian core present at all.

This file imports ``meridian.temp_artifacts`` through a guarded
``pytest.importorskip`` and SKIPS (never fails/errors) when meridian core
is not installed in the current environment -- exactly mirroring
``temp_artifacts.cross_reference_outputs_fingerprint``'s own soft-import
composition pattern on the meridian_outputs side. In an environment where
BOTH packages ARE installed (the normal case for this repo's own worktree/
dev environment), these tests run for real and prove the two systems:

  1. Leave each other's existing, independently-tested behavior completely
     unaffected (no shared global state, no import-order coupling).
  2. Never write into each other's ledger files (disjoint filenames under
     each package's own cache/registry directory).
  3. Compute byte-identical SHA-256 hashes for the same script content, so
     ``temp_artifacts.cross_reference_outputs_fingerprint`` can correlate
     entries across the two systems rather than silently disagreeing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import annotate, fingerprint

TA = pytest.importorskip(
    "meridian.temp_artifacts",
    reason=(
        "meridian core is not installed in this environment -- "
        "meridian_outputs has zero dependency on it by design (see "
        "meridian/local_resilience.py's module docstring); the "
        "reconciliation composition below is skipped, not failed."
    ),
)


@pytest.fixture(autouse=True)
def _clear_onedrive_env(monkeypatch):
    # default_registry_root()'s OneDrive probing is irrelevant to these
    # tests (an explicit registry_root is always passed) -- cleared so a
    # developer machine's real OneDrive env vars can never affect them.
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(var, raising=False)


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


class TestExistingLedgersUnaffected:
    """fingerprint.py/annotate.py's OWN existing, already-tested behavior
    is completely unaffected by temp_artifacts.py's mere existence/import/
    concurrent use in the same process."""

    def test_fingerprint_tag_output_and_check_staleness_unaffected(self, tmp_path):
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        script = _write(tmp_path / "make_thing.py", "print('v1')\n")
        output = outputs_dir / "thing.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")

        # Register the SAME script in temp_artifacts alongside using
        # fingerprint.py -- if there were any shared-state collision this
        # is where it would surface.
        registry_root = tmp_path / ".meridian-temp-artifacts"
        TA.register_artifact(str(registry_root), name="make_thing", script_path=str(script))

        tagged = fingerprint.tag_output(str(output), str(outputs_dir), script_path=str(script))
        assert tagged.script_hash == fingerprint.script_content_hash(str(script))

        results = fingerprint.check_staleness(str(outputs_dir))
        assert len(results) == 1
        assert results[0].is_stale is False

        # Script changes -- fingerprint.py's OWN staleness detection must
        # still behave exactly as it did before temp_artifacts existed.
        _write(script, "print('v2')\n")
        results = fingerprint.check_staleness(str(outputs_dir))
        assert results[0].is_stale is True

    def test_annotate_record_and_get_provenance_unaffected(self, tmp_path):
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        output = outputs_dir / "result.json"
        output.write_text("{}", encoding="utf-8")

        registry_root = tmp_path / ".meridian-temp-artifacts"
        TA.register_artifact(str(registry_root), name="noop", script_path=str(tmp_path / "noop.py"))

        record = annotate.record_provenance(str(outputs_dir), str(output), generating_script="noop.py")
        assert "error" not in record
        fetched = annotate.get_provenance(str(outputs_dir), str(output))
        assert fetched is not None
        assert fetched["generating_script"] == "noop.py"


class TestComposesRatherThanConflicts:
    """Disjoint on-disk storage, and a genuine, hash-verified composition
    point rather than a second, competing provenance system."""

    def test_ledgers_live_in_disjoint_files_never_collide(self, tmp_path):
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        script = _write(tmp_path / "s.py", "x = 1\n")
        output = outputs_dir / "out.txt"
        output.write_text("hi", encoding="utf-8")

        fingerprint.tag_output(str(output), str(outputs_dir), script_path=str(script))
        registry_root = tmp_path / ".meridian-temp-artifacts"
        TA.register_artifact(str(registry_root), name="s", script_path=str(script))

        cache_dir = outputs_dir / ".meridian-outputs-cache"
        assert (cache_dir / "fingerprint_ledger.json").is_file()
        assert (registry_root / "temp_artifact_registry.json").is_file()
        # Neither system ever writes into the other's file, under either
        # of their own cache directories.
        assert not (cache_dir / "temp_artifact_registry.json").exists()
        assert not (registry_root / "fingerprint_ledger.json").exists()
        assert not (registry_root / "provenance_ledger.json").exists()

    def test_cross_reference_agrees_when_hashes_match(self, tmp_path):
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        script = _write(tmp_path / "s.py", "x = 1\n")
        output = outputs_dir / "out.txt"
        output.write_text("hi", encoding="utf-8")

        fingerprint.tag_output(str(output), str(outputs_dir), script_path=str(script))
        registry_root = tmp_path / ".meridian-temp-artifacts"
        entry = TA.register_artifact(str(registry_root), name="s", script_path=str(script))

        result = TA.cross_reference_outputs_fingerprint(
            str(registry_root), entry["artifact_id"], str(outputs_dir),
        )

        assert result["available"] is True
        assert result["reason"] is None
        assert result["agrees"] is True
        expected_hash = fingerprint.script_content_hash(str(script))
        assert result["temp_artifacts_script_sha256"] == expected_hash
        assert result["fingerprint_ledger_script_hash"] == expected_hash

    def test_cross_reference_detects_real_disagreement(self, tmp_path):
        """If the script changed BETWEEN fingerprint.py's tag_output call
        and this registry's own registration, the two hashes genuinely
        differ -- cross_reference_outputs_fingerprint must surface that
        (agrees=False), never paper over it."""
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        script = _write(tmp_path / "s.py", "x = 1\n")
        output = outputs_dir / "out.txt"
        output.write_text("hi", encoding="utf-8")

        fingerprint.tag_output(str(output), str(outputs_dir), script_path=str(script))
        _write(script, "x = 2\n")  # script changed after fingerprint tagging
        registry_root = tmp_path / ".meridian-temp-artifacts"
        entry = TA.register_artifact(str(registry_root), name="s", script_path=str(script))

        result = TA.cross_reference_outputs_fingerprint(
            str(registry_root), entry["artifact_id"], str(outputs_dir),
        )

        assert result["available"] is True
        assert result["agrees"] is False
        assert result["temp_artifacts_script_sha256"] != result["fingerprint_ledger_script_hash"]

    def test_cross_reference_reports_never_tagged_without_erroring(self, tmp_path):
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        script = _write(tmp_path / "s.py", "x = 1\n")
        registry_root = tmp_path / ".meridian-temp-artifacts"
        entry = TA.register_artifact(str(registry_root), name="s", script_path=str(script))

        # fingerprint.py's ledger is empty -- this script was never tagged.
        result = TA.cross_reference_outputs_fingerprint(
            str(registry_root), entry["artifact_id"], str(outputs_dir),
        )

        assert result["available"] is True
        assert result["agrees"] is None
        assert "never fingerprint-tagged" in result["reason"]
