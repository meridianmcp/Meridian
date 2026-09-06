"""Tests for meridian_outputs.fingerprint's explicit digest metadata and its
backward-compatibility path (sprint item fa600e42).

Prior state (confirmed by investigation): script_content_hash /
ScriptTaggedFingerprint.script_hash / StalenessResult.*_hash were all bare
hex strings with no algorithm/version metadata -- a caller comparing two
digests from different points in time had no way to tell "content changed"
from "we started hashing differently", and there was no dedicated test file
for this module at all (only indirect coverage via test_provenance_status.py's
stale_by_script tests).

Covers:
  - script_content_digest wraps the same digest script_content_hash returns,
    with explicit algorithm/version metadata.
  - _digest_hex reads both the OLD bare-string and NEW structured shapes.
  - tag_output populates the new content_digest field additively -- the
    existing bare script_hash field is unchanged.
  - check_staleness works identically against an OLD-style ledger row
    (script_hash only, no content_digest -- as if written before this item
    existed) and a NEW-style row, proving the compatibility path actually
    functions, not just exists on paper.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import fingerprint as FP


# ---------------------------------------------------------------------------
# script_content_digest
# ---------------------------------------------------------------------------

class TestScriptContentDigest:
    def test_digest_matches_bare_hash(self, tmp_path: Path) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('hello')\n", encoding="utf-8")

        bare = FP.script_content_hash(str(script))
        digest = FP.script_content_digest(str(script))

        assert digest is not None
        assert digest["digest"] == bare

    def test_digest_carries_algorithm_and_version(self, tmp_path: Path) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('hello')\n", encoding="utf-8")

        digest = FP.script_content_digest(str(script))

        assert digest["algorithm"] == "sha256"
        assert digest["version"] == 1

    def test_missing_script_returns_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.py"
        assert FP.script_content_digest(str(missing)) is None


# ---------------------------------------------------------------------------
# _digest_hex -- compatibility reader
# ---------------------------------------------------------------------------

class TestDigestHexCompat:
    def test_reads_old_bare_string_format(self) -> None:
        assert FP._digest_hex("abc123") == "abc123"

    def test_reads_new_structured_dict_format(self) -> None:
        value = {"digest": "abc123", "algorithm": "sha256", "version": 1}
        assert FP._digest_hex(value) == "abc123"

    def test_none_for_missing_value(self) -> None:
        assert FP._digest_hex(None) is None

    def test_none_for_unexpected_type(self) -> None:
        assert FP._digest_hex(12345) is None


# ---------------------------------------------------------------------------
# tag_output populates content_digest additively
# ---------------------------------------------------------------------------

class TestTagOutputPopulatesContentDigest:
    def test_content_digest_matches_script_hash(self, tmp_path: Path) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        output = tmp_path / "predictions.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")

        tagged = FP.tag_output(str(output), str(tmp_path), script_path=str(script))

        assert tagged.script_hash is not None
        assert tagged.content_digest is not None
        assert tagged.content_digest["digest"] == tagged.script_hash
        assert tagged.content_digest["algorithm"] == "sha256"
        assert tagged.content_digest["version"] == 1

    def test_unresolvable_script_leaves_both_hash_fields_none(
        self, tmp_path: Path,
    ) -> None:
        output = tmp_path / "predictions.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")  # no generating_script hint

        tagged = FP.tag_output(str(output), str(tmp_path))

        assert tagged.script_hash is None
        assert tagged.content_digest is None


# ---------------------------------------------------------------------------
# find_stale_by_script -- compatibility path (code-review fix, fa600e42)
# ---------------------------------------------------------------------------

class TestFindStaleByScriptDigestCompat:
    """find_stale_by_script must agree with check_staleness about the same
    ledger data -- both now read via _digest_hex's OLD-bare-string/NEW-
    structured compatibility path, instead of find_stale_by_script reading
    the bare script_hash field directly while check_staleness preferred
    content_digest (a latent inconsistency an adversarial review caught)."""

    def test_old_style_row_detected_stale_after_script_change(
        self, tmp_path: Path,
    ) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        output = tmp_path / "predictions.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")

        old_style_hash = FP.script_content_hash(str(script))
        ledger_path = FP._ledger_path(str(tmp_path))
        with open(ledger_path, "w", encoding="utf-8") as fh:
            json.dump({
                str(output): {
                    "path": str(output),
                    "kind": "text_content",
                    "csv_columns": None,
                    "json_keys": None,
                    "generating_script": "train.py",
                    "script_path": str(script),
                    "script_hash": old_style_hash,
                    "tagged_at": "2026-01-01T00:00:00+00:00",
                },
            }, fh)

        assert FP.find_stale_by_script(str(tmp_path), str(script)) == []

        script.write_text("print('v2 fixed')\n", encoding="utf-8")
        assert FP.find_stale_by_script(str(tmp_path), str(script)) == [str(output)]

    def test_digest_only_row_not_falsely_reported_stale(
        self, tmp_path: Path,
    ) -> None:
        """The exact regression the review caught: a row whose recorded
        hash lives ONLY under content_digest (no bare script_hash) must
        not be misread as "no recorded hash" (None) and falsely flagged
        stale by comparing None != current_hash."""
        script = tmp_path / "train.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        output = tmp_path / "predictions.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")

        digest_only_hash = FP.script_content_hash(str(script))
        ledger_path = FP._ledger_path(str(tmp_path))
        with open(ledger_path, "w", encoding="utf-8") as fh:
            json.dump({
                str(output): {
                    "path": str(output),
                    "kind": "text_content",
                    "csv_columns": None,
                    "json_keys": None,
                    "generating_script": "train.py",
                    "script_path": str(script),
                    "script_hash": None,  # deliberately missing/None
                    "content_digest": {
                        "digest": digest_only_hash,
                        "algorithm": "sha256",
                        "version": 1,
                    },
                    "tagged_at": "2026-01-01T00:00:00+00:00",
                },
            }, fh)

        assert FP.find_stale_by_script(str(tmp_path), str(script)) == []


# ---------------------------------------------------------------------------
# check_staleness -- compatibility path (OLD bare-string vs NEW structured
# ledger rows), verified in both directions
# ---------------------------------------------------------------------------

class TestCheckStalenessCompatPath:
    def test_old_style_row_without_content_digest_still_works(
        self, tmp_path: Path,
    ) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        output = tmp_path / "predictions.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")

        old_style_hash = FP.script_content_hash(str(script))
        ledger_path = FP._ledger_path(str(tmp_path))
        with open(ledger_path, "w", encoding="utf-8") as fh:
            json.dump({
                str(output): {
                    "path": str(output),
                    "kind": "text_content",
                    "csv_columns": None,
                    "json_keys": None,
                    "generating_script": "train.py",
                    "script_path": str(script),
                    "script_hash": old_style_hash,
                    "tagged_at": "2026-01-01T00:00:00+00:00",
                    # deliberately NO "content_digest" key -- simulates a
                    # row written before this item existed.
                },
            }, fh)

        results = FP.check_staleness(str(tmp_path))
        assert len(results) == 1
        assert results[0].is_stale is False
        assert results[0].reason == "generating script unchanged since tagging"

        script.write_text("print('v2 fixed')\n", encoding="utf-8")
        results = FP.check_staleness(str(tmp_path))
        assert results[0].is_stale is True
        assert results[0].reason == "generating script content changed since tagging"

    def test_new_style_row_with_content_digest_also_works(
        self, tmp_path: Path,
    ) -> None:
        script = tmp_path / "train.py"
        script.write_text("print('v1')\n", encoding="utf-8")
        output = tmp_path / "predictions.csv"
        output.write_text("a,b\n1,2\n", encoding="utf-8")

        tagged = FP.tag_output(str(output), str(tmp_path), script_path=str(script))
        assert tagged.content_digest is not None  # sanity: this IS a new-style row

        results = FP.check_staleness(str(tmp_path))
        assert len(results) == 1
        assert results[0].is_stale is False

        script.write_text("print('v2 fixed')\n", encoding="utf-8")
        results = FP.check_staleness(str(tmp_path))
        assert results[0].is_stale is True
