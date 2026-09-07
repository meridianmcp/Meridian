"""Tests for meridian/slot_manifest_receipt.py (d44e7692, 5cc3d745
follow-up) — the durable, project-scoped receipt store for figure-slot-
manifest reconciliation results.

Covers the write/read round trip, project-scoping, ``since`` freshness
bounding, and the "no receipt recorded" -> ``None`` cases — same coverage
shape as tests/test_code_intel_guard.py's own
``TestRecordAndFindReceipt`` class for the sibling ``code_intel_receipt``
module this one mirrors.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import slot_manifest_receipt as smr

from tools.meridian_fallbacks.figure_slot_manifest import (
    MANIFEST_COMPLETE,
    MANIFEST_INCOMPLETE,
    reconcile_slot_manifest,
)


def _complete_reconciliation() -> dict:
    result = reconcile_slot_manifest(
        ["fig-1"],
        promoted=[{"slot_id": "fig-1", "reason": "typography-only", "owner": "executor-1"}],
    )
    assert result["verdict"] == MANIFEST_COMPLETE
    return result


def _incomplete_reconciliation() -> dict:
    result = reconcile_slot_manifest(["fig-1", "fig-2"], promoted=[
        {"slot_id": "fig-1", "reason": "typography-only", "owner": "executor-1"},
    ])
    assert result["verdict"] == MANIFEST_INCOMPLETE
    return result


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# record_slot_manifest_receipt — write path.
# ---------------------------------------------------------------------------

class TestRecordSlotManifestReceipt:
    async def test_record_writes_durable_action_audit_log_row(self, db):
        pid = await _project(db, "slot-receipt-write-proj")
        row = await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-1",
            reconciliation=_complete_reconciliation(),
        )
        assert row is not None
        assert row["event_type"] == smr.RECEIPT_EVENT_TYPE
        assert row["project_id"] == pid

        log = await db_module.get_action_audit_log(
            db, project_id=pid, event_type=smr.RECEIPT_EVENT_TYPE,
        )
        assert len(log) == 1

    async def test_record_without_project_id_is_a_noop(self, db):
        row = await smr.record_slot_manifest_receipt(
            db, project_id=None, item_id="item-1",
            reconciliation=_complete_reconciliation(),
        )
        assert row is None

    async def test_record_without_item_id_is_a_noop(self, db):
        pid = await _project(db, "slot-receipt-no-item-proj")
        row = await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="", reconciliation=_complete_reconciliation(),
        )
        assert row is None

    async def test_record_rejects_a_non_mapping_reconciliation(self, db):
        pid = await _project(db, "slot-receipt-bad-type-proj")
        with pytest.raises(TypeError):
            await smr.record_slot_manifest_receipt(
                db, project_id=pid, item_id="item-1",
                reconciliation=["not", "a", "mapping"],
            )

    async def test_record_stores_the_verdict_and_reasons_in_detail(self, db):
        pid = await _project(db, "slot-receipt-detail-proj")
        reconciliation = _incomplete_reconciliation()
        row = await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-1", reconciliation=reconciliation,
        )
        detail = smr._receipt_detail(row)
        assert detail["item_id"] == "item-1"
        assert detail["verdict"] == MANIFEST_INCOMPLETE
        assert detail["unclassified_slot_ids"] == ["fig-2"]
        assert detail["reasons"] == reconciliation["reasons"]


# ---------------------------------------------------------------------------
# find_recent_slot_manifest_receipt — read path.
# ---------------------------------------------------------------------------

class TestFindRecentSlotManifestReceipt:
    async def test_find_returns_none_when_nothing_recorded(self, db):
        pid = await _project(db, "slot-receipt-find-empty-proj")
        found = await smr.find_recent_slot_manifest_receipt(
            db, project_id=pid, item_id="item-1",
        )
        assert found is None

    async def test_find_returns_the_recorded_receipt(self, db):
        pid = await _project(db, "slot-receipt-find-proj")
        await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-1",
            reconciliation=_complete_reconciliation(),
        )
        found = await smr.find_recent_slot_manifest_receipt(
            db, project_id=pid, item_id="item-1",
        )
        assert found is not None
        assert found["project_id"] == pid
        assert smr.receipt_verdict(found) == MANIFEST_COMPLETE

    async def test_find_is_scoped_to_the_requested_item_id(self, db):
        """Two different items in the SAME project each get their own
        receipt; find_recent_slot_manifest_receipt for one must never return
        the other's row -- action_audit_log has no item_id column, so this
        exercises the client-side detail["item_id"] matching directly."""
        pid = await _project(db, "slot-receipt-item-scope-proj")
        await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-a",
            reconciliation=_complete_reconciliation(),
        )
        await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-b",
            reconciliation=_incomplete_reconciliation(),
        )
        found_a = await smr.find_recent_slot_manifest_receipt(
            db, project_id=pid, item_id="item-a",
        )
        found_b = await smr.find_recent_slot_manifest_receipt(
            db, project_id=pid, item_id="item-b",
        )
        assert smr.receipt_verdict(found_a) == MANIFEST_COMPLETE
        assert smr.receipt_verdict(found_b) == MANIFEST_INCOMPLETE

    async def test_find_is_scoped_to_the_requested_project(self, db):
        """A receipt recorded for one project must never satisfy a lookup
        for a different project, even with the same item_id."""
        pid_a = await _project(db, "slot-receipt-proj-scope-a")
        pid_b = await _project(db, "slot-receipt-proj-scope-b")
        await smr.record_slot_manifest_receipt(
            db, project_id=pid_a, item_id="shared-item-id",
            reconciliation=_complete_reconciliation(),
        )
        found_in_b = await smr.find_recent_slot_manifest_receipt(
            db, project_id=pid_b, item_id="shared-item-id",
        )
        assert found_in_b is None

    async def test_find_respects_since_freshness_filter(self, db):
        """A receipt recorded before the 'since' floor must not count as
        evidence for the current claim -- mirrors code_intel_receipt's
        find_recent_prospect_receipt / EVIDENCE_STALE freshness contract."""
        pid = await _project(db, "slot-receipt-stale-proj")
        await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-1",
            reconciliation=_complete_reconciliation(),
        )
        found = await smr.find_recent_slot_manifest_receipt(
            db, project_id=pid, item_id="item-1", since="2999-01-01 00:00:00",
        )
        assert found is None

    async def test_find_without_project_id_or_item_id_returns_none(self, db):
        pid = await _project(db, "slot-receipt-missing-args-proj")
        assert await smr.find_recent_slot_manifest_receipt(
            db, project_id=None, item_id="item-1",
        ) is None
        assert await smr.find_recent_slot_manifest_receipt(
            db, project_id=pid, item_id="",
        ) is None


# ---------------------------------------------------------------------------
# receipt_verdict — public accessor.
# ---------------------------------------------------------------------------

class TestReceiptVerdict:
    def test_receipt_verdict_none_for_none_row(self):
        assert smr.receipt_verdict(None) is None

    def test_receipt_verdict_none_for_row_with_unparsable_detail(self):
        assert smr.receipt_verdict({"detail": "not json"}) is None

    def test_receipt_verdict_extracts_the_stored_verdict(self):
        row = {"detail": '{"item_id": "x", "verdict": "manifest_complete"}'}
        assert smr.receipt_verdict(row) == "manifest_complete"
