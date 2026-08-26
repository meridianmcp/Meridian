"""Tests for 1bd5e810 — receiver-side manifest acceptance and board-divergence
diagnostics: meridian.handoff.accept_handoff_envelope /
compute_required_tools_hash.

Scope: accept_handoff_envelope composes token verification, capability/tool
availability, tool-manifest drift, and board-revision divergence into ONE
structured verdict (one of 'ok'/STALE_HANDOFF/BOARD_DIVERGENCE/
TOOL_MANIFEST_DRIFT/BODY_HASH_MISMATCH/CAPABILITY_UNAVAILABLE), reusing
existing primitives (verify_handoff_token, acf6f51a's compute_board_revision)
rather than reinventing board-staleness detection. Every input is optional
and independently gated; checks run in a fixed precedence order and
short-circuit on the first failure.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


def _items(*rows):
    return [
        {"id": r[0], "status": r[1], "depends_on": r[2] if len(r) > 2 else None}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# compute_required_tools_hash
# ---------------------------------------------------------------------------


def test_compute_required_tools_hash_deterministic_and_order_independent():
    a = [
        {"tool_requirements": [{"name": "meridian"}, {"name": "Serena"}]},
        {"tool_requirements": [{"name": "pytest"}]},
    ]
    b = [
        {"tool_requirements": [{"name": "pytest"}]},
        {"tool_requirements": [{"name": "Serena"}, {"name": "meridian"}]},
    ]
    assert handoff_module.compute_required_tools_hash(a) == handoff_module.compute_required_tools_hash(b)


def test_compute_required_tools_hash_changes_when_tool_set_changes():
    a = [{"tool_requirements": [{"name": "meridian"}]}]
    b = [{"tool_requirements": [{"name": "meridian"}, {"name": "Serena"}]}]
    assert handoff_module.compute_required_tools_hash(a) != handoff_module.compute_required_tools_hash(b)


def test_compute_required_tools_hash_handles_json_string_and_missing_field():
    as_json_string = [{"tool_requirements": '[{"name": "meridian"}]'}]
    as_list = [{"tool_requirements": [{"name": "meridian"}]}]
    assert (
        handoff_module.compute_required_tools_hash(as_json_string)
        == handoff_module.compute_required_tools_hash(as_list)
    )
    # Missing/malformed tool_requirements degrades to "no requirements" rather than raising.
    assert handoff_module.compute_required_tools_hash([{"tool_requirements": None}]) == (
        handoff_module.compute_required_tools_hash([{}])
    )
    handoff_module.compute_required_tools_hash([{"tool_requirements": "not json"}])  # must not raise


# ---------------------------------------------------------------------------
# accept_handoff_envelope — no inputs at all is a no-op accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_with_no_inputs_is_accepted_noop(db):
    p = await db_module.create_project(db, "accept-noop")
    result = await handoff_module.accept_handoff_envelope(db, p["id"])
    assert result == {
        "accepted": True,
        "result": handoff_module.ACCEPT_RESULT_OK,
        "reasons": [],
        "token_check": None,
        # 22f2604d — identity_check is None (never even attempted) when no
        # presented_body is supplied at all.
        "identity_check": None,
        "capability_check": None,
        "tool_manifest_check": None,
        "board_check": None,
        "is_trusted_channel": False,
        "delivery_source": "chat_paste",
    }


# ---------------------------------------------------------------------------
# Token check — STALE_HANDOFF vs BODY_HASH_MISMATCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_rejects_unknown_token_as_stale_handoff(db):
    p = await db_module.create_project(db, "accept-token-not-found")
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token="never-minted-token",
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_STALE_HANDOFF
    assert result["token_check"]["reason"] == "not_found"
    # Short-circuits before any later check runs.
    assert result["capability_check"] is None
    assert result["tool_manifest_check"] is None
    assert result["board_check"] is None


@pytest.mark.asyncio
async def test_accept_rejects_wrong_project_token_as_stale_handoff(db):
    p1 = await db_module.create_project(db, "accept-token-proj-a")
    p2 = await db_module.create_project(db, "accept-token-proj-b")
    token = await handoff_module.mint_handoff_token(db, p1["id"])
    result = await handoff_module.accept_handoff_envelope(
        db, p2["id"], goal_token=token,
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_STALE_HANDOFF
    assert result["token_check"]["reason"] == "wrong_project"


@pytest.mark.asyncio
async def test_accept_flags_tampered_body_as_body_hash_mismatch(db):
    p = await db_module.create_project(db, "accept-body-mismatch")
    token = await handoff_module.mint_handoff_token(db, p["id"], body="original text")
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token=token, presented_body="tampered text",
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_BODY_HASH_MISMATCH
    assert result["token_check"]["reason"] == "body_mismatch"


@pytest.mark.asyncio
async def test_accept_passes_token_check_with_matching_body(db):
    p = await db_module.create_project(db, "accept-body-match")
    token = await handoff_module.mint_handoff_token(db, p["id"], body="the real body")
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], goal_token=token, presented_body="the real body",
    )
    assert result["accepted"] is True
    assert result["result"] == handoff_module.ACCEPT_RESULT_OK
    assert result["token_check"]["valid"] is True


# ---------------------------------------------------------------------------
# Capability check — CAPABILITY_UNAVAILABLE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_flags_missing_required_tool_as_capability_unavailable(db):
    p = await db_module.create_project(db, "accept-capability")
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"],
        required_tools=["meridian", "Serena"],
        available_tools=["meridian"],
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_CAPABILITY_UNAVAILABLE
    assert result["capability_check"]["missing_tools"] == ["Serena"]


@pytest.mark.asyncio
async def test_accept_passes_capability_check_when_all_tools_available(db):
    p = await db_module.create_project(db, "accept-capability-ok")
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"],
        required_tools=["meridian", "Serena"],
        available_tools=["meridian", "Serena", "pytest"],
    )
    assert result["accepted"] is True
    assert result["capability_check"]["missing_tools"] == []


@pytest.mark.asyncio
async def test_accept_skips_capability_check_when_available_tools_omitted(db):
    p = await db_module.create_project(db, "accept-capability-skip")
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], required_tools=["meridian"],
    )
    assert result["accepted"] is True
    assert result["capability_check"] is None


# ---------------------------------------------------------------------------
# Tool-manifest drift — TOOL_MANIFEST_DRIFT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_flags_tool_manifest_drift(db):
    p = await db_module.create_project(db, "accept-tool-drift")
    original_items = [{"tool_requirements": [{"name": "meridian"}]}]
    expected_hash = handoff_module.compute_required_tools_hash(original_items)

    drifted_items = [{"tool_requirements": [{"name": "meridian"}, {"name": "Serena"}]}]
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"],
        live_items=drifted_items,
        expected_required_tools_hash=expected_hash,
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_TOOL_MANIFEST_DRIFT
    assert result["tool_manifest_check"]["matches"] is False


@pytest.mark.asyncio
async def test_accept_passes_tool_manifest_check_when_unchanged(db):
    p = await db_module.create_project(db, "accept-tool-nodrift")
    items = [{"tool_requirements": [{"name": "meridian"}]}]
    expected_hash = handoff_module.compute_required_tools_hash(items)
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=items, expected_required_tools_hash=expected_hash,
    )
    assert result["accepted"] is True
    assert result["tool_manifest_check"]["matches"] is True


# ---------------------------------------------------------------------------
# Board revision — BOARD_DIVERGENCE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_flags_board_divergence_on_status_change(db):
    p = await db_module.create_project(db, "accept-board-divergence")
    original = _items(("i1", "todo"), ("i2", "todo"))
    expected_revision = handoff_module.compute_board_revision(original)

    live = _items(("i1", "in_progress"), ("i2", "todo"))
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=live, expected_board_revision=expected_revision,
    )
    assert result["accepted"] is False
    assert result["result"] == handoff_module.ACCEPT_RESULT_BOARD_DIVERGENCE
    assert result["board_check"]["matches"] is False
    assert result["board_check"]["expected_board_revision"] == expected_revision


@pytest.mark.asyncio
async def test_accept_passes_board_check_when_unchanged(db):
    p = await db_module.create_project(db, "accept-board-nodivergence")
    items = _items(("i1", "todo"), ("i2", "todo", "i1"))
    expected_revision = handoff_module.compute_board_revision(items)
    result = await handoff_module.accept_handoff_envelope(
        db, p["id"], live_items=items, expected_board_revision=expected_revision,
    )
    assert result["accepted"] is True
    assert result["board_check"]["matches"] is True


# ---------------------------------------------------------------------------
# Precedence ordering — earlier checks short-circuit later ones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capability_check_short_circuits_before_board_check(db):
    p = await db_module.create_project(db, "accept-precedence")
    items = _items(("i1", "todo"))
    expected_revision = handoff_module.compute_board_revision(items)
    diverged_live = _items(("i1", "done"))  # would fail the board check if reached

    result = await handoff_module.accept_handoff_envelope(
        db, p["id"],
        required_tools=["meridian"],
        available_tools=[],  # missing -> CAPABILITY_UNAVAILABLE fires first
        live_items=diverged_live,
        expected_board_revision=expected_revision,
    )
    assert result["result"] == handoff_module.ACCEPT_RESULT_CAPABILITY_UNAVAILABLE
    assert result["board_check"] is None  # never reached


@pytest.mark.asyncio
async def test_all_checks_pass_together_yields_ok(db):
    p = await db_module.create_project(db, "accept-all-pass")
    token = await handoff_module.mint_handoff_token(db, p["id"], body="body-x")
    items = [{"id": "i1", "status": "todo", "depends_on": None, "tool_requirements": [{"name": "meridian"}]}]
    expected_board_revision = handoff_module.compute_board_revision(items)
    expected_tools_hash = handoff_module.compute_required_tools_hash(items)

    result = await handoff_module.accept_handoff_envelope(
        db, p["id"],
        goal_token=token,
        presented_body="body-x",
        live_items=items,
        expected_board_revision=expected_board_revision,
        expected_required_tools_hash=expected_tools_hash,
        required_tools=["meridian"],
        available_tools=["meridian", "Serena"],
    )
    assert result["accepted"] is True
    assert result["result"] == handoff_module.ACCEPT_RESULT_OK
    assert result["token_check"]["valid"] is True
    assert result["capability_check"]["missing_tools"] == []
    assert result["tool_manifest_check"]["matches"] is True
    assert result["board_check"]["matches"] is True
