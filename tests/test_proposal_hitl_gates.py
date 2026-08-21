"""Tests for sprint item c6d13571 — typed proposal HITL gates and decision
receipts.

Covers :mod:`meridian.proposal_gates` (the durable, lane-blocking gate
primitive: closed category/state/reopen_policy enums, the affected-
items/pointers shape reusing :func:`meridian.pointers.validate_pointer`,
the one-shot-decision-receipt state machine, and expiry-aware effective
state), its wiring into ``meridian.db`` (migration + CRUD re-exports),
``meridian.db.sprint_items.get_sprint_item_blocking_gates``, the four new
MCP tool handlers in ``meridian.mcp.handlers.notes_decisions``, and
``meridian.handoff.build_proposal_gate_readiness_for_handoff``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — must be imported before handler/db to avoid cycle
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import proposal_gates as gates_module
from meridian.mcp import handler as mh
from meridian.mcp.handlers import notes_decisions as nd_mod
from meridian.pointers import PointerValidationError


_DATA_DIR = "/tmp/meridian-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "test-proj-proposal-gates")


# ---------------------------------------------------------------------------
# Migration — table + indexes, idempotent; not inline in either base literal
# (the 2026-07-04 outage rule: never inline a CREATE INDEX for a migration-
# added table in the unguarded base schema literals).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposal_gates_migration_creates_table_and_indexes_idempotently():
    import aiosqlite

    conn = await aiosqlite.connect(":memory:")
    try:
        conn.row_factory = aiosqlite.Row
        await gates_module._migrate_proposal_gates(conn)
        # Re-run must be a no-op (idempotent) and not raise.
        await gates_module._migrate_proposal_gates(conn)

        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='proposal_gates'"
        ) as cur:
            assert await cur.fetchone() is not None
        for index_name in (
            "idx_proposal_gates_project",
            "idx_proposal_gates_project_state",
            "idx_proposal_gates_project_category",
        ):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ) as cur:
                assert await cur.fetchone() is not None, index_name
        async with conn.execute("PRAGMA table_info(proposal_gates)") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        assert cols == {
            "id", "project_id", "category", "state", "question", "affected",
            "evidence", "decision", "actor", "decided_at",
            "previous_decision", "previous_actor", "previous_decided_at",
            "created_by", "created_at", "updated_at", "expires_at",
            "reopen_policy", "reopen_count", "reopened_at", "reopen_reason",
            "reopened_by",
        }
    finally:
        await conn.close()


def test_proposal_gates_not_inline_in_base_literals():
    from meridian.pg_adapter import CREATE_TABLES_CORE
    from meridian.db import CREATE_TABLES

    for name, literal in (
        ("CREATE_TABLES_CORE", CREATE_TABLES_CORE),
        ("CREATE_TABLES", CREATE_TABLES),
    ):
        assert "proposal_gates" not in literal, name


def test_proposal_gates_pg_migration_registered():
    from meridian.pg_adapter import _PG_MIGRATIONS_LATE, _migrate_pg_proposal_gates

    assert _migrate_pg_proposal_gates in _PG_MIGRATIONS_LATE


@pytest.mark.asyncio
async def test_link_proposal_gate_works_through_full_init_db(db, project):
    """Sanity check the migration is actually wired into init_db's startup
    chain (not just directly callable)."""
    gate = await db_module.create_proposal_gate(
        db, project["id"], "destructive_ops", "Can we drop the table?",
        ["item-1"], "found via schema audit",
    )
    assert gate["state"] == "blocked"
    assert gate["category"] == "destructive_ops"


# ---------------------------------------------------------------------------
# Validation — pure, no DB
# ---------------------------------------------------------------------------

def test_gate_categories_cover_acceptance_criteria():
    """The six categories the sprint item's acceptance criteria calls out,
    verbatim: legal/IP, product scope, destructive operations, production
    deployment, human acceptance of contradictions, other materially
    ambiguous decisions."""
    assert gates_module.GATE_CATEGORIES == (
        "legal_ip",
        "product_scope",
        "destructive_ops",
        "production_deploy",
        "contradiction_acceptance",
        "other_ambiguous",
    )
    for cat in gates_module.GATE_CATEGORIES:
        assert cat in gates_module.GATE_CATEGORY_LABELS


def test_gate_states_are_blocked_quarantined_allowed():
    assert gates_module.GATE_STATES == ("blocked", "quarantined", "allowed")
    assert gates_module.DEFAULT_GATE_STATE == "blocked"


@pytest.mark.parametrize("category", gates_module.GATE_CATEGORIES)
def test_validate_category_accepts_every_declared_category(category):
    assert gates_module.validate_category(category) == category
    # Case/whitespace tolerant.
    assert gates_module.validate_category(f"  {category.upper()}  ") == category


def test_validate_category_rejects_unknown():
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.validate_category("made_up_category")
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.validate_category("")
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.validate_category(None)


@pytest.mark.parametrize("state", gates_module.GATE_STATES)
def test_validate_state_accepts_every_declared_state(state):
    assert gates_module.validate_state(state) == state


def test_validate_state_rejects_unknown():
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.validate_state("in_review")


def test_validate_reopen_policy_defaults_and_validates():
    assert gates_module.validate_reopen_policy(None) == "manual"
    for policy in gates_module.REOPEN_POLICIES:
        assert gates_module.validate_reopen_policy(policy) == policy
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.validate_reopen_policy("whenever")


def test_normalize_affected_accepts_bare_string_shorthand():
    out = gates_module.normalize_affected(["item-123"])
    assert out == [{"sprint_item_id": "item-123"}]


def test_normalize_affected_accepts_explicit_sprint_item_dict():
    out = gates_module.normalize_affected([{"sprint_item_id": "item-9"}])
    assert out == [{"sprint_item_id": "item-9"}]


def test_normalize_affected_accepts_generic_pointer():
    pointer = {
        "source_type": "code",
        "targets": [{"uri": "meridian/db/__init__.py",
                     "selector": {"type": "symbol", "qualified_name": "init_db"}}],
    }
    out = gates_module.normalize_affected([pointer])
    assert len(out) == 1
    assert "pointer" in out[0]
    assert out[0]["pointer"]["source_type"] == "code"


def test_normalize_affected_mixed_list():
    pointer = {"source_type": "code", "targets": [
        {"uri": "x.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}},
    ]}
    out = gates_module.normalize_affected(["item-1", pointer, {"sprint_item_id": "item-2"}])
    assert len(out) == 3
    assert out[0] == {"sprint_item_id": "item-1"}
    assert "pointer" in out[1]
    assert out[2] == {"sprint_item_id": "item-2"}


def test_normalize_affected_rejects_empty_list():
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.normalize_affected([])


def test_normalize_affected_rejects_non_list():
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.normalize_affected("item-1")


def test_normalize_affected_rejects_malformed_entry():
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.normalize_affected([{"nonsense": True}])
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.normalize_affected([123])
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.normalize_affected([""])


def test_normalize_affected_rejects_malformed_pointer():
    with pytest.raises(gates_module.ProposalGateError):
        gates_module.normalize_affected([{"source_type": "code", "targets": []}])


# ---------------------------------------------------------------------------
# create_gate — one per named category, all named fields present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("category", gates_module.GATE_CATEGORIES)
async def test_create_gate_for_every_category(db, project, category):
    gate = await db_module.create_proposal_gate(
        db, project["id"], category, f"Ambiguous {category} question?",
        ["item-1", {"source_type": "docs", "targets": [
            {"uri": "doc:spec", "selector": {"type": "node_id", "id": "n1"}}]}],
        f"evidence supporting the {category} ambiguity",
        created_by="raiser-session",
        expires_at="2030-01-01 00:00:00",
        reopen_policy="auto_on_expiry",
    )
    # Named fields from the acceptance criteria, all present:
    assert gate["category"] == category
    assert gate["question"] == f"Ambiguous {category} question?"
    assert gate["affected"] == [
        {"sprint_item_id": "item-1"},
        {"pointer": {
            "source_type": "docs",
            "targets": [{"uri": "doc:spec", "selector": {"type": "node_id", "id": "n1"},
                         "target_kind": "existing"}],
        }},
    ]
    assert gate["evidence"] == f"evidence supporting the {category} ambiguity"
    assert gate["decision"] is None  # not yet decided
    assert gate["actor"] is None
    assert gate["decided_at"] is None
    assert gate["created_at"]  # timestamp present
    assert gate["expires_at"] == "2030-01-01 00:00:00"
    assert gate["reopen_policy"] == "auto_on_expiry"
    # Fail-safe default lane state:
    assert gate["state"] == "blocked"
    assert gate["reopen_count"] == 0


@pytest.mark.asyncio
async def test_create_gate_rejects_bad_category(db, project):
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.create_proposal_gate(
            db, project["id"], "not_a_real_category", "q?", ["item-1"], "ev",
        )


@pytest.mark.asyncio
async def test_create_gate_rejects_empty_question_or_evidence(db, project):
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.create_proposal_gate(
            db, project["id"], "legal_ip", "", ["item-1"], "ev",
        )
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.create_proposal_gate(
            db, project["id"], "legal_ip", "q?", ["item-1"], "",
        )


@pytest.mark.asyncio
async def test_create_gate_rejects_empty_affected(db, project):
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.create_proposal_gate(
            db, project["id"], "legal_ip", "q?", [], "ev",
        )


# ---------------------------------------------------------------------------
# resolve_gate — the blocked/quarantined/allowed state machine
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("target_state", gates_module.GATE_STATES)
async def test_resolve_gate_transitions_to_each_state(db, project, target_state):
    gate = await db_module.create_proposal_gate(
        db, project["id"], "product_scope", "Ship the extra field?",
        ["item-1"], "PM flagged scope creep",
    )
    resolved = await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], target_state,
        f"decision: {target_state}", "adam",
    )
    assert resolved["state"] == target_state
    assert resolved["decision"] == f"decision: {target_state}"
    assert resolved["actor"] == "adam"
    assert resolved["decided_at"] is not None


@pytest.mark.asyncio
async def test_resolve_gate_missing_gate_raises(db, project):
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.resolve_proposal_gate(
            db, project["id"], "does-not-exist", "allowed", "d", "adam",
        )


@pytest.mark.asyncio
async def test_resolve_gate_refuses_second_decision_without_reopen(db, project):
    gate = await db_module.create_proposal_gate(
        db, project["id"], "production_deploy", "Deploy on Friday?",
        ["item-1"], "on-call is thin",
    )
    await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], "quarantined", "staging only", "adam",
    )
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.resolve_proposal_gate(
            db, project["id"], gate["id"], "allowed", "changed my mind", "adam",
        )


@pytest.mark.asyncio
async def test_resolve_gate_rejects_bad_decision_or_actor(db, project):
    gate = await db_module.create_proposal_gate(
        db, project["id"], "other_ambiguous", "q?", ["item-1"], "ev",
    )
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.resolve_proposal_gate(
            db, project["id"], gate["id"], "allowed", "", "adam",
        )
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.resolve_proposal_gate(
            db, project["id"], gate["id"], "allowed", "d", "",
        )
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.resolve_proposal_gate(
            db, project["id"], gate["id"], "not_a_state", "d", "adam",
        )


@pytest.mark.asyncio
async def test_resolve_gate_allows_fresh_decision_after_expiry_without_reopen(db, project):
    """Expiry is always sufficient grounds for a new decision, regardless of
    reopen_policy — no reopen_gate call required once expires_at has passed."""
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    gate = await db_module.create_proposal_gate(
        db, project["id"], "contradiction_acceptance", "Accept the contradiction?",
        ["item-1"], "two docs disagree", expires_at=past, reopen_policy="manual",
    )
    await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], "allowed", "accepted doc A", "adam",
    )
    # Would normally be refused (already decided) — but the decision already
    # expired, so a fresh decision is accepted directly.
    refreshed = await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], "blocked", "re-litigated, doc B wins", "priya",
    )
    assert refreshed["state"] == "blocked"
    assert refreshed["decision"] == "re-litigated, doc B wins"
    assert refreshed["actor"] == "priya"
    assert refreshed["previous_decision"] == "accepted doc A"
    assert refreshed["previous_actor"] == "adam"


# ---------------------------------------------------------------------------
# reopen_gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reopen_gate_requires_prior_decision(db, project):
    gate = await db_module.create_proposal_gate(
        db, project["id"], "legal_ip", "q?", ["item-1"], "ev",
    )
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.reopen_proposal_gate(
            db, project["id"], gate["id"], "adam", "no decision yet",
        )


@pytest.mark.asyncio
async def test_reopen_gate_resets_to_blocked_and_snapshots_history(db, project):
    gate = await db_module.create_proposal_gate(
        db, project["id"], "legal_ip", "Can we reuse this license?",
        ["item-1"], "license text ambiguous",
    )
    await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], "allowed", "counsel said ok", "legal-bot",
    )
    reopened = await db_module.reopen_proposal_gate(
        db, project["id"], gate["id"], "adam", "counsel reversed their opinion",
    )
    assert reopened["state"] == "blocked"
    assert reopened["decision"] is None
    assert reopened["actor"] is None
    assert reopened["decided_at"] is None
    assert reopened["previous_decision"] == "counsel said ok"
    assert reopened["previous_actor"] == "legal-bot"
    assert reopened["reopen_count"] == 1
    assert reopened["reopen_reason"] == "counsel reversed their opinion"
    assert reopened["reopened_by"] == "adam"

    # And now a fresh decision can be recorded again.
    resolved_again = await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], "blocked", "confirmed unlicensed", "adam",
    )
    assert resolved_again["state"] == "blocked"
    assert resolved_again["reopen_count"] == 1


@pytest.mark.asyncio
async def test_reopen_gate_missing_gate_raises(db, project):
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.reopen_proposal_gate(
            db, project["id"], "does-not-exist", "adam", "reason",
        )


# ---------------------------------------------------------------------------
# effective_state / is_gate_expired
# ---------------------------------------------------------------------------

def test_effective_state_matches_stored_when_not_expired():
    gate = {"state": "allowed", "reopen_policy": "manual", "expires_at": None}
    assert gates_module.effective_state(gate) == "allowed"


def test_effective_state_manual_policy_does_not_auto_revert_on_expiry():
    past = "2000-01-01 00:00:00"
    gate = {"state": "allowed", "reopen_policy": "manual", "expires_at": past}
    assert gates_module.is_gate_expired(gate) is True
    assert gates_module.effective_state(gate) == "allowed"


def test_effective_state_auto_on_expiry_reverts_to_blocked():
    past = "2000-01-01 00:00:00"
    gate = {"state": "allowed", "reopen_policy": "auto_on_expiry", "expires_at": past}
    assert gates_module.effective_state(gate) == "blocked"


def test_effective_state_auto_on_expiry_future_expiry_unaffected():
    future = "2999-01-01 00:00:00"
    gate = {"state": "quarantined", "reopen_policy": "auto_on_expiry", "expires_at": future}
    assert gates_module.effective_state(gate) == "quarantined"


def test_is_gate_expired_handles_missing_or_malformed_expiry():
    assert gates_module.is_gate_expired({"expires_at": None}) is False
    assert gates_module.is_gate_expired({}) is False
    assert gates_module.is_gate_expired({"expires_at": "not-a-date"}) is False


# ---------------------------------------------------------------------------
# blocking_gates_for_sprint_item / db.sprint_items wrapper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_blocking_gates_for_sprint_item_tracks_state(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Delete legacy rows")
    gate = await db_module.create_proposal_gate(
        db, project["id"], "destructive_ops", "Really delete them?",
        [item["id"]], "no backup confirmed yet",
    )
    blocking = await db_module.blocking_gates_for_sprint_item(db, project["id"], item["id"])
    assert [g["id"] for g in blocking] == [gate["id"]]

    # Quarantined still blocks (lane open only in a restricted scope).
    await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], "quarantined", "dry-run only", "adam",
    )
    blocking = await db_module.blocking_gates_for_sprint_item(db, project["id"], item["id"])
    assert [g["id"] for g in blocking] == [gate["id"]]

    # Allowed clears it.
    await db_module.reopen_proposal_gate(db, project["id"], gate["id"], "adam", "re-eval")
    await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], "allowed", "backup confirmed", "adam",
    )
    blocking = await db_module.blocking_gates_for_sprint_item(db, project["id"], item["id"])
    assert blocking == []


@pytest.mark.asyncio
async def test_get_sprint_item_blocking_gates_matches_module_function(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Ship it")
    gate = await db_module.create_proposal_gate(
        db, project["id"], "production_deploy", "Ship on a Friday?",
        [item["id"]], "on-call thin",
    )
    via_wrapper = await db_module.get_sprint_item_blocking_gates(db, project["id"], item["id"])
    via_module = await gates_module.blocking_gates_for_sprint_item(db, project["id"], item["id"])
    assert [g["id"] for g in via_wrapper] == [g["id"] for g in via_module] == [gate["id"]]


@pytest.mark.asyncio
async def test_blocking_gates_for_sprint_item_ignores_unrelated_items(db, project):
    item_a = await db_module.add_sprint_item(db, project["id"], "v1", "A")
    item_b = await db_module.add_sprint_item(db, project["id"], "v1", "B")
    await db_module.create_proposal_gate(
        db, project["id"], "legal_ip", "q?", [item_a["id"]], "ev",
    )
    blocking_b = await db_module.blocking_gates_for_sprint_item(db, project["id"], item_b["id"])
    assert blocking_b == []


# ---------------------------------------------------------------------------
# list_gates filters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_gates_filters_by_category_and_state(db, project):
    g1 = await db_module.create_proposal_gate(
        db, project["id"], "legal_ip", "q1", ["item-1"], "ev1",
    )
    g2 = await db_module.create_proposal_gate(
        db, project["id"], "product_scope", "q2", ["item-2"], "ev2",
    )
    await db_module.resolve_proposal_gate(
        db, project["id"], g2["id"], "allowed", "cleared", "adam",
    )

    all_gates = await db_module.list_proposal_gates(db, project["id"])
    assert {g["id"] for g in all_gates} == {g1["id"], g2["id"]}

    legal_only = await db_module.list_proposal_gates(db, project["id"], category="legal_ip")
    assert [g["id"] for g in legal_only] == [g1["id"]]

    allowed_only = await db_module.list_proposal_gates(db, project["id"], state="allowed")
    assert [g["id"] for g in allowed_only] == [g2["id"]]

    blocked_only = await db_module.list_proposal_gates(db, project["id"], state="blocked")
    assert [g["id"] for g in blocked_only] == [g1["id"]]


# ---------------------------------------------------------------------------
# MCP tool handlers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_add_proposal_gate_direct(db, project):
    result = await nd_mod.handle_add_proposal_gate(
        {
            "project_id": project["id"],
            "category": "destructive_ops",
            "question": "Drop the old index?",
            "affected": ["item-1"],
            "evidence": "index unused for 90 days",
        },
        db, _DATA_DIR, None, None,
    )
    assert result["category"] == "destructive_ops"
    assert result["state"] == "blocked"


@pytest.mark.asyncio
async def test_handle_add_proposal_gate_missing_project_id(db):
    result = await nd_mod.handle_add_proposal_gate(
        {"category": "legal_ip", "question": "q", "affected": ["i"], "evidence": "e"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_add_proposal_gate_bad_category_returns_error(db, project):
    result = await nd_mod.handle_add_proposal_gate(
        {
            "project_id": project["id"], "category": "nonsense",
            "question": "q", "affected": ["i"], "evidence": "e",
        },
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_resolve_proposal_gate_direct(db, project):
    created = await nd_mod.handle_add_proposal_gate(
        {
            "project_id": project["id"], "category": "product_scope",
            "question": "q", "affected": ["item-1"], "evidence": "e",
        },
        db, _DATA_DIR, None, None,
    )
    result = await nd_mod.handle_resolve_proposal_gate(
        {
            "project_id": project["id"], "gate_id": created["id"],
            "state": "allowed", "decision": "cleared by PM", "actor": "pm-bot",
        },
        db, _DATA_DIR, None, None,
    )
    assert result["state"] == "allowed"
    assert result["decision"] == "cleared by PM"


@pytest.mark.asyncio
async def test_handle_resolve_proposal_gate_missing_fields(db, project):
    result = await nd_mod.handle_resolve_proposal_gate(
        {"project_id": project["id"]}, db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_reopen_proposal_gate_direct(db, project):
    created = await nd_mod.handle_add_proposal_gate(
        {
            "project_id": project["id"], "category": "legal_ip",
            "question": "q", "affected": ["item-1"], "evidence": "e",
        },
        db, _DATA_DIR, None, None,
    )
    await nd_mod.handle_resolve_proposal_gate(
        {
            "project_id": project["id"], "gate_id": created["id"],
            "state": "allowed", "decision": "d", "actor": "a",
        },
        db, _DATA_DIR, None, None,
    )
    result = await nd_mod.handle_reopen_proposal_gate(
        {
            "project_id": project["id"], "gate_id": created["id"],
            "actor": "adam", "reason": "new evidence",
        },
        db, _DATA_DIR, None, None,
    )
    assert result["state"] == "blocked"
    assert result["reopen_count"] == 1


@pytest.mark.asyncio
async def test_handle_reopen_proposal_gate_never_decided_returns_error(db, project):
    created = await nd_mod.handle_add_proposal_gate(
        {
            "project_id": project["id"], "category": "legal_ip",
            "question": "q", "affected": ["item-1"], "evidence": "e",
        },
        db, _DATA_DIR, None, None,
    )
    result = await nd_mod.handle_reopen_proposal_gate(
        {"project_id": project["id"], "gate_id": created["id"], "actor": "a", "reason": "r"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_handle_get_proposal_gates_lists_and_filters_by_sprint_item(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Risky item")
    gate = await nd_mod.handle_add_proposal_gate(
        {
            "project_id": project["id"], "category": "destructive_ops",
            "question": "q", "affected": [item["id"]], "evidence": "e",
        },
        db, _DATA_DIR, None, None,
    )
    all_gates = await nd_mod.handle_get_proposal_gates(
        {"project_id": project["id"]}, db, _DATA_DIR, None, None,
    )
    assert isinstance(all_gates, list)
    assert any(g["id"] == gate["id"] for g in all_gates)

    filtered = await nd_mod.handle_get_proposal_gates(
        {"project_id": project["id"], "sprint_item_id": item["id"]},
        db, _DATA_DIR, None, None,
    )
    assert [g["id"] for g in filtered] == [gate["id"]]


@pytest.mark.asyncio
async def test_handle_get_proposal_gates_missing_project_id(db):
    result = await nd_mod.handle_get_proposal_gates({}, db, _DATA_DIR, None, None)
    assert "error" in result


# ---------------------------------------------------------------------------
# Dispatch-table wiring (meridian.mcp.handler._handle_notes_decisions)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposal_gate_tools_wired_into_dispatch_table(db, project):
    created = await mh._handle_notes_decisions(
        "add_proposal_gate",
        {
            "project_id": project["id"], "category": "other_ambiguous",
            "question": "q", "affected": ["item-1"], "evidence": "e",
        },
        db, _DATA_DIR, None, None,
    )
    assert created["state"] == "blocked"

    resolved = await mh._handle_notes_decisions(
        "resolve_proposal_gate",
        {
            "project_id": project["id"], "gate_id": created["id"],
            "state": "quarantined", "decision": "d", "actor": "a",
        },
        db, _DATA_DIR, None, None,
    )
    assert resolved["state"] == "quarantined"

    reopened = await mh._handle_notes_decisions(
        "reopen_proposal_gate",
        {
            "project_id": project["id"], "gate_id": created["id"],
            "actor": "a", "reason": "r",
        },
        db, _DATA_DIR, None, None,
    )
    assert reopened["state"] == "blocked"

    listed = await mh._handle_notes_decisions(
        "get_proposal_gates", {"project_id": project["id"]},
        db, _DATA_DIR, None, None,
    )
    assert any(g["id"] == created["id"] for g in listed)


# ---------------------------------------------------------------------------
# handoff.py — best-effort readiness wrapper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_proposal_gate_readiness_for_handoff_reports_open_gates(db, project):
    item = await db_module.add_sprint_item(db, project["id"], "v1", "Pending item")
    gate = await db_module.create_proposal_gate(
        db, project["id"], "production_deploy", "Deploy now?",
        [item["id"]], "on-call thin",
    )
    other_item_gate = await db_module.create_proposal_gate(
        db, project["id"], "legal_ip", "q?", ["some-other-item"], "ev",
    )

    readiness = await handoff_module.build_proposal_gate_readiness_for_handoff(
        db, project["id"], pending_items=[item],
    )
    assert readiness is not None
    assert readiness["open_gate_count"] == 2
    assert gate["id"] in readiness["blocking_pending_item_gate_ids"]
    assert other_item_gate["id"] not in readiness["blocking_pending_item_gate_ids"]

    # Clearing the gate removes it from the open set.
    await db_module.resolve_proposal_gate(
        db, project["id"], gate["id"], "allowed", "cleared", "adam",
    )
    readiness_after = await handoff_module.build_proposal_gate_readiness_for_handoff(
        db, project["id"], pending_items=[item],
    )
    assert readiness_after["open_gate_count"] == 1
    assert readiness_after["blocking_pending_item_gate_ids"] == []


@pytest.mark.asyncio
async def test_build_proposal_gate_readiness_for_handoff_never_raises(monkeypatch, db, project):
    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gates_module, "list_gates", _boom)
    result = await handoff_module.build_proposal_gate_readiness_for_handoff(db, project["id"])
    assert result is None
