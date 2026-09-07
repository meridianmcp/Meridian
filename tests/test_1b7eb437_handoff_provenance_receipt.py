"""Tests for sprint item 1b7eb437 (follow-up to 833649f1): a
capability-manifest-gated handoff/goal-token provenance receipt gate at
claim_sprint_item time, mirroring meridian/code_intel_receipt.py.

Covers three layers:
  1. meridian.handoff_receipt — the pure/DB module (record/find/verify).
  2. meridian/mcp/handler.py's _handle_task_tools wiring — verify_handoff_token
     and accept_handoff now write a best-effort receipt on success, WITHOUT
     changing either tool's returned dict shape.
  3. meridian/mcp/handlers/sprint_tools.py's handle_claim_sprint_item wiring —
     surfaces handoff_provenance_warning/handoff_provenance_receipt on the
     claimed item, opt-in via the project's capability manifest, WARN-ONLY
     (never blocks a claim) in this pass.
"""
from __future__ import annotations

import json

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import handoff_receipt as hr
from meridian.mcp import handler as mh
from meridian.mcp.handlers import sprint_tools as st_mod


_DATA_DIR = "/tmp/meridian-test"


def _cap(**overrides):
    base = {
        "id": hr.HANDOFF_PROVENANCE_CAPABILITY_ID,
        "purpose": "verify a handoff/goal-token was independently checked before claiming",
        "required_tools": ["verify_handoff_token"],
        "fallback_chain": [],
        "availability_policy": "required",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# record_handoff_provenance_receipt
# ---------------------------------------------------------------------------

class TestRecordHandoffProvenanceReceipt:
    @pytest.mark.asyncio
    async def test_writes_exactly_one_audit_row(self, db):
        project = await db_module.create_project(db, "hp-record-proj")
        row = await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="verify_handoff_token", outcome="valid",
        )
        assert row is not None
        rows = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=hr.RECEIPT_EVENT_TYPE,
        )
        assert len(rows) == 1
        assert rows[0]["actor"] == "sess-1"
        assert rows[0]["project_id"] == project["id"]
        detail = json.loads(rows[0]["detail"])
        assert detail["tool"] == "verify_handoff_token"
        assert detail["outcome"] == "valid"

    @pytest.mark.asyncio
    async def test_no_project_id_writes_nothing(self, db):
        row = await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=None, session_id="sess-1",
            tool_name="verify_handoff_token", outcome="valid",
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_write_failure_is_swallowed_not_raised(self, db, monkeypatch):
        project = await db_module.create_project(db, "hp-record-fail-proj")

        async def _boom(*a, **k):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(hr.db_module, "record_action_audit_event", _boom)
        row = await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="verify_handoff_token", outcome="valid",
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_missing_session_id_still_writes_a_receipt(self, db):
        """A caller that never passes session_id still gets a receipt written
        — it just can't be attributed to a specific session later (see
        find_recent_handoff_provenance_receipt's fallback)."""
        project = await db_module.create_project(db, "hp-record-no-session-proj")
        row = await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id=None,
            tool_name="accept_handoff", outcome="accepted",
        )
        assert row is not None
        assert row.get("actor") is None


# ---------------------------------------------------------------------------
# find_recent_handoff_provenance_receipt
# ---------------------------------------------------------------------------

class TestFindRecentHandoffProvenanceReceipt:
    @pytest.mark.asyncio
    async def test_no_receipts_returns_none(self, db):
        project = await db_module.create_project(db, "hp-find-empty-proj")
        found = await hr.find_recent_handoff_provenance_receipt(
            db, project_id=project["id"], session_id="sess-x",
        )
        assert found is None

    @pytest.mark.asyncio
    async def test_prefers_session_attributed_match_over_newer_other_session(self, db):
        project = await db_module.create_project(db, "hp-find-attrib-proj")
        await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-mine",
            tool_name="verify_handoff_token", outcome="valid",
        )
        # A NEWER receipt from a different (sibling) session must not shadow
        # the caller's own attributed receipt.
        await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-sibling",
            tool_name="verify_handoff_token", outcome="valid",
        )
        found = await hr.find_recent_handoff_provenance_receipt(
            db, project_id=project["id"], session_id="sess-mine",
        )
        assert found is not None
        assert found["actor"] == "sess-mine"

    @pytest.mark.asyncio
    async def test_no_session_id_falls_back_to_newest_receipt(self, db):
        project = await db_module.create_project(db, "hp-find-fallback-proj")
        await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-other",
            tool_name="verify_handoff_token", outcome="valid",
        )
        found = await hr.find_recent_handoff_provenance_receipt(
            db, project_id=project["id"], session_id=None,
        )
        assert found is not None

    @pytest.mark.asyncio
    async def test_session_id_with_no_matching_actor_falls_back_to_newest(self, db):
        """No receipt attributed to THIS session, but one exists for the
        project — this module never blocks, so the weaker fallback signal is
        accepted rather than treated as a hard mismatch (see module
        docstring)."""
        project = await db_module.create_project(db, "hp-find-no-match-proj")
        await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-other",
            tool_name="verify_handoff_token", outcome="valid",
        )
        found = await hr.find_recent_handoff_provenance_receipt(
            db, project_id=project["id"], session_id="sess-mine-never-recorded",
        )
        assert found is not None
        assert found["actor"] == "sess-other"


# ---------------------------------------------------------------------------
# verify_handoff_provenance — the claim-time gate
# ---------------------------------------------------------------------------

class TestVerifyHandoffProvenance:
    @pytest.mark.asyncio
    async def test_unconfigured_project_is_not_applicable_zero_behavior_change(self, db):
        project = await db_module.create_project(db, "hp-verify-unconfigured-proj")
        result = await hr.verify_handoff_provenance(db, None, project["id"])
        assert result == {
            "applicable": False, "ok": True, "code": None, "message": None,
            "capability": None, "receipt": None, "degraded": False, "warning": None,
        }

    @pytest.mark.asyncio
    async def test_unconfigured_project_never_touches_live_inventory(self, db, monkeypatch):
        """Module docstring point 3 — zero extra I/O for a project that never
        opted in: check_capability_availability (and its live-inventory
        build) must never even be imported/called."""
        project = await db_module.create_project(db, "hp-verify-no-io-proj")

        async def _boom(*a, **k):
            raise AssertionError("check_capability_availability must not be called")

        import meridian.mcp.handlers.project_tools as pt_mod
        monkeypatch.setattr(pt_mod, "check_capability_availability", _boom)
        result = await hr.verify_handoff_provenance(db, None, project["id"])
        assert result["applicable"] is False

    @pytest.mark.asyncio
    async def test_declared_capability_no_receipt_warns_never_blocks(self, db):
        project = await db_module.create_project(db, "hp-verify-warn-proj")
        await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
        live_inventory = {
            "tunnel_reachable": False,
            "builtin_tools": {"verify_handoff_token", "accept_handoff"},
            "plugins": {}, "stdio_registry": {},
        }
        result = await hr.verify_handoff_provenance(
            db, None, project["id"], session_id="sess-1", live_inventory=live_inventory,
        )
        assert result["applicable"] is True
        assert result["ok"] is True
        assert result["degraded"] is True
        assert result["warning"]
        assert result["receipt"] is None

    @pytest.mark.asyncio
    async def test_required_policy_still_never_blocks(self, db):
        """Explicit regression guard for the item's deliberate scope
        reduction: even availability_policy='required' only ever warns in
        this pass."""
        project = await db_module.create_project(db, "hp-verify-required-warn-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap(availability_policy="required")],
        )
        live_inventory = {
            "tunnel_reachable": False,
            "builtin_tools": {"verify_handoff_token"},
            "plugins": {}, "stdio_registry": {},
        }
        result = await hr.verify_handoff_provenance(
            db, None, project["id"], live_inventory=live_inventory,
        )
        assert result["ok"] is True
        assert "code" not in result or result["code"] is None

    @pytest.mark.asyncio
    async def test_declared_capability_with_matching_receipt_surfaces_it(self, db):
        project = await db_module.create_project(db, "hp-verify-receipt-proj")
        await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
        await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="verify_handoff_token", outcome="valid",
        )
        live_inventory = {
            "tunnel_reachable": False,
            "builtin_tools": {"verify_handoff_token"},
            "plugins": {}, "stdio_registry": {},
        }
        result = await hr.verify_handoff_provenance(
            db, None, project["id"], session_id="sess-1", live_inventory=live_inventory,
        )
        assert result["applicable"] is True
        assert result["ok"] is True
        assert result["degraded"] is False
        assert result["receipt"] is not None
        assert result["receipt"]["actor"] == "sess-1"

    @pytest.mark.asyncio
    async def test_no_project_id_is_not_applicable(self, db):
        result = await hr.verify_handoff_provenance(db, None, "")
        assert result["applicable"] is False

    @pytest.mark.asyncio
    async def test_unexpected_error_fails_open_to_not_applicable(self, db, monkeypatch):
        project = await db_module.create_project(db, "hp-verify-error-proj")
        await db_module.set_project_capability_manifest(db, project["id"], [_cap()])

        async def _boom(*a, **k):
            raise RuntimeError("infra trouble")

        monkeypatch.setattr(db_module, "get_project_capability_manifest", _boom)
        result = await hr.verify_handoff_provenance(db, None, project["id"])
        assert result["applicable"] is False
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Handler-layer wiring: verify_handoff_token / accept_handoff write receipts
# on success without changing their returned dict shape.
# ---------------------------------------------------------------------------

class TestHandlerWritesReceiptOnSuccess:
    @pytest.mark.asyncio
    async def test_valid_token_writes_receipt_and_return_shape_is_unchanged(self, db):
        project = await db_module.create_project(db, "hp-handler-valid-proj")
        token = await handoff_module.mint_handoff_token(db, project["id"])

        direct = await handoff_module.verify_handoff_token(db, token, project["id"])
        # Re-mint since the direct call above already consumed the token —
        # we want to compare SHAPE (key set), not replay the same token.
        token2 = await handoff_module.mint_handoff_token(db, project["id"])
        result = await mh._handle_task_tools(
            "verify_handoff_token",
            {"project_id": project["id"], "token": token2, "session_id": "claiming-sess"},
            db, _DATA_DIR, None, None,
        )
        assert set(result.keys()) == set(direct.keys()) == {"valid", "reason"}
        assert result == {"valid": True, "reason": "ok"}

        rows = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=hr.RECEIPT_EVENT_TYPE,
        )
        assert len(rows) == 1
        assert rows[0]["actor"] == "claiming-sess"
        detail = json.loads(rows[0]["detail"])
        assert detail["tool"] == "verify_handoff_token"

    @pytest.mark.asyncio
    async def test_invalid_token_writes_no_receipt(self, db):
        project = await db_module.create_project(db, "hp-handler-invalid-proj")
        result = await mh._handle_task_tools(
            "verify_handoff_token",
            {"project_id": project["id"], "token": "not-a-real-token", "session_id": "sess-x"},
            db, _DATA_DIR, None, None,
        )
        assert result["valid"] is False
        rows = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=hr.RECEIPT_EVENT_TYPE,
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_verify_handoff_token_without_session_id_is_unaffected(self, db):
        """Omitting the new optional session_id argument entirely must not
        change verify_handoff_token's behavior at all (still writes a
        receipt, just unattributed)."""
        project = await db_module.create_project(db, "hp-handler-no-session-proj")
        token = await handoff_module.mint_handoff_token(db, project["id"])
        result = await mh._handle_task_tools(
            "verify_handoff_token",
            {"project_id": project["id"], "token": token},
            db, _DATA_DIR, None, None,
        )
        assert result == {"valid": True, "reason": "ok"}
        rows = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=hr.RECEIPT_EVENT_TYPE,
        )
        assert len(rows) == 1
        assert rows[0]["actor"] is None

    @pytest.mark.asyncio
    async def test_accept_handoff_accepted_writes_receipt(self, db):
        project = await db_module.create_project(db, "hp-handler-accept-proj")
        token = await handoff_module.mint_handoff_token(db, project["id"])
        result = await mh._handle_task_tools(
            "accept_handoff",
            {"project_id": project["id"], "goal_token": token, "session_id": "accept-sess"},
            db, _DATA_DIR, None, None,
        )
        assert result["accepted"] is True
        rows = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=hr.RECEIPT_EVENT_TYPE,
        )
        assert len(rows) == 1
        assert rows[0]["actor"] == "accept-sess"
        detail = json.loads(rows[0]["detail"])
        assert detail["tool"] == "accept_handoff"

    @pytest.mark.asyncio
    async def test_accept_handoff_rejected_writes_no_receipt(self, db):
        project = await db_module.create_project(db, "hp-handler-reject-proj")
        result = await mh._handle_task_tools(
            "accept_handoff",
            {"project_id": project["id"], "goal_token": "bogus-token", "session_id": "sess-x"},
            db, _DATA_DIR, None, None,
        )
        assert result["accepted"] is False
        rows = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=hr.RECEIPT_EVENT_TYPE,
        )
        assert rows == []


# ---------------------------------------------------------------------------
# handle_claim_sprint_item wiring — opt-in, warn-only, never blocks.
# ---------------------------------------------------------------------------

async def _claim_setup(db, title="Claim provenance test item"):
    project = await db_module.create_project(db, f"hp-claim-{title}"[:60])
    session = await db_module.register_session(db, project["id"], "claim-sess")
    item = await db_module.add_sprint_item(db, project["id"], "v1", title)
    return project, session, item


class TestClaimSprintItemHandoffProvenanceWiring:
    @pytest.mark.asyncio
    async def test_no_capability_declared_adds_no_new_keys(self, db):
        """Regression guard mirroring test_ba4f879b's own contract: a
        project that never opted in sees the exact same claim response shape
        as before this item existed."""
        project, session, item = await _claim_setup(db, "no-cap")
        result = await st_mod.handle_claim_sprint_item(
            {"project_id": project["id"], "item_id": item["id"], "session_id": session["id"]},
            db, _DATA_DIR, None, None,
        )
        assert result.get("id") == item["id"]
        assert "handoff_provenance_warning" not in result
        assert "handoff_provenance_receipt" not in result

    @pytest.mark.asyncio
    async def test_capability_declared_no_receipt_warns_but_still_claims(self, db):
        project, session, item = await _claim_setup(db, "warn")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap(availability_policy="required")],
        )
        result = await st_mod.handle_claim_sprint_item(
            {"project_id": project["id"], "item_id": item["id"], "session_id": session["id"]},
            db, _DATA_DIR, None, None,
        )
        # The claim itself must succeed — this gate is WARN-ONLY, even under
        # availability_policy="required".
        assert result.get("id") == item["id"]
        assert result.get("handoff_provenance_warning")
        assert "handoff_provenance_receipt" not in result

    @pytest.mark.asyncio
    async def test_capability_declared_with_matching_receipt_surfaces_receipt(self, db):
        project, session, item = await _claim_setup(db, "receipt")
        await db_module.set_project_capability_manifest(db, project["id"], [_cap()])
        await hr.record_handoff_provenance_receipt(
            db, tenant_id=None, project_id=project["id"], session_id=session["id"],
            tool_name="verify_handoff_token", outcome="valid",
        )
        result = await st_mod.handle_claim_sprint_item(
            {"project_id": project["id"], "item_id": item["id"], "session_id": session["id"]},
            db, _DATA_DIR, None, None,
        )
        assert result.get("id") == item["id"]
        assert "handoff_provenance_warning" not in result
        assert result.get("handoff_provenance_receipt") is not None
        assert result["handoff_provenance_receipt"]["actor"] == session["id"]

    @pytest.mark.asyncio
    async def test_non_compliant_client_never_forced_still_claims_with_warning(self, db):
        """The documented hard limit, exercised directly: capability
        declared as availability_policy='required', but no goal_token /
        verify_handoff_token call was EVER made for this project — a
        non-compliant (or perfectly legitimate trusted-channel) client. The
        claim must still succeed, with only an informational warning, never
        a hard block — this is the test that keeps the WARN-ONLY contract
        honest against ever silently regressing into a hard block."""
        project, session, item = await _claim_setup(db, "honest")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap(availability_policy="required")],
        )
        result = await st_mod.handle_claim_sprint_item(
            {"project_id": project["id"], "item_id": item["id"], "session_id": session["id"]},
            db, _DATA_DIR, None, None,
        )
        assert result.get("id") == item["id"]
        assert result.get("status") != "already_claimed"
        assert "error" not in result
        assert result.get("handoff_provenance_warning")

    @pytest.mark.asyncio
    async def test_claim_gate_never_raises_even_if_verify_helper_blows_up(self, db, monkeypatch):
        project, session, item = await _claim_setup(db, "safety")
        await db_module.set_project_capability_manifest(db, project["id"], [_cap()])

        async def _boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(hr, "verify_handoff_provenance", _boom)
        result = await st_mod.handle_claim_sprint_item(
            {"project_id": project["id"], "item_id": item["id"], "session_id": session["id"]},
            db, _DATA_DIR, None, None,
        )
        assert result.get("id") == item["id"]


# ---------------------------------------------------------------------------
# Capability-manifest round trip for the new capability id.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capability_manifest_round_trip_for_new_capability_id(db):
    project = await db_module.create_project(db, "hp-manifest-roundtrip-proj")
    await db_module.set_project_capability_manifest(
        db, project["id"],
        [_cap(availability_policy="optional")],
    )
    manifest = await db_module.get_project_capability_manifest(db, project["id"])
    ids = [c["id"] for c in manifest["capabilities"]]
    assert hr.HANDOFF_PROVENANCE_CAPABILITY_ID in ids
    entry = next(c for c in manifest["capabilities"] if c["id"] == hr.HANDOFF_PROVENANCE_CAPABILITY_ID)
    assert entry["availability_policy"] == "optional"
    assert entry["required_tools"] == ["verify_handoff_token"]
