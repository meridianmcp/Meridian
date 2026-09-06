"""meridian.handoff.build_slot_manifest_readiness_for_handoff and
generate_handoff's slot_manifest_readiness out-param (d44e7692, 5cc3d745
follow-up).

Mirrors tests/test_docx_integrity_gate.py's own coverage of the sibling
promotion_readiness feature this one is modeled on (see that file's
section 5, around build_promotion_readiness_for_handoff) — a standalone
new file rather than an addition to that already-large, hot-contention
file.
"""
from __future__ import annotations

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import slot_manifest_receipt as smr

from tools.meridian_fallbacks.figure_slot_manifest import (
    MANIFEST_COMPLETE,
    MANIFEST_INCOMPLETE,
    reconcile_slot_manifest,
)


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


def _complete_reconciliation() -> dict:
    return reconcile_slot_manifest(
        ["fig-1"],
        promoted=[{"slot_id": "fig-1", "reason": "typography-only", "owner": "executor-1"}],
    )


def _incomplete_reconciliation() -> dict:
    return reconcile_slot_manifest(["fig-1", "fig-2"], promoted=[
        {"slot_id": "fig-1", "reason": "typography-only", "owner": "executor-1"},
    ])


# ---------------------------------------------------------------------------
# 1. handoff.build_slot_manifest_readiness_for_handoff — direct unit tests.
# ---------------------------------------------------------------------------

class TestBuildSlotManifestReadinessForHandoff:
    async def test_reports_has_receipt_and_verdict_per_item(self, db):
        pid = await _project(db, "slot-readiness-basic")
        await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-complete",
            reconciliation=_complete_reconciliation(),
        )
        await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-incomplete",
            reconciliation=_incomplete_reconciliation(),
        )
        items = [
            {"id": "item-complete", "claimed_at": None},
            {"id": "item-incomplete", "claimed_at": None},
            {"id": "item-no-receipt", "claimed_at": None},
        ]

        result = await handoff_module.build_slot_manifest_readiness_for_handoff(
            db, pid, items,
        )

        by_item = {c["item_id"]: c for c in result["checked"]}
        assert by_item["item-complete"]["has_receipt"] is True
        assert by_item["item-complete"]["verdict"] == MANIFEST_COMPLETE
        assert by_item["item-incomplete"]["has_receipt"] is True
        assert by_item["item-incomplete"]["verdict"] == MANIFEST_INCOMPLETE
        assert by_item["item-no-receipt"]["has_receipt"] is False
        assert by_item["item-no-receipt"]["verdict"] is None
        # Only the item with a genuinely COMPLETE receipt counts as resolved.
        assert result["unresolved_count"] == 2

    async def test_empty_for_no_items(self, db):
        pid = await _project(db, "slot-readiness-empty")
        result = await handoff_module.build_slot_manifest_readiness_for_handoff(
            db, pid, None,
        )
        assert result == {"checked": [], "unresolved_count": 0}

    async def test_skips_malformed_items_without_raising(self, db):
        pid = await _project(db, "slot-readiness-malformed")
        result = await handoff_module.build_slot_manifest_readiness_for_handoff(
            db, pid, [{"no_id_field": True}, "not-a-dict", None],
        )
        assert result == {"checked": [], "unresolved_count": 0}

    async def test_respects_max_checked_items_bound(self, db):
        pid = await _project(db, "slot-readiness-bound")
        items = [{"id": f"item-{i}", "claimed_at": None} for i in range(5)]
        result = await handoff_module.build_slot_manifest_readiness_for_handoff(
            db, pid, items, max_checked_items=2,
        )
        assert len(result["checked"]) == 2

    async def test_stale_receipt_before_claimed_at_does_not_count(self, db):
        """A receipt recorded before the item's own claimed_at must not
        satisfy readiness for the CURRENT claim -- same freshness contract
        find_recent_slot_manifest_receipt's own since= param enforces."""
        pid = await _project(db, "slot-readiness-stale")
        await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id="item-1",
            reconciliation=_complete_reconciliation(),
        )
        items = [{"id": "item-1", "claimed_at": "2999-01-01 00:00:00"}]

        result = await handoff_module.build_slot_manifest_readiness_for_handoff(
            db, pid, items,
        )

        assert result["checked"][0]["has_receipt"] is False
        assert result["unresolved_count"] == 1


# ---------------------------------------------------------------------------
# 2. generate_handoff's slot_manifest_readiness out-param — purely additive.
# ---------------------------------------------------------------------------

class TestGenerateHandoffSlotManifestReadinessOutParam:
    async def test_out_param_is_purely_additive(self, db, tmp_path):
        """A caller that never passes slot_manifest_readiness sees zero
        behavior change -- and a caller that DOES pass it gets it populated
        for the full/delta modes without altering the returned (path,
        content, amended). Mirrors test_generate_handoff_promotion_
        readiness_out_param_is_purely_additive in test_docx_integrity_gate.py
        exactly."""
        pid = await _project(db, "handoff-slot-manifest-readiness")
        item = await db_module.add_sprint_item(db, pid, "v1", "An ordinary item")

        out_dir = str(tmp_path / "handoff_out")
        readiness: dict = {}
        path, content, amended = await handoff_module.generate_handoff(
            db, pid, out_dir, mode="delta", skip_ai_summary=True,
            slot_manifest_readiness=readiness,
        )
        assert path  # rendered successfully
        assert isinstance(readiness, dict)
        assert "checked" in readiness
        # Unlike promotion_readiness (gated on a declared field), every
        # pending item is checked -- an ordinary item with no recorded
        # slot-manifest receipt correctly reports has_receipt=False, not
        # "skipped" (see build_slot_manifest_readiness_for_handoff's own
        # docstring for why there is no declared-field pre-filter here).
        assert len(readiness["checked"]) == 1
        assert readiness["checked"][0]["item_id"] == item["id"]
        assert readiness["checked"][0]["has_receipt"] is False
        assert readiness["unresolved_count"] == 1

        # And the None-default path (every pre-existing caller) is unaffected.
        path2, content2, amended2 = await handoff_module.generate_handoff(
            db, pid, out_dir, mode="delta", skip_ai_summary=True,
        )
        assert path2

    async def test_out_param_surfaces_a_recorded_receipt(self, db, tmp_path):
        pid = await _project(db, "handoff-slot-manifest-readiness-receipt")
        item = await db_module.add_sprint_item(db, pid, "v1", "Figure-slot promotion item")
        await smr.record_slot_manifest_receipt(
            db, project_id=pid, item_id=item["id"],
            reconciliation=_complete_reconciliation(),
        )

        out_dir = str(tmp_path / "handoff_out2")
        readiness: dict = {}
        path, content, amended = await handoff_module.generate_handoff(
            db, pid, out_dir, mode="delta", skip_ai_summary=True,
            slot_manifest_readiness=readiness,
        )
        assert path
        by_item = {c["item_id"]: c for c in readiness["checked"]}
        assert by_item[item["id"]]["has_receipt"] is True
        assert by_item[item["id"]]["verdict"] == MANIFEST_COMPLETE
        assert readiness["unresolved_count"] == 0

    async def test_both_readiness_out_params_together_are_independent(self, db, tmp_path):
        """promotion_readiness and slot_manifest_readiness are independent
        out-params -- passing both populates both, neither one clobbers the
        other's dict."""
        pid = await _project(db, "handoff-both-readiness-params")
        await db_module.add_sprint_item(db, pid, "v1", "An ordinary item")

        out_dir = str(tmp_path / "handoff_out3")
        promo: dict = {}
        slot: dict = {}
        path, content, amended = await handoff_module.generate_handoff(
            db, pid, out_dir, mode="delta", skip_ai_summary=True,
            promotion_readiness=promo, slot_manifest_readiness=slot,
        )
        assert path
        assert "checked" in promo
        assert "checked" in slot
        assert promo is not slot
