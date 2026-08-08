"""Tests for sprint item 7f23cd62 — generalized scoped-discovery and
explicit-degraded fallback contract (meridian.tool_discovery, section 5:
the domain-neutral discovery-scope classifier + audited override).

Distinct from tests/test_86b36617_tool_discovery.py, which covers this same
module's ORIGINAL tool_requirements-availability pieces (compiler,
classify_requirement_state, verify_pre_edit_receipt, run_targeted_tests).
This file covers only the NEW section 5 additions:

1. classify_discovery_scope — pure (scope, evidence) -> {resolution, reason}
   resolver, generalized across the four named discovery_scope kinds
   (tracked, allowlisted_ignored, remote_snapshot, artifact_subtree).
2. is_mutation_safe — the fail-closed gate: only RESOLUTION_READY is safe
   for a write/claim/promotion/completion path.
3. validate_discovery_override / apply_discovery_override — audited,
   EXPIRING override (never an unbounded bypass): requires actor + reason +
   a real, unexpired ISO-8601 expires_at.
4. record_discovery_scope_override — the DB-backed audit-log writer,
   mirroring code_intel_receipt.record_prospect_receipt_override's shape.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from meridian import db as db_module
from meridian import tool_discovery as td


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "discovery-scope-7f23cd62")


# ---------------------------------------------------------------------------
# classify_discovery_scope
# ---------------------------------------------------------------------------

def test_tracked_indexed_fresh_is_ready():
    result = td.classify_discovery_scope("tracked", evidence={"indexed": True})
    assert result["resolution"] == td.RESOLUTION_READY
    assert result["discovery_scope"] == "tracked"


def test_tracked_indexed_stale_is_degraded():
    result = td.classify_discovery_scope(
        "tracked", evidence={"indexed": True, "index_stale": True},
    )
    assert result["resolution"] == td.RESOLUTION_DEGRADED


def test_tracked_never_indexed_is_unavailable():
    result = td.classify_discovery_scope("tracked", evidence={"indexed": False})
    assert result["resolution"] == td.RESOLUTION_UNAVAILABLE


def test_tracked_no_evidence_at_all_is_unavailable():
    """Missing evidence must never be silently treated as 'not applicable' —
    the item's own explicit non-goal. It resolves UNAVAILABLE, not a bare
    pass-through or an exception."""
    result = td.classify_discovery_scope("tracked")
    assert result["resolution"] == td.RESOLUTION_UNAVAILABLE


def test_allowlisted_ignored_is_always_quarantined():
    """Quarantine by POLICY, not absence — distinct from unavailable, and
    unconditional regardless of any evidence passed."""
    result = td.classify_discovery_scope(
        "allowlisted_ignored", evidence={"indexed": True},
    )
    assert result["resolution"] == td.RESOLUTION_QUARANTINED


def test_remote_snapshot_not_fetched_is_unavailable():
    result = td.classify_discovery_scope("remote_snapshot", evidence={})
    assert result["resolution"] == td.RESOLUTION_UNAVAILABLE


def test_remote_snapshot_fetched_stale_is_degraded():
    result = td.classify_discovery_scope(
        "remote_snapshot", evidence={"snapshot_fetched": True, "snapshot_stale": True},
    )
    assert result["resolution"] == td.RESOLUTION_DEGRADED


def test_remote_snapshot_fetched_fresh_is_ready():
    result = td.classify_discovery_scope(
        "remote_snapshot", evidence={"snapshot_fetched": True},
    )
    assert result["resolution"] == td.RESOLUTION_READY


def test_artifact_subtree_outside_is_quarantined():
    result = td.classify_discovery_scope(
        "artifact_subtree", evidence={"within_declared_subtree": False},
    )
    assert result["resolution"] == td.RESOLUTION_QUARANTINED


def test_artifact_subtree_inside_is_ready():
    result = td.classify_discovery_scope(
        "artifact_subtree", evidence={"within_declared_subtree": True},
    )
    assert result["resolution"] == td.RESOLUTION_READY


def test_unrecognized_scope_is_unavailable_not_an_exception():
    """An unknown scope must never crash a caller iterating heterogeneous
    resources, and must never be silently treated as 'not applicable'."""
    result = td.classify_discovery_scope("some_made_up_scope")
    assert result["resolution"] == td.RESOLUTION_UNAVAILABLE
    assert "unrecognized" in result["reason"]


# ---------------------------------------------------------------------------
# is_mutation_safe
# ---------------------------------------------------------------------------

def test_only_ready_is_mutation_safe():
    assert td.is_mutation_safe(td.RESOLUTION_READY) is True
    assert td.is_mutation_safe(td.RESOLUTION_DEGRADED) is False
    assert td.is_mutation_safe(td.RESOLUTION_UNAVAILABLE) is False
    assert td.is_mutation_safe(td.RESOLUTION_QUARANTINED) is False


# ---------------------------------------------------------------------------
# validate_discovery_override — never an unbounded bypass
# ---------------------------------------------------------------------------

def _future_iso(seconds: float = 3600.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: float = 3600.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_override_valid_with_actor_reason_future_expiry():
    result = td.validate_discovery_override(
        actor="adam", reason="confirmed manually via dashboard", expires_at=_future_iso(),
    )
    assert result["valid"] is True
    assert result["errors"] == []


def test_override_rejects_empty_reason():
    result = td.validate_discovery_override(actor="adam", reason="", expires_at=_future_iso())
    assert result["valid"] is False
    assert any("reason" in e for e in result["errors"])


def test_override_rejects_empty_actor():
    result = td.validate_discovery_override(actor="", reason="ok", expires_at=_future_iso())
    assert result["valid"] is False
    assert any("actor" in e for e in result["errors"])


def test_override_rejects_missing_expiry():
    """No expires_at at all — an unbounded override is refused outright."""
    result = td.validate_discovery_override(actor="adam", reason="ok", expires_at=None)
    assert result["valid"] is False
    assert any("expires_at is required" in e for e in result["errors"])


def test_override_rejects_already_lapsed_expiry():
    result = td.validate_discovery_override(actor="adam", reason="ok", expires_at=_past_iso())
    assert result["valid"] is False
    assert any("lapsed" in e for e in result["errors"])


def test_override_rejects_malformed_expiry():
    result = td.validate_discovery_override(actor="adam", reason="ok", expires_at="not-a-timestamp")
    assert result["valid"] is False
    assert any("not a valid ISO-8601" in e for e in result["errors"])


def test_override_accepts_timezone_naive_expiry_coerced_to_utc():
    """A naive ISO timestamp (no offset) must be coerced to UTC before
    comparison, not raise or silently mismatch against an aware 'now'."""
    naive_future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
    result = td.validate_discovery_override(
        actor="adam", reason="ok", expires_at=naive_future.isoformat(),
    )
    assert result["valid"] is True


def test_override_naive_expiry_still_lapses_correctly():
    naive_past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    result = td.validate_discovery_override(
        actor="adam", reason="ok", expires_at=naive_past.isoformat(),
    )
    assert result["valid"] is False
    assert any("lapsed" in e for e in result["errors"])


def test_classify_discovery_scope_explicit_none_evidence_same_as_omitted():
    assert td.classify_discovery_scope("tracked", evidence=None) == td.classify_discovery_scope("tracked")


def test_override_injectable_now_for_deterministic_boundary_check():
    """A fixed 'now' just past a fixed expiry must lapse deterministically —
    no reliance on real wall-clock timing in the assertion."""
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expiry = datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    result = td.validate_discovery_override(
        actor="adam", reason="ok", expires_at=expiry.isoformat(), now=fixed_now,
    )
    assert result["valid"] is False


# ---------------------------------------------------------------------------
# apply_discovery_override
# ---------------------------------------------------------------------------

def test_apply_override_upgrades_quarantined_to_ready():
    classification = td.classify_discovery_scope("allowlisted_ignored")
    assert classification["resolution"] == td.RESOLUTION_QUARANTINED

    overridden = td.apply_discovery_override(
        classification, actor="adam", reason="reviewed and approved", expires_at=_future_iso(),
    )
    assert overridden["resolution"] == td.RESOLUTION_READY
    assert overridden["override"]["actor"] == "adam"
    assert overridden["override"]["prior_resolution"] == td.RESOLUTION_QUARANTINED
    # Original input must never be mutated in place.
    assert classification["resolution"] == td.RESOLUTION_QUARANTINED


def test_apply_override_refuses_on_invalid_override_and_preserves_original():
    classification = td.classify_discovery_scope("allowlisted_ignored")
    rejected = td.apply_discovery_override(
        classification, actor="adam", reason="", expires_at=_future_iso(),
    )
    # Resolution is UNCHANGED — a rejected override must never silently swap state.
    assert rejected["resolution"] == td.RESOLUTION_QUARANTINED
    assert "override_rejected" in rejected
    assert any("reason" in e for e in rejected["override_rejected"])


def test_apply_override_refuses_lapsed_expiry():
    classification = td.classify_discovery_scope("remote_snapshot", evidence={})
    rejected = td.apply_discovery_override(
        classification, actor="adam", reason="ok", expires_at=_past_iso(),
    )
    assert rejected["resolution"] == td.RESOLUTION_UNAVAILABLE
    assert "override_rejected" in rejected


# ---------------------------------------------------------------------------
# record_discovery_scope_override — DB-backed audit trail
# ---------------------------------------------------------------------------

async def test_record_override_writes_audit_event(db, project):
    event = await td.record_discovery_scope_override(
        db, project["id"], actor="adam", reason="reviewed and approved",
        discovery_scope="allowlisted_ignored", expires_at=_future_iso(),
    )
    assert event["event_type"] == td.DISCOVERY_SCOPE_OVERRIDE_EVENT_TYPE
    assert event["project_id"] == project["id"]
    assert event["actor"] == "adam"


async def test_record_override_refuses_empty_reason(db, project):
    with pytest.raises(ValueError, match="reason is required"):
        await td.record_discovery_scope_override(
            db, project["id"], actor="adam", reason="",
            discovery_scope="allowlisted_ignored", expires_at=_future_iso(),
        )
