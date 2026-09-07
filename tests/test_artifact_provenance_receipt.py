"""Tests for meridian/artifact_provenance_receipt.py (b1fee417, W31-A) --
the durable, project-scoped receipt store for artifact-provenance
resolutions.

Covers the write/read round trip (single + batch), project/artifact
scoping, the audit-tool status-vocabulary translation table, ``since``
freshness bounding, and the "no receipt recorded" -> ``None``/``[]`` cases
-- same coverage shape as ``tests/test_slot_manifest_receipt.py``'s own
suite for the sibling ``slot_manifest_receipt`` module this one mirrors.
"""
from __future__ import annotations

from meridian import artifact_provenance_receipt as apr
from meridian import db as db_module


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


def _binding(**overrides) -> dict:
    base = {
        "artifact_id": "fig-1",
        "kind": "figure",
        "canonical_path": "/outputs/loss_curve.png",
        "status": apr.RESOLVED,
        "match_type": "exact",
        "evidence": "meridian_outputs_exact",
        "generating_script": "train.py",
        "resolved_sha256": "abc123",
        "reason": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# record_artifact_provenance_receipt -- single write path.
# ---------------------------------------------------------------------------

class TestRecordArtifactProvenanceReceipt:
    async def test_record_writes_durable_action_audit_log_row(self, db):
        pid = await _project(db, "apr-write-proj")
        row = await apr.record_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-1", kind="figure",
            canonical_path="/outputs/loss_curve.png", status=apr.RESOLVED,
            match_type="exact", generating_script="train.py",
            resolved_sha256="abc123",
        )
        assert row is not None
        assert row["event_type"] == apr.RECEIPT_EVENT_TYPE
        assert row["project_id"] == pid

        log = await db_module.get_action_audit_log(
            db, project_id=pid, event_type=apr.RECEIPT_EVENT_TYPE,
        )
        assert len(log) == 1

    async def test_record_without_project_id_is_a_noop(self, db):
        row = await apr.record_artifact_provenance_receipt(
            db, project_id=None, artifact_id="fig-1", status=apr.RESOLVED,
        )
        assert row is None

    async def test_record_without_artifact_id_is_a_noop(self, db):
        pid = await _project(db, "apr-no-artifact-proj")
        row = await apr.record_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="", status=apr.RESOLVED,
        )
        assert row is None

    async def test_record_stores_every_field_in_detail(self, db):
        pid = await _project(db, "apr-detail-proj")
        row = await apr.record_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-2", kind="figure",
            canonical_path="/outputs/plot.png", status=apr.HASH_MISMATCH,
            match_type="exact", evidence="meridian_outputs_exact",
            generating_script="sweep.py", resolved_sha256="deadbeef",
            reason="hash differs", item_id="item-9", document_id="doc-7",
        )
        detail = apr._receipt_detail(row)
        assert detail["artifact_id"] == "fig-2"
        assert detail["status"] == apr.HASH_MISMATCH
        assert detail["generating_script"] == "sweep.py"
        assert detail["resolved_sha256"] == "deadbeef"
        assert detail["item_id"] == "item-9"
        assert detail["document_id"] == "doc-7"


# ---------------------------------------------------------------------------
# record_artifact_provenance_receipts_batch -- batch write path.
# ---------------------------------------------------------------------------

class TestRecordArtifactProvenanceReceiptsBatch:
    async def test_batch_writes_one_receipt_per_binding(self, db):
        pid = await _project(db, "apr-batch-proj")
        bindings = [
            _binding(artifact_id="fig-1"),
            _binding(artifact_id="fig-2", status=apr.ORPHANED, match_type=None,
                      generating_script=None, resolved_sha256=None,
                      reason="canonical_path is not resolvable"),
        ]
        written = await apr.record_artifact_provenance_receipts_batch(
            db, project_id=pid, bindings=bindings, document_id="doc-1",
        )
        assert len(written) == 2

        log = await db_module.get_action_audit_log(
            db, project_id=pid, event_type=apr.RECEIPT_EVENT_TYPE,
        )
        assert len(log) == 2

    async def test_batch_skips_a_binding_missing_artifact_id(self, db):
        pid = await _project(db, "apr-batch-skip-proj")
        bindings = [_binding(artifact_id=None), _binding(artifact_id="fig-ok")]
        written = await apr.record_artifact_provenance_receipts_batch(
            db, project_id=pid, bindings=bindings,
        )
        assert len(written) == 1

    async def test_batch_skips_a_non_dict_entry(self, db):
        pid = await _project(db, "apr-batch-nondict-proj")
        written = await apr.record_artifact_provenance_receipts_batch(
            db, project_id=pid, bindings=["not-a-dict", _binding()],
        )
        assert len(written) == 1

    async def test_empty_bindings_writes_nothing(self, db):
        pid = await _project(db, "apr-batch-empty-proj")
        written = await apr.record_artifact_provenance_receipts_batch(
            db, project_id=pid, bindings=[],
        )
        assert written == []


# ---------------------------------------------------------------------------
# find_recent_artifact_provenance_receipt -- single-artifact read path.
# ---------------------------------------------------------------------------

class TestFindRecentArtifactProvenanceReceipt:
    async def test_find_returns_none_when_nothing_recorded(self, db):
        pid = await _project(db, "apr-find-empty-proj")
        found = await apr.find_recent_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-1",
        )
        assert found is None

    async def test_find_returns_the_recorded_receipt(self, db):
        pid = await _project(db, "apr-find-proj")
        await apr.record_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-1", status=apr.RESOLVED,
            generating_script="train.py",
        )
        found = await apr.find_recent_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-1",
        )
        assert found is not None
        assert found["project_id"] == pid
        assert apr.receipt_status(found) == apr.RESOLVED

    async def test_find_is_scoped_to_the_requested_artifact_id(self, db):
        """Two different artifacts in the SAME project each get their own
        receipt; find_recent_artifact_provenance_receipt for one must never
        return the other's row -- action_audit_log has no artifact_id
        column, so this exercises the client-side detail["artifact_id"]
        matching directly."""
        pid = await _project(db, "apr-artifact-scope-proj")
        await apr.record_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-a", status=apr.RESOLVED,
        )
        await apr.record_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-b", status=apr.ORPHANED,
        )
        found_a = await apr.find_recent_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-a",
        )
        found_b = await apr.find_recent_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-b",
        )
        assert apr.receipt_status(found_a) == apr.RESOLVED
        assert apr.receipt_status(found_b) == apr.ORPHANED

    async def test_find_is_scoped_to_the_requested_project(self, db):
        pid_a = await _project(db, "apr-proj-scope-a")
        pid_b = await _project(db, "apr-proj-scope-b")
        await apr.record_artifact_provenance_receipt(
            db, project_id=pid_a, artifact_id="shared-artifact-id",
            status=apr.RESOLVED,
        )
        found_in_b = await apr.find_recent_artifact_provenance_receipt(
            db, project_id=pid_b, artifact_id="shared-artifact-id",
        )
        assert found_in_b is None

    async def test_find_respects_since_freshness_filter(self, db):
        pid = await _project(db, "apr-stale-proj")
        await apr.record_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-1", status=apr.RESOLVED,
        )
        found = await apr.find_recent_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-1", since="2999-01-01 00:00:00",
        )
        assert found is None

    async def test_find_without_project_id_or_artifact_id_returns_none(self, db):
        pid = await _project(db, "apr-missing-args-proj")
        assert await apr.find_recent_artifact_provenance_receipt(
            db, project_id=None, artifact_id="fig-1",
        ) is None
        assert await apr.find_recent_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="",
        ) is None


# ---------------------------------------------------------------------------
# find_recent_artifact_provenance_receipts_for_document -- document-scoped
# read path.
# ---------------------------------------------------------------------------

class TestFindRecentArtifactProvenanceReceiptsForDocument:
    async def test_returns_every_receipt_for_the_document(self, db):
        pid = await _project(db, "apr-doc-proj")
        bindings = [_binding(artifact_id="fig-1"), _binding(artifact_id="fig-2")]
        await apr.record_artifact_provenance_receipts_batch(
            db, project_id=pid, bindings=bindings, document_id="doc-1",
        )
        # A receipt for a DIFFERENT document must never leak into this doc's
        # results.
        await apr.record_artifact_provenance_receipt(
            db, project_id=pid, artifact_id="fig-9", status=apr.RESOLVED,
            document_id="doc-other",
        )
        found = await apr.find_recent_artifact_provenance_receipts_for_document(
            db, project_id=pid, document_id="doc-1",
        )
        assert len(found) == 2
        assert {apr._receipt_detail(r)["artifact_id"] for r in found} == {"fig-1", "fig-2"}

    async def test_returns_empty_list_when_nothing_recorded(self, db):
        pid = await _project(db, "apr-doc-empty-proj")
        found = await apr.find_recent_artifact_provenance_receipts_for_document(
            db, project_id=pid, document_id="doc-1",
        )
        assert found == []

    async def test_without_project_id_or_document_id_returns_empty_list(self, db):
        pid = await _project(db, "apr-doc-missing-args-proj")
        assert await apr.find_recent_artifact_provenance_receipts_for_document(
            db, project_id=None, document_id="doc-1",
        ) == []
        assert await apr.find_recent_artifact_provenance_receipts_for_document(
            db, project_id=pid, document_id="",
        ) == []


# ---------------------------------------------------------------------------
# receipt_status -- public accessor.
# ---------------------------------------------------------------------------

class TestReceiptStatus:
    def test_receipt_status_none_for_none_row(self):
        assert apr.receipt_status(None) is None

    def test_receipt_status_none_for_row_with_unparsable_detail(self):
        assert apr.receipt_status({"detail": "not json"}) is None

    def test_receipt_status_extracts_the_stored_status(self):
        row = {"detail": '{"artifact_id": "x", "status": "resolved"}'}
        assert apr.receipt_status(row) == "resolved"


# ---------------------------------------------------------------------------
# AUDIT_STATUS_TO_BINDING_STATUS -- the audit-tool <-> bind_artifact_
# provenance vocabulary translation table.
# ---------------------------------------------------------------------------

class TestAuditStatusToBindingStatus:
    def test_every_audit_tool_status_maps_to_a_valid_binding_status(self):
        """meridian.mcp.handlers.notes_decisions.handle_audit_figure_table_
        provenance's own five statuses (ok/ambiguous/orphan/mismatch/
        unresolved) must all translate onto one of bind_artifact_
        provenance's four canonical statuses -- an unmapped audit status
        would silently drop out of the translation table this module's
        real call site relies on."""
        for audit_status in ("ok", "ambiguous", "orphan", "mismatch", "unresolved"):
            assert audit_status in apr.AUDIT_STATUS_TO_BINDING_STATUS
            assert apr.AUDIT_STATUS_TO_BINDING_STATUS[audit_status] in apr.BINDING_STATUSES

    def test_ambiguous_maps_to_unresolved_not_its_own_status(self):
        """bind_artifact_provenance itself classifies an ambiguous
        multi-candidate basename match as UNRESOLVED, never a status of its
        own -- the translation table must agree, not invent a fifth
        status."""
        assert apr.AUDIT_STATUS_TO_BINDING_STATUS["ambiguous"] == apr.UNRESOLVED

    def test_ok_maps_to_resolved(self):
        assert apr.AUDIT_STATUS_TO_BINDING_STATUS["ok"] == apr.RESOLVED

    def test_mismatch_maps_to_hash_mismatch(self):
        assert apr.AUDIT_STATUS_TO_BINDING_STATUS["mismatch"] == apr.HASH_MISMATCH

    def test_orphan_maps_to_orphaned(self):
        assert apr.AUDIT_STATUS_TO_BINDING_STATUS["orphan"] == apr.ORPHANED
