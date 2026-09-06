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

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from meridian_outputs import fingerprint as FP


def _make_minimal_single_page_pdf_bytes(text: str) -> bytes:
    """Minimal single-page PDF, hand-built the same way as
    test_outputs_local.py's ``_make_minimal_pdf_bytes`` (no PDF-writing
    library is available in this environment). Kept deliberately smaller
    (single page only) since this file only needs one PDF-shaped fixture,
    to confirm tag_output's ``kind`` field correctly reports the new
    "pdf_content" classification (sprint item aa423c7e) rather than the
    pre-existing "binary_metadata" a PDF used to get."""
    # Object numbers: 1=Catalog, 2=Pages, 3=Page, 4=Contents (stream), 5=Font.
    stream_body = f"BT /F1 12 Tf 20 250 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> "
            b"/MediaBox [0 0 300 300] /Contents 4 0 R >>"
        ),
        f"<< /Length {len(stream_body)} >>\nstream\n".encode("ascii")
        + stream_body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{i} 0 obj\n".encode("ascii"))
        buf.write(body)
        buf.write(b"\nendobj\n")
    xref_offset = buf.tell()
    n = len(objects) + 1
    buf.write(f"xref\n0 {n}\n".encode("ascii"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.write(
        f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF"
        .encode("ascii")
    )
    return buf.getvalue()


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
# tag_output against a PDF output (sprint item aa423c7e -- PDF body-content
# indexing). Before this item, outputs_local.file_fingerprint classified
# every .pdf as "binary_metadata"; tag_output must now report the real
# "pdf_content" kind, without disturbing any of the text_content/json/csv
# fingerprint-shaped cases already covered elsewhere in this file.
# ---------------------------------------------------------------------------

class TestTagOutputAgainstPdf:
    def test_kind_is_pdf_content_not_binary_metadata(self, tmp_path: Path) -> None:
        try:
            import pypdf  # noqa: F401
        except ImportError:
            import pytest
            pytest.skip("pypdf not installed")

        output = tmp_path / "report.pdf"
        output.write_bytes(
            _make_minimal_single_page_pdf_bytes("generated by train.py")
        )

        tagged = FP.tag_output(str(output), str(tmp_path))

        assert tagged.kind == "pdf_content"
        assert tagged.csv_columns is None
        assert tagged.json_keys is None
        # Same generating-script-hint inference every other text-shaped kind
        # gets, now extended to PDF body text (see outputs_local.py's
        # file_fingerprint pdf_content branch).
        assert tagged.generating_script == "train.py"


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
