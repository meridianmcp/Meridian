"""MDE-3 -- release-transaction crash-recovery evidence surfaced in handoff.

Covers meridian/handoff.py's render_release_transaction_evidence_xml,
_release_transaction_evidence_summary, and generate_handoff(mode="goal",
emit_manifest=True) end to end: a project with release-transaction activity
(especially anything stuck in RECOVERY_REQUIRED) gets that surfaced in the
token-signed /goal body without a separate query.
"""
from __future__ import annotations

import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian.db import docx_merge as DM


def _extract_manifest_block(text: str) -> "str | None":
    m = re.search(r"<handoff_manifest\b.*?</handoff_manifest>", text, re.DOTALL)
    return m.group(0) if m else None


def _extract_release_block(text: str) -> "str | None":
    m = re.search(r"<release_transactions\b.*?</release_transactions>", text, re.DOTALL)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# render_release_transaction_evidence_xml -- pure rendering.
# ---------------------------------------------------------------------------

class TestRenderReleaseTransactionEvidenceXml:
    def test_none_renders_empty(self):
        assert handoff_module.render_release_transaction_evidence_xml(None) == ""

    def test_empty_summary_renders_empty(self):
        assert handoff_module.render_release_transaction_evidence_xml({}) == ""
        assert handoff_module.render_release_transaction_evidence_xml(
            {"transaction_count": 0}
        ) == ""

    def test_renders_counts_and_states(self):
        summary = {
            "transaction_count": 2,
            "state_counts": {"RELEASED": 1, "RECOVERY_REQUIRED": 1},
            "recovery_required": [],
            "all_released": False,
        }
        xml = handoff_module.render_release_transaction_evidence_xml(summary)
        assert xml.startswith("<release_transactions ")
        assert xml.endswith("</release_transactions>")
        assert 'count="2"' in xml
        assert 'all_released="False"' in xml
        assert '<state name="RECOVERY_REQUIRED" count="1"/>' in xml
        assert '<state name="RELEASED" count="1"/>' in xml

    def test_renders_recovery_required_entries(self):
        summary = {
            "transaction_count": 1,
            "state_counts": {"RECOVERY_REQUIRED": 1},
            "recovery_required": [{
                "transaction_id": "t1", "change_set_id": "cs-1",
                "file_path": "report.docx", "error": "UNRESOLVED: mismatch",
            }],
            "all_released": False,
        }
        xml = handoff_module.render_release_transaction_evidence_xml(summary)
        assert '<recovery_required transaction_id="t1"' in xml
        assert 'change_set_id="cs-1"' in xml
        assert 'file_path="report.docx"' in xml
        assert "UNRESOLVED: mismatch" in xml

    def test_escapes_untrusted_fields(self):
        summary = {
            "transaction_count": 1,
            "state_counts": {},
            "recovery_required": [{
                "transaction_id": 't1" evil="injected', "change_set_id": "cs",
                "file_path": "x", "error": "e",
            }],
            "all_released": False,
        }
        xml = handoff_module.render_release_transaction_evidence_xml(summary)
        assert 'evil="injected"' not in xml
        assert "&quot;" in xml


# ---------------------------------------------------------------------------
# _release_transaction_evidence_summary -- best-effort DB fetch.
# ---------------------------------------------------------------------------

class TestReleaseTransactionEvidenceSummary:
    @pytest.mark.asyncio
    async def test_none_project_id_returns_none(self, db):
        assert await handoff_module._release_transaction_evidence_summary(db, None) is None

    @pytest.mark.asyncio
    async def test_no_transactions_returns_none(self, db):
        project = await db_module.create_project(db, "evidence-none-proj")
        result = await handoff_module._release_transaction_evidence_summary(db, project["id"])
        assert result is None

    @pytest.mark.asyncio
    async def test_real_transactions_produce_summary(self, db):
        project = await db_module.create_project(db, "evidence-real-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        for state in DM._RELEASE_STATE_ORDER[1:]:
            await DM.advance_release_state(db, opened["transaction_id"], state, project_id=project["id"])
        result = await handoff_module._release_transaction_evidence_summary(db, project["id"])
        assert result is not None
        assert result["transaction_count"] == 1
        assert result["state_counts"][DM.RELEASE_STATE_RELEASED] == 1

    @pytest.mark.asyncio
    async def test_scoped_to_the_given_project_only(self, db):
        project_a = await db_module.create_project(db, "evidence-scope-a-proj")
        project_b = await db_module.create_project(db, "evidence-scope-b-proj")
        await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project_a["id"])
        result_b = await handoff_module._release_transaction_evidence_summary(db, project_b["id"])
        assert result_b is None


# ---------------------------------------------------------------------------
# End-to-end: generate_handoff(mode="goal", emit_manifest=True)
# ---------------------------------------------------------------------------

class TestGoalModeEndToEnd:
    @pytest.mark.asyncio
    async def test_emit_manifest_surfaces_release_transaction_evidence(self, db, tmp_path):
        project = await db_module.create_project(db, "goal-release-evidence-proj")
        await db_module.set_goal(db, project["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, project["id"], "v1", "do the thing")
        opened = await DM.open_release_transaction(
            db, "cs-1", "report.docx", base_hash="BASE", project_id=project["id"],
        )
        await DM.resolve_release_recovery(
            db, opened["transaction_id"], "SOMETHING-ELSE", project_id=project["id"],
        )

        _path, content, _amended = await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
            emit_manifest=True,
        )
        block = _extract_release_block(content)
        assert block is not None, "release-transaction evidence must be embedded when emit_manifest=True"
        assert 'count="1"' in block
        assert "RECOVERY_REQUIRED" in block

    @pytest.mark.asyncio
    async def test_no_release_activity_omits_the_block(self, db, tmp_path):
        project = await db_module.create_project(db, "goal-no-release-activity-proj")
        await db_module.set_goal(db, project["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, project["id"], "v1", "do the thing")
        _path, content, _amended = await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
            emit_manifest=True,
        )
        assert _extract_release_block(content) is None

    @pytest.mark.asyncio
    async def test_emit_manifest_false_never_queries_or_renders_it(self, db, tmp_path):
        project = await db_module.create_project(db, "goal-no-emit-manifest-proj")
        await db_module.set_goal(db, project["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, project["id"], "v1", "do the thing")
        await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        _path, content, _amended = await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
        )
        assert _extract_release_block(content) is None

    @pytest.mark.asyncio
    async def test_release_evidence_covered_by_goal_token_body_hash(self, db, tmp_path):
        project = await db_module.create_project(db, "goal-release-token-proj")
        await db_module.set_goal(db, project["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, project["id"], "v1", "do the thing")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        await DM.resolve_release_recovery(db, opened["transaction_id"], "X", project_id=project["id"])

        _path, content, _amended = await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), mode="goal", skip_ai_summary=True,
            emit_manifest=True,
        )
        m = re.search(r"<goal_token>([^<]+)</goal_token>", content)
        token = m.group(1).strip()
        body = handoff_module.strip_goal_token_banner(content)
        verified = await handoff_module.verify_handoff_token(db, token, project["id"], body=body)
        assert verified["valid"] is True

        tampered = body.replace("RECOVERY_REQUIRED", "RELEASED")
        tampered_verify = await handoff_module.verify_handoff_token(
            db, token, project["id"], body=tampered,
        )
        assert tampered_verify["valid"] is False
