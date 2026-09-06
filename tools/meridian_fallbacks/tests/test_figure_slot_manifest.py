"""Acceptance tests for tools/meridian_fallbacks/figure_slot_manifest.py and
tools/meridian_fallbacks/transactional_merge.promote() (sprint item
5cc3d745, "W31-C: enforce complete promoted/held/skipped/ambiguous slot
manifests before asset promotion").

Covers, per that item's acceptance criteria, one fixture for each fail-closed
case plus JSON round-tripping:

  - Complete manifest (every expected slot classified exactly once)
                                                        -> MANIFEST_COMPLETE
  - Missing slot (no classification anywhere)          -> MANIFEST_INCOMPLETE
  - Duplicate assignment (same slot in two buckets, and twice in the SAME
    bucket)                                             -> MANIFEST_CONTRADICTORY
  - Slot classified with a missing/blank reason or owner
                                                        -> MANIFEST_CONTRADICTORY
  - Unknown slot (classified but absent from expected_slot_ids)
                                                        -> MANIFEST_CONTRADICTORY
  - Empty expected-slot list edge case (nothing expected, nothing supplied)
                                                        -> MANIFEST_COMPLETE
  - JSON round-trip of the reconciliation result and of SlotClassification
    itself.

Also covers ``transactional_merge.promote()``'s enforcement of this gate:
promotion succeeds (and actually writes) only when the manifest is complete,
and refuses -- without ever calling ``apply_patch_manifest`` or touching the
target file -- when it is not.

No canonical thesis document is touched anywhere in this file -- every
fixture is a small, disposable, synthetic payload built in-memory or under
``tmp_path`` (the ``docx_path``/``minimal_docx_parts`` fixtures come from
this package's shared ``conftest.py``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.meridian_fallbacks import figure_slot_manifest as fsm
from tools.meridian_fallbacks.figure_slot_manifest import (
    MANIFEST_COMPLETE,
    MANIFEST_CONTRADICTORY,
    MANIFEST_INCOMPLETE,
    SLOT_AMBIGUOUS,
    SLOT_HELD,
    SLOT_PROMOTED,
    SLOT_SKIPPED,
    SlotClassification,
    reconcile_slot_manifest,
)


def _entry(slot_id: str, reason: str = "reviewed", owner: str = "executor-1") -> dict[str, str]:
    return {"slot_id": slot_id, "reason": reason, "owner": owner}


# ---------------------------------------------------------------------------
# 1. MANIFEST_COMPLETE -- every expected slot classified exactly once.
# ---------------------------------------------------------------------------

class TestManifestComplete:
    def test_all_expected_slots_classified_once_across_all_four_buckets(self):
        result = reconcile_slot_manifest(
            ["fig-1", "fig-2", "fig-3", "fig-4"],
            promoted=[_entry("fig-1")],
            held=[_entry("fig-2", reason="pending review")],
            skipped=[_entry("fig-3", reason="unaffected by this revision")],
            ambiguous=[_entry("fig-4", reason="source could not be pinned")],
        )
        assert result["verdict"] == MANIFEST_COMPLETE
        assert result["unclassified_slot_ids"] == []
        assert result["duplicate_assignments"] == {}
        assert result["unknown_slot_ids"] == []
        assert result["structural_errors"] == []
        assert result["counts"] == {
            "expected_total": 4,
            "classified_total": 4,
            "counts_match": True,
        }
        assert result["buckets"][SLOT_PROMOTED] == [
            {"slot_id": "fig-1", "bucket": SLOT_PROMOTED, "reason": "reviewed", "owner": "executor-1"}
        ]

    def test_accepts_slotclassification_instances_directly(self):
        result = reconcile_slot_manifest(
            ["fig-1"],
            promoted=[SlotClassification(slot_id="fig-1", bucket=SLOT_PROMOTED, reason="ok", owner="e1")],
        )
        assert result["verdict"] == MANIFEST_COMPLETE

    def test_empty_expected_slot_list_with_no_entries_is_vacuously_complete(self):
        result = reconcile_slot_manifest([])
        assert result["verdict"] == MANIFEST_COMPLETE
        assert result["counts"] == {"expected_total": 0, "classified_total": 0, "counts_match": True}


# ---------------------------------------------------------------------------
# 2. MANIFEST_INCOMPLETE -- a slot with no classification anywhere.
# ---------------------------------------------------------------------------

class TestManifestIncomplete:
    def test_missing_slot_is_reported_incomplete(self):
        result = reconcile_slot_manifest(
            ["fig-1", "fig-2"],
            promoted=[_entry("fig-1")],
        )
        assert result["verdict"] == MANIFEST_INCOMPLETE
        assert result["unclassified_slot_ids"] == ["fig-2"]
        assert any("fig-2" in r for r in result["reasons"])

    def test_all_slots_missing_is_incomplete_not_contradictory(self):
        result = reconcile_slot_manifest(["fig-1", "fig-2", "fig-3"])
        assert result["verdict"] == MANIFEST_INCOMPLETE
        assert result["unclassified_slot_ids"] == ["fig-1", "fig-2", "fig-3"]
        assert result["counts"]["counts_match"] is False


# ---------------------------------------------------------------------------
# 3. MANIFEST_CONTRADICTORY -- duplicate assignment.
# ---------------------------------------------------------------------------

class TestManifestContradictoryDuplicates:
    def test_slot_classified_in_two_different_buckets_is_contradictory(self):
        result = reconcile_slot_manifest(
            ["fig-1"],
            promoted=[_entry("fig-1")],
            held=[_entry("fig-1", reason="also held??")],
        )
        assert result["verdict"] == MANIFEST_CONTRADICTORY
        assert result["duplicate_assignments"] == {"fig-1": [SLOT_HELD, SLOT_PROMOTED]}

    def test_slot_classified_twice_within_the_same_bucket_is_contradictory(self):
        result = reconcile_slot_manifest(
            ["fig-1"],
            promoted=[_entry("fig-1", reason="first"), _entry("fig-1", reason="second")],
        )
        assert result["verdict"] == MANIFEST_CONTRADICTORY
        assert result["duplicate_assignments"] == {"fig-1": [SLOT_PROMOTED]}


# ---------------------------------------------------------------------------
# 4. MANIFEST_CONTRADICTORY -- structural errors (missing reason/owner).
# ---------------------------------------------------------------------------

class TestManifestContradictoryStructuralErrors:
    def test_missing_reason_is_a_structural_error_not_a_silent_pass(self):
        result = reconcile_slot_manifest(
            ["fig-1"],
            promoted=[{"slot_id": "fig-1", "owner": "executor-1"}],
        )
        assert result["verdict"] == MANIFEST_CONTRADICTORY
        assert any("reason" in e for e in result["structural_errors"])
        # The malformed entry never appears in the accepted buckets output.
        assert result["buckets"][SLOT_PROMOTED] == []
        # ...and since it was rejected, the slot is ALSO reported unclassified.
        assert result["unclassified_slot_ids"] == ["fig-1"]

    def test_blank_owner_is_a_structural_error(self):
        result = reconcile_slot_manifest(
            ["fig-1"],
            skipped=[{"slot_id": "fig-1", "reason": "n/a", "owner": "   "}],
        )
        assert result["verdict"] == MANIFEST_CONTRADICTORY
        assert any("owner" in e for e in result["structural_errors"])

    def test_missing_slot_id_is_a_structural_error(self):
        result = reconcile_slot_manifest(
            ["fig-1"],
            ambiguous=[{"reason": "no source", "owner": "executor-1"}],
        )
        assert result["verdict"] == MANIFEST_CONTRADICTORY
        assert any("slot_id" in e for e in result["structural_errors"])

    def test_entry_declaring_a_conflicting_bucket_field_is_a_structural_error(self):
        result = reconcile_slot_manifest(
            ["fig-1"],
            promoted=[{"slot_id": "fig-1", "bucket": SLOT_HELD, "reason": "x", "owner": "y"}],
        )
        assert result["verdict"] == MANIFEST_CONTRADICTORY
        assert any("declares bucket" in e for e in result["structural_errors"])

    def test_non_mapping_entry_raises_type_error(self):
        with pytest.raises(TypeError):
            reconcile_slot_manifest(["fig-1"], promoted=["not-a-mapping"])


# ---------------------------------------------------------------------------
# 5. MANIFEST_CONTRADICTORY -- unknown slot (classified but not expected).
# ---------------------------------------------------------------------------

class TestManifestContradictoryUnknownSlot:
    def test_classified_slot_absent_from_expected_ids_is_contradictory(self):
        result = reconcile_slot_manifest(
            ["fig-1"],
            promoted=[_entry("fig-1")],
            held=[_entry("fig-99")],
        )
        assert result["verdict"] == MANIFEST_CONTRADICTORY
        assert result["unknown_slot_ids"] == ["fig-99"]
        assert result["counts"]["counts_match"] is False


# ---------------------------------------------------------------------------
# 6. JSON round-tripping.
# ---------------------------------------------------------------------------

class TestJsonRoundTrip:
    def test_reconciliation_result_round_trips_through_json_with_no_loss(self):
        result = reconcile_slot_manifest(
            ["fig-1", "fig-2"],
            promoted=[_entry("fig-1")],
            skipped=[_entry("fig-2", reason="unaffected")],
        )
        text = json.dumps(result)
        assert json.loads(text) == result

    def test_slot_classification_round_trips_through_to_dict_from_dict(self):
        original = SlotClassification(slot_id="fig-1", bucket=SLOT_AMBIGUOUS, reason="r", owner="o")
        rebuilt = SlotClassification.from_dict(original.to_dict())
        assert rebuilt == original
        assert json.loads(json.dumps(original.to_dict())) == original.to_dict()


# ---------------------------------------------------------------------------
# 7. transactional_merge.promote() -- enforcement before asset promotion.
# ---------------------------------------------------------------------------

class TestPromoteEnforcesCompleteManifest:
    def test_promote_succeeds_and_writes_when_manifest_is_complete(self, docx_path, minimal_docx_parts):
        from tools.meridian_fallbacks import PatchManifest
        from tools.meridian_fallbacks.transactional_merge import promote

        original_bytes = Path(docx_path).read_bytes()
        manifest_obj = PatchManifest.create_from_file(str(docx_path))
        new_doc_xml = minimal_docx_parts["word/document.xml"].replace(b"Introduction", b"Replaced")
        op = manifest_obj.add_operation(
            "replace_part", "word/document.xml", "promote figure caption fix", payload=new_doc_xml,
        )
        reconciliation = reconcile_slot_manifest(
            ["fig-1"], promoted=[_entry("fig-1", reason="typography-only, invariant holds")],
        )

        result = promote(manifest_obj, reconciliation, payloads={op.op_id: new_doc_xml})

        assert result.success, result.error
        assert manifest_obj.status == "applied"
        assert Path(docx_path).read_bytes() != original_bytes

    def test_promote_refuses_without_touching_disk_when_manifest_is_incomplete(self, docx_path):
        from tools.meridian_fallbacks import PatchManifest
        from tools.meridian_fallbacks.transactional_merge import promote

        original_bytes = Path(docx_path).read_bytes()
        manifest_obj = PatchManifest.create_from_file(str(docx_path))
        op = manifest_obj.add_operation(
            "replace_part", "word/document.xml", "should never apply", payload=b"<doc/>",
        )
        reconciliation = reconcile_slot_manifest(["fig-1", "fig-2"], promoted=[_entry("fig-1")])
        assert reconciliation["verdict"] == MANIFEST_INCOMPLETE

        result = promote(manifest_obj, reconciliation, payloads={op.op_id: b"<doc/>"})

        assert result.success is False
        assert "manifest_incomplete" in result.error
        assert "fig-2" in result.error
        # Refusal never touches the target file or burns the manifest.
        assert Path(docx_path).read_bytes() == original_bytes
        assert manifest_obj.status == "draft"
        assert result.applied_operation_ids == []

    def test_promote_refuses_when_manifest_is_contradictory(self, docx_path):
        from tools.meridian_fallbacks import PatchManifest
        from tools.meridian_fallbacks.transactional_merge import promote

        manifest_obj = PatchManifest.create_from_file(str(docx_path))
        op = manifest_obj.add_operation(
            "replace_part", "word/document.xml", "should never apply", payload=b"<doc/>",
        )
        reconciliation = reconcile_slot_manifest(
            ["fig-1"],
            promoted=[_entry("fig-1")],
            held=[_entry("fig-1", reason="conflicting")],
        )
        assert reconciliation["verdict"] == MANIFEST_CONTRADICTORY

        result = promote(manifest_obj, reconciliation, payloads={op.op_id: b"<doc/>"})

        assert result.success is False
        assert "manifest_contradictory" in result.error
        assert manifest_obj.status == "draft"

    def test_promote_rejects_a_non_mapping_reconciliation(self, docx_path):
        from tools.meridian_fallbacks import PatchManifest
        from tools.meridian_fallbacks.transactional_merge import promote

        manifest_obj = PatchManifest.create_from_file(str(docx_path))
        with pytest.raises(TypeError):
            promote(manifest_obj, ["not", "a", "mapping"])

    def test_promote_forwards_dry_run_when_manifest_is_complete(self, docx_path):
        from tools.meridian_fallbacks import PatchManifest
        from tools.meridian_fallbacks.transactional_merge import promote

        original_bytes = Path(docx_path).read_bytes()
        manifest_obj = PatchManifest.create_from_file(str(docx_path))
        op = manifest_obj.add_operation(
            "replace_part", "word/document.xml", "preview only", payload=b"<doc/>",
        )
        reconciliation = reconcile_slot_manifest(["fig-1"], promoted=[_entry("fig-1")])

        result = promote(manifest_obj, reconciliation, payloads={op.op_id: b"<doc/>"}, dry_run=True)

        assert result.success, result.error
        assert result.dry_run is True
        # A dry run never mutates manifest.status, matching apply_patch_manifest.
        assert manifest_obj.status == "draft"
        assert Path(docx_path).read_bytes() == original_bytes
