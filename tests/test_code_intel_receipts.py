"""Tests for the two new evidence-receipt modules added by sprint item
8c047a44 ("DOCS-R2-E: make convergence, reindex, local-pointer, and manifest
evidence explicit and generated"):

* ``meridian/code_index_receipt.py`` -- durable receipts for
  ``code_index.compute_bounded_reindex_scope()`` (the "reindex" evidence).
* ``meridian/docs_structure_receipt.py`` -- durable receipts for
  ``docs_intel.get_structure_freshness()`` / ``check_structure_staleness()``
  (the "local-pointer" evidence).

Both modules follow the exact write/read/scope/freshness contract already
proven by ``tests/test_slot_manifest_receipt.py`` for the sibling
``meridian/slot_manifest_receipt.py`` module -- this file mirrors that
coverage shape for the two new receipt kinds rather than inventing a new one.

Convergence evidence (``meridian/outputs_indexer.py::get_convergence_state``)
is intentionally NOT re-tested here -- it is already fully covered by
``tests/test_outputs_convergence.py`` (R2-B) and already wired as an opt-in
input to ``meridian.fallbacks.check_docx_promotion_evidence``; this item does
not duplicate or modify that coverage. Manifest evidence
(``tools/meridian_fallbacks/capability_manifest.json``) has its own dedicated
top-level suite, ``tests/test_manifest_consistency.py``.
"""
from __future__ import annotations

import pytest

from meridian import code_index_receipt as cir
from meridian import db as db_module
from meridian import docs_structure_receipt as dsr


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


def _safe_scope(repo_path: str) -> dict:
    return {
        "repo_path": repo_path,
        "excluded_paths": [],
        "nested_worktree_count": 0,
        "safe": True,
        "recommended_repo_path": repo_path,
    }


def _unsafe_scope(repo_path: str, recommended: str) -> dict:
    return {
        "repo_path": repo_path,
        "excluded_paths": [f"{repo_path}/.claude/worktrees"],
        "nested_worktree_count": 42,
        "safe": False,
        "recommended_repo_path": recommended,
    }


def _trustworthy_freshness() -> dict:
    return {
        "indexed": True,
        "complete": True,
        "stale": False,
        "trustworthy": True,
        "source_path": "/tmp/thesis.docx",
        "source_sha256": "abc123",
        "reason": "current",
    }


def _stale_freshness() -> dict:
    return {
        "indexed": True,
        "complete": True,
        "stale": True,
        "trustworthy": False,
        "source_path": "/tmp/thesis.docx",
        "source_sha256": "def456",
        "reason": "sha256-mismatch",
    }


# ---------------------------------------------------------------------------
# meridian/code_index_receipt.py -- reindex-scope evidence.
# ---------------------------------------------------------------------------

class TestRecordReindexScopeReceipt:
    async def test_record_writes_durable_action_audit_log_row(self, db):
        pid = await _project(db, "reindex-receipt-write-proj")
        row = await cir.record_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1", scope=_safe_scope("/repo"),
        )
        assert row is not None
        assert row["event_type"] == cir.RECEIPT_EVENT_TYPE
        assert row["project_id"] == pid

        log = await db_module.get_action_audit_log(
            db, project_id=pid, event_type=cir.RECEIPT_EVENT_TYPE,
        )
        assert len(log) == 1

    async def test_record_without_project_id_is_a_noop(self, db):
        row = await cir.record_reindex_scope_receipt(
            db, project_id=None, item_id="item-1", scope=_safe_scope("/repo"),
        )
        assert row is None

    async def test_record_without_item_id_is_a_noop(self, db):
        pid = await _project(db, "reindex-receipt-no-item-proj")
        row = await cir.record_reindex_scope_receipt(
            db, project_id=pid, item_id="", scope=_safe_scope("/repo"),
        )
        assert row is None

    async def test_record_rejects_a_non_mapping_scope(self, db):
        pid = await _project(db, "reindex-receipt-bad-type-proj")
        with pytest.raises(TypeError):
            await cir.record_reindex_scope_receipt(
                db, project_id=pid, item_id="item-1", scope=["not", "a", "mapping"],
            )

    async def test_record_stores_the_scope_fields_in_detail(self, db):
        pid = await _project(db, "reindex-receipt-detail-proj")
        scope = _unsafe_scope("/repo", "/repo/meridian")
        row = await cir.record_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1", scope=scope,
        )
        detail = cir._receipt_detail(row)
        assert detail["item_id"] == "item-1"
        assert detail["safe"] is False
        assert detail["nested_worktree_count"] == 42
        assert detail["recommended_repo_path"] == "/repo/meridian"
        assert detail["excluded_paths"] == scope["excluded_paths"]


class TestFindRecentReindexScopeReceipt:
    async def test_find_returns_none_when_nothing_recorded(self, db):
        pid = await _project(db, "reindex-receipt-find-empty-proj")
        found = await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1",
        )
        assert found is None

    async def test_find_returns_the_recorded_receipt(self, db):
        pid = await _project(db, "reindex-receipt-find-proj")
        await cir.record_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1", scope=_safe_scope("/repo"),
        )
        found = await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1",
        )
        assert found is not None
        assert found["project_id"] == pid
        assert cir.receipt_safe(found) is True

    async def test_find_is_scoped_to_the_requested_item_id(self, db):
        pid = await _project(db, "reindex-receipt-item-scope-proj")
        await cir.record_reindex_scope_receipt(
            db, project_id=pid, item_id="item-a", scope=_safe_scope("/repo"),
        )
        await cir.record_reindex_scope_receipt(
            db, project_id=pid, item_id="item-b",
            scope=_unsafe_scope("/repo", "/repo/meridian"),
        )
        found_a = await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid, item_id="item-a",
        )
        found_b = await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid, item_id="item-b",
        )
        assert cir.receipt_safe(found_a) is True
        assert cir.receipt_safe(found_b) is False

    async def test_find_is_scoped_to_the_requested_project(self, db):
        pid_a = await _project(db, "reindex-receipt-proj-scope-a")
        pid_b = await _project(db, "reindex-receipt-proj-scope-b")
        await cir.record_reindex_scope_receipt(
            db, project_id=pid_a, item_id="shared-item-id", scope=_safe_scope("/repo"),
        )
        found_in_b = await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid_b, item_id="shared-item-id",
        )
        assert found_in_b is None

    async def test_find_respects_since_freshness_filter(self, db):
        pid = await _project(db, "reindex-receipt-stale-proj")
        await cir.record_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1", scope=_safe_scope("/repo"),
        )
        found = await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1", since="2999-01-01 00:00:00",
        )
        assert found is None

    async def test_find_without_project_id_or_item_id_returns_none(self, db):
        pid = await _project(db, "reindex-receipt-missing-args-proj")
        assert await cir.find_recent_reindex_scope_receipt(
            db, project_id=None, item_id="item-1",
        ) is None
        assert await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid, item_id="",
        ) is None


class TestReceiptSafe:
    def test_receipt_safe_none_for_none_row(self):
        assert cir.receipt_safe(None) is None

    def test_receipt_safe_none_for_row_with_unparsable_detail(self):
        assert cir.receipt_safe({"detail": "not json"}) is None

    def test_receipt_safe_extracts_the_stored_value(self):
        row = {"detail": '{"item_id": "x", "safe": false}'}
        assert cir.receipt_safe(row) is False


class TestGenerateReindexScopeReceipt:
    """Covers the compute-AND-record convenience entry point against a REAL
    filesystem layout -- proving this item's "explicit and generated" claim
    end to end, not just the persistence half.
    """

    async def test_generate_computes_and_persists_a_safe_scope(self, db, tmp_path):
        pid = await _project(db, "reindex-generate-safe-proj")
        (tmp_path / "meridian").mkdir()
        result = await cir.generate_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1", repo_path=str(tmp_path),
        )
        assert result["scope"]["safe"] is True
        assert result["receipt"] is not None
        found = await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1",
        )
        assert found is not None
        assert cir.receipt_safe(found) is True

    async def test_generate_computes_and_persists_an_unsafe_scope(self, db, tmp_path):
        pid = await _project(db, "reindex-generate-unsafe-proj")
        (tmp_path / "meridian").mkdir()
        worktrees_dir = tmp_path / ".claude" / "worktrees"
        worktrees_dir.mkdir(parents=True)
        for i in range(6):
            (worktrees_dir / f"wt{i}").mkdir()
        result = await cir.generate_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1", repo_path=str(tmp_path),
            worktree_threshold=5,
        )
        assert result["scope"]["safe"] is False
        assert result["scope"]["nested_worktree_count"] == 6
        found = await cir.find_recent_reindex_scope_receipt(
            db, project_id=pid, item_id="item-1",
        )
        assert cir.receipt_safe(found) is False

    async def test_generate_without_project_id_still_returns_scope_but_no_receipt(
        self, db, tmp_path,
    ):
        result = await cir.generate_reindex_scope_receipt(
            db, project_id=None, item_id="item-1", repo_path=str(tmp_path),
        )
        assert result["scope"] is not None
        assert result["receipt"] is None


# ---------------------------------------------------------------------------
# meridian/docs_structure_receipt.py -- local-pointer freshness evidence.
# ---------------------------------------------------------------------------

class TestRecordStructureFreshnessReceipt:
    async def test_record_writes_durable_action_audit_log_row(self, db):
        pid = await _project(db, "docs-structure-receipt-write-proj")
        row = await dsr.record_structure_freshness_receipt(
            db, project_id=pid, item_id="item-1", freshness=_trustworthy_freshness(),
        )
        assert row is not None
        assert row["event_type"] == dsr.RECEIPT_EVENT_TYPE
        assert row["project_id"] == pid

        log = await db_module.get_action_audit_log(
            db, project_id=pid, event_type=dsr.RECEIPT_EVENT_TYPE,
        )
        assert len(log) == 1

    async def test_record_without_project_id_is_a_noop(self, db):
        row = await dsr.record_structure_freshness_receipt(
            db, project_id=None, item_id="item-1", freshness=_trustworthy_freshness(),
        )
        assert row is None

    async def test_record_without_item_id_is_a_noop(self, db):
        pid = await _project(db, "docs-structure-receipt-no-item-proj")
        row = await dsr.record_structure_freshness_receipt(
            db, project_id=pid, item_id="", freshness=_trustworthy_freshness(),
        )
        assert row is None

    async def test_record_rejects_a_non_mapping_freshness(self, db):
        pid = await _project(db, "docs-structure-receipt-bad-type-proj")
        with pytest.raises(TypeError):
            await dsr.record_structure_freshness_receipt(
                db, project_id=pid, item_id="item-1", freshness=["not", "a", "mapping"],
            )

    async def test_record_stores_the_freshness_fields_in_detail(self, db):
        pid = await _project(db, "docs-structure-receipt-detail-proj")
        row = await dsr.record_structure_freshness_receipt(
            db, project_id=pid, item_id="item-1", freshness=_stale_freshness(),
        )
        detail = dsr._receipt_detail(row)
        assert detail["item_id"] == "item-1"
        assert detail["stale"] is True
        assert detail["trustworthy"] is False
        assert detail["reason"] == "sha256-mismatch"
        assert detail["source_sha256"] == "def456"

    async def test_record_accepts_the_narrower_check_structure_staleness_shape(self, db):
        """check_structure_staleness() returns only {stale, source_path,
        reason} -- no indexed/complete/trustworthy/source_sha256 keys. The
        recorder must accept this narrower shape unmodified (see the module
        docstring's _FRESHNESS_FIELDS note) rather than requiring the full
        get_structure_freshness() shape."""
        pid = await _project(db, "docs-structure-receipt-narrow-proj")
        narrow = {"stale": True, "source_path": "/tmp/x.docx", "reason": "sha256-mismatch"}
        row = await dsr.record_structure_freshness_receipt(
            db, project_id=pid, item_id="item-1", freshness=narrow,
        )
        assert row is not None
        detail = dsr._receipt_detail(row)
        assert detail["stale"] is True
        assert "trustworthy" not in detail
        assert "indexed" not in detail
        # receipt_trustworthy must not fabricate a value for a field this
        # narrower shape never carried.
        assert dsr.receipt_trustworthy(row) is None


class TestFindRecentStructureFreshnessReceipt:
    async def test_find_returns_none_when_nothing_recorded(self, db):
        pid = await _project(db, "docs-structure-receipt-find-empty-proj")
        found = await dsr.find_recent_structure_freshness_receipt(
            db, project_id=pid, item_id="item-1",
        )
        assert found is None

    async def test_find_returns_the_recorded_receipt(self, db):
        pid = await _project(db, "docs-structure-receipt-find-proj")
        await dsr.record_structure_freshness_receipt(
            db, project_id=pid, item_id="item-1", freshness=_trustworthy_freshness(),
        )
        found = await dsr.find_recent_structure_freshness_receipt(
            db, project_id=pid, item_id="item-1",
        )
        assert found is not None
        assert found["project_id"] == pid
        assert dsr.receipt_trustworthy(found) is True

    async def test_find_is_scoped_to_the_requested_item_id(self, db):
        pid = await _project(db, "docs-structure-receipt-item-scope-proj")
        await dsr.record_structure_freshness_receipt(
            db, project_id=pid, item_id="item-a", freshness=_trustworthy_freshness(),
        )
        await dsr.record_structure_freshness_receipt(
            db, project_id=pid, item_id="item-b", freshness=_stale_freshness(),
        )
        found_a = await dsr.find_recent_structure_freshness_receipt(
            db, project_id=pid, item_id="item-a",
        )
        found_b = await dsr.find_recent_structure_freshness_receipt(
            db, project_id=pid, item_id="item-b",
        )
        assert dsr.receipt_trustworthy(found_a) is True
        assert dsr.receipt_trustworthy(found_b) is False

    async def test_find_is_scoped_to_the_requested_project(self, db):
        pid_a = await _project(db, "docs-structure-receipt-proj-scope-a")
        pid_b = await _project(db, "docs-structure-receipt-proj-scope-b")
        await dsr.record_structure_freshness_receipt(
            db, project_id=pid_a, item_id="shared-item-id", freshness=_trustworthy_freshness(),
        )
        found_in_b = await dsr.find_recent_structure_freshness_receipt(
            db, project_id=pid_b, item_id="shared-item-id",
        )
        assert found_in_b is None

    async def test_find_respects_since_freshness_filter(self, db):
        pid = await _project(db, "docs-structure-receipt-stale-window-proj")
        await dsr.record_structure_freshness_receipt(
            db, project_id=pid, item_id="item-1", freshness=_trustworthy_freshness(),
        )
        found = await dsr.find_recent_structure_freshness_receipt(
            db, project_id=pid, item_id="item-1", since="2999-01-01 00:00:00",
        )
        assert found is None

    async def test_find_without_project_id_or_item_id_returns_none(self, db):
        pid = await _project(db, "docs-structure-receipt-missing-args-proj")
        assert await dsr.find_recent_structure_freshness_receipt(
            db, project_id=None, item_id="item-1",
        ) is None
        assert await dsr.find_recent_structure_freshness_receipt(
            db, project_id=pid, item_id="",
        ) is None


class TestReceiptTrustworthy:
    def test_receipt_trustworthy_none_for_none_row(self):
        assert dsr.receipt_trustworthy(None) is None

    def test_receipt_trustworthy_none_for_row_with_unparsable_detail(self):
        assert dsr.receipt_trustworthy({"detail": "not json"}) is None

    def test_receipt_trustworthy_extracts_the_stored_value(self):
        row = {"detail": '{"item_id": "x", "trustworthy": true}'}
        assert dsr.receipt_trustworthy(row) is True
