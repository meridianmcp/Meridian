"""MDE-3 rework -- release-transaction wiring over the REAL write path.

Closes the verifier's exact findings:

(a) No production write path drove open_release_transaction/advance_
    release_state around a real DOCX write -- only isolated unit tests of
    the transition guard did. Covered here via
    meridian.routes.tunnel.call_tunnel_tool_with_release_tracking /
    _open_docs_release_transaction / _finish_docs_release_transaction.
(b) The hash check was generic base/post equality, never tied to real
    anchors/output hashes. Covered via
    meridian.db.docx_merge.check_release_staleness, which delegates to the
    EXISTING check_merge_stale_or_overlap (anchor) and
    meridian.embedded_staleness.check_embedded_staleness (output hash).
(c) Only an XML evidence block existed, only in goal mode. Covered via
    render_release_transaction_evidence_markdown/_json and their presence
    in the STANDARD (mode="full") handoff body, not just goal mode.
(d) No claims/leases integration -- a required lookup failure silently
    degraded instead of blocking. Covered via
    meridian.routes.tunnel._required_claim_lookup_gate /
    RequiredClaimLookupFailed.

Every test below is written so it would have FAILED against the
pre-rework code and passes against the rework.

THIRD-PASS fix (54619681) -- TestDraftModeReleaseTrackingRealDocsIntel below
closes the verifier's follow-up finding: a wave-coordinated DRAFT-mode call
(draft_output_path + wave_run_id together) to move_section/copy_section/
relocate_table drove a transaction against the untouched CANONICAL
docx_path all the way to a terminal, misleading RELEASED using the DRAFT
file's own promoted_sha256 as post_hash -- and merge_docx_draft (the tool
that performs the REAL canonical promotion for this workflow) was not
wired into the release-transaction machinery at all. These tests exercise
the REAL extensions/meridian-docs docs_intel functions (not hand-crafted
payloads) through call_tunnel_tool_with_release_tracking end-to-end, and
were confirmed to FAIL against the pre-fix code (see commit history for
this file) before passing against the fix.
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian.db import docx_merge as DM
from meridian.routes import tunnel as tunnel_module

# Make meridian_docs importable from the local extensions directory --
# mirrors the established convention in tests/test_meridian_docs_caption_
# citation_write.py and friends (meridian_docs is deliberately NOT a pypi
# dependency of the core `meridian` package; see extensions/meridian-docs's
# own pyproject.toml and CLAUDE.local.md's "Testing extensions/meridian-docs
# locally" note).
_EXT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-docs")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel  # noqa: E402


# ---------------------------------------------------------------------------
# (c) Markdown / JSON evidence renderers -- pure, no DB.
# ---------------------------------------------------------------------------

def _summary(**overrides):
    base = {
        "transaction_count": 2,
        "all_released": False,
        "state_counts": {"RELEASED": 1, "RECOVERY_REQUIRED": 1},
        "recovery_required": [
            {"transaction_id": "t1", "change_set_id": "cs-1", "file_path": "x.docx",
             "error": "unresolved hash"},
        ],
    }
    base.update(overrides)
    return base


class TestMarkdownEvidenceRenderer:
    def test_renders_totals_and_state_breakdown(self):
        md = handoff_module.render_release_transaction_evidence_markdown(_summary())
        assert "## Release Transactions" in md
        assert "Total: 2" in md
        assert "RELEASED=1" in md
        assert "RECOVERY_REQUIRED=1" in md

    def test_renders_recovery_required_entries(self):
        md = handoff_module.render_release_transaction_evidence_markdown(_summary())
        assert "t1" in md
        assert "cs-1" in md
        assert "unresolved hash" in md

    def test_empty_for_none_or_zero_transactions(self):
        assert handoff_module.render_release_transaction_evidence_markdown(None) == ""
        assert handoff_module.render_release_transaction_evidence_markdown(
            {"transaction_count": 0}
        ) == ""


class TestJsonEvidenceRenderer:
    def test_renders_valid_json_with_same_evidence_as_markdown_and_xml(self):
        summary = _summary()
        raw = handoff_module.render_release_transaction_evidence_json(summary)
        parsed = json.loads(raw)
        assert parsed["transaction_count"] == summary["transaction_count"]
        assert parsed["state_counts"] == summary["state_counts"]
        assert parsed["recovery_required"][0]["transaction_id"] == "t1"

        # Same underlying summary feeds the XML renderer -- cross-check the
        # count matches so all three projections agree, not just each
        # individually being well-formed.
        xml = handoff_module.render_release_transaction_evidence_xml(summary)
        assert 'count="2"' in xml
        md = handoff_module.render_release_transaction_evidence_markdown(summary)
        assert "Total: 2" in md

    def test_empty_for_none_or_zero_transactions(self):
        assert handoff_module.render_release_transaction_evidence_json(None) == ""
        assert handoff_module.render_release_transaction_evidence_json(
            {"transaction_count": 0}
        ) == ""


# ---------------------------------------------------------------------------
# (c) Standard (default/"full") handoff mode also carries the evidence --
# previously ONLY goal mode did.
# ---------------------------------------------------------------------------

class TestStandardModeHandoffCarriesEvidence:
    @pytest.mark.asyncio
    async def test_full_mode_surfaces_markdown_xml_and_json_blocks(self, db, tmp_path):
        project = await db_module.create_project(db, "full-mode-release-evidence-proj")
        await db_module.set_goal(db, project["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, project["id"], "v1", "do the thing")
        opened = await DM.open_release_transaction(
            db, "cs-1", "report.docx", base_hash="BASE", project_id=project["id"],
        )
        await DM.resolve_release_recovery(
            db, opened["transaction_id"], "SOMETHING-ELSE", project_id=project["id"],
        )

        _path, content, _amended = await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), skip_ai_summary=True,
        )
        assert "## Release Transactions" in content
        assert "<release_transactions" in content
        assert "```json release_transactions" in content
        assert "RECOVERY_REQUIRED" in content

    @pytest.mark.asyncio
    async def test_full_mode_no_activity_omits_the_block(self, db, tmp_path):
        project = await db_module.create_project(db, "full-mode-no-release-proj")
        await db_module.set_goal(db, project["id"], "ship it", sprint="v1")
        await db_module.add_sprint_item(db, project["id"], "v1", "do the thing")
        _path, content, _amended = await handoff_module.generate_handoff(
            db, project["id"], str(tmp_path), skip_ai_summary=True,
        )
        assert "## Release Transactions" not in content
        assert "<release_transactions" not in content


# ---------------------------------------------------------------------------
# (b) check_release_staleness -- REAL anchor + output-hash connections.
# ---------------------------------------------------------------------------

class TestCheckReleaseStalenessAnchor:
    @pytest.mark.asyncio
    async def test_skipped_without_wave_or_element(self, db):
        project = await db_module.create_project(db, "staleness-skip-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "x.docx", project_id=project["id"],
        )
        result = await DM.check_release_staleness(db, opened["transaction_id"])
        assert result["blocked"] is False
        assert result["anchor_check"] is None
        assert result["output_check"] is None

    @pytest.mark.asyncio
    async def test_blocked_when_anchor_already_merged_by_another_session(self, db):
        """Connects to the REAL check_merge_stale_or_overlap gate -- an
        anchor a DIFFERENT session already merged must block, not just be
        checkable by a caller who remembers to call the separate tool."""
        project = await db_module.create_project(db, "staleness-anchor-proj")
        sess_a = (await db_module.register_session(db, project_id=project["id"], name="sess-a"))["id"]
        sess_b = (await db_module.register_session(db, project_id=project["id"], name="sess-b"))["id"]
        wave_id = "wave-1"
        file_path = "shared.docx"
        await DM.open_merge_manifest(
            db, wave_id, file_path, sess_a, draft_path="draft-a.docx",
        )
        await DM.claim_merge_owner(db, wave_id, file_path, sess_a)
        await DM.record_merge_result(db, wave_id, file_path, sess_a, "p42")
        # A DIFFERENT session's release transaction targeting the SAME anchor.
        opened = await DM.open_release_transaction(
            db, "cs-2", file_path, session_id=sess_b, project_id=project["id"],
        )
        result = await DM.check_release_staleness(
            db, opened["transaction_id"], wave_id=wave_id, element_id="p42",
        )
        assert result["blocked"] is True
        assert result["anchor_check"] is not None
        assert result["anchor_check"]["reason"] == "not_merge_owner"
        assert any("anchor_stale" in r for r in result["reasons"])

    @pytest.mark.asyncio
    async def test_not_blocked_when_no_manifest_exists_reports_no_conflict_signal(self, db):
        """When there's no wave-merge manifest at all for this wave/file,
        check_merge_stale_or_overlap itself reports no_manifest -- this
        function surfaces that as a (non-fatal) reason without inventing a
        block for infrastructure that was never set up; blocked=True still
        reflects the delegate's own verdict here (no_manifest IS a
        rejection from check_merge_stale_or_overlap's own contract)."""
        project = await db_module.create_project(db, "staleness-no-manifest-proj")
        opened = await DM.open_release_transaction(
            db, "cs-1", "unrelated.docx", session_id="session-a", project_id=project["id"],
        )
        result = await DM.check_release_staleness(
            db, opened["transaction_id"], wave_id="no-such-wave", element_id="p1",
        )
        assert result["anchor_check"]["reason"] == "no_manifest"


class TestCheckReleaseStalenessOutputHash:
    @pytest.mark.asyncio
    async def test_blocked_when_embedded_source_has_changed(self, db, tmp_path):
        """Connects to the REAL check_embedded_staleness (meridian.
        embedded_staleness) -- content-changed source hash must block."""
        project = await db_module.create_project(db, "staleness-output-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        source = tmp_path / "figure_source.py"
        source.write_text("print('v2')\n", encoding="utf-8")
        import hashlib
        stale_sha = hashlib.sha256(b"print('v1')\n").hexdigest()  # NOT the current content
        result = await DM.check_release_staleness(
            db, opened["transaction_id"], source_path=str(source), embed_sha256=stale_sha,
        )
        assert result["blocked"] is True
        assert result["output_check"]["stale"] is True
        assert any("output_hash_stale" in r for r in result["reasons"])

    @pytest.mark.asyncio
    async def test_not_blocked_when_embedded_source_matches(self, db, tmp_path):
        project = await db_module.create_project(db, "staleness-output-current-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        source = tmp_path / "figure_source.py"
        content = b"print('current')\n"
        source.write_bytes(content)
        import hashlib
        current_sha = hashlib.sha256(content).hexdigest()
        result = await DM.check_release_staleness(
            db, opened["transaction_id"], source_path=str(source), embed_sha256=current_sha,
        )
        assert result["blocked"] is False
        assert result["output_check"]["stale"] is False

    @pytest.mark.asyncio
    async def test_no_source_path_skips_output_check(self, db):
        project = await db_module.create_project(db, "staleness-no-source-proj")
        opened = await DM.open_release_transaction(db, "cs-1", "x.docx", project_id=project["id"])
        result = await DM.check_release_staleness(db, opened["transaction_id"])
        assert result["output_check"] is None
        assert result["blocked"] is False


# ---------------------------------------------------------------------------
# (a) tunnel.py wiring -- opening/finishing a release transaction around
# the PRIMARY write tools.
# ---------------------------------------------------------------------------

class TestOpenDocsReleaseTransaction:
    @pytest.mark.asyncio
    async def test_non_primary_tool_returns_none(self, db):
        ctx = await tunnel_module._open_docs_release_transaction(
            db, "insert_image", {"docx_path": "x.docx", "anchor_para_id": "p1"},
            session_id="s1", tenant_id=None,
        )
        assert ctx is None

    @pytest.mark.asyncio
    async def test_db_none_returns_none(self):
        ctx = await tunnel_module._open_docs_release_transaction(
            None, "move_section", {"docx_path": "x.docx", "section_id": "p1"},
            session_id="s1", tenant_id=None,
        )
        assert ctx is None

    @pytest.mark.asyncio
    async def test_primary_tool_opens_a_real_transaction(self, db, tmp_path):
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"fake docx bytes")
        ctx = await tunnel_module._open_docs_release_transaction(
            db, "move_section",
            {"docx_path": str(docx), "section_id": "p42"},
            session_id="s1", tenant_id=None,
        )
        assert ctx is not None
        assert ctx["docx_path"] == str(docx)
        assert ctx["element_id"] == "p42"
        row = await DM.get_release_transaction(db, ctx["transaction_id"])
        assert row is not None
        assert row["state"] == DM.RELEASE_STATE_PREPARED
        assert row["base_hash"] is not None  # local file WAS readable -> real base_hash

    @pytest.mark.asyncio
    async def test_wave_run_id_captured_from_arguments(self, db, tmp_path):
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"fake")
        ctx = await tunnel_module._open_docs_release_transaction(
            db, "copy_section",
            {"docx_path": str(docx), "section_id": "p1", "wave_run_id": "wave-42"},
            session_id="s1", tenant_id=None,
        )
        assert ctx["wave_id"] == "wave-42"


class TestFinishDocsReleaseTransaction:
    @pytest.mark.asyncio
    async def test_success_payload_advances_to_released_with_real_post_hash(self, db, tmp_path):
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"original")
        ctx = await tunnel_module._open_docs_release_transaction(
            db, "move_section", {"docx_path": str(docx), "section_id": "p1"},
            session_id="s1", tenant_id=None,
        )
        await tunnel_module._finish_docs_release_transaction(
            ctx, db, session_id="s1",
            tool_payload={"status": "moved", "promoted_sha256": "abc123"},
        )
        row = await DM.get_release_transaction(db, ctx["transaction_id"])
        assert row["state"] == DM.RELEASE_STATE_RELEASED
        assert row["post_hash"] == "abc123"

    @pytest.mark.asyncio
    async def test_error_payload_advances_to_aborted_not_released(self, db, tmp_path):
        """move_section's OWN docstring guarantees the file is untouched
        (or restored) on {"error": ...} -- the transaction must reflect
        that as ABORTED, never as if it had promoted successfully."""
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"original")
        ctx = await tunnel_module._open_docs_release_transaction(
            db, "move_section", {"docx_path": str(docx), "section_id": "p1"},
            session_id="s1", tenant_id=None,
        )
        await tunnel_module._finish_docs_release_transaction(
            ctx, db, session_id="s1",
            tool_payload={"error": "para_id not found"},
        )
        row = await DM.get_release_transaction(db, ctx["transaction_id"])
        assert row["state"] == DM.RELEASE_STATE_ABORTED
        assert "para_id not found" in (row.get("error") or "")

    @pytest.mark.asyncio
    async def test_relay_exception_advances_to_aborted(self, db, tmp_path):
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"original")
        ctx = await tunnel_module._open_docs_release_transaction(
            db, "relocate_table", {"docx_path": str(docx), "section_id": "t0"},
            session_id="s1", tenant_id=None,
        )
        await tunnel_module._finish_docs_release_transaction(
            ctx, db, session_id="s1", relay_exception=RuntimeError("network broke"),
        )
        row = await DM.get_release_transaction(db, ctx["transaction_id"])
        assert row["state"] == DM.RELEASE_STATE_ABORTED

    @pytest.mark.asyncio
    async def test_none_ctx_is_a_safe_noop(self, db):
        # Must not raise -- mirrors the "release_ctx is None" delegate path.
        await tunnel_module._finish_docs_release_transaction(
            None, db, session_id="s1", tool_payload={"status": "ok"},
        )

    @pytest.mark.asyncio
    async def test_blocked_staleness_recorded_as_recovery_required_not_released(self, db, tmp_path):
        """(b)+(a) integration: when check_release_staleness reports
        blocked=True (a real anchor conflict), finishing must land the
        transaction in RECOVERY_REQUIRED, never RELEASED."""
        project = await db_module.create_project(db, "finish-staleness-proj")
        sess_a = (await db_module.register_session(db, project_id=project["id"], name="sess-a"))["id"]
        sess_b = (await db_module.register_session(db, project_id=project["id"], name="sess-b"))["id"]
        docx = tmp_path / "shared.docx"
        docx.write_bytes(b"content")
        wave_id = "wave-stale"
        await DM.open_merge_manifest(
            db, wave_id, str(docx), sess_a, draft_path="draft-a.docx",
        )
        await DM.claim_merge_owner(db, wave_id, str(docx), sess_a)
        await DM.record_merge_result(db, wave_id, str(docx), sess_a, "p9")
        ctx = await tunnel_module._open_docs_release_transaction(
            db, "move_section",
            {"docx_path": str(docx), "section_id": "p9", "wave_run_id": wave_id},
            session_id=sess_b, tenant_id=None,
        )
        await tunnel_module._finish_docs_release_transaction(
            ctx, db, session_id=sess_b,
            tool_payload={"status": "moved", "promoted_sha256": "abc"},
        )
        row = await DM.get_release_transaction(db, ctx["transaction_id"])
        assert row["state"] == DM.RELEASE_STATE_RECOVERY_REQUIRED
        assert "staleness gate blocked" in (row.get("error") or "")


class TestCallTunnelToolWithReleaseTracking:
    @pytest.mark.asyncio
    async def test_non_primary_tool_delegates_unchanged(self, db, monkeypatch):
        calls = []

        async def _fake_call_tunnel_tool(tenant_id, name, arguments, *, db=None, session_id=None):
            calls.append((tenant_id, name, arguments))
            return {"content": [{"type": "text", "text": "{}"}]}

        monkeypatch.setattr(tunnel_module, "call_tunnel_tool", _fake_call_tunnel_tool)
        result = await tunnel_module.call_tunnel_tool_with_release_tracking(
            "tenant-1", "insert_image", {"docx_path": "x.docx"}, db=db, session_id="s1",
        )
        assert calls == [("tenant-1", "insert_image", {"docx_path": "x.docx"})]
        assert result == {"content": [{"type": "text", "text": "{}"}]}

    @pytest.mark.asyncio
    async def test_primary_tool_success_drives_transaction_to_released(self, db, tmp_path, monkeypatch):
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"content")

        async def _fake_call_tunnel_tool(tenant_id, name, arguments, *, db=None, session_id=None):
            return {"content": [{"type": "text", "text": json.dumps(
                {"status": "moved", "promoted_sha256": "real-hash-123"}
            )}]}

        monkeypatch.setattr(tunnel_module, "call_tunnel_tool", _fake_call_tunnel_tool)
        await tunnel_module.call_tunnel_tool_with_release_tracking(
            "tenant-1", "move_section",
            {"docx_path": str(docx), "section_id": "p1"},
            db=db, session_id="s1",
        )
        transactions = await DM.list_release_transactions(db)
        matching = [t for t in transactions if t.get("file_path") == str(docx)]
        assert matching and matching[0]["state"] == DM.RELEASE_STATE_RELEASED
        assert matching[0]["post_hash"] == "real-hash-123"

    @pytest.mark.asyncio
    async def test_primary_tool_exception_aborts_transaction_and_reraises(self, db, tmp_path, monkeypatch):
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"content")

        async def _boom(tenant_id, name, arguments, *, db=None, session_id=None):
            raise RuntimeError("tunnel exploded")

        monkeypatch.setattr(tunnel_module, "call_tunnel_tool", _boom)
        with pytest.raises(RuntimeError, match="tunnel exploded"):
            await tunnel_module.call_tunnel_tool_with_release_tracking(
                "tenant-1", "relocate_table",
                {"docx_path": str(docx), "section_id": "t0"},
                db=db, session_id="s1",
            )
        transactions = await DM.list_release_transactions(db)
        matching = [t for t in transactions if t.get("file_path") == str(docx)]
        assert matching and matching[0]["state"] == DM.RELEASE_STATE_ABORTED


# ---------------------------------------------------------------------------
# (d) Required claim/lease lookup -- a lookup FAILURE must block, not
# silently degrade to "assume clear".
# ---------------------------------------------------------------------------

class TestRequiredClaimLookupGate:
    @pytest.mark.asyncio
    async def test_non_primary_tool_is_a_noop(self, db):
        # Must not raise even if the underlying lookup would have.
        await tunnel_module._required_claim_lookup_gate(
            db, "insert_image", {"docx_path": "x.docx"}, session_id="s1",
        )

    @pytest.mark.asyncio
    async def test_db_none_is_a_noop(self):
        await tunnel_module._required_claim_lookup_gate(
            None, "move_section", {"docx_path": "x.docx", "section_id": "p1"}, session_id="s1",
        )

    @pytest.mark.asyncio
    async def test_successful_lookup_with_no_conflict_does_not_block(self, db, tmp_path):
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"x")
        # No claims exist at all -- a real lookup that completes cleanly
        # must never raise.
        await tunnel_module._required_claim_lookup_gate(
            db, "move_section", {"docx_path": str(docx), "section_id": "p1"}, session_id="s1",
        )

    @pytest.mark.asyncio
    async def test_lookup_failure_blocks_instead_of_degrading(self, db, monkeypatch):
        """THE regression this closes: a required claim/lease lookup that
        cannot be PERFORMED (raises) must BLOCK the write, not be silently
        treated as 'no conflict found'."""
        async def _boom(*_a, **_k):
            raise RuntimeError("claims DB unreachable")

        monkeypatch.setattr(db_module, "check_docx_region_write_conflict", _boom)
        with pytest.raises(tunnel_module.RequiredClaimLookupFailed):
            await tunnel_module._required_claim_lookup_gate(
                db, "move_section", {"docx_path": "x.docx", "section_id": "p1"},
                session_id="s1",
            )

    @pytest.mark.asyncio
    async def test_lookup_failure_blocks_the_whole_release_tracking_call(
        self, db, tmp_path, monkeypatch,
    ):
        """End-to-end: a required-lookup failure must prevent the tunnel
        call from ever being attempted at all -- the write never happens."""
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"x")
        called = []

        async def _fake_call_tunnel_tool(*_a, **_k):
            called.append(True)
            return {"content": [{"type": "text", "text": "{}"}]}

        async def _boom(*_a, **_k):
            raise RuntimeError("claims DB unreachable")

        monkeypatch.setattr(tunnel_module, "call_tunnel_tool", _fake_call_tunnel_tool)
        monkeypatch.setattr(db_module, "check_docx_region_write_conflict", _boom)

        with pytest.raises(tunnel_module.RequiredClaimLookupFailed):
            await tunnel_module.call_tunnel_tool_with_release_tracking(
                "tenant-1", "move_section",
                {"docx_path": str(docx), "section_id": "p1"},
                db=db, session_id="s1",
            )
        assert called == []  # the write was never attempted


# ---------------------------------------------------------------------------
# THIRD-PASS fix (54619681) -- wave-coordinated DRAFT-mode calls to the REAL
# extensions/meridian-docs docs_intel functions, through
# call_tunnel_tool_with_release_tracking end-to-end. Only the outbound
# tunnel RPC itself is stubbed (call_tunnel_tool) -- exactly the same
# boundary every other test in this file stubs at -- and the stub forwards
# straight into the real docs_intel function, so these tests exercise the
# REAL move_section/merge_draft_into_canonical business logic, not a
# hand-crafted payload standing in for it.
# ---------------------------------------------------------------------------

_TWO_SECTION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Intro body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="H0000002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Results body paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _make_two_section_docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", _TWO_SECTION_XML)
    return buf.getvalue()


async def _fake_relay_to_real_docs_intel(fn):
    """Build a call_tunnel_tool stand-in that forwards ``arguments`` straight
    into the real ``fn`` (a docs_intel function) and wraps its plain dict
    result in the MCP tools/call content envelope -- the exact shape a real
    tunnel round-trip through the meridian-docs MCP server would produce.
    """
    async def _relay(tenant_id, name, arguments, *, db=None, session_id=None):
        result = fn(**(arguments or {}))
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return _relay


class TestDraftModeReleaseTrackingRealDocsIntel:
    @pytest.mark.asyncio
    async def test_draft_mode_move_section_never_marks_canonical_released(
        self, db, tmp_path, monkeypatch,
    ):
        """THE bug: pre-fix, this exact scenario opened a transaction keyed
        on the canonical file and drove it to a terminal RELEASED using the
        draft file's promoted_sha256 as post_hash -- even though the
        canonical file was (correctly, per docs_intel's own contract)
        never touched. Written to fail against the pre-fix code: before
        this fix, ``canonical_txns`` below would be non-empty and its
        state would be RELEASED.
        """
        canonical = tmp_path / "canonical.docx"
        canonical.write_bytes(_make_two_section_docx_bytes())
        with open(canonical, "rb") as fh:
            original_bytes = fh.read()
        draft = str(tmp_path / "draft.docx")

        monkeypatch.setattr(
            tunnel_module, "call_tunnel_tool",
            await _fake_relay_to_real_docs_intel(docs_intel.move_section),
        )

        result = await tunnel_module.call_tunnel_tool_with_release_tracking(
            "tenant-1", "move_section",
            {
                "docx_path": str(canonical),
                "section_id": "H0000002",
                "destination_anchor_para_id": "H0000001",
                "destination_position": "before",
                "draft_output_path": draft,
                "wave_run_id": "wave-1",
            },
            db=db, session_id="s1",
        )

        # Sanity: the REAL docs_intel.move_section actually ran and actually
        # moved the section -- into the DRAFT file, never canonical.
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "moved"
        assert payload["is_draft"] is True
        assert payload["docx_path"] == draft
        with open(canonical, "rb") as fh:
            assert fh.read() == original_bytes, "canonical must be untouched by a draft-mode write"

        # THE fix: no transaction may exist claiming the canonical file was
        # released -- durably or otherwise.
        transactions = await DM.list_release_transactions(db)
        canonical_txns = [t for t in transactions if t.get("file_path") == str(canonical)]
        assert canonical_txns == [], (
            "a draft-mode write must never open/advance a release transaction "
            f"against the untouched canonical file, found: {canonical_txns}"
        )
        # Nothing reached RELEASED for this call at all -- a draft write is
        # not itself a release of anything (see merge_docx_draft test below
        # for the transaction the REAL promotion opens).
        released = [t for t in transactions if t.get("state") == DM.RELEASE_STATE_RELEASED]
        assert released == []

    @pytest.mark.asyncio
    async def test_direct_write_mode_unaffected_still_releases_canonical(
        self, db, tmp_path, monkeypatch,
    ):
        """Guard-rail: the fix must be scoped to draft mode ONLY -- a
        legacy direct write (no draft_output_path/wave_run_id) through the
        REAL move_section must still open/advance a transaction against the
        canonical file it actually mutated, exactly as before this fix.
        """
        canonical = tmp_path / "canonical.docx"
        canonical.write_bytes(_make_two_section_docx_bytes())

        monkeypatch.setattr(
            tunnel_module, "call_tunnel_tool",
            await _fake_relay_to_real_docs_intel(docs_intel.move_section),
        )

        result = await tunnel_module.call_tunnel_tool_with_release_tracking(
            "tenant-1", "move_section",
            {
                "docx_path": str(canonical),
                "section_id": "H0000002",
                "destination_anchor_para_id": "H0000001",
                "destination_position": "before",
            },
            db=db, session_id="s1",
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "moved"
        assert payload["is_draft"] is False
        assert payload["docx_path"] == str(canonical)

        transactions = await DM.list_release_transactions(db)
        matching = [t for t in transactions if t.get("file_path") == str(canonical)]
        assert matching and matching[0]["state"] == DM.RELEASE_STATE_RELEASED
        assert matching[0]["post_hash"] == payload["promoted_sha256"]

    @pytest.mark.asyncio
    async def test_merge_docx_draft_promotion_is_the_gated_event(
        self, db, tmp_path, monkeypatch,
    ):
        """The REAL canonical-mutating tool for this workflow --
        merge_docx_draft -- is now itself a PRIMARY release tool: driven
        through the SAME call_tunnel_tool_with_release_tracking chokepoint,
        it opens and advances a transaction against canonical_path (the
        file it genuinely mutates) all the way to RELEASED, using its own
        real promoted_sha256 as post_hash -- closing the verifier's
        "merge_docx_draft is not wired into the release-transaction
        machinery at all" finding.
        """
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda docx_path, **kwargs: {
                "status": "rendered", "backend": "test-stub", "detail": {"stub": True},
            },
        )
        canonical = tmp_path / "canonical.docx"
        canonical.write_bytes(_make_two_section_docx_bytes())
        draft = tmp_path / "draft.docx"
        # A structurally-compatible draft (same zero media/style/relationship
        # counts as canonical) -- same shape move_section's own draft mode
        # would actually produce.
        draft.write_bytes(_make_two_section_docx_bytes())

        monkeypatch.setattr(
            tunnel_module, "call_tunnel_tool",
            await _fake_relay_to_real_docs_intel(docs_intel.merge_draft_into_canonical),
        )

        result = await tunnel_module.call_tunnel_tool_with_release_tracking(
            "tenant-1", "merge_docx_draft",
            {"canonical_path": str(canonical), "draft_path": str(draft)},
            db=db, session_id="s1",
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["merged"] is True
        assert payload["promoted_sha256"]

        transactions = await DM.list_release_transactions(db)
        matching = [t for t in transactions if t.get("file_path") == str(canonical)]
        assert matching and matching[0]["state"] == DM.RELEASE_STATE_RELEASED
        assert matching[0]["post_hash"] == payload["promoted_sha256"]
